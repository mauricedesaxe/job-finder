import type { JobListing } from "../types";

export interface StructuralFilterResult {
  pass: boolean;
  reason: string;
}

const AGGREGATOR_URL_PATTERNS = [/jobs\.lever\.co\/jobgether\b/i, /jobs\.lever\.co\/toptal\b/i];

// A careers-index URL points at the company root on a job board, with no
// individual posting segment. Bodies for these pages list multiple roles
// instead of describing one job, so an LLM ends up evaluating the company's
// pitch rather than a real role and tends to wrongly pass.
const CAREERS_INDEX_URL_PATTERNS = [
  /^https?:\/\/apply\.workable\.com\/[^/]+\/?$/i,
  /^https?:\/\/jobs\.lever\.co\/[^/]+\/?$/i,
  /^https?:\/\/(?:job-)?boards\.greenhouse\.io\/[^/]+\/?$/i,
  /^https?:\/\/jobs\.ashbyhq\.com\/[^/]+\/?$/i,
];

const GENERIC_TITLE_PATTERNS = [
  // "General application" / "Join the team!" framing anywhere in the title —
  // not anchored to start, since real-world examples prefix the company name
  // (e.g. "Ethena Labs - Join the Team! General Application").
  /\bgeneral application\b/i,
  /\bopen application\b/i,
  /\btalent (pool|community|network)\b/i,
  /\bfuture opportunities\b/i,
  /\bjoin our talent\b/i,
  /\bjoin (?:the|our) team\b/i,
];

export function structuralFilter(job: JobListing): StructuralFilterResult {
  for (const re of AGGREGATOR_URL_PATTERNS) {
    if (re.test(job.url)) {
      return { pass: false, reason: `Aggregator/marketplace listing (${job.url})` };
    }
  }
  for (const re of CAREERS_INDEX_URL_PATTERNS) {
    if (re.test(job.url)) {
      return { pass: false, reason: `Careers-index page, not a specific role (${job.url})` };
    }
  }
  for (const re of GENERIC_TITLE_PATTERNS) {
    if (re.test(job.title)) {
      return { pass: false, reason: `Generic / talent-pool title (${job.title})` };
    }
  }
  return { pass: true, reason: "" };
}
