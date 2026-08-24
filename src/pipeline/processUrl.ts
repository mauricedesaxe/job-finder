import {
  atsApiRateLimiter,
  atsApiSemaphore,
  isRetryableJina,
  isRetryableLLM,
  jinaBreaker,
  jinaReaderSemaphore,
  llmBreaker,
  llmSemaphore,
  withRetry,
} from "../concurrency";
import type { JobFinderConfig } from "../config";
import type { EvaluationFilter } from "../config/evaluation";
import { logger } from "../logger";
import { atsStructuralFilter, fetchAtsData, formatAtsBlock } from "../services/ats";
import type { JobLedger } from "../services/jobLedger";
import { traced } from "../services/langsmith";
import { insertJob, type ResilientNotionClient } from "../services/notion";
import { checkFuzzyDuplicate } from "./dedup";
import { enrichJob } from "./enrich";
import { evaluateJob } from "./evaluate";
import { recordTerminalResult } from "./recordTerminalResult";
import { parseJobDetails, scrapeJobPage } from "./scrape";
import { structuralFilter } from "./structuralFilter";

const log = logger.child({ component: "processUrl" });

export type ProcessResult =
  | "inserted"
  | "rejected"
  | "duplicated"
  | "skipped"
  | "companyApplied"
  | "archived"
  | "errored";

export interface ScrapeStats {
  inserted: number;
  skipped: number;
  companyApplied: number;
  rejected: number;
  archived: number;
  duplicated: number;
  errored: number;
}

export interface ProcessContext {
  notion: ResilientNotionClient;
  config: JobFinderConfig;
  ledger: JobLedger;
  recentAppCompanies: ReadonlySet<string>;
  seenUrls: Set<string>;
  filters?: EvaluationFilter[];
}

interface ProcessResultState {
  source: string;
  ats: boolean;
  retries: number;
  outcome: ProcessResult;
  profile: string;
}

interface ProcessJobResult {
  outcome: ProcessResult;
  record: (traceId: string) => Promise<void>;
}

type TerminalOutcome = Exclude<ProcessResult, "skipped" | "errored">;

export async function processUrl(
  url: string,
  keyword: string,
  ctx: ProcessContext,
): Promise<ProcessResult> {
  const { ledger, seenUrls } = ctx;

  if (seenUrls.has(url)) return "skipped";
  seenUrls.add(url);

  if (ledger.findByRawUrl(url)) {
    log.debug({ url }, "skipped (exists in ledger)");
    return "skipped";
  }

  const state: ProcessResultState = {
    source: "",
    ats: false,
    retries: 0,
    outcome: "errored",
    profile: "",
  };

  return traced(
    {
      name: "process_job",
      runType: "chain",
      metadata: { url, discovery_keyword: keyword },
      finalMeta: () => ({
        source: state.source,
        ats_presence: state.ats,
        outcome: state.outcome,
        matched_profile: state.profile,
        retry_count: state.retries,
      }),
    },
    async ({ requireAccepted }) => {
      const { data: result } = await processJobBody(url, keyword, ctx, state);
      const traceId = await requireAccepted();
      await result.record(traceId);
      return { data: result.outcome };
    },
  );
}

