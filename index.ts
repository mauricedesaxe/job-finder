import { config } from "./config";
import { searchJobs } from "./search";
import { scrapeJobPage, parseJobDetails } from "./scrape";
import {
  createNotionClient,
  checkDuplicateUrl,
  checkRecentApplication,
  insertJob,
} from "./notion";
import { evaluateJob } from "./evaluate";
import { enrichJob } from "./enrich";

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
  const stats = { inserted: 0, skipped: 0, flagged: 0, rejected: 0, errored: 0 };
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

          // LLM evaluation — reject jobs that don't match requirements
          const evaluation = await evaluateJob(job, config.anthropicApiKey);
          if (!evaluation.pass) {
            console.log(`  ✗ Rejected: ${job.title} @ ${job.company} — ${evaluation.reason}`);
            stats.rejected++;
            continue;
          }

          // Enrich job data with LLM normalization
          const enriched = await enrichJob(job, config.anthropicApiKey);
          job.title = enriched.title;
          job.company = enriched.company;
          job.description = enriched.description;
          job.location = enriched.location;

          // Check for recent application from same company
          const recency = await checkRecentApplication(
            notion,
            config.notionDatabaseId,
            job.company,
          );

          if (recency.exists) {
            console.log(
              `  ⚠ Flagged (recent application): ${job.title} @ ${job.company}`,
            );
            await insertJob(notion, config.notionDatabaseId, job, true);
            stats.flagged++;
          } else {
            await insertJob(notion, config.notionDatabaseId, job, false);
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

  console.log("\n--- Summary ---");
  console.log(`Inserted: ${stats.inserted}`);
  console.log(`Flagged:  ${stats.flagged}`);
  console.log(`Rejected: ${stats.rejected}`);
  console.log(`Skipped:  ${stats.skipped}`);
  console.log(`Errored:  ${stats.errored}`);
}

main();
