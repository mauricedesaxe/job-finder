import { logger } from "../../logger";
import {
  type AtsJobData,
  type Fetcher,
  type WorkableJob,
  type WorkplaceType,
  workableListResponseSchema,
} from "./types";

const log = logger.child({ component: "ats/workable" });

const LIST_ENDPOINT = "https://apply.workable.com/api/v3/accounts";

export function parseWorkableUrl(url: string): { slug: string; shortcode: string } | null {
  try {
    const { hostname, pathname } = new URL(url);
    if (!hostname.includes("workable.com")) return null;
    const segments = pathname.split("/").filter(Boolean);
    // apply.workable.com/{slug}/j/{shortcode}/?
    const jIdx = segments.indexOf("j");
    if (jIdx <= 0 || jIdx === segments.length - 1) return null;
    const slug = segments[jIdx - 1];
    const shortcode = segments[jIdx + 1];
    if (!slug || !shortcode) return null;
    return { slug, shortcode };
  } catch {
    return null;
  }
}

function normalizeWorkplaceType(value: string | null | undefined): WorkplaceType | null {
  if (!value) return null;
  switch (value.toLowerCase()) {
    case "remote":
      return "Remote";
    case "hybrid":
      return "Hybrid";
    case "on_site":
    case "onsite":
    case "on-site":
      return "OnSite";
    default:
      return null;
  }
}

function formatLocation(loc: WorkableJob["location"]): string {
  if (!loc) return "";
  const parts = [loc.city, loc.country].filter((s): s is string => !!s);
  return parts.join(", ");
}

/**
 * Workable's list endpoint paginates 10 results at a time and we have no
 * single-job endpoint, so we use full-text `query` on the title to land the
 * target on the first page. The shortcode (extracted from the URL) is the
 * ground-truth identifier — we filter the results by it. If the query misses
 * (title got mangled, or the job is on page 2+), we return null and the
 * pipeline falls back to body-only evaluation, same as it does today.
 */
export async function fetchWorkableJob(
  url: string,
  query: string,
  fetcher: Fetcher = fetch,
): Promise<AtsJobData | null> {
  const parsed = parseWorkableUrl(url);
  if (!parsed) return null;
  const { slug, shortcode } = parsed;

  const apiUrl = `${LIST_ENDPOINT}/${slug}/jobs`;

  let res: Response;
  try {
    res = await fetcher(apiUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ query }),
    });
  } catch (err) {
    log.warn({ err, url }, "workable fetch failed");
    return null;
  }

  if (!res.ok) {
    log.warn({ status: res.status, url }, "workable non-ok response");
    return null;
  }

  let raw: unknown;
  try {
    raw = await res.json();
  } catch (err) {
    log.warn({ err, url }, "workable json parse failed");
    return null;
  }

  const result = workableListResponseSchema.safeParse(raw);
  if (!result.success) {
    log.warn({ url, issues: result.error.issues }, "workable schema mismatch");
    return null;
  }

  const job = result.data.results.find((j) => j.shortcode === shortcode);
  if (!job) {
    log.warn(
      { slug, shortcode, query, returned: result.data.results.length },
      "workable shortcode not found in query results",
    );
    return null;
  }

  const primary = formatLocation(job.location);
  const secondary = (job.locations ?? []).map(formatLocation).filter((s) => s.length > 0);
  const locations = primary && !secondary.includes(primary) ? [primary, ...secondary] : secondary;

  return {
    source: "workable",
    location: primary,
    locations,
    workplaceType: normalizeWorkplaceType(job.workplace),
    country: job.location?.country ?? null,
  };
}
