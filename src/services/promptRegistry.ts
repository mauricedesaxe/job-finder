export const PROMPT_NAMES = [
  "job-finder-filter-location-eligibility",
  "job-finder-filter-compensation",
  "job-finder-filter-role-quality",
  "job-finder-filter-company-quality",
  "job-finder-profile-crypto-web3-ts",
  "job-finder-profile-fintech-trading-infra-ts",
  "job-finder-profile-senior-fullstack-react",
  "job-finder-profile-ai-engineering",
  "job-finder-enrichment",
  "job-finder-title-deduplication",
] as const;

export type PromptName = (typeof PROMPT_NAMES)[number];

export const EVALUATION_PROMPTS = [
  "job-finder-filter-location-eligibility",
  "job-finder-filter-compensation",
  "job-finder-filter-role-quality",
  "job-finder-filter-company-quality",
  "job-finder-profile-crypto-web3-ts",
  "job-finder-profile-fintech-trading-infra-ts",
  "job-finder-profile-senior-fullstack-react",
  "job-finder-profile-ai-engineering",
] as const;

export type EvaluationPromptName = (typeof EVALUATION_PROMPTS)[number];

export const PROMPT_REGISTRY = {
  "job-finder-filter-location-eligibility": { input: ["job"], maxTokens: 256 },
  "job-finder-filter-compensation": { input: ["job", "rates"], maxTokens: 256 },
  "job-finder-filter-role-quality": { input: ["job"], maxTokens: 256 },
  "job-finder-filter-company-quality": { input: ["job"], maxTokens: 256 },
  "job-finder-profile-crypto-web3-ts": { input: ["job"], maxTokens: 256 },
  "job-finder-profile-fintech-trading-infra-ts": { input: ["job"], maxTokens: 256 },
  "job-finder-profile-senior-fullstack-react": { input: ["job"], maxTokens: 256 },
  "job-finder-profile-ai-engineering": { input: ["job"], maxTokens: 256 },
  "job-finder-enrichment": { input: ["job"], maxTokens: 1024 },
  "job-finder-title-deduplication": { input: ["newTitle", "existingTitles"], maxTokens: 128 },
} as const satisfies Record<PromptName, { input: readonly string[]; maxTokens: number }>;

export function promptRef(name: PromptName, version = "production"): string {
  return `${name}:${version}`;
}
