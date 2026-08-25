import { z } from "zod/v4";
import { SEARCH_DOMAINS, SEARCH_KEYWORDS } from "./search";

export interface JobFinderEnvironment {
  readonly NOTION_DATABASE_ID?: string;
  readonly NOTION_TOKEN?: string;
  readonly JINA_API_KEY?: string;
  readonly OPENROUTER_API_KEY?: string;
  readonly LLM_MODEL?: string;
  readonly LANGSMITH_API_KEY?: string;
  readonly LANGSMITH_ENDPOINT?: string;
  readonly LANGSMITH_PROJECT?: string;
  readonly SLACK_WEBHOOK_URL?: string;
  readonly ENABLE_ATS_ENRICHMENT?: string;
  readonly LOG_LEVEL?: string;
}

interface CliEnvironment extends JobFinderEnvironment {
  readonly [key: string]: string | undefined;
  readonly JOB_LEDGER_PATH?: string;
}

export const PINO_LOG_LEVELS = [
  "fatal",
  "error",
  "warn",
  "info",
  "debug",
  "trace",
  "silent",
] as const;
export type PinoLogLevel = (typeof PINO_LOG_LEVELS)[number];

const JobFinderConfigSchema = z.object({
  keywords: z.array(z.string()),
  domains: z.array(z.string()),
  notionDatabaseId: z.string().min(1, "NOTION_DATABASE_ID is required"),
  notionToken: z.string().min(1, "NOTION_TOKEN is required"),
  jinaApiKey: z.string().min(1, "JINA_API_KEY is required"),
  jinaBaseUrl: z.string(),
  openrouterApiKey: z.string().min(1, "OPENROUTER_API_KEY is required"),
  llmModel: z.string().default("google/gemini-2.5-flash"),
  langsmithApiKey: z.string().min(1, "LANGSMITH_API_KEY is required"),
  langsmithEndpoint: z.string().url().default("https://eu.api.smith.langchain.com"),
  langsmithProject: z.string().default("job-finder-production"),
  slackWebhookUrl: z.string().url().optional(),
  enableAtsEnrichment: z.boolean().default(true),
  logLevel: z.enum(PINO_LOG_LEVELS).default("info"),
});

const CliConfigSchema = JobFinderConfigSchema.extend({
  jobLedgerPath: z.string().min(1, "JOB_LEDGER_PATH is required"),
});

export type JobFinderConfig = z.infer<typeof JobFinderConfigSchema>;
export type CliConfig = z.infer<typeof CliConfigSchema>;

export function parseJobFinderConfig(
  environment: Readonly<JobFinderEnvironment>,
): Readonly<JobFinderConfig> {
  return Object.freeze(
    JobFinderConfigSchema.parse({
      keywords: SEARCH_KEYWORDS,
      domains: SEARCH_DOMAINS,
      notionDatabaseId: environment.NOTION_DATABASE_ID,
      notionToken: environment.NOTION_TOKEN,
      jinaApiKey: environment.JINA_API_KEY,
      jinaBaseUrl: "https://r.jina.ai",
      openrouterApiKey: environment.OPENROUTER_API_KEY,
      llmModel: environment.LLM_MODEL,
      langsmithApiKey: environment.LANGSMITH_API_KEY,
      langsmithEndpoint: environment.LANGSMITH_ENDPOINT,
      langsmithProject: environment.LANGSMITH_PROJECT,
      slackWebhookUrl: environment.SLACK_WEBHOOK_URL,
      enableAtsEnrichment: environment.ENABLE_ATS_ENRICHMENT
        ? environment.ENABLE_ATS_ENRICHMENT === "true"
        : undefined,
      logLevel: environment.LOG_LEVEL,
    }),
  );
}

export function parseCliConfig(environment: Readonly<CliEnvironment>): Readonly<CliConfig> {
  return Object.freeze(
    CliConfigSchema.parse({
      ...parseJobFinderConfig(environment),
      jobLedgerPath: environment.JOB_LEDGER_PATH,
    }),
  );
}

export function loadCliConfig(): Readonly<CliConfig> {
  return parseCliConfig(process.env);
}
