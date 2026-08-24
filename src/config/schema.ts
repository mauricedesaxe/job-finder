import { z } from "zod/v4";
import { SEARCH_DOMAINS, SEARCH_KEYWORDS } from "./search";

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
});

const CliConfigSchema = JobFinderConfigSchema.extend({
  jobLedgerPath: z.string().min(1, "JOB_LEDGER_PATH is required"),
});

export type JobFinderConfig = z.infer<typeof JobFinderConfigSchema>;
export type CliConfig = z.infer<typeof CliConfigSchema>;

export interface ContainerRuntimeConfig {
  config: Readonly<JobFinderConfig>;
  port: number;
}

export interface LoggingConfig {
  isProduction: boolean;
  level: string;
}

export function parseJobFinderConfig(
  environment: Readonly<Record<string, string | undefined>>,
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
    }),
  );
}

export function parseCliConfig(
  environment: Readonly<Record<string, string | undefined>>,
): Readonly<CliConfig> {
  return Object.freeze(
    CliConfigSchema.parse({
      ...parseJobFinderConfig(environment),
      jobLedgerPath: environment.JOB_LEDGER_PATH,
    }),
  );
}

export function loadJobFinderConfig(): Readonly<JobFinderConfig> {
  return parseJobFinderConfig(process.env);
}

export function loadCliConfig(): Readonly<CliConfig> {
  return parseCliConfig(process.env);
}

export function parseContainerRuntimeConfig(
  environment: Readonly<Record<string, string | undefined>>,
): Readonly<ContainerRuntimeConfig> {
  return Object.freeze({
    config: parseJobFinderConfig(environment),
    port: z.coerce.number().int().min(1).max(65_535).default(8080).parse(environment.PORT),
  });
}

export function loadContainerRuntimeConfig(): Readonly<ContainerRuntimeConfig> {
  return parseContainerRuntimeConfig(process.env);
}

export function parseLoggingConfig(
  environment: Readonly<Record<string, string | undefined>>,
): Readonly<LoggingConfig> {
  return Object.freeze({
    isProduction:
      environment.RAILWAY_ENVIRONMENT !== undefined || environment.NODE_ENV === "production",
    level: z.string().default("info").parse(environment.LOG_LEVEL),
  });
}

export function loadLoggingConfig(): Readonly<LoggingConfig> {
  return parseLoggingConfig(process.env);
}
