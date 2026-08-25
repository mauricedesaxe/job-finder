import { isRetryableJina, jinaBreaker, jinaSearchSemaphore, withRetry } from "./concurrency";
import type { JobFinderConfig } from "./config";
import { getEvaluationFilters } from "./config/evaluation";
import { logger } from "./logger";
import { replayPendingNotionProjections } from "./pipeline/notionProjection";
import { type ProcessResult, processUrl, type ScrapeStats } from "./pipeline/processUrl";
import { prune } from "./pipeline/prune";
import { reconcile } from "./pipeline/reconcile";
import { replayPendingReviewProjections } from "./pipeline/reviewProjection";
import { searchJobs } from "./pipeline/search";
import { runPreflight } from "./preflight";
import { replayCompletedReviewCompanyBlocks } from "./review";
import { clearAshbyCache } from "./services/ats";
import { fetchExchangeRates } from "./services/exchangeRates";
import type { JobLedger } from "./services/jobLedger";
import {
  completedReviews,
  flushPending,
  initLangSmith,
  LangSmithTraceUnavailableError,
} from "./services/langsmith";
import { createNotionClient, type ResilientNotionClient } from "./services/notion";
import { buildNotionCache } from "./services/notionCache";
import { backfillJobLedger } from "./services/notionLedgerBackfill";
import { sendRunReport } from "./services/slack";

export type JobFinderRunMode = { kind: "scrape" } | { kind: "reconcile" } | { kind: "backfill" };

const log = logger.child({ component: "main" });
const PIPELINE_WORKERS = 8;

export async function runJobFinder({
  mode,
  ledger,
  config,
}: {
  mode: JobFinderRunMode;
  ledger: JobLedger;
  config: Readonly<JobFinderConfig>;
}): Promise<void> {
  const startTime = Date.now();
  const notion = createNotionClient(config.notionToken);
  await runPreflight(notion, config.notionDatabaseId);

  clearAshbyCache();

  switch (mode.kind) {
    case "reconcile": {
      const stats = await reconcile(notion, config.notionDatabaseId);
      const durationMs = Date.now() - startTime;
      log.info({ stats, durationMs }, "reconciliation complete");
      return;
    }
    case "backfill": {
      const result = await backfillJobLedger({
        client: notion,
        databaseId: config.notionDatabaseId,
        ledger,
      });
      log.info({ ...result, durationMs: Date.now() - startTime }, "job ledger backfill complete");
      return;
    }
    case "scrape":
      return scrapeJobs({ ledger, notion, config, startTime });
  }
}

export function jobFinderRunMode(arguments_: readonly string[]): JobFinderRunMode {
  return arguments_.includes("--reconcile-only") ? { kind: "reconcile" } : { kind: "scrape" };
}

async function scrapeJobs({
  ledger,
  notion,
  config,
  startTime,
}: {
  ledger: JobLedger;
  notion: ResilientNotionClient;
  config: Readonly<JobFinderConfig>;
  startTime: number;
}): Promise<void> {
  await initLangSmith({
    apiKey: config.langsmithApiKey,
    endpoint: config.langsmithEndpoint,
    project: config.langsmithProject,
    openrouterApiKey: config.openrouterApiKey,
  });

  if (!(await ledger.isReadyForScrape())) {
    throw new Error("Backfill the job ledger before scraping");
  }

  await replayPendingReviewProjections(ledger);

  await replayPendingNotionProjections({
    ledger,
    notion,
    databaseId: config.notionDatabaseId,
  });

  const reviewStats = await replayCompletedReviewCompanyBlocks({
    reviews: completedReviews(),
    ledger,
  });
  log.info(reviewStats, "reviews replayed");

  const preReconcileStats = await reconcile(notion, config.notionDatabaseId, "Pre-scrape");

  const pruneStats = await prune(notion, config.notionDatabaseId);

  log.info("building notion cache");
  const cache = await buildNotionCache(notion, config.notionDatabaseId);
  log.info({ recentApps: cache.recentAppCompanies.size }, "notion cache built");

  const rates = await fetchExchangeRates();
  const filters = getEvaluationFilters(rates);

  const searchPairs = config.keywords.flatMap((keyword) =>
    config.domains.map((domain) => ({ keyword, domain })),
  );

  log.info({ pairs: searchPairs.length }, "searching");
  const searchResults = await Promise.allSettled(
    searchPairs.map(({ keyword, domain }) =>
      jinaSearchSemaphore.run(async () => {
        const urls = await jinaBreaker.run(() =>
          withRetry(() => searchJobs(keyword, domain, config), {
            shouldRetry: isRetryableJina,
            onRetry: (attempt) => log.warn({ keyword, domain, attempt }, "search retry"),
          }),
        );
        log.info({ domain, keyword, urls: urls.length }, "search complete");
        return { keyword, urls };
      }),
    ),
  );

  const urlMap = new Map<string, string>();
  let searchErrors = 0;
  for (const result of searchResults) {
    if (result.status === "fulfilled") {
      for (const url of result.value.urls) {
        if (!urlMap.has(url)) urlMap.set(url, result.value.keyword);
      }
    } else {
      log.error({ err: result.reason }, "search failed");
      searchErrors++;
    }
  }
  log.info({ uniqueUrls: urlMap.size, searchErrors }, "all searches complete");

  const urls = Array.from(urlMap.entries());
  const stats: ScrapeStats = {
    inserted: 0,
    skipped: 0,
    companyApplied: 0,
    rejected: 0,
    archived: 0,
    duplicated: 0,
    errored: 0,
  };
  let nextUrl = 0;
  const pipelineState: { fatalError?: LangSmithTraceUnavailableError } = {};

  await Promise.all(
    Array.from({ length: Math.min(PIPELINE_WORKERS, urls.length) }, async () => {
      while (!pipelineState.fatalError && nextUrl < urls.length) {
        const entry = urls[nextUrl++];
        if (!entry) return;
        const [url, keyword] = entry;
        try {
          const result = await processUrl(url, keyword, {
            config,
            ledger,
            recentAppCompanies: cache.recentAppCompanies,
            filters,
          });
          recordProcessResult(stats, result);
        } catch (error) {
          if (error instanceof LangSmithTraceUnavailableError) {
            pipelineState.fatalError ??= error;
            return;
          }
          log.error({ url, err: error }, "url processing failed");
          stats.errored++;
        }
      }
    }),
  );

  if (pipelineState.fatalError) throw pipelineState.fatalError;

  await replayPendingNotionProjections({
    ledger,
    notion,
    databaseId: config.notionDatabaseId,
  });

  const postReconcileStats = await reconcile(notion, config.notionDatabaseId, "Post-scrape");

  log.info({ stats }, "scrape summary");
  log.info({ reconcile: preReconcileStats }, "pre-scrape reconcile summary");
  log.info({ reconcile: postReconcileStats }, "post-scrape reconcile summary");
  log.info({ prune: pruneStats }, "prune summary");

  await flushPending();

  const durationMs = Date.now() - startTime;
  if (config.slackWebhookUrl) {
    await sendRunReport(
      config.slackWebhookUrl,
      stats,
      postReconcileStats,
      pruneStats,
      {
        urlCount: urlMap.size,
        searchErrors,
      },
      durationMs,
    );
  }
}

function recordProcessResult(stats: ScrapeStats, result: ProcessResult): void {
  if (result === "companyApplied") {
    stats.companyApplied++;
  } else if (result in stats) {
    stats[result as keyof ScrapeStats]++;
  }
}
