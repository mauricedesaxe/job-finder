import { logger } from "../../logger";
import {
  type AshbyOrgResponse,
  type AtsJobData,
  ashbyOrgResponseSchema,
  type Fetcher,
  type WorkplaceType,
} from "./types";

const log = logger.child({ component: "ats/ashby" });

// Per-run cache of org → response. The unauthenticated Ashby endpoint only
// returns whole org listings; caching avoids re-fetching the same payload for
// every job in that org. Call clearAshbyCache() at the start of each pipeline run.
const orgCache = new Map<string, AshbyOrgResponse>();

export function clearAshbyCache(): void {
  orgCache.clear();
}

export function parseAshbyUrl(url: string): { org: string; id: string } | null {
  try {
    const { hostname, pathname } = new URL(url);
    if (!hostname.includes("ashbyhq.com")) return null;
    const segments = pathname.split("/").filter(Boolean);
    const [org, id] = segments;
    if (!org || !id) return null;
    return { org, id };
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
    case "onsite":
    case "on-site":
      return "OnSite";
    default:
      return null;
  }
}

async function fetchOrg(org: string, fetcher: Fetcher): Promise<AshbyOrgResponse | null> {
  const cached = orgCache.get(org);
  if (cached) return cached;

  const apiUrl = `https://api.ashbyhq.com/posting-api/job-board/${org}`;

  let res: Response;
  try {
    res = await fetcher(apiUrl);
  } catch (err) {
    log.warn({ err, org }, "ashby fetch failed");
    return null;
  }

  if (!res.ok) {
    log.warn({ status: res.status, org }, "ashby non-ok response");
    return null;
  }

  let raw: unknown;
  try {
    raw = await res.json();
  } catch (err) {
    log.warn({ err, org }, "ashby json parse failed");
    return null;
  }

  const result = ashbyOrgResponseSchema.safeParse(raw);
  if (!result.success) {
    log.warn({ org, issues: result.error.issues }, "ashby schema mismatch");
    return null;
  }

  orgCache.set(org, result.data);
  return result.data;
}

export async function fetchAshbyJob(
  url: string,
  fetcher: Fetcher = fetch,
): Promise<AtsJobData | null> {
  const parsed = parseAshbyUrl(url);
  if (!parsed) return null;
  const { org, id } = parsed;

  const orgData = await fetchOrg(org, fetcher);
  if (!orgData) return null;

  const job = orgData.jobs.find((j) => j.id === id);
  if (!job) {
    log.warn({ org, id }, "ashby job not found in org listing");
    return null;
  }

  const primary = job.location ?? "";
  const secondary = (job.secondaryLocations ?? []).map((s) => s.location);
  const locations = primary && !secondary.includes(primary) ? [primary, ...secondary] : secondary;

  return {
    source: "ashby",
    location: primary,
    locations,
    workplaceType: normalizeWorkplaceType(job.workplaceType),
    country: job.address?.postalAddress?.addressCountry ?? null,
  };
}
