import type { JobLedger, PendingReviewProjection } from "../services/jobLedger";
import { enqueueReviewTrace, enqueueReviewTraceIfMissing } from "../services/langsmith";

export async function projectPendingReviewProjection({
  ledger,
  projection,
  enqueue = enqueueReviewTrace,
}: {
  ledger: JobLedger;
  projection: PendingReviewProjection;
  enqueue?: (traceId: string) => Promise<void>;
}): Promise<void> {
  await enqueue(projection.traceId);
  await ledger.markReviewProjectionComplete(projection.sourceKey);
}

export async function replayPendingReviewProjections(ledger: JobLedger): Promise<void> {
  const errors: unknown[] = [];
  for (const projection of await ledger.listPendingReviewProjections()) {
    try {
      await projectPendingReviewProjection({
        ledger,
        projection,
        enqueue: enqueueReviewTraceIfMissing,
      });
    } catch (error) {
      errors.push(error);
    }
  }
  if (errors.length > 0) {
    throw new AggregateError(errors, "Could not replay all pending review projections");
  }
}