async function processJobBody(
  url: string,
  keyword: string,
  ctx: ProcessContext,
  state: ProcessResultState,
): Promise<{ data: ProcessJobResult }> {
  const { config, ledger, notion, recentAppCompanies } = ctx;

  const markdown = await jinaReaderSemaphore.run(() =>
    jinaBreaker.run(() =>
      withRetry(() => scrapeJobPage(url, config), {
        shouldRetry: isRetryableJina,
        onRetry: (a) => {
          state.retries++;
          log.warn({ url, attempt: a }, "jina scrape retry");
        },
      }),
    ),
  );
  const job = parseJobDetails(markdown, url, keyword);
  state.source = job.source;

  if (config.enableAtsEnrichment) {
    const atsData = await atsApiSemaphore.run(() =>
      atsApiRateLimiter.run(() => fetchAtsData(url, { title: job.title })),
    );
    if (atsData) {
      state.ats = true;
      log.debug({ url, source: atsData.source }, "ats enriched");
      job.description = `${formatAtsBlock(atsData)}\n\n${job.description}`;

      const atsCheck = atsStructuralFilter(atsData);
      if (!atsCheck.pass) {
        log.info(
          { url, title: job.title, company: job.company, reason: atsCheck.reason },
          "rejected (ats)",
        );
        state.outcome = "rejected";
        return terminalResult({
          ledger,
          url,
          job,
          outcome: "rejected",
          project: () => insertJob(notion, config.notionDatabaseId, job, "Auto-Rejected"),
        });
      }
    }
  }

  const structural = structuralFilter(job);
  if (!structural.pass) {
    log.info(
      { url, title: job.title, company: job.company, reason: structural.reason },
      "rejected (structural)",
    );
    state.outcome = "rejected";
    return terminalResult({
      ledger,
      url,
      job,
      outcome: "rejected",
      project: () => insertJob(notion, config.notionDatabaseId, job, "Auto-Rejected"),
    });
  }

  const evaluation = await llmSemaphore.run(() =>
    llmBreaker.run(() =>
      withRetry(() => evaluateJob(job, { filters: ctx.filters }), {
        shouldRetry: isRetryableLLM,
        onRetry: (a) => {
          state.retries++;
          log.warn({ url, attempt: a }, "llm eval retry");
        },
      }),
    ),
  );

  if (evaluation.profileName) {
    job.profile = evaluation.profileName;
    state.profile = evaluation.profileName;
  }

  if (!evaluation.pass) {
    log.info(
      { url, title: job.title, company: job.company, reason: evaluation.reason },
      "rejected",
    );
    state.outcome = "rejected";
    return terminalResult({
      ledger,
      url,
      job,
      outcome: "rejected",
      project: () => insertJob(notion, config.notionDatabaseId, job, "Auto-Rejected"),
    });
  }

  const enriched = await llmSemaphore.run(() =>
    llmBreaker.run(() =>
      withRetry(() => enrichJob(job), {
        shouldRetry: isRetryableLLM,
        onRetry: (a) => {
          state.retries++;
          log.warn({ url, attempt: a }, "llm enrich retry");
        },
      }),
    ),
  );
  job.title = enriched.title;
  job.company = enriched.company;
  job.description = enriched.description;
  job.location = enriched.location;

  const existingTitles = ledger.titlesForCompany(job.company);
  if (existingTitles.length > 0) {
    const dedup = await llmSemaphore.run(() =>
      withRetry(() => checkFuzzyDuplicate(job.title, existingTitles), {
        shouldRetry: isRetryableLLM,
        onRetry: (a) => {
          state.retries++;
          log.warn({ url, attempt: a }, "llm dedup retry");
        },
      }),
    );
    if (dedup.isDuplicate) {
      log.info(
        { url, title: job.title, company: job.company, matchedTitle: dedup.matchedTitle },
        "duplicate",
      );
      state.outcome = "duplicated";
      return terminalResult({
        ledger,
        url,
        job,
        outcome: "duplicated",
      });
    }
  }

  if (ledger.findCompanyExclusion(job.company)) {
    log.info({ url, title: job.title, company: job.company }, "archived (company blocked)");
    state.outcome = "archived";
    return terminalResult({
      ledger,
      url,
      job,
      outcome: "archived",
      project: () => insertJob(notion, config.notionDatabaseId, job, "Archived"),
    });
  }

  if (recentAppCompanies.has(job.company)) {
    log.info({ url, title: job.title, company: job.company }, "company applied");
    state.outcome = "companyApplied";
    return terminalResult({
      ledger,
      url,
      job,
      outcome: "companyApplied",
      project: () => insertJob(notion, config.notionDatabaseId, job, "Company Applied"),
    });
  }

  state.outcome = "inserted";
  return terminalResult({
    ledger,
    url,
    job,
    outcome: "inserted",
    project: () => insertJob(notion, config.notionDatabaseId, job),
  });
}

function terminalResult(
  input: Omit<Parameters<typeof recordTerminalResult>[0], "traceId" | "outcome"> & {
    outcome: TerminalOutcome;
  },
): { data: ProcessJobResult } {
  return {
    data: {
      outcome: input.outcome,
      record: (traceId) => recordTerminalResult({ ...input, traceId }),
    },
  };
}
