import type { Client } from "@notionhq/client";
import { extractRichText, type RichTextItem } from "./notion/helpers";

export interface NotionCache {
  existingUrls: Set<string>;
  blockedCompanies: Set<string>;
  recentAppCompanies: Set<string>;
  jobsByCompany: Map<string, string[]>;
}

export interface BuildCacheOptions {
  onProgress?: (itemsFetched: number) => void;
}

export async function buildNotionCache(
  client: Client,
  databaseId: string,
  options: BuildCacheOptions = {},
): Promise<NotionCache> {
  const existingUrls = new Set<string>();
  const blockedCompanies = new Set<string>();
  const recentAppCompanies = new Set<string>();
  const jobsByCompany = new Map<string, string[]>();

  const sixMonthsAgo = new Date();
  sixMonthsAgo.setMonth(sixMonthsAgo.getMonth() - 6);

  let itemsFetched = 0;
  let cursor: string | undefined;

  do {
    const response = await client.databases.query({
      database_id: databaseId,
      start_cursor: cursor,
    });

    for (const page of response.results) {
      itemsFetched++;
      options.onProgress?.(itemsFetched);
      if (!("properties" in page)) continue;

      // Extract URL
      const urlProp = page.properties.URL;
      const rawUrl = urlProp?.type === "url" ? urlProp.url : null;
      const url = typeof rawUrl === "string" ? rawUrl : "";
      if (url) existingUrls.add(url);

      // Extract company
      const companyProp = page.properties.Company;
      const company =
        companyProp?.type === "rich_text"
          ? extractRichText(companyProp.rich_text as RichTextItem[])
          : "";

      // Extract title
      const titleProp = page.properties["Job Title"];
      const title =
        titleProp?.type === "title" ? extractRichText(titleProp.title as RichTextItem[]) : "";

      // Extract status
      const statusProp = page.properties.Status;
      const selectVal = statusProp?.type === "select" ? statusProp.select : null;
      const status = selectVal && "name" in selectVal ? (selectVal.name ?? "") : "";

      // Extract application date
      const appDateProp = page.properties["Application Date"];
      const appDate = appDateProp?.type === "date" ? (appDateProp.date?.start ?? null) : null;

      // Build blockedCompanies
      if (company && status === "Company Blocked") {
        blockedCompanies.add(company);
      }

      // Build recentAppCompanies
      if (company && appDate) {
        const appDateObj = new Date(appDate);
        if (appDateObj >= sixMonthsAgo) {
          recentAppCompanies.add(company);
        }
      }

      // Build jobsByCompany (title index)
      if (company && title) {
        const titles = jobsByCompany.get(company) ?? [];
        titles.push(title);
        jobsByCompany.set(company, titles);
      }
    }

    cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
  } while (cursor);

  return { existingUrls, blockedCompanies, recentAppCompanies, jobsByCompany };
}

export class NotionCacheUpdater {
  cache: NotionCache;

  constructor(initialCache: NotionCache) {
    this.cache = initialCache;
  }

  addUrl(url: string): void {
    this.cache.existingUrls.add(url);
  }

  addTitle(company: string, title: string): void {
    const titles = this.cache.jobsByCompany.get(company) ?? [];
    titles.push(title);
    this.cache.jobsByCompany.set(company, titles);
  }
}
