import { REAPPLY_WINDOW_MONTHS } from "../config/recency";
import { monthsAgo } from "../dates";
import type { ResilientNotionClient } from "./notion/client";
import { extractRichText, type RichTextItem } from "./notion/helpers";

export interface NotionCache {
  recentAppCompanies: Set<string>;
}

export interface BuildCacheOptions {
  onProgress?: (itemsFetched: number) => void;
}

export async function buildNotionCache(
  client: ResilientNotionClient,
  databaseId: string,
  options: BuildCacheOptions = {},
): Promise<NotionCache> {
  const recentAppCompanies = new Set<string>();
  const reapplyCutoff = monthsAgo(REAPPLY_WINDOW_MONTHS);
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

      const companyProp = page.properties.Company;
      const company =
        companyProp?.type === "rich_text"
          ? extractRichText(companyProp.rich_text as RichTextItem[])
          : "";
      const appDateProp = page.properties["Application Date"];
      const appDate = appDateProp?.type === "date" ? (appDateProp.date?.start ?? null) : null;

      if (company && appDate && new Date(appDate) >= reapplyCutoff) {
        recentAppCompanies.add(company);
      }
    }

    cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
  } while (cursor);

  return { recentAppCompanies };
}
