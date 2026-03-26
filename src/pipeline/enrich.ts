import Anthropic from "@anthropic-ai/sdk";
import type { JobListing } from "../../types";
import { getClient } from "../services/anthropic";

export interface JobEnrichment {
  title: string;
  company: string;
  description: string;
  location: string;
}

const ENRICH_PROMPT = `You normalize and clean up job listing data for a personal job search CRM.

Given raw scraped job data, return cleaned and normalized versions of each field:
- **title**: Just the job title, no company name, location, or other suffixes
- **company**: Proper company name with correct capitalization and spacing (e.g. "Monad Foundation" not "monad.foundation", "Paxos Labs" not "PaxosLabs")
- **description**: A clean, well-formatted summary of the role using markdown. Structure it with sections like "## Overview", "## Responsibilities", "## Requirements", "## Tech Stack", "## Compensation" (only include sections that have content). Use bullet points for lists. Strip navigation elements, boilerplate, legal disclaimers, and repeated company marketing. Should be readable in 30 seconds.
- **location**: Normalized location (e.g. "Remote (Global)", "Remote (US/EU)", "Remote (Europe)", "New York, NY", "London, UK"). If unclear, say "Not specified".`;

const ENRICH_TOOL: Anthropic.Messages.Tool = {
  name: "enrich_job",
  description: "Submit the normalized and cleaned job data",
  input_schema: {
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
};

export async function enrichJob(
  job: JobListing,
  apiKey: string,
): Promise<JobEnrichment> {
  const anthropic = getClient(apiKey);

  const userMessage = `Job Title: ${job.title}
Company: ${job.company}
Source: ${job.source}
URL: ${job.url}

Raw Description:
${job.description}`;

  const response = await anthropic.messages.create({
    model: "claude-haiku-4-5-20251001",
    max_tokens: 1024,
    system: ENRICH_PROMPT,
    messages: [{ role: "user", content: userMessage }],
    tools: [ENRICH_TOOL],
    tool_choice: { type: "tool", name: "enrich_job" },
  });

  const toolBlock = response.content.find((block) => block.type === "tool_use");
  if (!toolBlock || toolBlock.type !== "tool_use") {
    throw new Error(`Enrichment failed: no tool_use block in response`);
  }
  const input = toolBlock.input as JobEnrichment;

  return input;
}
