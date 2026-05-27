import { REAPPLY_WINDOW_MONTHS } from "../config/recency";
import { monthsAgo } from "../dates";
import { logger } from "../logger";
import { queryJobsScrapedBefore, type ResilientNotionClient, trashJob } from "../services/notion";
import type { JobStatus } from "../types";

const log = logger.child({ component: "prune" });

// Compared against the (untrusted) Notion status string. Typed as JobStatus so a
// rename of the status option surfaces here at compile time instead of silently
// never matching and quietly un-protecting blocked companies.
const COMPANY_BLOCKED: JobStatus = "Company Blocked";

export interface PruneStats {
  scanned: number;
  pruned: number;
  failed: number;
  keptBlocked: number;
  keptLocked: number;
  keptNoDate: number;
}

export interface PrunableJob {
  status: string;
  applicationDate: string | null;
  dateScraped: string | null;
}

export type PruneDecision = "prune" | "keep-blocked" | "keep-locked" | "keep-no-date";

/**
 * What to do with a candidate job page (one scraped before the reapply window).
 * We keep two kinds and prune the rest:
 *  - `keep-blocked`: "Company Blocked" is a permanent signal; trashing it would
 *    un-block the company on the next run.
 *  - `keep-locked`: an Application Date still inside the window is the live lock
 *    that stops us re-applying to the company.
 *  - `keep-no-date`: defensive — a candidate with no Date Scraped (the query
 *    shouldn't return these; we never guess at an unknown age).
 * Everything else past the window (To Review / Skipped / Rejected /
 * Auto-Rejected / Archived, and expired applications) is `prune`.
 */
export function pruneDecision(job: PrunableJob, cutoff: Date): PruneDecision {
  if (job.status === COMPANY_BLOCKED) return "keep-blocked";
  if (job.applicationDate && new Date(job.applicationDate) >= cutoff) return "keep-locked";
  if (job.dateScraped !== null && new Date(job.dateScraped) < cutoff) return "prune";
  return "keep-no-date";
}

/**
 * Trash job pages older than the reapply window so the database — and every
 * full-table scan that runs at startup (reconcile, cache build) — stays bounded
 * as scrape volume grows. Trashing (not hard-deleting) keeps a recovery window
 * in Notion. Idempotent: re-running only trashes newly-aged pages.
 *
 * A single page that fails to trash is counted and skipped, never thrown — one
 * bad page must not abort the run before the cache build (the exact startup
 * fragility this pipeline guards against).
 */
export async function prune(
  client: ResilientNotionClient,
  databaseId: string,
): Promise<PruneStats> {
  const cutoff = monthsAgo(REAPPLY_WINDOW_MONTHS);
  const candidates = await queryJobsScrapedBefore(client, databaseId, cutoff);

  const stats: PruneStats = {
    scanned: candidates.length,
    pruned: 0,
    failed: 0,
    keptBlocked: 0,
    keptLocked: 0,
    keptNoDate: 0,
  };

  for (const job of candidates) {
    switch (pruneDecision(job, cutoff)) {
      case "prune":
        try {
          await trashJob(client, job.id);
          stats.pruned++;
        } catch (err) {
          stats.failed++;
          log.warn({ err, pageId: job.id }, "prune: failed to trash page");
        }
        break;
      case "keep-blocked":
        stats.keptBlocked++;
        break;
      case "keep-locked":
        stats.keptLocked++;
        break;
      case "keep-no-date":
        stats.keptNoDate++;
        break;
    }
  }

  log.info(stats, "prune complete");
  return stats;
}
