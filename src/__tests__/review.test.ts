import { expect, test } from "bun:test";
import { ReviewFeedbackSchema, type ReviewSnapshot, ReviewSnapshotSchema } from "../review";

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

test("rejects a review feedback profile outside the target set", () => {
  expect(() =>
    ReviewFeedbackSchema.parse({
      decision: "pursue",
      targetProfile: "crypto-web3-ts",
      primaryReason: "technology-fit",
    }),
  ).toThrow();
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
