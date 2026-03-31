import type Anthropic from "@anthropic-ai/sdk";
import {
  EVALUATION_FILTERS,
  EVALUATION_PROFILES,
  type EvaluationCriteria,
} from "../config/evaluation";
import { getClient } from "../services/anthropic";
import type { TokenTracker } from "../services/tokenTracker";
import type { JobListing } from "../types";

export interface JobEvaluation {
  pass: boolean;
  reason: string;
  profileName?: string;
}

const EVALUATE_TOOL: Anthropic.Messages.Tool = {
  name: "evaluate_job",
  description: "Submit the evaluation result for a job listing",
  input_schema: {
    type: "object" as const,
    properties: {
      pass: {
        type: "boolean",
        description: "Whether the job passes all criteria",
      },
      reason: {
        type: "string",
        description: "Brief explanation for the decision",
      },
    },
    required: ["pass", "reason"],
  },
};

export async function evaluateSingle(
  job: JobListing,
  criteria: EvaluationCriteria,
  apiKey: string,
  tracker?: TokenTracker,
  options?: { temperature?: number },
): Promise<JobEvaluation> {
  const anthropic = getClient(apiKey);

  const userMessage = `Job Title: ${job.title}
Company: ${job.company}
Source: ${job.source}
URL: ${job.url}

Description:
${job.description}`;

  const response = await anthropic.messages.create({
    model: "claude-haiku-4-5-20251001",
    max_tokens: 256,
    temperature: options?.temperature,
    system: criteria.prompt,
    messages: [{ role: "user", content: userMessage }],
    tools: [EVALUATE_TOOL],
    tool_choice: { type: "tool", name: "evaluate_job" },
  });

  tracker?.add(response.model, "evaluation", response.usage);

  const toolBlock = response.content.find((block) => block.type === "tool_use");
  if (!toolBlock || toolBlock.type !== "tool_use") {
    throw new Error(`Evaluation failed: no tool_use block in response`);
  }
  return toolBlock.input as JobEvaluation;
}

export async function evaluateJob(
  job: JobListing,
  apiKey: string,
  deps?: {
    filters?: EvaluationCriteria[];
    profiles?: EvaluationCriteria[];
    evaluate?: typeof evaluateSingle;
    tracker?: TokenTracker;
    temperature?: number;
  },
): Promise<JobEvaluation> {
  const filters = deps?.filters ?? EVALUATION_FILTERS;
  const profiles = deps?.profiles ?? EVALUATION_PROFILES;
  const evaluate = deps?.evaluate ?? evaluateSingle;
  const tracker = deps?.tracker;
  const tempOpts = deps?.temperature !== undefined ? { temperature: deps.temperature } : undefined;

  // Phase 1: AND filters — run in parallel, reject on first failure in results
  if (filters.length > 0) {
    const filterResults = await Promise.allSettled(
      filters.map((filter) => evaluate(job, filter, apiKey, tracker, tempOpts)),
    );
    for (const result of filterResults) {
      if (result.status === "rejected") {
        throw result.reason;
      }
      if (!result.value.pass) {
        return { pass: false, reason: result.value.reason };
      }
    }
  }

  // Phase 2: OR profiles — any must pass
  if (profiles.length === 0) {
    // Filters passed and no profiles configured — job passes filters alone
    return filters.length > 0
      ? { pass: true, reason: "Passed all filters" }
      : { pass: false, reason: "No profiles configured" };
  }

  const results = await Promise.allSettled(
    profiles.map((profile) => evaluate(job, profile, apiKey, tracker, tempOpts)),
  );

  let lastRejection: JobEvaluation = { pass: false, reason: "No profiles matched" };

  for (const [i, result] of results.entries()) {
    if (result.status === "fulfilled" && result.value.pass) {
      return { pass: true, reason: result.value.reason, profileName: profiles[i]?.name };
    }
    if (result.status === "fulfilled") {
      lastRejection = { pass: false, reason: result.value.reason };
    }
  }

  // If all profiles errored (none fulfilled), surface the first error
  // so it can be retried by the circuit breaker/retry stack
  const firstError = results.find((r) => r.status === "rejected");
  if (lastRejection.reason === "No profiles matched" && firstError?.status === "rejected") {
    throw firstError.reason;
  }

  return lastRejection;
}
