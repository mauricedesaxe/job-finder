import { REAPPLY_WINDOW_MONTHS } from "../config/recency";
import { monthsAgo } from "../dates";
import type { ResilientNotionClient } from "./notion/client";
import { extractPageIdentity, type PageIdentity } from "./notion/helpers";

export interface NotionCache {
  recentAppCompanies: Set<string>;
}

export interface BuildCacheOptions {
  onProgress?: (itemsFetched: number) => void;
}

export interface NotionScan {
  cache: NotionCache;
  identities: PageIdentity[];
}

/**
 * One paginated pass over the database. Collects the companies that have an
 * application inside the reapply window (recent-application suppression) and
 * the raw identities used to backfill the processed-job ledger.
 */
export async function buildNotionCache(
  client: ResilientNotionClient,
  databaseId: string,
  options: BuildCacheOptions = {},
): Promise<NotionScan> {
  const recentAppCompanies = new Set<string>();
  const identities: PageIdentity[] = [];

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

      const identity = extractPageIdentity(page);
      identities.push(identity);

      if (identity.company && identity.appDate) {
        const appDateObj = new Date(identity.appDate);
        if (appDateObj >= reapplyCutoff) {
          recentAppCompanies.add(identity.company);
        }
      }
    }

    cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
  } while (cursor);

  return { cache: { recentAppCompanies }, identities };
}
