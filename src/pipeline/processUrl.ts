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
import { type ReviewSnapshot, ReviewSnapshotSchema } from "../review";
import {
  type AtsJobData,
  atsStructuralFilter,
  fetchAtsData,
  formatAtsBlock,
} from "../services/ats";
import type { JobLedger } from "../services/jobLedger";
import { enqueueReviewTrace, getPromptReleaseTag, traced } from "../services/langsmith";
import { checkFuzzyDuplicate } from "./dedup";
import { enrichJob } from "./enrich";
import { evaluateJob } from "./evaluate";
import { type PreparedTerminalResultInput, recordTerminalResult } from "./recordTerminalResult";
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
  config: JobFinderConfig;
  ledger: JobLedger;
  recentAppCompanies: ReadonlySet<string>;
  filters?: EvaluationFilter[];
}

interface ProcessResultState {
  source: string;
  ats: boolean;
  retries: number;
  outcome: ProcessResult;
  profile: string;
  atsData: AtsJobData | null;
  reviewSnapshot: ReviewSnapshot | undefined;
}

interface ProcessJobResult {
  outcome: ProcessResult;
  prepareTraceCompletion: (traceId: string) => Promise<(() => Promise<void>) | undefined>;
}

export async function processUrl(
  url: string,
  keyword: string,
  ctx: ProcessContext,
): Promise<ProcessResult> {
  const { ledger } = ctx;

  if (await ledger.findByRawUrl(url)) {
    log.debug({ url }, "skipped (exists in ledger)");
    return "skipped";
  }

  const state: ProcessResultState = {
    source: "",
    ats: false,
    retries: 0,
    outcome: "errored",
    profile: "",
    atsData: null,
    reviewSnapshot: undefined,
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
        ...(state.reviewSnapshot ? { review_snapshot: state.reviewSnapshot } : {}),
      }),
    },
    async ({ requireAccepted }) => {
      const traceId = await requireAccepted();
      const { data: result } = await processJobBody(url, keyword, ctx, state);
      const afterTraceComplete = await result.prepareTraceCompletion(traceId);
      return { data: result.outcome, afterTraceComplete };
    },
  );
}

async function processJobBody(
  url: string,
  keyword: string,
  ctx: ProcessContext,
  state: ProcessResultState,
): Promise<{ data: ProcessJobResult }> {
  const { config, ledger, recentAppCompanies } = ctx;

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
      state.atsData = atsData;
      log.debug({ url, source: atsData.source }, "ats enriched");
      job.description = `${formatAtsBlock(atsData)}\n\n${job.description}`;

      const atsCheck = atsStructuralFilter(atsData);
      if (!atsCheck.pass) {
        log.info(
          { url, title: job.title, company: job.company, reason: atsCheck.reason },
          "rejected (ats)",
        );
        return terminalResult(state, {
          ledger,
          job,
          outcome: "rejected",
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
    return terminalResult(state, {
      ledger,
      job,
      outcome: "rejected",
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
    return terminalResult(state, {
      ledger,
      job,
      outcome: "rejected",
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

  const existingTitles = await ledger.titlesForCompany(job.company);
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
      return terminalResult(state, {
        ledger,
        job,
        outcome: "duplicated",
      });
    }
  }

  if (await ledger.findCompanyExclusion(job.company)) {
    log.info({ url, title: job.title, company: job.company }, "archived (company blocked)");
    return terminalResult(state, {
      ledger,
      job,
      outcome: "archived",
    });
  }

  if (recentAppCompanies.has(job.company)) {
    log.info({ url, title: job.title, company: job.company }, "company applied");
    return terminalResult(state, {
      ledger,
      job,
      outcome: "companyApplied",
    });
  }

  const outcome = "inserted";
  return {
    data: {
      outcome,
      prepareTraceCompletion: async (traceId) => {
        const snapshot = ReviewSnapshotSchema.parse({
          traceId,
          promptRelease: getPromptReleaseTag(),
          job,
          ats: state.atsData,
          compensationRates:
            ctx.filters?.find((filter) => filter.name === "compensation-minimum")?.rates ?? null,
          evaluation: {
            profile: evaluation.profileName,
            reason: evaluation.reason,
          },
        });
        state.outcome = outcome;
        state.reviewSnapshot = snapshot;
        return async () => {
          await recordTerminalResult({
            ledger,
            job,
            outcome,
            traceId,
            review: { enqueue: enqueueReviewTrace },
          });
        };
      },
    },
  };
}

function terminalResult(
  state: ProcessResultState,
  input: PreparedTerminalResultInput,
): { data: ProcessJobResult } {
  state.outcome = input.outcome;
  return {
    data: {
      outcome: input.outcome,
      prepareTraceCompletion: async (traceId) => {
        await recordTerminalResult({ ...input, traceId });
        return undefined;
      },
    },
  };
}
