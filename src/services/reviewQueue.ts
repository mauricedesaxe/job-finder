import type { AnnotationQueueRubricItem, FeedbackConfig } from "langsmith/schemas";
import { z } from "zod/v4";
import { type CompletedReview, CompletedReviewSchema, ReviewSnapshotSchema } from "../review";

export const JOB_REVIEW_QUEUE_NAME = "job-finder-job-review";

const FEEDBACK_KEYS = [
  "job_decision",
  "target_profile",
  "primary_reason",
  "block_company",
  "review_note",
] as const;

type FeedbackKey = (typeof FEEDBACK_KEYS)[number];
type CategoricalFeedbackKey = Exclude<FeedbackKey, "review_note">;

const feedbackRegistry = {
  job_decision: {
    type: "categorical",
    categories: [
      { value: 0, label: "pursue" },
      { value: 1, label: "reject" },
      { value: 2, label: "unsure" },
    ] satisfies Array<{ value: number; label: CompletedReview["decision"] }>,
  },
  target_profile: {
    type: "categorical",
    categories: [
      { value: 0, label: "early-stage-product-engineer" },
      { value: 1, label: "applied-ai-product-engineer" },
      { value: 2, label: "neither" },
    ] satisfies Array<{ value: number; label: CompletedReview["targetProfile"] }>,
  },
  primary_reason: {
    type: "categorical",
    categories: [
      { value: 0, label: "crypto-company" },
      { value: 1, label: "location" },
      { value: 2, label: "compensation" },
      { value: 3, label: "role-scope" },
      { value: 4, label: "technology-fit" },
      { value: 5, label: "company-quality" },
      { value: 6, label: "work-environment" },
      { value: 7, label: "insufficient-information" },
      { value: 8, label: "other" },
    ] satisfies Array<{ value: number; label: CompletedReview["primaryReason"] }>,
  },
  block_company: {
    type: "categorical",
    categories: [
      { value: 1, label: "yes" },
      { value: 0, label: "no" },
    ],
  },
  review_note: { type: "freeform" },
} satisfies Record<FeedbackKey, FeedbackConfig>;

const feedbackConfigs = FEEDBACK_KEYS.map((feedbackKey) => ({
  feedbackKey,
  feedbackConfig: feedbackRegistry[feedbackKey],
}));

const rubricItems: AnnotationQueueRubricItem[] = [
  { feedback_key: "job_decision", is_required: true },
  { feedback_key: "target_profile", is_required: true },
  { feedback_key: "primary_reason", is_required: true },
  { feedback_key: "block_company", is_required: false },
  { feedback_key: "review_note", is_required: false },
];

const ArchivedRunItemSchema = z.object({
  run_id: z.string().min(1),
  last_reviewed_time: z.string().datetime({ offset: true }),
});

const ReviewRunSchema = z.object({
  extra: z.object({
    metadata: z.object({
      review_snapshot: z.unknown(),
    }),
  }),
});

const ReviewFeedbackSchema = z.object({
  run_id: z.string().min(1),
  key: z.enum(FEEDBACK_KEYS),
  score: z.unknown(),
  value: z.unknown(),
});

type ReviewFeedback = z.infer<typeof ReviewFeedbackSchema>;

interface ReviewQueueClient {
  createFeedbackConfig(input: {
    feedbackKey: string;
    feedbackConfig: FeedbackConfig;
  }): Promise<unknown>;
  listAnnotationQueues(input: { name: string }): AsyncIterable<{ id: string }>;
  createAnnotationQueue(input: {
    name: string;
    description: string;
    rubricInstructions: string;
    rubricItems: AnnotationQueueRubricItem[];
  }): Promise<{ id: string }>;
  updateAnnotationQueue(
    queueId: string,
    input: {
      description: string;
      rubricInstructions: string;
      rubricItems: AnnotationQueueRubricItem[];
    },
  ): Promise<void>;
  readRun(runId: string): Promise<unknown>;
  listFeedback(input: { runIds: string[]; feedbackKeys: string[] }): AsyncIterable<unknown>;
  annotationQueues: {
    items: {
      create(
        queueId: string,
        input: { extend_trace_retention: boolean; items: { item_type: "RUN"; run_id: string }[] },
        options?: { maxRetries: number },
      ): Promise<unknown>;
      list(
        queueId: string,
        input: {
          status: "needs_my_review" | "needs_others_review" | "archived";
          item_type: "RUN";
        },
      ): AsyncIterable<unknown>;
    };
  };
}

