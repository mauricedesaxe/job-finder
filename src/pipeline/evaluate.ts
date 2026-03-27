import type Anthropic from "@anthropic-ai/sdk";
import { EVALUATION_PROFILES, type EvaluationProfile } from "../profile";
import { getClient } from "../services/anthropic";
import type { JobListing } from "../types";

export interface JobEvaluation {
  pass: boolean;
  reason: string;
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

export async function evaluateSingleProfile(
  job: JobListing,
  profile: EvaluationProfile,
  apiKey: string,
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
    system: profile.prompt,
    messages: [{ role: "user", content: userMessage }],
    tools: [EVALUATE_TOOL],
    tool_choice: { type: "tool", name: "evaluate_job" },
  });

  const toolBlock = response.content.find((block) => block.type === "tool_use");
  if (!toolBlock || toolBlock.type !== "tool_use") {
    throw new Error(`Evaluation failed: no tool_use block in response`);
  }
  return toolBlock.input as JobEvaluation;
}

export async function evaluateJob(job: JobListing, apiKey: string): Promise<JobEvaluation> {
  const results = await Promise.allSettled(
    EVALUATION_PROFILES.map((profile) => evaluateSingleProfile(job, profile, apiKey)),
  );

  let lastRejection: JobEvaluation = { pass: false, reason: "No profiles configured" };

  for (const result of results) {
    if (result.status === "fulfilled" && result.value.pass) {
      return { pass: true, reason: result.value.reason };
    }
    if (result.status === "fulfilled") {
      lastRejection = { pass: false, reason: result.value.reason };
    }
  }

  return lastRejection;
}
