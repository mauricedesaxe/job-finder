import { expect, test } from "bun:test";
import type { ReviewSnapshot } from "../../review";
import { createReviewQueue, JOB_REVIEW_QUEUE_NAME } from "../reviewQueue";

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

test("creates the review queue and records a snapshot before it enqueues the trace", async () => {
  const calls: string[] = [];
  const queue = createReviewQueue({
    createFeedbackConfig: async ({ feedbackKey }) => {
      calls.push(`config:${feedbackKey}`);
    },
    async *listAnnotationQueues({ name }) {
      expect(name).toBe(JOB_REVIEW_QUEUE_NAME);
      yield* [];
    },
    createAnnotationQueue: async ({ name, rubricItems }) => {
      calls.push(`queue:${name}`);
      expect(rubricItems.map((item) => item.feedback_key)).toEqual([
        "review_decision",
        "target_profile",
        "primary_reason",
        "block_company",
        "review_note",
      ]);
      return { id: "queue-123" };
    },
    updateRun: async (traceId, run) => {
      calls.push(`snapshot:${traceId}`);
      expect(run.extra.metadata.review_snapshot).toEqual(snapshot);
    },
    annotationQueues: {
      items: {
        create: async (queueId, input) => {
          calls.push(`enqueue:${queueId}:${input.items.map((item) => item.run_id).join(",")}`);
          expect(input.extend_trace_retention).toBe(true);
        },
      },
    },
  });

  await queue.enqueue(snapshot);

  expect(calls).toContain("queue:job-finder-job-review");
  expect(calls.indexOf("snapshot:trace-123")).toBeLessThan(
    calls.indexOf("enqueue:queue-123:trace-123"),
  );
});

test("reuses an existing queue and its initialized queue id", async () => {
  let queueSearches = 0;
  let queueCreates = 0;
  const enqueued: string[][] = [];
  const queue = createReviewQueue({
    createFeedbackConfig: async () => {},
    async *listAnnotationQueues() {
      queueSearches++;
      yield { id: "existing-queue" };
    },
    createAnnotationQueue: async () => {
      queueCreates++;
      return { id: "new-queue" };
    },
    updateRun: async () => {},
    annotationQueues: {
      items: {
        create: async (_queueId, input) => {
          enqueued.push(input.items.map((item) => item.run_id));
        },
      },
    },
  });

  await queue.enqueue(snapshot);
  await queue.enqueue({ ...snapshot, traceId: "trace-456" });

  expect(queueSearches).toBe(1);
  expect(queueCreates).toBe(0);
  expect(enqueued).toEqual([["trace-123"], ["trace-456"]]);
});

test("shares queue initialization across concurrent qualified traces", async () => {
  let queueSearches = 0;
  const queue = createReviewQueue({
    createFeedbackConfig: async () => {},
    async *listAnnotationQueues() {
      queueSearches++;
      yield { id: "existing-queue" };
    },
    createAnnotationQueue: async () => ({ id: "new-queue" }),
    updateRun: async () => {},
    annotationQueues: { items: { create: async () => {} } },
  });

  await Promise.all([
    queue.enqueue(snapshot),
    queue.enqueue({ ...snapshot, traceId: "trace-456" }),
  ]);

  expect(queueSearches).toBe(1);
});

test("retries queue initialization after a transient failure", async () => {
  let attempts = 0;
  const queue = createReviewQueue({
    createFeedbackConfig: async () => {},
    async *listAnnotationQueues() {
      attempts++;
      if (attempts === 1) throw new Error("LangSmith unavailable");
      yield { id: "existing-queue" };
    },
    createAnnotationQueue: async () => ({ id: "new-queue" }),
    updateRun: async () => {},
    annotationQueues: { items: { create: async () => {} } },
  });

  await expect(queue.enqueue(snapshot)).rejects.toThrow("LangSmith unavailable");
  await queue.enqueue(snapshot);

  expect(attempts).toBe(2);
});

test("does not enqueue a trace when its snapshot cannot be recorded", async () => {
  let enqueued = false;
  const queue = createReviewQueue({
    createFeedbackConfig: async () => {},
    async *listAnnotationQueues() {
      yield { id: "existing-queue" };
    },
    createAnnotationQueue: async () => ({ id: "new-queue" }),
    updateRun: async () => {
      throw new Error("LangSmith unavailable");
    },
    annotationQueues: {
      items: {
        create: async () => {
          enqueued = true;
        },
      },
    },
  });

  await expect(queue.enqueue(snapshot)).rejects.toThrow("LangSmith unavailable");
  expect(enqueued).toBe(false);
});
