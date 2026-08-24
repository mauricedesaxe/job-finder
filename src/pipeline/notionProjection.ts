import type { JobLedger, PendingNotionProjection } from "../services/jobLedger";
import { checkDuplicateUrl, insertJob, type ResilientNotionClient } from "../services/notion";

export async function projectPendingNotionProjection({
  ledger,
  notion,
  databaseId,
  projection,
}: {
  ledger: JobLedger;
  notion: ResilientNotionClient;
  databaseId: string;
  projection: PendingNotionProjection;
}): Promise<void> {
  const pageExists = await checkDuplicateUrl(notion, databaseId, projection.job.url);
  if (!pageExists) {
    await insertJob(notion, databaseId, projection.job, projection.status);
  }
  await ledger.markNotionProjectionComplete(projection.sourceKey);
}

export async function replayPendingNotionProjections({
  ledger,
  notion,
  databaseId,
}: {
  ledger: JobLedger;
  notion: ResilientNotionClient;
  databaseId: string;
}): Promise<void> {
  const errors: unknown[] = [];
  for (const projection of await ledger.listPendingNotionProjections()) {
    try {
      await projectPendingNotionProjection({ ledger, notion, databaseId, projection });
    } catch (error) {
      errors.push(error);
    }
  }
  if (errors.length > 0) {
    throw new AggregateError(errors, "Could not replay all pending Notion projections");
  }
}
