import type { ScrapioConfig } from "./types";

export const config: ScrapioConfig = {
  keywords: ["defi", "solana", "typescript backend"],
  domains: [
    "jobs.ashbyhq.com",
    "jobs.lever.co",
    "boards.greenhouse.io",
  ],
  notionDatabaseId: process.env.NOTION_DATABASE_ID!,
  notionToken: process.env.NOTION_TOKEN!,
  jinaApiKey: process.env.JINA_API_KEY!,
  jinaBaseUrl: "https://r.jina.ai",
  delayBetweenRequests: 500,
  maxResultsPerQuery: 10,
  maxPages: 3,
};
