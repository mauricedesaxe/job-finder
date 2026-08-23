import { z } from "zod/v4";
import {
  getPromptCommitHash,
  getPromptReleaseTag,
  invokePrompt,
  type PromptTool,
  traced,
} from "../services/langsmith";
import type { JobListing } from "../types";

export const JobEnrichmentSchema = z.object({
  title: z.string(),
  company: z.string(),
  description: z.string(),
  location: z.string(),
});
export type JobEnrichment = z.infer<typeof JobEnrichmentSchema>;
const ENRICH_TOOL: PromptTool<JobEnrichment> = {
  name: "enrich_job",
  description: "Submit the normalized and cleaned job data",
  schema: JobEnrichmentSchema,
};

export async function enrichJob(job: JobListing): Promise<JobEnrichment> {
  const values = {
    job: `Job Title: ${job.title}
Company: ${job.company}
Source: ${job.source}
URL: ${job.url}

Raw Description:
${job.description}`,
  };
  return traced(
    {
      name: "enrich",
      runType: "llm",
      metadata: {
        prompt_commit: getPromptCommitHash("job-finder-enrichment"),
        prompt_release: getPromptReleaseTag(),
      },
    },
    () => invokePrompt({ name: "job-finder-enrichment", values, tool: ENRICH_TOOL }),
  );
}
