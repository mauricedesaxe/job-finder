import type { Client } from "@notionhq/client";

export interface NotionCache {
  existingUrls: Set<string>;
  blockedCompanies: Set<string>;
  recentAppCompanies: Set<string>;
  jobsByCompany: Map<string, string[]>;
}

export async function buildNotionCache(
  client: Client,
  databaseId: string,
): Promise<NotionCache> {
  const existingUrls = new Set<string>();
  const blockedCompanies = new Set<string>();
  const recentAppCompanies = new Set<string>();
  const jobsByCompany = new Map<string, string[]>();

  const sixMonthsAgo = new Date();
  sixMonthsAgo.setMonth(sixMonthsAgo.getMonth() - 6);

  // Fetch all pages in one paginated pass, extract everything we need
  let cursor: string | undefined;

  do {
    const response = await client.databases.query({
      database_id: databaseId,
      start_cursor: cursor,
    });

    for (const page of response.results) {
      if (!("properties" in page)) continue;

      // Extract URL
      const urlProp = page.properties.URL;
      const url = urlProp?.type === "url" ? (urlProp.url ?? "") : "";
      if (url) existingUrls.add(url);

      // Extract company
      const companyProp = page.properties.Company;
      const company =
        companyProp?.type === "rich_text"
          ? companyProp.rich_text.map((t: any) => t.plain_text).join("")
          : "";

      // Extract title
      const titleProp = page.properties["Job Title"];
      const title =
        titleProp?.type === "title"
          ? titleProp.title.map((t: any) => t.plain_text).join("")
          : "";

      // Extract status
      const statusProp = page.properties.Status;
      const status =
        statusProp?.type === "select" ? (statusProp.select?.name ?? "") : "";

      // Extract application date
      const appDateProp = page.properties["Application Date"];
      const appDate =
        appDateProp?.type === "date" ? (appDateProp.date?.start ?? null) : null;

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

export class CacheSyncer {
  cache: NotionCache;
  private interval: Timer | null = null;
  private localUrls = new Set<string>();
  private localTitles = new Map<string, string[]>();

  constructor(initialCache: NotionCache) {
    this.cache = initialCache;
  }

  addUrl(url: string): void {
    this.localUrls.add(url);
    this.cache.existingUrls.add(url);
  }

  addTitle(company: string, title: string): void {
    const localTitles = this.localTitles.get(company) ?? [];
    localTitles.push(title);
    this.localTitles.set(company, localTitles);

    const titles = this.cache.jobsByCompany.get(company) ?? [];
    titles.push(title);
    this.cache.jobsByCompany.set(company, titles);
  }

  start(client: Client, databaseId: string, intervalMs = 60_000): void {
    this.interval = setInterval(async () => {
      try {
        console.log("  🔄 Syncing Notion cache...");
        const fresh = await buildNotionCache(client, databaseId);

        // Merge local additions into the fresh cache
        for (const url of this.localUrls) {
          fresh.existingUrls.add(url);
        }
        for (const [company, titles] of this.localTitles) {
          const existing = fresh.jobsByCompany.get(company) ?? [];
          for (const title of titles) {
            if (!existing.includes(title)) {
              existing.push(title);
            }
          }
          fresh.jobsByCompany.set(company, existing);
        }

        this.cache = fresh;
        console.log(
          `  🔄 Cache synced: ${fresh.existingUrls.size} URLs, ${fresh.blockedCompanies.size} blocked, ` +
            `${fresh.recentAppCompanies.size} recent apps`,
        );
      } catch (err) {
        console.error(`  ✗ Cache sync failed: ${err}`);
      }
    }, intervalMs);
  }

  stop(): void {
    if (this.interval) {
      clearInterval(this.interval);
      this.interval = null;
    }
  }
}
