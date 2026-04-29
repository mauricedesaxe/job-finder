import type { JobListing } from "../types";

export interface StructuralFilterResult {
  pass: boolean;
  reason: string;
}

const AGGREGATOR_URL_PATTERNS = [/jobs\.lever\.co\/jobgether\b/i, /jobs\.lever\.co\/toptal\b/i];

export function structuralFilter(job: JobListing): StructuralFilterResult {
  for (const re of AGGREGATOR_URL_PATTERNS) {
    if (re.test(job.url)) {
      return { pass: false, reason: `Aggregator/marketplace listing (${job.url})` };
    }
  }
  return { pass: true, reason: "" };
}
