import { REAPPLY_WINDOW_MONTHS } from "../config/recency";
import { monthsAgo } from "../dates";
import type {
  ImportedNotionCompanyState,
  JobLedger,
  SelectiveNotionImportStats,
} from "./jobLedger";
import { normalizeJobLedgerText } from "./jobLedgerRecord";
import type { ResilientNotionClient } from "./notion/client";
import { extractRichText, type RichTextItem } from "./notion/helpers";

export const NOTION_COMPANY_STATE_IMPORT_MIGRATION = "notion-company-state-import-v2";

export interface InitializeJobLedgerFromNotionOptions {
  client: ResilientNotionClient;
  databaseId: string;
  ledger: JobLedger;
  completedAt?: string;
}

export type InitializeJobLedgerFromNotionResult =
  | {
      kind: "initialized";
      stats: SelectiveNotionImportStats;
      migrationName: string;
    }
  | {
      kind: "already-initialized";
      migrationName: string;
    };

interface NotionCompanyStateSnapshot {
  states: ImportedNotionCompanyState[];
  stats: SelectiveNotionImportStats;
}

export function isJobLedgerReadyForScrape(ledger: JobLedger): Promise<boolean> {
  return ledger.hasMigration(NOTION_COMPANY_STATE_IMPORT_MIGRATION);
}

export async function initializeJobLedgerFromNotion({
  client,
  databaseId,
  ledger,
  completedAt = new Date().toISOString(),
}: InitializeJobLedgerFromNotionOptions): Promise<InitializeJobLedgerFromNotionResult> {
  if (await ledger.hasMigration(NOTION_COMPANY_STATE_IMPORT_MIGRATION)) {
    return {
      kind: "already-initialized",
      migrationName: NOTION_COMPANY_STATE_IMPORT_MIGRATION,
    };
  }

  const cutoff = formatDate(monthsAgo(REAPPLY_WINDOW_MONTHS, new Date(completedAt)));
  const snapshot = await readNotionCompanyState(client, databaseId, completedAt, cutoff);
  const actualStats = await ledger.replaceImportedNotionCompanyState(snapshot.states);
  verifyImport(snapshot.stats, actualStats);
  await ledger.markMigration(NOTION_COMPANY_STATE_IMPORT_MIGRATION, completedAt);

  return {
    kind: "initialized",
    stats: actualStats,
    migrationName: NOTION_COMPANY_STATE_IMPORT_MIGRATION,
  };
}

async function readNotionCompanyState(
  client: ResilientNotionClient,
  databaseId: string,
  importedAt: string,
  cutoff: string,
): Promise<NotionCompanyStateSnapshot> {
  const blockedCompanies = new Map<
    string,
    Extract<ImportedNotionCompanyState, { kind: "blocked" }>
  >();
  const recentApplications = new Map<
    string,
    Extract<ImportedNotionCompanyState, { kind: "recent-application" }>
  >();
  let cursor: string | undefined;

  do {
    const response = await client.databases.query({
      database_id: databaseId,
      start_cursor: cursor,
      filter: {
        or: [
          { property: "Status", select: { equals: "Company Blocked" } },
          { property: "Application Date", date: { on_or_after: cutoff } },
        ],
      },
    });

    for (const page of response.results) {
      if (!("properties" in page)) continue;

      const company = propertyText(page.properties.Company);
      if (!company) continue;

      const normalizedCompany = normalizeJobLedgerText(company);
      const sourceKey = `notion:${page.id}`;
      if (propertySelect(page.properties.Status) === "Company Blocked") {
        blockedCompanies.set(normalizedCompany, {
          kind: "blocked",
          company,
          sourceKey,
          importedAt,
        });
      }

      const applicationDate = propertyDate(page.properties["Application Date"]);
      if (applicationDate && applicationDate >= cutoff) {
        const existing = recentApplications.get(normalizedCompany);
        if (!existing || applicationDate > existing.applicationDate) {
          recentApplications.set(normalizedCompany, {
            kind: "recent-application",
            company,
            sourceKey,
            importedAt,
            applicationDate,
          });
        }
      }
    }

    cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
  } while (cursor);

  return {
    states: [...blockedCompanies.values(), ...recentApplications.values()],
    stats: {
      blockedCompanies: blockedCompanies.size,
      recentApplications: recentApplications.size,
    },
  };
}

function propertyText(property: unknown): string {
  if (!hasPropertyType(property, "rich_text")) return "";
  const items = property.rich_text;
  return Array.isArray(items) ? extractRichText(items.filter(isRichTextItem)) : "";
}

function propertySelect(property: unknown): string | null {
  if (!hasPropertyType(property, "select")) return null;
  const select = property.select;
  return select && typeof select === "object" && "name" in select && typeof select.name === "string"
    ? select.name
    : null;
}

function propertyDate(property: unknown): string | null {
  if (!hasPropertyType(property, "date")) return null;
  const date = property.date;
  if (!date || typeof date !== "object" || !("start" in date)) return null;
  return typeof date.start === "string" ? date.start.slice(0, 10) : null;
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

function verifyImport(
  expected: SelectiveNotionImportStats,
  actual: SelectiveNotionImportStats,
): void {
  const mismatches: string[] = [];
  if (actual.blockedCompanies !== expected.blockedCompanies) {
    mismatches.push(
      `blockedCompanies: expected ${expected.blockedCompanies}, got ${actual.blockedCompanies}`,
    );
  }
  if (actual.recentApplications !== expected.recentApplications) {
    mismatches.push(
      `recentApplications: expected ${expected.recentApplications}, got ${actual.recentApplications}`,
    );
  }
  if (mismatches.length === 0) return;

  throw new Error(
    `Notion company state initialization verification failed. ${mismatches.join(", ")}`,
  );
}

function formatDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}
