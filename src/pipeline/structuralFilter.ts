import type { JobListing } from "../types";

export interface StructuralFilterResult {
  pass: boolean;
  reason: string;
}

const AGGREGATOR_URL_PATTERNS = [/jobs\.lever\.co\/jobgether\b/i, /jobs\.lever\.co\/toptal\b/i];

const GENERIC_TITLE_PATTERNS = [
  /^\s*general application\b/i,
  /^\s*talent (pool|community|network)\b/i,
  /^\s*future opportunities\b/i,
  /^\s*open application\b/i,
  /^\s*join our talent\b/i,
];

export function structuralFilter(job: JobListing): StructuralFilterResult {
  for (const re of AGGREGATOR_URL_PATTERNS) {
    if (re.test(job.url)) {
      return { pass: false, reason: `Aggregator/marketplace listing (${job.url})` };
    }
  }
  for (const re of GENERIC_TITLE_PATTERNS) {
    if (re.test(job.title)) {
      return { pass: false, reason: `Generic / talent-pool title (${job.title})` };
    }
  }
  return { pass: true, reason: "" };
}
