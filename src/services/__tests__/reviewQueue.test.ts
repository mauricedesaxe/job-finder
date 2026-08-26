import { expect, test } from "bun:test";
import { projectPendingReviewProjection } from "../../pipeline/reviewProjection";
import { ReviewSnapshotSchema } from "../../review";
import { createReviewQueue, JOB_REVIEW_QUEUE_NAME } from "../reviewQueue";
import { createSqliteJobLedger } from "../sqliteJobLedger";

const traceId = "trace-123";
const reviewedAt = "2026-08-24T10:30:00.000Z";
const snapshot = ReviewSnapshotSchema.parse({
  traceId,
  promptRelease: "release-2026-08-24-1",
  job: {
    title: "Applied AI Engineer",
    company: "Acme",
    url: "https://jobs.example.com/1",
    source: "jobs.example.com",
    keywordsMatched: ["ai engineer"],
    datePosted: null,
    dateScraped: "2026-08-24",
    description: "Build AI products.",
    location: "Europe",
    profile: "applied-ai-product-engineer",
  },
  ats: null,
  compensationRates: null,
  evaluation: {
    profile: "applied-ai-product-engineer",
    reason: "Ships model-backed products.",
  },
});

const unreadReviewData = {
  runs: {
    retrieve: async () => {
      throw new Error("unexpected run read");
    },
  },
  async *listFeedback() {
    yield* [];
  },
};

test("decodes completed archived reviews", async () => {
  const requests: Array<Record<string, unknown>> = [];
  const queue = createReviewQueue(
    completedReviewClient({
      feedback: [
        reviewFeedback("job_decision", 0),
        reviewFeedback("target_profile", 1),
        reviewFeedback("primary_reason", 4),
        reviewFeedback("block_company", 1),
        reviewFeedback("review_note", null, "Strong fit."),
      ],
      requests,
    }),
  );

  const reviews = await Array.fromAsync(queue.completedReviews());

  expect(reviews).toEqual([
    {
      runId: traceId,
      reviewedAt,
      snapshot,
      decision: "pursue",
      targetProfile: "applied-ai-product-engineer",
      primaryReason: "technology-fit",
      note: "Strong fit.",
      blockCompany: true,
    },
  ]);
  expect(requests).toContainEqual({ status: "archived", item_type: "RUN" });
  expect(requests).toContainEqual({
    runIds: [traceId],
    feedbackKeys: [
      "job_decision",
      "target_profile",
      "primary_reason",
      "block_company",
      "review_note",
    ],
  });
});

test("defaults absent optional feedback", async () => {
  const queue = createReviewQueue(
    completedReviewClient({
      feedback: [
        reviewFeedback("job_decision", 2),
        reviewFeedback("target_profile", 2),
        reviewFeedback("primary_reason", 7),
      ],
    }),
  );

  const reviews = await Array.fromAsync(queue.completedReviews());

  expect(reviews[0]).toMatchObject({
    decision: "unsure",
    targetProfile: "neither",
    primaryReason: "insufficient-information",
    blockCompany: false,
  });
  expect(reviews[0]).not.toHaveProperty("note");
});

test("rejects malformed categorical feedback", async () => {
  const queue = createReviewQueue(
    completedReviewClient({
      feedback: [
        reviewFeedback("job_decision", "pursue"),
        reviewFeedback("target_profile", 1),
        reviewFeedback("primary_reason", 4),
      ],
    }),
  );

  await expect(Array.fromAsync(queue.completedReviews())).rejects.toThrow();
});

test("rejects missing required feedback", async () => {
  const queue = createReviewQueue(
    completedReviewClient({
      feedback: [reviewFeedback("job_decision", 0), reviewFeedback("target_profile", 1)],
    }),
  );

  await expect(Array.fromAsync(queue.completedReviews())).rejects.toThrow(
    "missing primary_reason feedback",
  );
});

test("rejects duplicate feedback", async () => {
  const queue = createReviewQueue(
    completedReviewClient({
      feedback: [
        reviewFeedback("job_decision", 0),
        reviewFeedback("job_decision", 1),
        reviewFeedback("target_profile", 1),
        reviewFeedback("primary_reason", 4),
      ],
    }),
  );

  await expect(Array.fromAsync(queue.completedReviews())).rejects.toThrow(
    "duplicate job_decision feedback",
  );
});

