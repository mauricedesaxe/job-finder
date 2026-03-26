import Anthropic from "@anthropic-ai/sdk";
import type { JobListing } from "./types";

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
- Completely unrelated tech stack with no TypeScript/JavaScript involvement

Respond with ONLY valid JSON, no markdown:
{"pass": true/false, "reason": "brief explanation"}`;

let client: Anthropic | null = null;

function getClient(apiKey: string): Anthropic {
  if (!client) {
    client = new Anthropic({ apiKey });
  }
  return client;
}

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
  });

  const text =
    response.content[0].type === "text" ? response.content[0].text : "";

  try {
    return JSON.parse(text) as JobEvaluation;
  } catch {
    // If parsing fails, assume pass to avoid losing valid jobs
    return { pass: true, reason: "Could not parse LLM response, defaulting to pass" };
  }
}
