import { Client } from "@notionhq/client";
import type { JobListing } from "./types";

export function createNotionClient(token: string): Client {
  return new Client({ auth: token });
}

export async function checkDuplicateUrl(
  client: Client,
  databaseId: string,
  url: string,
): Promise<boolean> {
  const response = await client.databases.query({
    database_id: databaseId,
    filter: {
      property: "URL",
      url: { equals: url },
    },
    page_size: 1,
  });
  return response.results.length > 0;
}

export async function checkRecentApplication(
  client: Client,
  databaseId: string,
  company: string,
): Promise<{ exists: boolean; pageUrl?: string }> {
  const sixMonthsAgo = new Date();
  sixMonthsAgo.setMonth(sixMonthsAgo.getMonth() - 6);

  const response = await client.databases.query({
    database_id: databaseId,
    filter: {
      and: [
        {
          property: "Company",
          rich_text: { equals: company },
        },
        {
          property: "Application Date",
          date: { on_or_after: sixMonthsAgo.toISOString().split("T")[0] },
        },
      ],
    },
    page_size: 1,
  });

  if (response.results.length > 0) {
    const page = response.results[0];
    return {
      exists: true,
      pageUrl: `https://notion.so/${page.id.replace(/-/g, "")}`,
    };
  }

  return { exists: false };
}

export function buildNotionProperties(job: JobListing) {
  return {
    "Job Title": {
      title: [{ text: { content: job.title } }],
    },
    Company: {
      rich_text: [{ text: { content: job.company } }],
    },
    URL: {
      url: job.url,
    },
    Source: {
      select: { name: job.source },
    },
    Keywords: {
      multi_select: job.keywordsMatched.map((k) => ({ name: k })),
    },
    "Date Scraped": {
      date: { start: job.dateScraped },
    },
    ...(job.datePosted
      ? {
          "Date Posted": {
            date: { start: job.datePosted },
          },
        }
      : {}),
    Status: {
      select: { name: "To Review" },
    },
  };
}

export async function insertJob(
  client: Client,
  databaseId: string,
  job: JobListing,
  flagged: boolean = false,
): Promise<string> {
  const properties = buildNotionProperties(job);
  if (flagged) {
    properties.Status = { select: { name: "Flagged" } };
  }

  const response = await client.pages.create({
    parent: { database_id: databaseId },
    properties: properties as any,
  });

  return response.id;
}