test("rejects reviews whose snapshot trace differs from the queue run", async () => {
  const queue = createReviewQueue(
    completedReviewClient({
      run: { metadata: { review_snapshot: { ...snapshot, traceId: "other-trace" } } },
      feedback: [
        reviewFeedback("job_decision", 0),
        reviewFeedback("target_profile", 1),
        reviewFeedback("primary_reason", 4),
      ],
    }),
  );

  await expect(Array.fromAsync(queue.completedReviews())).rejects.toThrow(
    "Completed review trace must match its run",
  );
});

test("creates the review queue and enqueues the trace with extended retention", async () => {
  const calls: string[] = [];
  let itemLists = 0;
  const queue = createReviewQueue({
    ...unreadReviewData,
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
          itemLists++;
          yield* [];
        },
      },
    },
  });

  await queue.enqueue(traceId);

  expect(calls).toContain("queue:job-finder-job-review");
  expect(calls).toContain("enqueue:queue-123:trace-123");
  expect(itemLists).toBe(0);
});

test("reuses an existing queue and its initialized queue id", async () => {
  let queueSearches = 0;
  let queueCreates = 0;
  let queueUpdates = 0;
  const enqueued: string[][] = [];
  const queue = createReviewQueue({
    ...unreadReviewData,
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
    ...unreadReviewData,
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
    ...unreadReviewData,
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

test("skips a duplicate external write when an outbox replay finds the trace", async () => {
  let queued = false;
  let writes = 0;
  const queue = createReviewQueue({
    ...unreadReviewData,
    createFeedbackConfig: async () => {},
    async *listAnnotationQueues() {
      yield { id: "existing-queue" };
    },
    createAnnotationQueue: async () => ({ id: "new-queue" }),
    updateAnnotationQueue: async () => {},
    annotationQueues: {
      items: {
        create: async () => {
          writes++;
          queued = true;
        },
        async *list(_queueId, { status }) {
          if (queued && status === "needs_my_review") yield { run_id: traceId };
        },
      },
    },
  });

  const ledger = createSqliteJobLedger(":memory:");
  try {
    const stored = await ledger.recordProcessedJob({
      rawUrl: snapshot.job.url,
      company: snapshot.job.company,
      title: snapshot.job.title,
      outcome: "inserted",
      projections: {
        kind: "notion-and-review",
        notion: { job: snapshot.job, status: "To Review", createdAt: reviewedAt },
        review: { traceId, createdAt: reviewedAt },
      },
    });
    if (stored.kind !== "notion-and-review") throw new Error("Expected pending projections");

    await queue.enqueue(traceId);
    expect(await ledger.listPendingReviewProjections()).toEqual([stored.review]);
    await projectPendingReviewProjection({
      ledger,
      projection: stored.review,
      enqueue: queue.enqueueIfMissing,
    });

    expect(writes).toBe(1);
    expect(await ledger.listPendingReviewProjections()).toEqual([]);
  } finally {
    await ledger.close();
  }
});

test("propagates an ambiguous queue failure when the run is absent", async () => {
  let attempted = false;
  const queue = createReviewQueue({
    ...unreadReviewData,
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
    ...unreadReviewData,
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
type ReviewQueueClient = Parameters<typeof createReviewQueue>[0];

function completedReviewClient(input: {
  feedback: unknown[];
  run?: unknown;
  requests?: Array<Record<string, unknown>>;
}): ReviewQueueClient {
  return {
    createFeedbackConfig: async () => {},
    async *listAnnotationQueues() {
      yield { id: "existing-queue" };
    },
    createAnnotationQueue: async () => ({ id: "new-queue" }),
    updateAnnotationQueue: async () => {},
    runs: {
      retrieve: async () => input.run ?? { metadata: { review_snapshot: snapshot } },
    },
    async *listFeedback(request) {
      input.requests?.push(request);
      yield* input.feedback;
    },
    annotationQueues: {
      items: {
        create: async () => {},
        async *list(_queueId, request) {
          input.requests?.push(request);
          if (request.status === "archived") {
            yield {
              run_id: traceId,
              project_id: "22222222-2222-4222-8222-222222222222",
              start_time: "2026-08-24T10:00:00.000Z",
              last_reviewed_time: reviewedAt,
            };
          }
        },
      },
    },
  };
}

function reviewFeedback(key: string, score: unknown, value: unknown = null) {
  return { run_id: traceId, key, score, value };
}
