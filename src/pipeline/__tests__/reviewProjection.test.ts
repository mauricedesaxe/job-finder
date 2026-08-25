import { afterEach, expect, test } from "bun:test";
import { createSqliteJobLedger } from "../../services/sqliteJobLedger";
import { projectPendingReviewProjection } from "../reviewProjection";

let ledger: ReturnType<typeof createSqliteJobLedger> | undefined;

afterEach(async () => {
  await ledger?.close();
  ledger = undefined;
});

test("replays a pending review projection with the same trace", async () => {
  ledger = createSqliteJobLedger(":memory:");
  const stored = await ledger.recordProcessedJob({
    rawUrl: "https://jobs.example.com/review",
    company: "Review Co",
    title: "Review Engineer",
    outcome: "inserted",
    projections: {
      kind: "notion-and-review",
      notion: {
        job: reviewJob("https://jobs.example.com/review"),
        status: "To Review",
        createdAt: "2026-08-24T22:00:00.000Z",
      },
      review: {
        traceId: "trace-1",
        createdAt: "2026-08-24T22:00:00.000Z",
      },
    },
  });
  if (stored.kind !== "notion-and-review") throw new Error("Expected pending projections");
  const enqueued: string[] = [];

  await projectPendingReviewProjection({
    ledger,
    projection: stored.review,
    enqueue: async (traceId) => {
      enqueued.push(traceId);
    },
  });

  expect(enqueued).toEqual(["trace-1"]);
  expect(await ledger.listPendingReviewProjections()).toEqual([]);
});

test("retains a pending review projection when queue projection fails", async () => {
  ledger = createSqliteJobLedger(":memory:");
  const stored = await ledger.recordProcessedJob({
    rawUrl: "https://jobs.example.com/review-failure",
    company: "Review Co",
    title: "Review Engineer",
    outcome: "inserted",
    projections: {
      kind: "notion-and-review",
      notion: {
        job: reviewJob("https://jobs.example.com/review-failure"),
        status: "To Review",
        createdAt: "2026-08-24T22:00:00.000Z",
      },
      review: {
        traceId: "trace-2",
        createdAt: "2026-08-24T22:00:00.000Z",
      },
    },
  });
  if (stored.kind !== "notion-and-review") throw new Error("Expected pending projections");

  await expect(
    projectPendingReviewProjection({
      ledger,
      projection: stored.review,
      enqueue: async () => {
        throw new Error("queue unavailable");
      },
    }),
  ).rejects.toThrow("queue unavailable");
  expect(await ledger.listPendingReviewProjections()).toEqual([stored.review]);
});

function reviewJob(url: string) {
  return {
    title: "Review Engineer",
    company: "Review Co",
    url,
    source: "Example",
    keywordsMatched: ["engineer"],
    datePosted: null,
    dateScraped: "2026-08-24",
    description: "Review projection",
    location: "Remote",
    profile: "Backend",
  };
}
