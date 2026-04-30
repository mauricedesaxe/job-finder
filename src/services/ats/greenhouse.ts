import { logger } from "../../logger";
import { type AtsJobData, type Fetcher, greenhouseJobSchema } from "./types";

const log = logger.child({ component: "ats/greenhouse" });

export function parseGreenhouseUrl(url: string): { org: string; id: string } | null {
  try {
    const { hostname, pathname } = new URL(url);
    if (!hostname.includes("greenhouse.io")) return null;
    const segments = pathname.split("/").filter(Boolean);
    // boards.greenhouse.io/{org}/jobs/{id} and job-boards.greenhouse.io/{org}/jobs/{id}
    const jobsIdx = segments.indexOf("jobs");
    if (jobsIdx <= 0 || jobsIdx === segments.length - 1) return null;
    const org = segments[jobsIdx - 1];
    const id = segments[jobsIdx + 1];
    if (!org || !id) return null;
    return { org, id };
  } catch {
    return null;
  }
}

function extractCountry(officeLocation: string | null | undefined): string | null {
  if (!officeLocation) return null;
  // offices[].location is "City, Region, Country" — take the last comma-segment.
  const parts = officeLocation
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return parts.length > 1 ? (parts[parts.length - 1] ?? null) : null;
}

export async function fetchGreenhouseJob(
  url: string,
  fetcher: Fetcher = fetch,
): Promise<AtsJobData | null> {
  const parsed = parseGreenhouseUrl(url);
  if (!parsed) return null;
  const { org, id } = parsed;

  const apiUrl = `https://boards-api.greenhouse.io/v1/boards/${org}/jobs/${id}`;

  let res: Response;
  try {
    res = await fetcher(apiUrl);
  } catch (err) {
    log.warn({ err, url }, "greenhouse fetch failed");
    return null;
  }

  if (!res.ok) {
    log.warn({ status: res.status, url }, "greenhouse non-ok response");
    return null;
  }

  let raw: unknown;
  try {
    raw = await res.json();
  } catch (err) {
    log.warn({ err, url }, "greenhouse json parse failed");
    return null;
  }

  const result = greenhouseJobSchema.safeParse(raw);
  if (!result.success) {
    log.warn({ url, issues: result.error.issues }, "greenhouse schema mismatch");
    return null;
  }

  const job = result.data;
  const primary = job.location?.name ?? "";
  const officeLocations = (job.offices ?? [])
    .map((o) => o.location)
    .filter((l): l is string => !!l);
  const locations =
    primary && !officeLocations.includes(primary) ? [primary, ...officeLocations] : officeLocations;

  return {
    source: "greenhouse",
    location: primary,
    locations,
    workplaceType: null,
    country: extractCountry(job.offices?.[0]?.location),
  };
}
