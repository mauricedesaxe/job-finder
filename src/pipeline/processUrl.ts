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
import { traced } from "../services/langsmith";
import type { ProcessLedger } from "../services/ledger";
import { insertJob, type ResilientNotionClient } from "../services/notion";
import type { NotionCache } from "../services/notionCache";
import { checkFuzzyDuplicate } from "./dedup";
import { enrichJob } from "./enrich";
import { evaluateJob } from "./evaluate";
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
  cache: NotionCache;
  ledger: ProcessLedger;
  seenUrls: Set<string>;
  filters?: EvaluationFilter[];
}

interface ProcessResultState {
  source: string;
  ats: boolean;
  retries: number;
  outcome: ProcessResult;
  profile: string;
  traceId: string;
}

export async function processUrl(
  url: string,
  keyword: string,
  ctx: ProcessContext,
): Promise<ProcessResult> {
  const { seenUrls } = ctx;
  const ledger = ctx.ledger;

  if (seenUrls.has(url)) return "skipped";
  seenUrls.add(url);

  if (ledger.hasUrl(url)) {
    log.debug({ url }, "skipped (exists in ledger)");
    return "skipped";
  }

  const state: ProcessResultState = {
    source: "",
    ats: false,
    retries: 0,
    outcome: "errored",
    profile: "",
    traceId: "",
  };

  return await traced(
    {
      name: "process_job",
      runType: "chain",
      metadata: { url, discovery_keyword: keyword },
      onRunId: (id) => {
        state.traceId = id;
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
  const { notion, config, cache, ledger } = ctx;

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

  const record = (
    outcome: Extract<ProcessResult, "inserted" | "rejected" | "archived" | "companyApplied">,
  ): void => {
    ledger.record({
      url,
      company: job.company,
      title: job.title,
      outcome,
      traceId: state.traceId || undefined,
    });
  };

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
        await insertJob(notion, config.notionDatabaseId, job, "Auto-Rejected");
        state.outcome = "rejected";
        record("rejected");
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
    await insertJob(notion, config.notionDatabaseId, job, "Auto-Rejected");
    state.outcome = "rejected";
    record("rejected");
    return { data: "rejected" };
  }

  const evaluation = await llmSemaphore.run(() =>
    llmBreaker.run(() =>
      withRetry(
        () =>
          evaluateJob(job, config.openrouterApiKey, {
            filters: ctx.filters,
            model: config.llmModel,
          }),
        {
          shouldRetry: isRetryableLLM,
          onRetry: (a) => {
            state.retries++;
            log.warn({ url, attempt: a }, "llm eval retry");
          },
        },
      ),
    ),
  );

  if (evaluation.profileName) {
    job.profile = evaluation.profileName;
  }

  if (!evaluation.pass) {
    log.info(
      { url, title: job.title, company: job.company, reason: evaluation.reason },
      "rejected",
    );
    await insertJob(notion, config.notionDatabaseId, job, "Auto-Rejected");
    state.outcome = "rejected";
    record("rejected");
    return { data: "rejected" };
  }

  const enriched = await llmSemaphore.run(() =>
    llmBreaker.run(() =>
      withRetry(() => enrichJob(job, config.openrouterApiKey, config.llmModel), {
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
      withRetry(
        () =>
          checkFuzzyDuplicate(job.title, existingTitles, config.openrouterApiKey, config.llmModel),
        {
          shouldRetry: isRetryableLLM,
          onRetry: (a) => {
            state.retries++;
            log.warn({ url, attempt: a }, "llm dedup retry");
          },
        },
      ),
    );
    if (dedup.isDuplicate) {
      log.info(
        { url, title: job.title, company: job.company, matchedTitle: dedup.matchedTitle },
        "duplicate",
      );
      state.outcome = "duplicated";
      return { data: "duplicated" };
    }
  }

  if (ledger.isExcluded(job.company)) {
    log.info({ url, title: job.title, company: job.company }, "archived (company excluded)");
    await insertJob(notion, config.notionDatabaseId, job, "Archived");
    state.outcome = "archived";
    record("archived");
    return { data: "archived" };
  }

  if (cache.recentAppCompanies.has(job.company)) {
    log.info({ url, title: job.title, company: job.company }, "company applied");
    await insertJob(notion, config.notionDatabaseId, job, "Company Applied");
    state.outcome = "companyApplied";
    record("companyApplied");
    return { data: "companyApplied" };
  }

  await insertJob(notion, config.notionDatabaseId, job);
  log.info({ url, title: job.title, company: job.company }, "inserted");

  state.outcome = "inserted";
  state.profile = evaluation.profileName ?? "";
  record("inserted");
  return { data: "inserted" };
}
