import type OpenAI from "openai";
import { logger } from "../logger";
import { getClient } from "../services/llm";
import type { TokenTracker } from "../services/tokenTracker";
import type { JobListing } from "../types";

export interface JobEnrichment {
  title: string;
  company: string;
  description: string;
  location: string;
}

const log = logger.child({ component: "enrich" });

const ENRICH_PROMPT = `You normalize and clean up job listing data for a personal job search CRM.

Given raw scraped job data, return cleaned and normalized versions of each field:
- **title**: Just the job title, no company name, location, or other suffixes
- **company**: Proper company name with correct capitalization and spacing (e.g. "Monad Foundation" not "monad.foundation", "Paxos Labs" not "PaxosLabs")
- **description**: A clean, well-formatted summary of the role using markdown. Structure it with sections like "## Overview", "## Responsibilities", "## Requirements", "## Tech Stack", "## Compensation" (only include sections that have content). Use bullet points for lists. Strip navigation elements, boilerplate, legal disclaimers, and repeated company marketing. Should be readable in 30 seconds.
- **location**: A short canonical location string (e.g. "Remote (Global)", "Remote (US/EU)", "Remote (Europe)", "Remote (US)", "Remote", "New York, NY", "London, UK").

  STRICT RULES — be conservative; do NOT infer or invent restrictions:
  * Only encode geographic restrictions that are EXPLICITLY stated in the listing (e.g. "US only", "EU residents only", "must be eligible to work in Canada").
  * A mentioned office is NOT a restriction. "Remote-friendly with an NYC office" → "Remote", NOT "Remote (US/NYC preferred)". "Hybrid in San Francisco" → "Hybrid (San Francisco)" only because the listing states the requirement.
  * Do not write phrases like "preferred" or "primarily" unless those exact words appear in the source.
  * If the listing says "remote" without a region → "Remote".
  * If the listing gives no location/eligibility signal at all → "Not specified".`;

const ENRICH_TOOL: OpenAI.ChatCompletionTool = {
  type: "function",
  function: {
    name: "enrich_job",
    description: "Submit the normalized and cleaned job data",
    parameters: {
      type: "object" as const,
      properties: {
        title: {
          type: "string",
          description: "Clean job title without company name or location",
        },
        company: {
          type: "string",
          description: "Properly capitalized company name",
        },
        description: {
          type: "string",
          description: "Clean, concise role summary",
        },
        location: {
          type: "string",
          description: "Normalized location info",
        },
      },
      required: ["title", "company", "description", "location"],
    },
  },
};

export async function enrichJob(
  job: JobListing,
  apiKey: string,
  tracker?: TokenTracker,
  model?: string,
): Promise<JobEnrichment> {
  const client = getClient(apiKey);
  const modelName = model ?? "google/gemini-2.5-flash";

  const userMessage = `Job Title: ${job.title}
Company: ${job.company}
Source: ${job.source}
URL: ${job.url}

Raw Description:
${job.description}`;

  const response = await client.chat.completions.create({
    model: modelName,
    max_tokens: 1024,
    messages: [
      { role: "system", content: ENRICH_PROMPT },
      { role: "user", content: userMessage },
    ],
    tools: [ENRICH_TOOL],
    tool_choice: { type: "function", function: { name: "enrich_job" } },
  });

  if (response.usage) {
    tracker?.add(response.model ?? modelName, "enrichment", {
      input_tokens: response.usage.prompt_tokens,
      output_tokens: response.usage.completion_tokens,
    });
  } else {
    log.warn({ model: modelName }, "No usage data in response");
  }

  const toolCall = response.choices[0]?.message?.tool_calls?.[0];
  if (!toolCall || toolCall.type !== "function") {
    throw new Error("Enrichment failed: no function tool_call in response");
  }

  try {
    return JSON.parse(toolCall.function.arguments) as JobEnrichment;
  } catch {
    throw new Error(
      `Enrichment failed: could not parse tool arguments: ${toolCall.function.arguments}`,
    );
  }
}
