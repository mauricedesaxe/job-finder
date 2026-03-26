import type { ScrapioConfig } from "./types";

export function buildSearchQuery(
  keyword: string,
  domain: string,
  page: number = 0,
): string {
  const query = `site:${domain} ${keyword}`;
  const params = new URLSearchParams({ q: query, num: "10" });
  if (page > 0) params.set("start", String(page * 10));
  return `https://www.google.com/search?${params}`;
}

export function buildJinaUrl(targetUrl: string, jinaBaseUrl: string): string {
  return `${jinaBaseUrl}/${targetUrl}`;
}

export function extractJobUrls(markdown: string, domain: string): string[] {
  const escapedDomain = domain.replace(/\./g, "\\.");
  const pattern = new RegExp(
    `https?://${escapedDomain}/[^\\s)\\]"'>]+`,
    "g",
  );
  const matches = markdown.match(pattern) ?? [];

  // Deduplicate and clean trailing punctuation
  const seen = new Set<string>();
  const urls: string[] = [];
  for (const raw of matches) {
    const url = raw.replace(/[.,;:!?]+$/, "");
    if (!seen.has(url)) {
      seen.add(url);
      urls.push(url);
    }
  }
  return urls;
}

export async function fetchViaJina(
  targetUrl: string,
  config: Pick<ScrapioConfig, "jinaBaseUrl" | "jinaApiKey">,
): Promise<string> {
  const jinaUrl = buildJinaUrl(targetUrl, config.jinaBaseUrl);
  const headers: Record<string, string> = {
    Accept: "text/markdown",
    "User-Agent":
      "Mozilla/5.0 (compatible; Scrapio/1.0; +https://github.com/scrapio)",
  };
  if (config.jinaApiKey) {
    headers["Authorization"] = `Bearer ${config.jinaApiKey}`;
  }

  const res = await fetch(jinaUrl, { headers });
  if (!res.ok) {
    throw new Error(`Jina fetch failed (${res.status}): ${jinaUrl}`);
  }
  return res.text();
}

export async function searchJobs(
  keyword: string,
  domain: string,
  config: ScrapioConfig,
): Promise<string[]> {
  const allUrls: string[] = [];
  const seen = new Set<string>();

  for (let page = 0; page < config.maxPages; page++) {
    const googleUrl = buildSearchQuery(keyword, domain, page);
    const markdown = await fetchViaJina(googleUrl, config);
    const urls = extractJobUrls(markdown, domain);

    if (urls.length === 0) break;

    for (const url of urls) {
      if (!seen.has(url)) {
        seen.add(url);
        allUrls.push(url);
      }
    }

    if (page < config.maxPages - 1) {
      await Bun.sleep(config.delayBetweenRequests);
    }
  }

  return allUrls;
}
