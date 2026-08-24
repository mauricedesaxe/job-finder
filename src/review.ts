import { z } from "zod/v4";
import { EVALUATION_PROFILE_NAMES } from "./config/evaluation";
import { AtsJobDataSchema } from "./services/ats";
import type { JobLedger } from "./services/jobLedger";

export const REVIEW_DECISION_CATEGORIES = [
  { value: 0, label: "pursue" },
  { value: 1, label: "reject" },
  { value: 2, label: "unsure" },
] as const;

export const REVIEW_TARGET_PROFILE_CATEGORIES = [
  { value: 0, label: EVALUATION_PROFILE_NAMES[0] },
  { value: 1, label: EVALUATION_PROFILE_NAMES[1] },
  { value: 2, label: "neither" },
] as const;

export const REVIEW_REASON_CATEGORIES = [
  { value: 0, label: "crypto-company" },
  { value: 1, label: "location" },
  { value: 2, label: "compensation" },
  { value: 3, label: "role-scope" },
  { value: 4, label: "technology-fit" },
  { value: 5, label: "company-quality" },
  { value: 6, label: "work-environment" },
  { value: 7, label: "insufficient-information" },
  { value: 8, label: "other" },
] as const;

const TargetProfileSchema = z.enum(EVALUATION_PROFILE_NAMES);
const ReviewDecisionSchema = z.enum(REVIEW_DECISION_CATEGORIES.map(({ label }) => label));
const ReviewTargetProfileSchema = z.enum(
  REVIEW_TARGET_PROFILE_CATEGORIES.map(({ label }) => label),
);
const ReviewReasonSchema = z.enum(REVIEW_REASON_CATEGORIES.map(({ label }) => label));

export const JobSnapshotSchema = z.object({
  title: z.string(),
  company: z.string(),
  url: z.string().url(),
  source: z.string(),
  keywordsMatched: z.array(z.string()),
  datePosted: z.string().date().nullable(),
  dateScraped: z.string().date(),
  description: z.string(),
  location: z.string(),
  profile: TargetProfileSchema,
});

export const ReviewSnapshotSchema = z
  .object({
    traceId: z.string().min(1),
    promptRelease: z.string().min(1),
    job: JobSnapshotSchema,
    ats: AtsJobDataSchema.nullable(),
    compensationRates: z.string().nullable(),
    evaluation: z.object({
      profile: TargetProfileSchema,
      reason: z.string(),
    }),
  })
  .refine((snapshot) => snapshot.job.profile === snapshot.evaluation.profile, {
    path: ["evaluation", "profile"],
    message: "Review snapshot profiles must match",
  });

export const CompletedReviewSchema = z
  .object({
    runId: z.string().min(1),
    reviewedAt: z.string().datetime({ offset: true }),
    snapshot: ReviewSnapshotSchema,
    decision: ReviewDecisionSchema,
    targetProfile: ReviewTargetProfileSchema,
    primaryReason: ReviewReasonSchema,
    note: z.string().optional(),
    blockCompany: z.boolean(),
  })
  .refine((review) => review.snapshot.traceId === review.runId, {
    path: ["snapshot", "traceId"],
    message: "Completed review trace must match its run",
  });

export type ReviewSnapshot = z.infer<typeof ReviewSnapshotSchema>;
export type CompletedReview = z.infer<typeof CompletedReviewSchema>;

export async function replayCompletedReviewCompanyBlocks(input: {
  reviews: AsyncIterable<CompletedReview>;
  ledger: Pick<JobLedger, "excludeCompany">;
}): Promise<{ reviews: number; companyExclusions: number }> {
  let reviews = 0;
  let companyExclusions = 0;
  for await (const review of input.reviews) {
    reviews++;
    if (review.blockCompany) {
      input.ledger.excludeCompany({
        company: review.snapshot.job.company,
        excludedAt: review.reviewedAt,
        sourceKey: `langsmith-review:${review.runId}`,
      });
      companyExclusions++;
    }
  }
  return { reviews, companyExclusions };
}
