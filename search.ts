import type { ScrapioConfig } from "./types";

interface JinaSearchResult {
  title: string;
  url: string;
  description: string;
}

interface JinaSearchResponse {
  code: number;
  data: JinaSearchResult[];
}

async function fetchWithRetry(
  url: string,
  options: RequestInit,
  maxRetries = 3,
): Promise<Response> {
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    const res = await fetch(url, options);
    if (res.ok) return res;

    if (res.status === 429 && attempt < maxRetries - 1) {
      const delay = 1000 * 2 ** attempt; // 1s, 2s, 4s
      console.log(`  ⏳ Rate limited, retrying in ${delay / 1000}s...`);
      await Bun.sleep(delay);
      continue;
    }

    throw new Error(`Jina request failed (${res.status}): ${url}`);
  }

  throw new Error(`Jina request failed after ${maxRetries} retries: ${url}`);
}

export function buildSearchQuery(
  keyword: string,
  domain: string,
): string {
  return `site:${domain} ${keyword}`;
}

export function filterJobUrls(
  results: JinaSearchResult[],
  domain: string,
): string[] {
  const seen = new Set<string>();
  const urls: string[] = [];

  for (const result of results) {
    const url = result.url.replace(/[.,;:!?]+$/, "");
    if (url.includes(domain) && !seen.has(url)) {
      seen.add(url);
      urls.push(url);
    }
  }

  return urls;
}

export async function fetchJinaSearch(
  query: string,
  config: Pick<ScrapioConfig, "jinaApiKey">,
): Promise<JinaSearchResult[]> {
  const url = `https://s.jina.ai/?q=${encodeURIComponent(query)}`;
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  if (config.jinaApiKey) {
    headers["Authorization"] = `Bearer ${config.jinaApiKey}`;
  }

  const res = await fetchWithRetry(url, { headers });
  const json = (await res.json()) as JinaSearchResponse;
  return json.data ?? [];
}

export async function fetchViaJina(
  targetUrl: string,
  config: Pick<ScrapioConfig, "jinaBaseUrl" | "jinaApiKey">,
): Promise<string> {
  const jinaUrl = `${config.jinaBaseUrl}/${targetUrl}`;
  const headers: Record<string, string> = {
    Accept: "text/markdown",
  };
  if (config.jinaApiKey) {
    headers["Authorization"] = `Bearer ${config.jinaApiKey}`;
  }

  const res = await fetchWithRetry(jinaUrl, { headers });
  return res.text();
}

export async function searchJobs(
  keyword: string,
  domain: string,
  config: ScrapioConfig,
): Promise<string[]> {
  const query = buildSearchQuery(keyword, domain);
  const results = await fetchJinaSearch(query, config);
  return filterJobUrls(results, domain);
}
