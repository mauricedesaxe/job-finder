import { z } from "zod/v4";
import {
  getPromptCommitHash,
  getPromptReleaseTag,
  invokePrompt,
  type PromptTool,
  traced,
} from "../services/langsmith";

export const DedupResultSchema = z.object({
  isDuplicate: z.boolean(),
  matchedTitle: z.string().optional(),
});
export type DedupResult = z.infer<typeof DedupResultSchema>;
const DEDUP_TOOL: PromptTool<DedupResult> = {
  name: "check_duplicate",
  description:
    "Decide whether the new job title refers to the same role as any existing title at the same company",
  schema: DedupResultSchema,
};

export async function checkFuzzyDuplicate(
  newTitle: string,
  existingTitles: string[],
  ..._legacy: unknown[]
): Promise<DedupResult> {
  if (!existingTitles.length) return { isDuplicate: false };
  const normalizedTitle = newTitle.toLowerCase().trim();
  const exactMatch = existingTitles.find((title) => title.toLowerCase().trim() === normalizedTitle);
  if (exactMatch) return { isDuplicate: true, matchedTitle: exactMatch };
  return traced(
    {
      name: "dedup",
      runType: "llm",
      metadata: {
        prompt_commit: getPromptCommitHash("job-finder-title-deduplication"),
        prompt_release: getPromptReleaseTag(),
      },
    },
    () =>
      invokePrompt({
        name: "job-finder-title-deduplication",
        values: {
          newTitle,
          existingTitles: existingTitles.map((title, index) => `${index + 1}. ${title}`).join("\n"),
        },
        tool: DEDUP_TOOL,
      }),
  );
}
