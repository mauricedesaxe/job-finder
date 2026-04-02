import { z } from "zod/v4";
import { SEARCH_DOMAINS, SEARCH_KEYWORDS } from "./search";

const ConfigSchema = z.object({
  keywords: z.array(z.string()),
  domains: z.array(z.string()),
  notionDatabaseId: z.string().min(1, "NOTION_DATABASE_ID is required"),
  notionToken: z.string().min(1, "NOTION_TOKEN is required"),
  jinaApiKey: z.string().min(1, "JINA_API_KEY is required"),
  jinaBaseUrl: z.string(),
  openrouterApiKey: z.string().min(1, "OPENROUTER_API_KEY is required"),
  llmModel: z.string().default("google/gemini-2.5-flash"),
  slackWebhookUrl: z.string().url().optional(),
});

export type JobFinderConfig = z.infer<typeof ConfigSchema>;

export const config: Readonly<JobFinderConfig> = Object.freeze(
  ConfigSchema.parse({
    keywords: SEARCH_KEYWORDS,
    domains: SEARCH_DOMAINS,
    notionDatabaseId: process.env.NOTION_DATABASE_ID,
    notionToken: process.env.NOTION_TOKEN,
    jinaApiKey: process.env.JINA_API_KEY,
    jinaBaseUrl: "https://r.jina.ai",
    openrouterApiKey: process.env.OPENROUTER_API_KEY,
    llmModel: process.env.LLM_MODEL,
    slackWebhookUrl: process.env.SLACK_WEBHOOK_URL,
  }),
);
