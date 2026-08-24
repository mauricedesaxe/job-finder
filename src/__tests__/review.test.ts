import { expect, test } from "bun:test";
import {
  type CompletedReview,
  type ReviewSnapshot,
  ReviewSnapshotSchema,
  replayCompletedReviewCompanyBlocks,
} from "../review";
import { createJobLedger } from "../services/jobLedger";

const snapshot: ReviewSnapshot = {
  traceId: "trace-123",
  promptRelease: "release-2026-08-23-1",
  job: {
    title: "Founding Product Engineer",
    company: "Acme",
    url: "https://jobs.example.com/1",
    source: "jobs.example.com",
    keywordsMatched: ["product engineer"],
    datePosted: null,
    dateScraped: "2026-08-23",
    description: "Build the product.",
    location: "Europe",
    profile: "early-stage-product-engineer",
  },
  ats: null,
  compensationRates: "EURUSD=1.16",
  evaluation: {
    profile: "early-stage-product-engineer",
    reason: "Hands-on product delivery.",
  },
};

test("parses a review snapshot with frozen evaluation inputs", () => {
  expect(ReviewSnapshotSchema.parse(snapshot)).toEqual(snapshot);
});

test("rejects dates the job parser cannot produce", () => {
  expect(() =>
    ReviewSnapshotSchema.parse({
      ...snapshot,
      job: { ...snapshot.job, datePosted: "23 August 2026" },
    }),
  ).toThrow();
});

test("rejects snapshots with conflicting target profiles", () => {
  expect(() =>
    ReviewSnapshotSchema.parse({
      ...snapshot,
      evaluation: { ...snapshot.evaluation, profile: "applied-ai-product-engineer" },
    }),
  ).toThrow("profiles must match");
});

test("applies company blocks idempotently before URL processing", async () => {
  const ledger = createJobLedger(":memory:");
  try {
    const firstReplay = await replayCompletedReviewCompanyBlocks({
      reviews: completedReviews(),
      ledger,
    });

    expect(firstReplay).toEqual({ reviews: 2, companyExclusions: 1 });
    expect(ledger.findCompanyExclusion("ACME")).toEqual({
      company: "Acme",
      excludedAt: "2026-08-24T10:30:00.000Z",
    });
    expect(ledger.findCompanyExclusion("Beta")).toBeNull();

    const secondReplay = await replayCompletedReviewCompanyBlocks({
      reviews: completedReviews(),
      ledger,
    });

    expect(secondReplay).toEqual(firstReplay);
    expect(ledger.findCompanyExclusion("Acme")).toEqual({
      company: "Acme",
      excludedAt: "2026-08-24T10:30:00.000Z",
    });
  } finally {
    ledger.close();
  }
});

async function* completedReviews(): AsyncIterable<CompletedReview> {
  yield {
    runId: "trace-acme",
    reviewedAt: "2026-08-24T10:30:00.000Z",
    snapshot: { ...snapshot, traceId: "trace-acme" },
    decision: "reject",
    targetProfile: "early-stage-product-engineer",
    primaryReason: "company-quality",
    blockCompany: true,
  };
  yield {
    runId: "trace-beta",
    reviewedAt: "2026-08-24T10:31:00.000Z",
    snapshot: {
      ...snapshot,
      traceId: "trace-beta",
      job: { ...snapshot.job, company: "Beta" },
    },
    decision: "reject",
    targetProfile: "early-stage-product-engineer",
    primaryReason: "role-scope",
    blockCompany: false,
  };
}
