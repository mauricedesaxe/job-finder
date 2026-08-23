import { z } from "zod/v4";

export const TARGET_PROFILE_NAMES = [
  "early-stage-product-engineer",
  "applied-ai-product-engineer",
] as const;

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

export const JobSnapshotSchema = z.object({
  title: z.string(),
  company: z.string(),
  url: z.string().url(),
  source: z.string(),
  keywordsMatched: z.array(z.string()),
  datePosted: z.string().nullable(),
  dateScraped: z.string(),
  description: z.string(),
  location: z.string(),
  profile: z.string(),
});

export const AtsSnapshotSchema = z.object({
  source: z.enum(["lever", "ashby", "greenhouse", "workable"]),
  location: z.string(),
  locations: z.array(z.string()),
  workplaceType: z.enum(["Remote", "Hybrid", "OnSite"]).nullable(),
  country: z.string().nullable(),
});

export const ReviewSnapshotSchema = z.object({
  traceId: z.string().min(1),
  promptRelease: z.string().min(1),
  job: JobSnapshotSchema,
  ats: AtsSnapshotSchema.nullable(),
  compensationRates: z.string().nullable(),
  evaluation: z.object({
    profile: z.enum(TARGET_PROFILE_NAMES),
    reason: z.string(),
  }),
});

export const ReviewFeedbackSchema = z.object({
  decision: z.enum(REVIEW_DECISIONS),
  targetProfile: z.enum([...TARGET_PROFILE_NAMES, "neither"] as const),
  primaryReason: z.enum(REVIEW_REASONS),
  note: z.string().optional(),
  blockCompany: z.boolean().default(false),
});

export type ReviewSnapshot = z.infer<typeof ReviewSnapshotSchema>;
export type ReviewFeedback = z.infer<typeof ReviewFeedbackSchema>;
