import type { JobLedger, NotionBackfillStats } from "./jobLedger";
import type { ResilientNotionClient } from "./notion/client";
import { extractRichText, type RichTextItem } from "./notion/helpers";

export const NOTION_JOB_LEDGER_BACKFILL_MIGRATION = "notion-job-ledger-backfill-v1";

interface NotionJobRecord {
  sourceKey: string;
  rawUrl: string | null;
  company: string;
  title: string;
  processedAt: string;
}

interface NotionCompanyExclusion {
  sourceKey: string;
  company: string;
  excludedAt: string;
}

interface NotionLedgerSnapshot {
  jobs: NotionJobRecord[];
  exclusions: NotionCompanyExclusion[];
  stats: NotionBackfillStats;
}

export interface BackfillJobLedgerOptions {
  client: ResilientNotionClient;
  databaseId: string;
  ledger: JobLedger;
  completedAt?: string;
}

export interface BackfillJobLedgerResult {
  stats: NotionBackfillStats;
  migrationName: string;
}

export async function backfillJobLedger({
  client,
  databaseId,
  ledger,
  completedAt = new Date().toISOString(),
}: BackfillJobLedgerOptions): Promise<BackfillJobLedgerResult> {
  const snapshot = await scanNotionJobs(client, databaseId, completedAt);

  for (const job of snapshot.jobs) {
    ledger.recordProcessedJob({
      sourceKey: job.sourceKey,
      rawUrl: job.rawUrl ?? undefined,
      company: job.company,
      title: job.title,
      outcome: "historical",
      processedAt: job.processedAt,
    });
  }

  for (const exclusion of snapshot.exclusions) {
    ledger.excludeCompany(exclusion);
  }

  const actualStats = ledger.notionBackfillStats();
  verifyBackfill(snapshot.stats, actualStats);
  ledger.markMigration(NOTION_JOB_LEDGER_BACKFILL_MIGRATION, completedAt);

  return { stats: actualStats, migrationName: NOTION_JOB_LEDGER_BACKFILL_MIGRATION };
}

async function scanNotionJobs(
  client: ResilientNotionClient,
  databaseId: string,
  fallbackProcessedAt: string,
): Promise<NotionLedgerSnapshot> {
  const jobs: NotionJobRecord[] = [];
  const exclusions: NotionCompanyExclusion[] = [];
  let cursor: string | undefined;

  do {
    const response = await client.databases.query({
      database_id: databaseId,
      start_cursor: cursor,
    });

    for (const page of response.results) {
      if (!("properties" in page)) continue;

      const company = propertyText(page.properties.Company, "rich_text");
      const title = propertyText(page.properties["Job Title"], "title");
      const url = propertyUrl(page.properties.URL);
      const status = propertySelect(page.properties.Status);
      const processedAt =
        "created_time" in page && typeof page.created_time === "string"
          ? page.created_time
          : fallbackProcessedAt;
      const sourceKey = `notion:${page.id}`;

      if (company && title) {
        jobs.push({ sourceKey, rawUrl: url, company, title, processedAt });
      }

      if (company && status === "Company Blocked") {
        exclusions.push({ sourceKey, company, excludedAt: processedAt });
      }
    }

    cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
  } while (cursor);

  return { jobs, exclusions, stats: snapshotStats(jobs, exclusions) };
}

function propertyText(property: unknown, type: "rich_text" | "title"): string {
  if (!hasPropertyType(property, type)) return "";

  const items = property[type];
  return Array.isArray(items) ? extractRichText(items.filter(isRichTextItem)) : "";
}

function propertyUrl(property: unknown): string | null {
  if (!hasPropertyType(property, "url")) return null;

  const url = property.url;
  return typeof url === "string" && url.length > 0 ? url : null;
}

function propertySelect(property: unknown): string | null {
  if (!hasPropertyType(property, "select")) return null;

  const select = property.select;
  return select && typeof select === "object" && "name" in select && typeof select.name === "string"
    ? select.name
    : null;
}

function hasPropertyType(property: unknown, type: string): property is Record<string, unknown> {
  return !!property && typeof property === "object" && "type" in property && property.type === type;
}

function isRichTextItem(value: unknown): value is RichTextItem {
  return (
    !!value &&
    typeof value === "object" &&
    "plain_text" in value &&
    typeof value.plain_text === "string"
  );
}

function snapshotStats(
  jobs: NotionJobRecord[],
  exclusions: NotionCompanyExclusion[],
): NotionBackfillStats {
  return {
    sourceRows: new Set(jobs.map((job) => job.sourceKey)).size,
    urls: new Set(jobs.flatMap((job) => (job.rawUrl ? [job.rawUrl] : []))).size,
    companyTitlePairs: new Set(jobs.map((job) => companyTitleKey(job))).size,
    urlLessRows: jobs.filter((job) => job.rawUrl === null).length,
    exclusions: new Set(exclusions.map((exclusion) => normalizeText(exclusion.company))).size,
  };
}

function verifyBackfill(expected: NotionBackfillStats, actual: NotionBackfillStats): void {
  const mismatches = Object.entries(expected).filter(
    ([key, value]) => actual[key as keyof NotionBackfillStats] !== value,
  );
  if (mismatches.length === 0) return;

  const details = mismatches
    .map(
      ([key, value]) =>
        `${key}: expected ${value}, got ${actual[key as keyof NotionBackfillStats]}`,
    )
    .join(", ");
  throw new Error(`Notion ledger backfill verification failed. ${details}`);
}

function companyTitleKey(job: NotionJobRecord): string {
  return `${normalizeText(job.company)}\u0000${normalizeText(job.title)}`;
}

function normalizeText(value: string): string {
  return value.trim().replace(/\s+/g, " ").toLocaleLowerCase();
}
