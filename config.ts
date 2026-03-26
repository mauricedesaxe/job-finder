import type { ScrapioConfig } from "./types";

export const config: ScrapioConfig = {
  keywords: [
    "senior backend engineer crypto",
    "senior fullstack engineer web3",
    "senior typescript engineer blockchain",
    "lead backend engineer defi",
    "senior software engineer defi",
    "senior software engineer web3",
    "typescript engineer crypto",
    "node.js engineer blockchain",
    "protocol engineer",
    "senior engineer solana",
    "senior engineer ethereum",
    "fullstack engineer defi",
    "backend engineer blockchain infrastructure",
  ],
  domains: ["jobs.ashbyhq.com", "jobs.lever.co", "boards.greenhouse.io"],
  notionDatabaseId: process.env.NOTION_DATABASE_ID!,
  notionToken: process.env.NOTION_TOKEN!,
  jinaApiKey: process.env.JINA_API_KEY!,
  jinaBaseUrl: "https://r.jina.ai",
  delayBetweenRequests: 500,
  maxResultsPerQuery: 10,
  maxPages: 3,
  anthropicApiKey: process.env.ANTHROPIC_API_KEY!,
};
