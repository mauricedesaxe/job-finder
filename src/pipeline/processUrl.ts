import type { Client } from "@notionhq/client";
import {
  anthropicBreaker,
  anthropicSemaphore,
  isRetryableAnthropic,
  isRetryableJina,
  isRetryableNotion,
  jinaBreaker,
  jinaReaderSemaphore,
  notionBreaker,
  notionRateLimiter,
  withRetry,
} from "../concurrency";
import type { JobFinderConfig } from "../config";
import type { EvaluationFilter } from "../config/evaluation";
import { logger } from "../logger";
import { insertJob } from "../services/notion";
import type { NotionCacheUpdater } from "../services/notionCache";
import type { TokenTracker } from "../services/tokenTracker";
import { checkFuzzyDuplicate } from "./dedup";
import { enrichJob } from "./enrich";
import { evaluateJob } from "./evaluate";
import { parseJobDetails, scrapeJobPage } from "./scrape";

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
  notion: Client;
  config: JobFinderConfig;
  syncer: NotionCacheUpdater;
  seenUrls: Set<string>;
  tracker?: TokenTracker;
  filters?: EvaluationFilter[];
}

export async function processUrl(
  url: string,
  keyword: string,
  ctx: ProcessContext,
): Promise<ProcessResult> {
  const { notion, config, syncer, seenUrls, tracker } = ctx;
  const cache = syncer.cache;

  // In-run dedup
  if (seenUrls.has(url)) return "skipped";
  seenUrls.add(url);

  // Cache-based URL dedup
  if (cache.existingUrls.has(url)) {
    log.debug({ url }, "skipped (exists in cache)");
    return "skipped";
  }

  // Scrape
  const markdown = await jinaReaderSemaphore.run(() =>
    jinaBreaker.run(() =>
      withRetry(() => scrapeJobPage(url, config), {
        shouldRetry: isRetryableJina,
        onRetry: (a) => log.warn({ url, attempt: a }, "jina scrape retry"),
      }),
    ),
  );
  const job = parseJobDetails(markdown, url, keyword);

  // Evaluate (profiles run in parallel internally)
  const evaluation = await anthropicSemaphore.run(() =>
    anthropicBreaker.run(() =>
      withRetry(() => evaluateJob(job, config.anthropicApiKey, { tracker, filters: ctx.filters }), {
        shouldRetry: isRetryableAnthropic,
        onRetry: (a) => log.warn({ url, attempt: a }, "anthropic eval retry"),
      }),
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
      withRetry(() => enrichJob(job, config.anthropicApiKey, tracker), {
        shouldRetry: isRetryableAnthropic,
        onRetry: (a) => log.warn({ url, attempt: a }, "anthropic enrich retry"),
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
      withRetry(
        () => checkFuzzyDuplicate(job.title, existingTitles, config.anthropicApiKey, tracker),
        { shouldRetry: isRetryableAnthropic },
      ),
    );
    if (dedup.isDuplicate) {
      log.info(
        { url, title: job.title, company: job.company, matchedTitle: dedup.matchedTitle },
        "duplicate",
      );
      return "duplicated";
    }
  }

  // Company blocked (cache lookup)
  if (cache.blockedCompanies.has(job.company)) {
    log.info({ url, title: job.title, company: job.company }, "archived (company blocked)");
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
    log.info({ url, title: job.title, company: job.company }, "company applied");
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
  log.info({ url, title: job.title, company: job.company }, "inserted");

  // Update cache for within-run dedup
  syncer.addTitle(job.company, job.title);
  syncer.addUrl(url);

  return "inserted";
}
