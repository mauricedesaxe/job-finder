import type OpenAI from "openai";
import { logger } from "../logger";
import { getClient } from "../services/llm";
import type { TokenTracker } from "../services/tokenTracker";

export interface DedupResult {
  isDuplicate: boolean;
  matchedTitle?: string;
}

const log = logger.child({ component: "dedup" });

const DEDUP_TOOL: OpenAI.ChatCompletionTool = {
  type: "function",
  function: {
    name: "check_duplicate",
    description:
      "Decide whether the new job title refers to the same role as any existing title at the same company",
    parameters: {
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
  },
};

export async function checkFuzzyDuplicate(
  newTitle: string,
  existingTitles: string[],
  apiKey: string,
  tracker?: TokenTracker,
  model?: string,
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

  const client = getClient(apiKey);
  const modelName = model ?? "google/gemini-2.5-flash";

  const numbered = existingTitles.map((t, i) => `${i + 1}. ${t}`).join("\n");

  const response = await client.chat.completions.create({
    model: modelName,
    max_tokens: 128,
    messages: [
      {
        role: "system",
        content: `You compare job titles at the same company to detect duplicates. Two titles are duplicates if they refer to the same role despite minor wording differences: abbreviations (Sr. = Senior, Eng = Engineer), reordering (Backend Engineer = Engineer, Backend), or trivial additions (e.g. adding a team name). They are NOT duplicates if the seniority level, domain, or function differs (e.g. "Senior Backend Engineer" vs "Staff Frontend Engineer").`,
      },
      {
        role: "user",
        content: `New title: "${newTitle}"\n\nExisting titles at the same company:\n${numbered}\n\nIs the new title a duplicate of any existing title?`,
      },
    ],
    tools: [DEDUP_TOOL],
    tool_choice: { type: "function", function: { name: "check_duplicate" } },
  });

  if (response.usage) {
    tracker?.add(response.model ?? modelName, "dedup", {
      input_tokens: response.usage.prompt_tokens,
      output_tokens: response.usage.completion_tokens,
    });
  } else {
    log.warn({ model: modelName }, "No usage data in response");
  }

  const toolCall = response.choices[0]?.message?.tool_calls?.[0];
  if (!toolCall || toolCall.type !== "function") {
    return { isDuplicate: false };
  }

  try {
    const input = JSON.parse(toolCall.function.arguments) as {
      isDuplicate: boolean;
      matchedTitle?: string;
    };
    return {
      isDuplicate: input.isDuplicate,
      matchedTitle: input.matchedTitle ?? undefined,
    };
  } catch {
    log.warn({ arguments: toolCall.function.arguments }, "Failed to parse dedup tool arguments");
    return { isDuplicate: false };
  }
}