export function createReviewQueue(client: ReviewQueueClient): {
  enqueue(traceId: string): Promise<void>;
  completedReviews(): AsyncIterable<CompletedReview>;
} {
  let queueId: Promise<string> | undefined;

  return {
    async enqueue(traceId) {
      const id = await getQueueId();
      try {
        await client.annotationQueues.items.create(
          id,
          {
            extend_trace_retention: true,
            items: [{ item_type: "RUN", run_id: traceId }],
          },
          { maxRetries: 0 },
        );
      } catch (error) {
        if (!(await queueContainsRun(client, id, traceId))) throw error;
      }
    },
    async *completedReviews() {
      const id = await getQueueId();
      for await (const rawItem of client.annotationQueues.items.list(id, {
        status: "archived",
        item_type: "RUN",
      })) {
        const item = ArchivedRunItemSchema.parse(rawItem);
        yield await readCompletedReview(client, item);
      }
    },
  };

  async function getQueueId(): Promise<string> {
    queueId ??= findOrCreateQueue(client);
    try {
      return await queueId;
    } catch (error) {
      queueId = undefined;
      throw error;
    }
  }
}

async function readCompletedReview(
  client: ReviewQueueClient,
  item: z.infer<typeof ArchivedRunItemSchema>,
): Promise<CompletedReview> {
  const run = ReviewRunSchema.parse(await client.readRun(item.run_id));
  const snapshot = ReviewSnapshotSchema.parse(run.extra.metadata.review_snapshot);
  const feedback = await readReviewFeedback(client, item.run_id);
  const noteFeedback = feedback.get("review_note");
  const blockFeedback = feedback.get("block_company");

  return CompletedReviewSchema.parse({
    runId: item.run_id,
    reviewedAt: item.last_reviewed_time,
    snapshot,
    decision: decodeCategorical("job_decision", requireFeedback(feedback, "job_decision")),
    targetProfile: decodeCategorical("target_profile", requireFeedback(feedback, "target_profile")),
    primaryReason: decodeCategorical("primary_reason", requireFeedback(feedback, "primary_reason")),
    ...(noteFeedback ? { note: z.string().parse(noteFeedback.value) } : {}),
    blockCompany: blockFeedback
      ? decodeCategorical("block_company", blockFeedback) === "yes"
      : false,
  });
}

async function readReviewFeedback(
  client: ReviewQueueClient,
  runId: string,
): Promise<Map<FeedbackKey, ReviewFeedback>> {
  const feedback = new Map<FeedbackKey, ReviewFeedback>();
  for await (const rawEntry of client.listFeedback({
    runIds: [runId],
    feedbackKeys: [...FEEDBACK_KEYS],
  })) {
    const entry = ReviewFeedbackSchema.parse(rawEntry);
    if (entry.run_id !== runId) {
      throw new Error(`LangSmith returned feedback for ${entry.run_id} while reading ${runId}`);
    }
    if (feedback.has(entry.key)) {
      throw new Error(`LangSmith returned duplicate ${entry.key} feedback for ${runId}`);
    }
    feedback.set(entry.key, entry);
  }
  return feedback;
}

function requireFeedback(
  feedback: Map<FeedbackKey, ReviewFeedback>,
  key: CategoricalFeedbackKey,
): ReviewFeedback {
  const entry = feedback.get(key);
  if (!entry) throw new Error(`Completed LangSmith review is missing ${key} feedback`);
  return entry;
}

function decodeCategorical(key: CategoricalFeedbackKey, feedback: ReviewFeedback): string {
  const score = z.number().int().parse(feedback.score);
  const category = feedbackRegistry[key].categories?.find((entry) => entry.value === score);
  if (!category?.label) {
    throw new Error(`LangSmith ${key} feedback has unknown score ${score}`);
  }
  return category.label;
}

async function findOrCreateQueue(client: ReviewQueueClient): Promise<string> {
  await Promise.all(feedbackConfigs.map((config) => client.createFeedbackConfig(config)));
  for await (const queue of client.listAnnotationQueues({ name: JOB_REVIEW_QUEUE_NAME })) {
    await client.updateAnnotationQueue(queue.id, {
      description: "Qualified jobs awaiting manual review.",
      rubricInstructions: "Review the normalized job snapshot and record every required decision.",
      rubricItems,
    });
    return queue.id;
  }
  const queue = await client.createAnnotationQueue({
    name: JOB_REVIEW_QUEUE_NAME,
    description: "Qualified jobs awaiting manual review.",
    rubricInstructions: "Review the normalized job snapshot and record every required decision.",
    rubricItems,
  });
  return queue.id;
}

async function queueContainsRun(
  client: ReviewQueueClient,
  queueId: string,
  runId: string,
): Promise<boolean> {
  for (const status of ["needs_my_review", "needs_others_review", "archived"] as const) {
    for await (const rawItem of client.annotationQueues.items.list(queueId, {
      status,
      item_type: "RUN",
    })) {
      const item = z.object({ run_id: z.string().optional() }).parse(rawItem);
      if (item.run_id === runId) return true;
    }
  }
  return false;
}
