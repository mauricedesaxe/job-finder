import { Client } from "@notionhq/client";
import type { JobListing, JobStatus } from "../types";

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
    ...(job.location
      ? {
          Location: {
            rich_text: [{ text: { content: job.location } }],
          },
        }
      : {}),
    Status: {
      select: { name: "To Review" },
    },
  };
}

export function descriptionToBlocks(description: string) {
  if (!description) return [];

  // Split on double newlines to preserve paragraph structure
  const paragraphs = description
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);

  const blocks: Array<{
    object: "block";
    type: "paragraph";
    paragraph: { rich_text: Array<{ type: "text"; text: { content: string } }> };
  }> = [];

  for (const paragraph of paragraphs) {
    // Notion limits rich_text content to 2000 chars per block
    if (paragraph.length <= 2000) {
      blocks.push({
        object: "block",
        type: "paragraph",
        paragraph: {
          rich_text: [{ type: "text", text: { content: paragraph } }],
        },
      });
    } else {
      // Chunk oversized paragraphs
      for (let i = 0; i < paragraph.length; i += 2000) {
        blocks.push({
          object: "block",
          type: "paragraph",
          paragraph: {
            rich_text: [
              { type: "text", text: { content: paragraph.slice(i, i + 2000) } },
            ],
          },
        });
      }
    }
  }

  return blocks;
}

export async function queryAppliedCompanies(
  client: Client,
  databaseId: string,
): Promise<Set<string>> {
  const sixMonthsAgo = new Date();
  sixMonthsAgo.setMonth(sixMonthsAgo.getMonth() - 6);

  const companies = new Set<string>();
  let cursor: string | undefined;

  do {
    const response = await client.databases.query({
      database_id: databaseId,
      filter: {
        property: "Application Date",
        date: { on_or_after: sixMonthsAgo.toISOString().split("T")[0] },
      },
      start_cursor: cursor,
    });

    for (const page of response.results) {
      if (!("properties" in page)) continue;
      const companyProp = page.properties.Company;
      const company =
        companyProp?.type === "rich_text"
          ? companyProp.rich_text.map((t: any) => t.plain_text).join("")
          : "";
      if (company) companies.add(company);
    }

    cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
  } while (cursor);

  return companies;
}

export async function queryJobsByStatus(
  client: Client,
  databaseId: string,
  status: JobStatus,
): Promise<Array<{ id: string; company: string }>> {
  const results: Array<{ id: string; company: string }> = [];
  let cursor: string | undefined;

  do {
    const response = await client.databases.query({
      database_id: databaseId,
      filter: {
        property: "Status",
        select: { equals: status },
      },
      start_cursor: cursor,
    });

    for (const page of response.results) {
      if (!("properties" in page)) continue;
      const companyProp = page.properties.Company;
      const company =
        companyProp?.type === "rich_text"
          ? companyProp.rich_text.map((t: any) => t.plain_text).join("")
          : "";
      results.push({ id: page.id, company });
    }

    cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
  } while (cursor);

  return results;
}

export async function queryJobsByStatusAndCompany(
  client: Client,
  databaseId: string,
  status: JobStatus,
  company: string,
): Promise<Array<{ id: string }>> {
  const results: Array<{ id: string }> = [];
  let cursor: string | undefined;

  do {
    const response = await client.databases.query({
      database_id: databaseId,
      filter: {
        and: [
          { property: "Status", select: { equals: status } },
          { property: "Company", rich_text: { equals: company } },
        ],
      },
      start_cursor: cursor,
    });

    for (const page of response.results) {
      results.push({ id: page.id });
    }

    cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
  } while (cursor);

  return results;
}

export async function updateJobStatus(
  client: Client,
  pageId: string,
  status: JobStatus,
): Promise<void> {
  await client.pages.update({
    page_id: pageId,
    properties: {
      Status: { select: { name: status } },
    },
  });
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

  const children = job.description
    ? descriptionToBlocks(job.description)
    : [];

  const response = await client.pages.create({
    parent: { database_id: databaseId },
    properties: properties as any,
    children: children as any,
  });

  return response.id;
}
