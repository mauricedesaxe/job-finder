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
- **location**: Normalized location (e.g. "Remote (Global)", "Remote (US/EU)", "Remote (Europe)", "New York, NY", "London, UK"). If unclear, say "Not specified".`;

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
