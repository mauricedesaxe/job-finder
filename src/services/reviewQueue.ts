import type { AnnotationQueueRubricItem, FeedbackConfig } from "langsmith/schemas";
import { EVALUATION_PROFILE_NAMES } from "../config/evaluation";
import { REVIEW_DECISIONS, REVIEW_REASONS } from "../review";

export const JOB_REVIEW_QUEUE_NAME = "job-finder-job-review";

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
      ): AsyncIterable<{ run_id?: string }>;
    };
  };
}

const feedbackConfigs = [
  {
    feedbackKey: "job_decision",
    feedbackConfig: {
      type: "categorical",
      categories: [...REVIEW_DECISIONS.map((label, value) => ({ value, label }))],
    },
  },
  {
    feedbackKey: "target_profile",
    feedbackConfig: {
      type: "categorical",
      categories: [
        ...[...EVALUATION_PROFILE_NAMES, "neither"].map((label, value) => ({ value, label })),
      ],
    },
  },
  {
    feedbackKey: "primary_reason",
    feedbackConfig: {
      type: "categorical",
      categories: [...REVIEW_REASONS.map((label, value) => ({ value, label }))],
    },
  },
  {
    feedbackKey: "block_company",
    feedbackConfig: {
      type: "categorical",
      categories: [
        { value: 1, label: "yes" },
        { value: 0, label: "no" },
      ],
    },
  },
  { feedbackKey: "review_note", feedbackConfig: { type: "freeform" } },
] satisfies Array<{ feedbackKey: string; feedbackConfig: FeedbackConfig }>;

const rubricItems: AnnotationQueueRubricItem[] = [
  { feedback_key: "job_decision", is_required: true },
  { feedback_key: "target_profile", is_required: true },
  { feedback_key: "primary_reason", is_required: true },
  { feedback_key: "block_company", is_required: false },
  { feedback_key: "review_note", is_required: false },
];

export function createReviewQueue(client: ReviewQueueClient): {
  enqueue(traceId: string): Promise<void>;
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
    for await (const item of client.annotationQueues.items.list(queueId, {
      status,
      item_type: "RUN",
    })) {
      if (item.run_id === runId) return true;
    }
  }
  return false;
}
