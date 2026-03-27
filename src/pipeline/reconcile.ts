import type { Client } from "@notionhq/client";
import {
  checkRecentApplication,
  queryAppliedCompanies,
  queryJobsByStatus,
  queryJobsByStatusAndCompany,
  updateJobStatus,
} from "../services/notion";

export interface ReconcileStats {
  unflagged: number;
  propagated: number;
}

export async function reconcile(
  client: Client,
  databaseId: string,
): Promise<ReconcileStats> {
  const stats: ReconcileStats = { unflagged: 0, propagated: 0 };

  console.log("\n--- Reconciling flags ---\n");

  // Pass 1: Unflag stale flags
  const flaggedJobs = await queryJobsByStatus(client, databaseId, "Flagged");
  const flaggedCompanies = new Map<string, string[]>();

  for (const job of flaggedJobs) {
    const ids = flaggedCompanies.get(job.company) ?? [];
    ids.push(job.id);
    flaggedCompanies.set(job.company, ids);
  }

  for (const [company, pageIds] of flaggedCompanies) {
    const recency = await checkRecentApplication(client, databaseId, company);
    if (!recency.exists) {
      console.log(`  Unflagging ${pageIds.length} job(s) from "${company}" (no recent application)`);
      for (const id of pageIds) {
        await updateJobStatus(client, id, "To Review");
        stats.unflagged++;
      }
    }
  }

  // Pass 2: Propagate missing flags
  const stillFlagged = await queryJobsByStatus(client, databaseId, "Flagged");
  const stillFlaggedCompanies = new Set(stillFlagged.map((j) => j.company));

  for (const company of stillFlaggedCompanies) {
    const unflaggedJobs = await queryJobsByStatusAndCompany(
      client,
      databaseId,
      "To Review",
      company,
    );
    if (unflaggedJobs.length > 0) {
      console.log(`  Flagging ${unflaggedJobs.length} job(s) from "${company}"`);
      for (const job of unflaggedJobs) {
        await updateJobStatus(client, job.id, "Flagged");
        stats.propagated++;
      }
    }
  }

  // Pass 3: Flag jobs from companies with recent applications
  const appliedCompanies = await queryAppliedCompanies(client, databaseId);

  for (const company of appliedCompanies) {
    const unflaggedJobs = await queryJobsByStatusAndCompany(
      client,
      databaseId,
      "To Review",
      company,
    );
    if (unflaggedJobs.length > 0) {
      console.log(`  Flagging ${unflaggedJobs.length} job(s) from "${company}" (recent application)`);
      for (const job of unflaggedJobs) {
        await updateJobStatus(client, job.id, "Flagged");
        stats.propagated++;
      }
    }
  }

  return stats;
}
