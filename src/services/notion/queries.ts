import type { Client } from "@notionhq/client";
import type { JobStatus } from "../../types";
import { extractRichText, type RichTextItem, toDateString } from "./helpers";

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
          date: { on_or_after: toDateString(sixMonthsAgo) ?? "" },
        },
      ],
    },
    page_size: 1,
  });

  const page = response.results[0];
  if (page) {
    return {
      exists: true,
      pageUrl: `https://notion.so/${page.id.replace(/-/g, "")}`,
    };
  }

  return { exists: false };
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
        date: { on_or_after: toDateString(sixMonthsAgo) },
      },
      start_cursor: cursor,
    });

    for (const page of response.results) {
      if (!("properties" in page)) continue;
      const companyProp = page.properties.Company;
      const company =
        companyProp?.type === "rich_text"
          ? extractRichText(companyProp.rich_text as RichTextItem[])
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
          ? extractRichText(companyProp.rich_text as RichTextItem[])
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

export async function queryJobsWithApplicationDateNotStatus(
  client: Client,
  databaseId: string,
  excludeStatus: JobStatus,
): Promise<Array<{ id: string; company: string }>> {
  const results: Array<{ id: string; company: string }> = [];
  let cursor: string | undefined;

  do {
    const response = await client.databases.query({
      database_id: databaseId,
      filter: {
        and: [
          { property: "Application Date", date: { is_not_empty: true } },
          { property: "Status", select: { does_not_equal: excludeStatus } },
        ],
      },
      start_cursor: cursor,
    });

    for (const page of response.results) {
      if (!("properties" in page)) continue;
      const companyProp = page.properties.Company;
      const company =
        companyProp?.type === "rich_text"
          ? extractRichText(companyProp.rich_text as RichTextItem[])
          : "";
      results.push({ id: page.id, company });
    }

    cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
  } while (cursor);

  return results;
}

export async function queryJobsByCompany(
  client: Client,
  databaseId: string,
  company: string,
): Promise<Array<{ title: string; url: string }>> {
  const results: Array<{ title: string; url: string }> = [];
  let cursor: string | undefined;

  do {
    const response = await client.databases.query({
      database_id: databaseId,
      filter: {
        property: "Company",
        rich_text: { equals: company },
      },
      start_cursor: cursor,
    });

    for (const page of response.results) {
      if (!("properties" in page)) continue;
      const titleProp = page.properties["Job Title"];
      const title =
        titleProp?.type === "title" ? extractRichText(titleProp.title as RichTextItem[]) : "";
      const urlProp = page.properties.URL;
      const rawUrl = urlProp?.type === "url" ? urlProp.url : null;
      const url = typeof rawUrl === "string" ? rawUrl : "";
      if (title) results.push({ title, url });
    }

    cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
  } while (cursor);

  return results;
}

export async function queryCompanyBlocked(
  client: Client,
  databaseId: string,
  company: string,
): Promise<boolean> {
  const response = await client.databases.query({
    database_id: databaseId,
    filter: {
      and: [
        { property: "Company", rich_text: { equals: company } },
        { property: "Status", select: { equals: "Company Blocked" } },
      ],
    },
    page_size: 1,
  });
  return response.results.length > 0;
}

export async function queryRecentJobsByStatus(
  client: Client,
  databaseId: string,
  status: JobStatus,
  withinDays: number,
): Promise<Array<{ id: string; company: string }>> {
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - withinDays);

  const results: Array<{ id: string; company: string }> = [];
  let cursor: string | undefined;

  do {
    const response = await client.databases.query({
      database_id: databaseId,
      filter: {
        and: [
          { property: "Status", select: { equals: status } },
          {
            property: "Date Scraped",
            date: { on_or_after: toDateString(cutoff) },
          },
        ],
      },
      start_cursor: cursor,
    });

    for (const page of response.results) {
      if (!("properties" in page)) continue;
      const companyProp = page.properties.Company;
      const company =
        companyProp?.type === "rich_text"
          ? extractRichText(companyProp.rich_text as RichTextItem[])
          : "";
      results.push({ id: page.id, company });
    }

    cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
  } while (cursor);

  return results;
}
