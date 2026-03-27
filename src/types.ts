export const JOB_STATUSES = [
  "To Review",
  "Applied",
  "Skipped",
  "Rejected",
  "Company Applied",
  "Company Blocked",
  "Archived",
] as const;

export type JobStatus = (typeof JOB_STATUSES)[number];

export interface JobListing {
  title: string;
  company: string;
  url: string;
  source: string;
  keywordsMatched: string[];
  datePosted: string | null;
  dateScraped: string;
  description: string;
  location: string;
}
