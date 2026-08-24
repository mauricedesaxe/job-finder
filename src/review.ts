import { z } from "zod/v4";
import { EVALUATION_PROFILE_NAMES } from "./config/evaluation";
import { AtsJobDataSchema } from "./services/ats";

export const REVIEW_DECISIONS = ["pursue", "reject", "unsure"] as const;

export const REVIEW_REASONS = [
  "crypto-company",
  "location",
  "compensation",
  "role-scope",
  "technology-fit",
  "company-quality",
  "work-environment",
  "insufficient-information",
  "other",
] as const;

const TargetProfileSchema = z.enum(EVALUATION_PROFILE_NAMES);
const ReviewTargetProfileSchema = z.enum([...EVALUATION_PROFILE_NAMES, "neither"]);

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
    decision: z.enum(REVIEW_DECISIONS),
    targetProfile: ReviewTargetProfileSchema,
    primaryReason: z.enum(REVIEW_REASONS),
    note: z.string().optional(),
    blockCompany: z.boolean(),
  })
  .refine((review) => review.snapshot.traceId === review.runId, {
    path: ["snapshot", "traceId"],
    message: "Completed review trace must match its run",
  });

export type ReviewSnapshot = z.infer<typeof ReviewSnapshotSchema>;
export type CompletedReview = z.infer<typeof CompletedReviewSchema>;
