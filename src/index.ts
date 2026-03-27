import { isRetryableJina, jinaBreaker, jinaSearchSemaphore, withRetry } from "./concurrency";
import { config } from "./config";
import { type ProcessResult, processUrl } from "./pipeline/processUrl";
import { reconcile } from "./pipeline/reconcile";
import { searchJobs } from "./pipeline/search";
import { runPreflight } from "./preflight";
import { createNotionClient } from "./services/notion";
import { buildNotionCache, CacheSyncer } from "./services/notionCache";

function validateConfig() {
  const missing: string[] = [];
  if (!config.notionToken) missing.push("NOTION_TOKEN");
  if (!config.notionDatabaseId) missing.push("NOTION_DATABASE_ID");
  if (!config.jinaApiKey) missing.push("JINA_API_KEY");
  if (!config.anthropicApiKey) missing.push("ANTHROPIC_API_KEY");

  if (missing.length > 0) {
    console.error(`Missing env vars: ${missing.join(", ")}`);
    console.error("Copy .env.example to .env and fill in your credentials.");
    process.exit(1);
  }
}

async function main() {
  validateConfig();

  const notion = createNotionClient(config.notionToken);
  await runPreflight(notion, config.notionDatabaseId);

  const preReconcileStats = await reconcile(notion, config.notionDatabaseId, "Pre-scrape");

  // Pre-cache Notion data to avoid per-URL queries
  console.log("\nBuilding Notion cache...");
  const cache = await buildNotionCache(notion, config.notionDatabaseId, {
    onProgress: (n) => process.stdout.write(`\r  Fetching... ${n} items`),
  });
  process.stdout.write("\n");
  console.log(
    `  Cached: ${cache.existingUrls.size} URLs, ${cache.blockedCompanies.size} blocked companies, ` +
      `${cache.recentAppCompanies.size} recent app companies, ${cache.jobsByCompany.size} companies with jobs`,
  );

  const syncer = new CacheSyncer(cache);

  // Phase 1: Parallel search — collect all URLs
  const searchPairs = config.keywords.flatMap((keyword) =>
    config.domains.map((domain) => ({ keyword, domain })),
  );

  console.log(
    `\nPhase 1: Searching ${searchPairs.length} keyword×domain pairs (concurrency: 5)...\n`,
  );

  const urlMap = new Map<string, string>(); // url → keyword

  const searchResults = await Promise.allSettled(
    searchPairs.map(({ keyword, domain }) =>
      jinaSearchSemaphore.run(async () => {
        const urls = await jinaBreaker.run(() =>
          withRetry(() => searchJobs(keyword, domain, config), {
            shouldRetry: isRetryableJina,
            onRetry: (a) => console.log(`  ⏳ Search retry ${a}: site:${domain} ${keyword}`),
          }),
        );
        console.log(`  🔍 site:${domain} ${keyword} → ${urls.length} URLs`);
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
      console.error(`  ✗ Search failed: ${result.reason}`);
      searchErrors++;
    }
  }

  console.log(
    `\nSearch complete: ${urlMap.size} unique URLs found (${searchErrors} search errors)`,
  );

  // Phase 2: Parallel URL processing
  const seenUrls = new Set<string>();

  console.log(
    `\nPhase 2: Processing ${urlMap.size} URLs (Jina: 8, Anthropic: 10, Notion: 3 req/s)...\n`,
  );

  syncer.start(notion, config.notionDatabaseId);

  const processResults = await Promise.allSettled(
    Array.from(urlMap.entries()).map(([url, keyword]) =>
      processUrl(url, keyword, { notion, config, syncer, seenUrls }),
    ),
  );

  syncer.stop();

  // Aggregate stats
  const stats = {
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
      console.error(`  ✗ Process failed: ${result.reason}`);
      stats.errored++;
    }
  }

  const postReconcileStats = await reconcile(notion, config.notionDatabaseId, "Post-scrape");

  console.log("\n--- Scrape Summary ---");
  console.log(`Inserted:        ${stats.inserted}`);
  console.log(`Company Applied: ${stats.companyApplied}`);
  console.log(`Rejected:        ${stats.rejected}`);
  console.log(`Duplicated:      ${stats.duplicated}`);
  console.log(`Archived:        ${stats.archived}`);
  console.log(`Skipped:         ${stats.skipped}`);
  console.log(`Errored:         ${stats.errored}`);

  console.log("\n--- Pre-scrape Reconcile Summary ---");
  console.log(`Auto-Applied:    ${preReconcileStats.applied}`);
  console.log(`Unstaled:        ${preReconcileStats.unstaled}`);
  console.log(`Company Applied: ${preReconcileStats.companyApplied}`);
  console.log(`Archived:        ${preReconcileStats.archived}`);

  console.log("\n--- Post-scrape Reconcile Summary ---");
  console.log(`Auto-Applied:    ${postReconcileStats.applied}`);
  console.log(`Unstaled:        ${postReconcileStats.unstaled}`);
  console.log(`Company Applied: ${postReconcileStats.companyApplied}`);
  console.log(`Archived:        ${postReconcileStats.archived}`);
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
