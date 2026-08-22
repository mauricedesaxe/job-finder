import { isRetryableJina, jinaBreaker, jinaSearchSemaphore, withRetry } from "./concurrency";
import { config } from "./config";
import { getEvaluationFilters } from "./config/evaluation";
import { logger } from "./logger";
import { type ProcessResult, processUrl, type ScrapeStats } from "./pipeline/processUrl";
import { prune } from "./pipeline/prune";
import { reconcile } from "./pipeline/reconcile";
import { searchJobs } from "./pipeline/search";
import { runPreflight } from "./preflight";
import { clearAshbyCache } from "./services/ats";
import { fetchExchangeRates } from "./services/exchangeRates";
import { createJobLedger, type JobLedger } from "./services/jobLedger";
import { flushPending, initLangSmith } from "./services/langsmith";
import { createNotionClient } from "./services/notion";
import { buildNotionCache } from "./services/notionCache";
import { sendFatalError, sendRunReport } from "./services/slack";

const log = logger.child({ component: "main" });
const reconcileOnly = process.argv.includes("--reconcile-only");

async function main() {
  const ledger = createJobLedger(config.jobLedgerPath);
  try {
    await mainWithLedger(ledger);
  } finally {
    ledger.close();
  }
}

async function mainWithLedger(ledger: JobLedger) {
  const startTime = Date.now();
  const notion = createNotionClient(config.notionToken);
  await runPreflight(notion, config.notionDatabaseId);

  initLangSmith({
    apiKey: config.langsmithApiKey,
    endpoint: config.langsmithEndpoint,
    project: config.langsmithProject,
  });

  clearAshbyCache();

  if (reconcileOnly) {
    const stats = await reconcile(notion, config.notionDatabaseId);
    log.info({ stats, durationMs: Date.now() - startTime }, "reconciliation complete");
    return;
  }

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

  log.info({ pairs: searchPairs.length }, "phase 1: searching");

  const urlMap = new Map<string, string>();

  const searchResults = await Promise.allSettled(
    searchPairs.map(({ keyword, domain }) =>
      jinaSearchSemaphore.run(async () => {
        const urls = await jinaBreaker.run(() =>
          withRetry(() => searchJobs(keyword, domain, config), {
            shouldRetry: isRetryableJina,
            onRetry: (a) => log.warn({ keyword, domain, attempt: a }, "search retry"),
          }),
        );
        log.info({ domain, keyword, urls: urls.length }, "search complete");
        return { keyword, urls };
      }),
    ),
  );

  let searchErrors = 0;
  for (const result of searchResults) {
    if (result.status === "fulfilled") {
      for (const url of result.value.urls) {
        if (!urlMap.has(url)) {
          urlMap.set(url, result.value.keyword);
        }
      }
    } else {
      log.error({ err: result.reason }, "search failed");
      searchErrors++;
    }
  }

  log.info({ uniqueUrls: urlMap.size, searchErrors }, "all searches complete");

  const seenUrls = new Set<string>();

  log.info({ urls: urlMap.size }, "phase 2: processing urls");

  const processResults = await Promise.allSettled(
    Array.from(urlMap.entries()).map(([url, keyword]) =>
      processUrl(url, keyword, {
        notion,
        config,
        ledger,
        recentAppCompanies: cache.recentAppCompanies,
        seenUrls,
        filters,
      }),
    ),
  );

  const stats: ScrapeStats = {
    inserted: 0,
    skipped: 0,
    companyApplied: 0,
    rejected: 0,
    archived: 0,
    duplicated: 0,
    errored: 0,
  };

  for (const result of processResults) {
    if (result.status === "fulfilled") {
      const key = result.value as ProcessResult;
      if (key === "companyApplied") stats.companyApplied++;
      else if (key in stats) stats[key as keyof typeof stats]++;
    } else {
      log.error({ err: result.reason }, "url processing failed");
      stats.errored++;
    }
  }

  const postReconcileStats = await reconcile(notion, config.notionDatabaseId, "Post-scrape");

  log.info({ stats }, "scrape summary");
  log.info({ reconcile: preReconcileStats }, "pre-scrape reconcile summary");
  log.info({ reconcile: postReconcileStats }, "post-scrape reconcile summary");
  log.info({ prune: pruneStats }, "prune summary");

  await flushPending();

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
      Date.now() - startTime,
    );
  }
}

main().catch(async (err) => {
  log.fatal({ err }, "fatal error");
  await flushPending();
  if (config.slackWebhookUrl) {
    await sendFatalError(config.slackWebhookUrl, err);
  }
  process.exit(1);
});
