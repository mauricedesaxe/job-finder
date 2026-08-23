import { ChatOpenAI } from "@langchain/openai";
import { z } from "zod/v4";
import {
  EVALUATION_PROFILES,
  type EvaluationCriteria,
  getEvaluationFilters,
} from "../config/evaluation";
import { logger } from "../logger";
import { getLocationEligibilityPrompt, traced } from "../services/langsmith";
import { getClient } from "../services/llm";
import type { JobListing } from "../types";

export interface JobEvaluation {
  pass: boolean;
  reason: string;
  profileName?: string;
}

const log = logger.child({ component: "evaluate" });

export const JobEvaluationSchema = z.object({
  pass: z.boolean(),
  reason: z.string(),
});

const EVALUATE_TOOL = {
  name: "evaluate_job",
  description: "Submit the evaluation result for a job listing",
  schema: JobEvaluationSchema,
};

export async function evaluateSingle(
  job: JobListing,
  criteria: EvaluationCriteria,
  apiKey: string,
  options?: { temperature?: number; model?: string },
): Promise<JobEvaluation> {
  const model = options?.model ?? "google/gemini-2.5-flash";

  const userMessage = `Job Title: ${job.title}
Company: ${job.company}
Source: ${job.source}
URL: ${job.url}

Description:
${job.description}`;

  return traced(
    {
      name: "evaluate",
      runType: "llm",
      metadata: { name: criteria.name },
      model: { name: model, temperature: options?.temperature },
    },
    async () => {
      if ("promptSource" in criteria) {
        const response = await getLocationEligibilityPrompt()
          .pipe(
            new ChatOpenAI({
              apiKey,
              configuration: { baseURL: "https://openrouter.ai/api/v1" },
              maxRetries: 0,
              maxTokens: 256,
              model,
              temperature: options?.temperature,
            }).bindTools([EVALUATE_TOOL], {
              tool_choice: { type: "function", function: { name: EVALUATE_TOOL.name } },
            }),
          )
          .invoke({ job: userMessage });
        const toolCall = response.tool_calls?.[0];
        if (!toolCall || toolCall.name !== EVALUATE_TOOL.name) {
          throw new Error("Evaluation failed: no evaluate_job tool call in response");
        }

        const usage = response.usage_metadata;
        return {
          data: JobEvaluationSchema.parse(toolCall.args),
          usage: usage
            ? {
                input: usage.input_tokens,
                output: usage.output_tokens,
                total: usage.total_tokens,
              }
            : undefined,
        };
      }

      const client = getClient(apiKey);
      const response = await client.chat.completions.create({
        model,
        max_tokens: 256,
        temperature: options?.temperature,
        messages: [
          { role: "system", content: criteria.prompt },
          { role: "user", content: userMessage },
        ],
        tools: [
          {
            type: "function",
            function: {
              name: EVALUATE_TOOL.name,
              description: EVALUATE_TOOL.description,
              parameters: z.toJSONSchema(EVALUATE_TOOL.schema),
            },
          },
        ],
        tool_choice: { type: "function", function: { name: EVALUATE_TOOL.name } },
      });

      const usage = response.usage;
      if (!usage) {
        log.warn({ model }, "No usage data in response");
      }

      const toolCall = response.choices[0]?.message?.tool_calls?.[0];
      if (!toolCall || toolCall.type !== "function") {
        throw new Error("Evaluation failed: no function tool_call in response");
      }

      try {
        return {
          data: JobEvaluationSchema.parse(JSON.parse(toolCall.function.arguments)),
          usage: usage
            ? {
                input: usage.prompt_tokens,
                output: usage.completion_tokens,
                total: usage.total_tokens,
              }
            : undefined,
        };
      } catch {
        throw new Error(
          `Evaluation failed: could not parse tool arguments: ${toolCall.function.arguments}`,
        );
      }
    },
  );
}

export async function evaluateJob(
  job: JobListing,
  apiKey: string,
  deps?: {
    filters?: EvaluationCriteria[];
    profiles?: EvaluationCriteria[];
    evaluate?: typeof evaluateSingle;
    temperature?: number;
    model?: string;
  },
): Promise<JobEvaluation> {
  const filters = deps?.filters ?? getEvaluationFilters();
  const profiles = deps?.profiles ?? EVALUATION_PROFILES;
  const evaluate = deps?.evaluate ?? evaluateSingle;
  const tempOpts =
    deps?.temperature !== undefined || deps?.model !== undefined
      ? { temperature: deps.temperature, model: deps.model }
      : undefined;

  // Phase 1: AND filters — run in parallel, reject on first failure in results
  if (filters.length > 0) {
    const filterResults = await Promise.allSettled(
      filters.map((filter) => evaluate(job, filter, apiKey, tempOpts)),
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
    profiles.map((profile) => evaluate(job, profile, apiKey, tempOpts)),
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
