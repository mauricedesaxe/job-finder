import type Anthropic from "@anthropic-ai/sdk";
import { getClient } from "../services/anthropic";
import type { TokenTracker } from "../services/tokenTracker";

export interface DedupResult {
  isDuplicate: boolean;
  matchedTitle?: string;
}

const DEDUP_TOOL: Anthropic.Messages.Tool = {
  name: "check_duplicate",
  description:
    "Decide whether the new job title refers to the same role as any existing title at the same company",
  input_schema: {
    type: "object" as const,
    properties: {
      isDuplicate: {
        type: "boolean",
        description: "True if the new title is the same role as an existing title",
      },
      matchedTitle: {
        type: "string",
        description: "The existing title that matches, or null if no match",
      },
    },
    required: ["isDuplicate"],
  },
};

export async function checkFuzzyDuplicate(
  newTitle: string,
  existingTitles: string[],
  apiKey: string,
  tracker?: TokenTracker,
): Promise<DedupResult> {
  if (existingTitles.length === 0) {
    return { isDuplicate: false };
  }

  // Short-circuit: exact case-insensitive match
  const normalizedNew = newTitle.toLowerCase().trim();
  for (const existing of existingTitles) {
    if (existing.toLowerCase().trim() === normalizedNew) {
      return { isDuplicate: true, matchedTitle: existing };
    }
  }

  const anthropic = getClient(apiKey);

  const numbered = existingTitles.map((t, i) => `${i + 1}. ${t}`).join("\n");

  const response = await anthropic.messages.create({
    model: "claude-haiku-4-5-20251001",
    max_tokens: 128,
    system: `You compare job titles at the same company to detect duplicates. Two titles are duplicates if they refer to the same role despite minor wording differences: abbreviations (Sr. = Senior, Eng = Engineer), reordering (Backend Engineer = Engineer, Backend), or trivial additions (e.g. adding a team name). They are NOT duplicates if the seniority level, domain, or function differs (e.g. "Senior Backend Engineer" vs "Staff Frontend Engineer").`,
    messages: [
      {
        role: "user",
        content: `New title: "${newTitle}"\n\nExisting titles at the same company:\n${numbered}\n\nIs the new title a duplicate of any existing title?`,
      },
    ],
    tools: [DEDUP_TOOL],
    tool_choice: { type: "tool", name: "check_duplicate" },
  });

  tracker?.add("dedup", response.usage);

  const toolBlock = response.content.find((block) => block.type === "tool_use");
  if (!toolBlock || toolBlock.type !== "tool_use") {
    return { isDuplicate: false };
  }

  const input = toolBlock.input as { isDuplicate: boolean; matchedTitle?: string };
  return {
    isDuplicate: input.isDuplicate,
    matchedTitle: input.matchedTitle ?? undefined,
  };
}
