import { z } from "zod/v4";
import {
  EVALUATION_PROFILES,
  type EvaluationCriteria,
  getEvaluationFilters,
} from "../config/evaluation";
import {
  getPromptCommitHash,
  getPromptReleaseTag,
  invokePrompt,
  type PromptTool,
  traced,
} from "../services/langsmith";
import type { EvaluationPromptName } from "../services/promptRegistry";
import type { JobListing } from "../types";

export interface JobEvaluation {
  pass: boolean;
  reason: string;
  profileName?: string;
}

export const JobEvaluationSchema = z.object({ pass: z.boolean(), reason: z.string() });
export const EVALUATE_TOOL: PromptTool<JobEvaluation> = {
  name: "evaluate_job",
  description: "Submit the evaluation result for a job listing",
  schema: JobEvaluationSchema,
};

function jobMessage(job: JobListing): string {
  return `Job Title: ${job.title}
Company: ${job.company}
Source: ${job.source}
URL: ${job.url}

Description:
${job.description}`;
}

function promptName(criteria: EvaluationCriteria): EvaluationPromptName {
  if (criteria.promptName) return criteria.promptName;
  if (criteria.name === "remote-europe-eligible") return "job-finder-filter-location-eligibility";
  throw new Error(`Evaluation criteria ${criteria.name} has no LangSmith prompt`);
}

export async function evaluateSingle(
  job: JobListing,
  criteria: EvaluationCriteria,
  ..._legacy: unknown[]
): Promise<JobEvaluation> {
  const name = promptName(criteria);
  return traced(
    {
      name: "evaluate",
      runType: "llm",
      metadata: {
        name: criteria.name,
        prompt_commit: getPromptCommitHash(name),
        prompt_release: getPromptReleaseTag(),
        rates: criteria.rates,
      },
    },
    () =>
      invokePrompt({
        name,
        values: { job: jobMessage(job), ...(criteria.rates ? { rates: criteria.rates } : {}) },
        tool: EVALUATE_TOOL,
      }),
  );
}

export async function evaluateJob(
  job: JobListing,
  deps?: {
    filters?: EvaluationCriteria[];
    profiles?: EvaluationCriteria[];
    evaluate?: typeof evaluateSingle;
  },
): Promise<JobEvaluation> {
  const filters = deps?.filters ?? getEvaluationFilters();
  const profiles = deps?.profiles ?? EVALUATION_PROFILES;
  const evaluate = deps?.evaluate ?? evaluateSingle;
  const filterResults = await Promise.allSettled(filters.map((filter) => evaluate(job, filter)));
  for (const result of filterResults) {
    if (result.status === "rejected") throw result.reason;
    if (!result.value.pass) return { pass: false, reason: result.value.reason };
  }
  if (!profiles.length)
    return filters.length
      ? { pass: true, reason: "Passed all filters" }
      : { pass: false, reason: "No profiles configured" };
  const results = await Promise.allSettled(profiles.map((profile) => evaluate(job, profile)));
  let lastRejection: JobEvaluation = { pass: false, reason: "No profiles matched" };
  for (const [index, result] of results.entries()) {
    if (result.status === "fulfilled" && result.value.pass)
      return { pass: true, reason: result.value.reason, profileName: profiles[index]?.name };
    if (result.status === "fulfilled") lastRejection = { pass: false, reason: result.value.reason };
  }
  const firstError = results.find((result) => result.status === "rejected");
  if (lastRejection.reason === "No profiles matched" && firstError?.status === "rejected")
    throw firstError.reason;
  return lastRejection;
}
