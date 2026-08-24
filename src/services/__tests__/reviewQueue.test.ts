import { expect, test } from "bun:test";
import { createReviewQueue, JOB_REVIEW_QUEUE_NAME } from "../reviewQueue";

const traceId = "trace-123";

test("creates the review queue and enqueues the trace with extended retention", async () => {
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
        "job_decision",
        "target_profile",
        "primary_reason",
        "block_company",
        "review_note",
      ]);
      expect(rubricItems.find((item) => item.feedback_key === "block_company")?.is_required).toBe(
        false,
      );
      return { id: "queue-123" };
    },
    updateAnnotationQueue: async () => {},
    annotationQueues: {
      items: {
        create: async (queueId, input, options) => {
          calls.push(`enqueue:${queueId}:${input.items.map((item) => item.run_id).join(",")}`);
          expect(input.extend_trace_retention).toBe(true);
          expect(options?.maxRetries).toBe(0);
        },
        async *list() {
          yield* [];
        },
      },
    },
  });

  await queue.enqueue(traceId);

  expect(calls).toContain("queue:job-finder-job-review");
  expect(calls).toContain("enqueue:queue-123:trace-123");
});

test("reuses an existing queue and its initialized queue id", async () => {
  let queueSearches = 0;
  let queueCreates = 0;
  let queueUpdates = 0;
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
    updateAnnotationQueue: async (queueId) => {
      expect(queueId).toBe("existing-queue");
      queueUpdates++;
    },
    annotationQueues: {
      items: {
        create: async (_queueId, input) => {
          enqueued.push(input.items.map((item) => item.run_id));
        },
        async *list() {
          yield* [];
        },
      },
    },
  });

  await queue.enqueue(traceId);
  await queue.enqueue("trace-456");

  expect(queueSearches).toBe(1);
  expect(queueCreates).toBe(0);
  expect(queueUpdates).toBe(1);
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
    updateAnnotationQueue: async () => {},
    annotationQueues: {
      items: {
        create: async () => {},
        async *list() {
          yield* [];
        },
      },
    },
  });

  await Promise.all([queue.enqueue(traceId), queue.enqueue("trace-456")]);

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
    updateAnnotationQueue: async () => {},
    annotationQueues: {
      items: {
        create: async () => {},
        async *list() {
          yield* [];
        },
      },
    },
  });

  await expect(queue.enqueue(traceId)).rejects.toThrow("LangSmith unavailable");
  await queue.enqueue(traceId);

  expect(attempts).toBe(2);
});

test("propagates an ambiguous queue failure when the run is absent", async () => {
  let attempted = false;
  const queue = createReviewQueue({
    createFeedbackConfig: async () => {},
    async *listAnnotationQueues() {
      yield { id: "existing-queue" };
    },
    createAnnotationQueue: async () => ({ id: "new-queue" }),
    updateAnnotationQueue: async () => {},
    annotationQueues: {
      items: {
        create: async () => {
          attempted = true;
          throw new Error("LangSmith unavailable");
        },
        async *list() {
          yield* [];
        },
      },
    },
  });

  await expect(queue.enqueue(traceId)).rejects.toThrow("LangSmith unavailable");
  expect(attempted).toBe(true);
});

test("accepts an ambiguous queue response when the run is present", async () => {
  const queue = createReviewQueue({
    createFeedbackConfig: async () => {},
    async *listAnnotationQueues() {
      yield { id: "existing-queue" };
    },
    createAnnotationQueue: async () => ({ id: "new-queue" }),
    updateAnnotationQueue: async () => {},
    annotationQueues: {
      items: {
        create: async () => {
          throw new Error("response lost");
        },
        async *list(_queueId, { status }) {
          if (status === "needs_my_review") yield { run_id: traceId };
        },
      },
    },
  });

  await expect(queue.enqueue(traceId)).resolves.toBeUndefined();
});
