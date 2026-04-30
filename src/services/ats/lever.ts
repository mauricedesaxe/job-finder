import { logger } from "../../logger";
import { type AtsJobData, type Fetcher, leverJobSchema, type WorkplaceType } from "./types";

const log = logger.child({ component: "ats/lever" });

export function parseLeverUrl(url: string): { org: string; id: string } | null {
  try {
    const { hostname, pathname } = new URL(url);
    if (!hostname.includes("lever.co")) return null;
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
    case "on-site":
    case "onsite":
      return "OnSite";
    default:
      return null;
  }
}

export async function fetchLeverJob(
  url: string,
  fetcher: Fetcher = fetch,
): Promise<AtsJobData | null> {
  const parsed = parseLeverUrl(url);
  if (!parsed) return null;
  const { org, id } = parsed;

  const apiUrl = `https://api.lever.co/v0/postings/${org}/${id}?mode=json`;

  let res: Response;
  try {
    res = await fetcher(apiUrl);
  } catch (err) {
    log.warn({ err, url }, "lever fetch failed");
    return null;
  }

  if (!res.ok) {
    log.warn({ status: res.status, url }, "lever non-ok response");
    return null;
  }

  let raw: unknown;
  try {
    raw = await res.json();
  } catch (err) {
    log.warn({ err, url }, "lever json parse failed");
    return null;
  }

  const result = leverJobSchema.safeParse(raw);
  if (!result.success) {
    log.warn({ url, issues: result.error.issues }, "lever schema mismatch");
    return null;
  }

  const job = result.data;
  const primary = job.categories?.location ?? "";
  const all = job.categories?.allLocations ?? [];
  const locations = primary && !all.includes(primary) ? [primary, ...all] : all;

  return {
    source: "lever",
    location: primary,
    locations,
    workplaceType: normalizeWorkplaceType(job.workplaceType),
    country: job.country ?? null,
  };
}
