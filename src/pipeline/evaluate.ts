import type OpenAI from "openai";
import {
  EVALUATION_PROFILES,
  type EvaluationCriteria,
  getEvaluationFilters,
} from "../config/evaluation";
import { logger } from "../logger";
import { getClient } from "../services/llm";
import type { TokenTracker } from "../services/tokenTracker";
import type { JobListing } from "../types";

export interface JobEvaluation {
  pass: boolean;
  reason: string;
  profileName?: string;
}

const log = logger.child({ component: "evaluate" });

const EVALUATE_TOOL: OpenAI.ChatCompletionTool = {
  type: "function",
  function: {
    name: "evaluate_job",
    description: "Submit the evaluation result for a job listing",
    parameters: {
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
  },
};

export async function evaluateSingle(
  job: JobListing,
  criteria: EvaluationCriteria,
  apiKey: string,
  tracker?: TokenTracker,
  options?: { temperature?: number; model?: string },
): Promise<JobEvaluation> {
  const client = getClient(apiKey);
  const model = options?.model ?? "google/gemini-2.5-flash";

  const userMessage = `Job Title: ${job.title}
Company: ${job.company}
Source: ${job.source}
URL: ${job.url}

Description:
${job.description}`;

  const response = await client.chat.completions.create({
    model,
    max_tokens: 256,
    temperature: options?.temperature,
    messages: [
      { role: "system", content: criteria.prompt },
      { role: "user", content: userMessage },
    ],
    tools: [EVALUATE_TOOL],
    tool_choice: { type: "function", function: { name: "evaluate_job" } },
  });

  if (response.usage) {
    tracker?.add(response.model ?? model, "evaluation", {
      input_tokens: response.usage.prompt_tokens,
      output_tokens: response.usage.completion_tokens,
    });
  } else {
    log.warn({ model }, "No usage data in response");
  }

  const toolCall = response.choices[0]?.message?.tool_calls?.[0];
  if (!toolCall || toolCall.type !== "function") {
    throw new Error("Evaluation failed: no function tool_call in response");
  }

  try {
    return JSON.parse(toolCall.function.arguments) as JobEvaluation;
  } catch {
    throw new Error(
      `Evaluation failed: could not parse tool arguments: ${toolCall.function.arguments}`,
    );
  }
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
    model?: string;
  },
): Promise<JobEvaluation> {
  const filters = deps?.filters ?? getEvaluationFilters();
  const profiles = deps?.profiles ?? EVALUATION_PROFILES;
  const evaluate = deps?.evaluate ?? evaluateSingle;
  const tracker = deps?.tracker;
  const tempOpts =
    deps?.temperature !== undefined || deps?.model !== undefined
      ? { temperature: deps.temperature, model: deps.model }
      : undefined;

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
