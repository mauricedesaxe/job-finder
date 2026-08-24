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

export const ReviewFeedbackSchema = z.object({
  decision: z.enum(REVIEW_DECISIONS),
  targetProfile: z.enum([...EVALUATION_PROFILE_NAMES, "neither"] as const),
  primaryReason: z.enum(REVIEW_REASONS),
  note: z.string().optional(),
  blockCompany: z.boolean().default(false),
});

export type ReviewSnapshot = z.infer<typeof ReviewSnapshotSchema>;
export type ReviewFeedback = z.infer<typeof ReviewFeedbackSchema>;
