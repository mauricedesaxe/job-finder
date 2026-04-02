import type { Client } from "@notionhq/client";
import { logger } from "../logger";
import {
  checkRecentApplication,
  queryAppliedCompanies,
  queryJobsByStatus,
  queryJobsByStatusAndCompany,
  queryJobsWithApplicationDateNotStatus,
  queryRecentJobsByStatus,
  updateJobStatus,
} from "../services/notion";

const log = logger.child({ component: "reconcile" });

export interface ReconcileStats {
  applied: number;
  unstaled: number;
  companyApplied: number;
  archived: number;
}

export async function reconcile(
  client: Client,
  databaseId: string,
  label?: string,
): Promise<ReconcileStats> {
  const stats: ReconcileStats = { applied: 0, unstaled: 0, companyApplied: 0, archived: 0 };

  log.info({ label }, "reconciling statuses");

  // Pass 0: Auto-mark "Applied" from Application Date
  const jobsWithAppDate = await queryJobsWithApplicationDateNotStatus(
    client,
    databaseId,
    "Applied",
  );
  for (const job of jobsWithAppDate) {
    log.info({ jobId: job.id }, "marking as applied (has application date)");
    await updateJobStatus(client, job.id, "Applied");
    stats.applied++;
  }

  // Pass 1: Unstale "Company Applied" (only recent jobs, within 30 days)
  const companyAppliedJobs = await queryRecentJobsByStatus(
    client,
    databaseId,
    "Company Applied",
    30,
  );
  const companyAppliedCompanies = new Map<string, string[]>();

  for (const job of companyAppliedJobs) {
    const ids = companyAppliedCompanies.get(job.company) ?? [];
    ids.push(job.id);
    companyAppliedCompanies.set(job.company, ids);
  }

  for (const [company, pageIds] of companyAppliedCompanies) {
    const recency = await checkRecentApplication(client, databaseId, company);
    if (!recency.exists) {
      log.info({ company, count: pageIds.length }, "unstaling jobs (no recent application)");
      for (const id of pageIds) {
        await updateJobStatus(client, id, "To Review");
        stats.unstaled++;
      }
    }
  }

  // Pass 2: Propagate "Company Applied"
  // Collect all jobs to update first (read phase), then write — interleaving
  // reads and writes can invalidate Notion pagination cursors.
  const appliedCompanies = await queryAppliedCompanies(client, databaseId);
  const companyAppliedUpdates: Array<{ company: string; jobId: string }> = [];

  for (const company of appliedCompanies) {
    const toReviewJobs = await queryJobsByStatusAndCompany(
      client,
      databaseId,
      "To Review",
      company,
    );
    for (const job of toReviewJobs) {
      companyAppliedUpdates.push({ company, jobId: job.id });
    }
  }

  for (const { company, jobId } of companyAppliedUpdates) {
    log.info({ company }, "propagating company applied status");
    await updateJobStatus(client, jobId, "Company Applied");
    stats.companyApplied++;
  }

  // Pass 3: Propagate "Company Blocked" → archive
  const blockedJobs = await queryJobsByStatus(client, databaseId, "Company Blocked");
  const blockedCompanies = new Set(blockedJobs.map((j) => j.company));

  for (const company of blockedCompanies) {
    const toReviewJobs = await queryJobsByStatusAndCompany(
      client,
      databaseId,
      "To Review",
      company,
    );
    if (toReviewJobs.length > 0) {
      log.info({ company, count: toReviewJobs.length }, "archiving jobs (company blocked)");
      for (const job of toReviewJobs) {
        await updateJobStatus(client, job.id, "Archived");
        stats.archived++;
      }
    }
  }

  return stats;
}
