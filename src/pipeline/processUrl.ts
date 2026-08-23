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
  traceId: string | undefined;
}

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
    traceId: undefined,
  };

  return await traced(
    {
      name: "process_job",
      runType: "chain",
      metadata: { url, discovery_keyword: keyword },
      onStart: (traceId) => {
        state.traceId = traceId;
      },
      finalMeta: () => ({
        source: state.source,
        ats_presence: state.ats,
        outcome: state.outcome,
        matched_profile: state.profile,
        retry_count: state.retries,
      }),
    },
    async () => processJobBody(url, keyword, ctx, state),
  );
}

async function processJobBody(
  url: string,
  keyword: string,
  ctx: ProcessContext,
  state: ProcessResultState,
): Promise<{ data: ProcessResult }> {
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
        await recordTerminalResult({
          ledger,
          url,
          job,
          outcome: "rejected",
          traceId: state.traceId,
          project: () => insertJob(notion, config.notionDatabaseId, job, "Auto-Rejected"),
        });
        state.outcome = "rejected";
        return { data: "rejected" };
      }
    }
  }

  const structural = structuralFilter(job);
  if (!structural.pass) {
    log.info(
      { url, title: job.title, company: job.company, reason: structural.reason },
      "rejected (structural)",
    );
    await recordTerminalResult({
      ledger,
      url,
      job,
      outcome: "rejected",
      traceId: state.traceId,
      project: () => insertJob(notion, config.notionDatabaseId, job, "Auto-Rejected"),
    });
    state.outcome = "rejected";
    return { data: "rejected" };
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
    await recordTerminalResult({
      ledger,
      url,
      job,
      outcome: "rejected",
      traceId: state.traceId,
      project: () => insertJob(notion, config.notionDatabaseId, job, "Auto-Rejected"),
    });
    state.outcome = "rejected";
    return { data: "rejected" };
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
      await recordTerminalResult({
        ledger,
        url,
        job,
        outcome: "duplicated",
        traceId: state.traceId,
      });
      state.outcome = "duplicated";
      return { data: "duplicated" };
    }
  }

  if (ledger.findCompanyExclusion(job.company)) {
    log.info({ url, title: job.title, company: job.company }, "archived (company blocked)");
    await recordTerminalResult({
      ledger,
      url,
      job,
      outcome: "archived",
      traceId: state.traceId,
      project: () => insertJob(notion, config.notionDatabaseId, job, "Archived"),
    });
    state.outcome = "archived";
    return { data: "archived" };
  }

  if (recentAppCompanies.has(job.company)) {
    log.info({ url, title: job.title, company: job.company }, "company applied");
    await recordTerminalResult({
      ledger,
      url,
      job,
      outcome: "companyApplied",
      traceId: state.traceId,
      project: () => insertJob(notion, config.notionDatabaseId, job, "Company Applied"),
    });
    state.outcome = "companyApplied";
    return { data: "companyApplied" };
  }

  await recordTerminalResult({
    ledger,
    url,
    job,
    outcome: "inserted",
    traceId: state.traceId,
    project: () => insertJob(notion, config.notionDatabaseId, job),
  });
  log.info({ url, title: job.title, company: job.company }, "inserted");

  state.outcome = "inserted";
  return { data: "inserted" };
}
