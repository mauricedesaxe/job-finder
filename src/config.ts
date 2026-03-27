import { SEARCH_DOMAINS, SEARCH_KEYWORDS } from "./profile";
import type { ScrapioConfig } from "./types";

export const config: ScrapioConfig = {
  keywords: SEARCH_KEYWORDS,
  domains: SEARCH_DOMAINS,
  notionDatabaseId: process.env.NOTION_DATABASE_ID!,
  notionToken: process.env.NOTION_TOKEN!,
  jinaApiKey: process.env.JINA_API_KEY!,
  jinaBaseUrl: "https://r.jina.ai",
  anthropicApiKey: process.env.ANTHROPIC_API_KEY!,
};
