import { isRetryableJina, jinaBreaker, jinaSearchSemaphore, withRetry } from "./concurrency";
import { config } from "./config";
import { logger } from "./logger";
import { type ProcessResult, processUrl, type ScrapeStats } from "./pipeline/processUrl";
import { reconcile } from "./pipeline/reconcile";
import { searchJobs } from "./pipeline/search";
import { runPreflight } from "./preflight";
import { createNotionClient } from "./services/notion";
import { buildNotionCache, CacheSyncer } from "./services/notionCache";
import { sendFatalError, sendRunReport } from "./services/slack";

const log = logger.child({ component: "main" });
const reconcileOnly = process.argv.includes("--reconcile-only");

async function main() {
  const startTime = Date.now();
  const notion = createNotionClient(config.notionToken);
  await runPreflight(notion, config.notionDatabaseId);

  if (reconcileOnly) {
    const stats = await reconcile(notion, config.notionDatabaseId);
    log.info({ stats, durationMs: Date.now() - startTime }, "reconciliation complete");
    return;
  }

  const preReconcileStats = await reconcile(notion, config.notionDatabaseId, "Pre-scrape");

  // Pre-cache Notion data to avoid per-URL queries
  log.info("building notion cache");
  const cache = await buildNotionCache(notion, config.notionDatabaseId);
  log.info(
    {
      urls: cache.existingUrls.size,
      blocked: cache.blockedCompanies.size,
      recentApps: cache.recentAppCompanies.size,
      companies: cache.jobsByCompany.size,
    },
    "notion cache built",
  );

  const syncer = new CacheSyncer(cache);

  // Phase 1: Parallel search — collect all URLs
  const searchPairs = config.keywords.flatMap((keyword) =>
    config.domains.map((domain) => ({ keyword, domain })),
  );

  log.info({ pairs: searchPairs.length }, "phase 1: searching");

  const urlMap = new Map<string, string>(); // url → keyword

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

  // Phase 2: Parallel URL processing
  const seenUrls = new Set<string>();

  log.info({ urls: urlMap.size }, "phase 2: processing urls");

  syncer.start(notion, config.notionDatabaseId);

  const processResults = await Promise.allSettled(
    Array.from(urlMap.entries()).map(([url, keyword]) =>
      processUrl(url, keyword, { notion, config, syncer, seenUrls }),
    ),
  );

  syncer.stop();

  // Aggregate stats
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

  if (config.slackWebhookUrl) {
    await sendRunReport(
      config.slackWebhookUrl,
      stats,
      postReconcileStats,
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
  if (config.slackWebhookUrl) {
    await sendFatalError(config.slackWebhookUrl, err);
  }
  process.exit(1);
});
