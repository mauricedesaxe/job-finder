import { config } from "./config";
import { searchJobs } from "./pipeline/search";
import { scrapeJobPage, parseJobDetails } from "./pipeline/scrape";
import {
  createNotionClient,
  checkDuplicateUrl,
  checkRecentApplication,
  queryCompanyBlocked,
  insertJob,
} from "./services/notion";
import { reconcile } from "./pipeline/reconcile";
import { evaluateJob } from "./pipeline/evaluate";
import { enrichJob } from "./pipeline/enrich";
import { runPreflight } from "./preflight";

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

  const stats = { inserted: 0, skipped: 0, companyApplied: 0, rejected: 0, archived: 0, errored: 0 };
  const seenUrls = new Set<string>();

  console.log(
    `Starting scrape: ${config.keywords.length} keywords × ${config.domains.length} domains (up to ${config.maxPages} pages each)\n`,
  );

  for (const keyword of config.keywords) {
    for (const domain of config.domains) {
      console.log(`\n🔍 Searching: site:${domain} ${keyword}`);

      let jobUrls: string[];
      try {
        jobUrls = await searchJobs(keyword, domain, config);
      } catch (err) {
        console.error(`  ✗ Search failed: ${err}`);
        stats.errored++;
        continue;
      }

      console.log(`  Found ${jobUrls.length} URLs`);

      for (const url of jobUrls) {
        // Skip URLs already processed in this run
        if (seenUrls.has(url)) continue;
        seenUrls.add(url);

        try {
          // Check Notion for duplicate URL
          const isDuplicate = await checkDuplicateUrl(
            notion,
            config.notionDatabaseId,
            url,
          );
          if (isDuplicate) {
            console.log(`  ⏭ Skipped (exists): ${url}`);
            stats.skipped++;
            continue;
          }

          // Scrape and parse the job page
          await Bun.sleep(config.delayBetweenRequests);
          const markdown = await scrapeJobPage(url, config);
          const job = parseJobDetails(markdown, url, keyword);

          // LLM evaluation
          const evaluation = await evaluateJob(job, config.anthropicApiKey);
          if (!evaluation.pass) {
            console.log(`  ✗ Rejected: ${job.title} @ ${job.company} — ${evaluation.reason}`);
            await insertJob(notion, config.notionDatabaseId, job, "Rejected");
            stats.rejected++;
            continue;
          }

          // Enrich job data with LLM normalization
          const enriched = await enrichJob(job, config.anthropicApiKey);
          job.title = enriched.title;
          job.company = enriched.company;
          job.description = enriched.description;
          job.location = enriched.location;

          // Determine status based on company state
          const isBlocked = await queryCompanyBlocked(
            notion,
            config.notionDatabaseId,
            job.company,
          );

          if (isBlocked) {
            console.log(`  ✗ Archived (company blocked): ${job.title} @ ${job.company}`);
            await insertJob(notion, config.notionDatabaseId, job, "Archived");
            stats.archived++;
            continue;
          }

          const recency = await checkRecentApplication(
            notion,
            config.notionDatabaseId,
            job.company,
          );

          if (recency.exists) {
            console.log(`  ⚠ Company Applied: ${job.title} @ ${job.company}`);
            await insertJob(notion, config.notionDatabaseId, job, "Company Applied");
            stats.companyApplied++;
          } else {
            await insertJob(notion, config.notionDatabaseId, job);
            console.log(`  ✓ Inserted: ${job.title} @ ${job.company}`);
            stats.inserted++;
          }
        } catch (err) {
          console.error(`  ✗ Failed: ${url} — ${err}`);
          stats.errored++;
        }
      }
    }
  }

  const reconcileStats = await reconcile(notion, config.notionDatabaseId);

  console.log("\n--- Scrape Summary ---");
  console.log(`Inserted:        ${stats.inserted}`);
  console.log(`Company Applied: ${stats.companyApplied}`);
  console.log(`Rejected:        ${stats.rejected}`);
  console.log(`Archived:        ${stats.archived}`);
  console.log(`Skipped:         ${stats.skipped}`);
  console.log(`Errored:         ${stats.errored}`);

  console.log("\n--- Reconcile Summary ---");
  console.log(`Auto-Applied:    ${reconcileStats.applied}`);
  console.log(`Unstaled:        ${reconcileStats.unstaled}`);
  console.log(`Company Applied: ${reconcileStats.companyApplied}`);
  console.log(`Archived:        ${reconcileStats.archived}`);
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
