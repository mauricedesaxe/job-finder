import type { Client } from "@notionhq/client";
import type { ScrapioConfig } from "../types";
import type { CacheSyncer } from "../services/notionCache";
import { scrapeJobPage, parseJobDetails } from "./scrape";
import { evaluateJob } from "./evaluate";
import { enrichJob } from "./enrich";
import { checkFuzzyDuplicate } from "./dedup";
import { insertJob } from "../services/notion";
import {
  jinaReaderSemaphore,
  anthropicSemaphore,
  notionRateLimiter,
  jinaBreaker,
  anthropicBreaker,
  notionBreaker,
  withRetry,
  isRetryableJina,
  isRetryableAnthropic,
  isRetryableNotion,
} from "../concurrency";

export type ProcessResult =
  | "inserted"
  | "rejected"
  | "duplicated"
  | "skipped"
  | "companyApplied"
  | "archived"
  | "errored";

export interface ProcessContext {
  notion: Client;
  config: ScrapioConfig;
  syncer: CacheSyncer;
  seenUrls: Set<string>;
}

export async function processUrl(
  url: string,
  keyword: string,
  ctx: ProcessContext,
): Promise<ProcessResult> {
  const { notion, config, syncer, seenUrls } = ctx;
  const cache = syncer.cache;

  // In-run dedup
  if (seenUrls.has(url)) return "skipped";
  seenUrls.add(url);

  // Cache-based URL dedup
  if (cache.existingUrls.has(url)) {
    console.log(`  ⏭ Skipped (exists): ${url}`);
    return "skipped";
  }

  // Scrape
  const markdown = await jinaReaderSemaphore.run(() =>
    jinaBreaker.run(() =>
      withRetry(() => scrapeJobPage(url, config), {
        shouldRetry: isRetryableJina,
        onRetry: (a) => console.log(`  ⏳ Jina retry ${a} for ${url}`),
      }),
    ),
  );
  const job = parseJobDetails(markdown, url, keyword);

  // Evaluate (profiles run in parallel internally)
  const evaluation = await anthropicSemaphore.run(() =>
    anthropicBreaker.run(() =>
      withRetry(() => evaluateJob(job, config.anthropicApiKey), {
        shouldRetry: isRetryableAnthropic,
        onRetry: (a) => console.log(`  ⏳ Anthropic eval retry ${a} for ${url}`),
      }),
    ),
  );

  if (!evaluation.pass) {
    console.log(`  ✗ Rejected: ${job.title} @ ${job.company} — ${evaluation.reason}`);
    await notionRateLimiter.run(() =>
      notionBreaker.run(() =>
        withRetry(() => insertJob(notion, config.notionDatabaseId, job, "Rejected"), {
          shouldRetry: isRetryableNotion,
        }),
      ),
    );
    return "rejected";
  }

  // Enrich
  const enriched = await anthropicSemaphore.run(() =>
    anthropicBreaker.run(() =>
      withRetry(() => enrichJob(job, config.anthropicApiKey), {
        shouldRetry: isRetryableAnthropic,
        onRetry: (a) => console.log(`  ⏳ Anthropic enrich retry ${a} for ${url}`),
      }),
    ),
  );
  job.title = enriched.title;
  job.company = enriched.company;
  job.description = enriched.description;
  job.location = enriched.location;

  // Fuzzy dedup (cache-based company lookup, LLM for title comparison)
  const existingTitles = cache.jobsByCompany.get(job.company) ?? [];
  if (existingTitles.length > 0) {
    const dedup = await anthropicSemaphore.run(() =>
      withRetry(() => checkFuzzyDuplicate(job.title, existingTitles, config.anthropicApiKey), {
        shouldRetry: isRetryableAnthropic,
      }),
    );
    if (dedup.isDuplicate) {
      console.log(`  ⏭ Duplicate: ${job.title} @ ${job.company} — matches "${dedup.matchedTitle}"`);
      return "duplicated";
    }
  }

  // Company blocked (cache lookup)
  if (cache.blockedCompanies.has(job.company)) {
    console.log(`  ✗ Archived (company blocked): ${job.title} @ ${job.company}`);
    await notionRateLimiter.run(() =>
      notionBreaker.run(() =>
        withRetry(() => insertJob(notion, config.notionDatabaseId, job, "Archived"), {
          shouldRetry: isRetryableNotion,
        }),
      ),
    );
    return "archived";
  }

  // Recent application (cache lookup)
  if (cache.recentAppCompanies.has(job.company)) {
    console.log(`  ⚠ Company Applied: ${job.title} @ ${job.company}`);
    await notionRateLimiter.run(() =>
      notionBreaker.run(() =>
        withRetry(() => insertJob(notion, config.notionDatabaseId, job, "Company Applied"), {
          shouldRetry: isRetryableNotion,
        }),
      ),
    );
    syncer.addTitle(job.company, job.title);
    syncer.addUrl(url);
    return "companyApplied";
  }

  // Insert
  await notionRateLimiter.run(() =>
    notionBreaker.run(() =>
      withRetry(() => insertJob(notion, config.notionDatabaseId, job), {
        shouldRetry: isRetryableNotion,
      }),
    ),
  );
  console.log(`  ✓ Inserted: ${job.title} @ ${job.company}`);

  // Update cache for within-run dedup
  syncer.addTitle(job.company, job.title);
  syncer.addUrl(url);

  return "inserted";
}
