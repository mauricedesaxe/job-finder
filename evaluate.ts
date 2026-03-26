import Anthropic from "@anthropic-ai/sdk";
import type { JobListing } from "./types";
import { getClient } from "./anthropic";

export interface JobEvaluation {
  pass: boolean;
  reason: string;
}

const SYSTEM_PROMPT = `You evaluate job listings for a senior/lead fullstack TypeScript developer focused on crypto/web3, based in Europe.

A job PASSES if ALL of these are true:
1. The role is remote-friendly OR available to European timezones (CET/EET). Reject if explicitly US-only, on-site only, or requires a specific non-European location.
2. The role is senior or lead level (or doesn't specify level, which is acceptable).
3. The role is relevant: software engineering involving TypeScript, Node.js, fullstack, or backend. Crypto/web3/blockchain context preferred but general senior TS roles at crypto companies also pass.

A job FAILS if ANY of these are true:
- Explicitly requires on-site presence
- Explicitly restricted to US/Asia timezones only with no European overlap
- Junior or internship level
- Non-engineering role (marketing, design, sales, HR, etc.)
- Completely unrelated tech stack with no TypeScript/JavaScript involvement`;

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

export async function evaluateJob(
  job: JobListing,
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
    system: SYSTEM_PROMPT,
    messages: [{ role: "user", content: userMessage }],
    tools: [EVALUATE_TOOL],
    tool_choice: { type: "tool", name: "evaluate_job" },
  });

  const toolBlock = response.content.find((block) => block.type === "tool_use");
  if (!toolBlock || toolBlock.type !== "tool_use") {
    throw new Error(`Evaluation failed: no tool_use block in response`);
  }
  const input = toolBlock.input as JobEvaluation;

  return { pass: input.pass, reason: input.reason };
}
