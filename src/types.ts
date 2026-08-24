import { z } from "zod/v4";

export const JOB_STATUSES = [
  "To Review",
  "Applied",
  "Skipped",
  "Rejected",
  "Auto-Rejected",
  "Company Applied",
  "Company Blocked",
  "Archived",
] as const;

export const JobStatusSchema = z.enum(JOB_STATUSES);
export type JobStatus = z.infer<typeof JobStatusSchema>;

export const JobListingSchema = z.object({
  title: z.string(),
  company: z.string(),
  url: z.string(),
  source: z.string(),
  keywordsMatched: z.array(z.string()),
  datePosted: z.string().nullable(),
  dateScraped: z.string(),
  description: z.string(),
  location: z.string(),
  profile: z.string(),
});

export type JobListing = z.infer<typeof JobListingSchema>;
