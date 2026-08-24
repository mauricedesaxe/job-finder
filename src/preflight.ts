import { logger } from "./logger";
import type { ResilientNotionClient } from "./services/notion";
import { JOB_STATUSES } from "./types";

const log = logger.child({ component: "preflight" });

const EXPECTED_PROPERTIES: Record<string, string> = {
  "Job Title": "title",
  Company: "rich_text",
  URL: "url",
  Source: "select",
  Keywords: "multi_select",
  "Date Scraped": "date",
  "Date Posted": "date",
  Location: "rich_text",
  Profile: "select",
  Status: "select",
  "Application Date": "date",
};

export async function runPreflight(
  notion: ResilientNotionClient,
  databaseId: string,
): Promise<void> {
  log.info("running preflight checks");

  // Retrieve database schema (also validates token + database access)
  const db = await notion.databases.retrieve({ database_id: databaseId });
  assertNotionSchema(db.properties);

  // Validate status options
  const statusProp = db.properties.Status;
  if (!statusProp || statusProp.type !== "select") return; // already validated above

  const existingOptions = new Set(statusProp.select.options.map((o) => o.name));
  const missingStatuses = JOB_STATUSES.filter((s) => !existingOptions.has(s));

  if (missingStatuses.length > 0) {
    log.info({ statuses: missingStatuses }, "creating missing status options");

    await notion.databases.update({
      database_id: databaseId,
      properties: {
        Status: {
          select: {
            options: [...statusProp.select.options, ...missingStatuses.map((name) => ({ name }))],
          },
        },
      },
    });
  }

  log.info("preflight checks passed");
}

export function assertNotionSchema(
  properties: Readonly<Record<string, { type: string } | undefined>>,
): void {
  const errors: string[] = [];

  for (const [name, expectedType] of Object.entries(EXPECTED_PROPERTIES)) {
    const prop = properties[name];
    if (!prop) {
      errors.push(`Missing property: "${name}" (expected type: ${expectedType})`);
    } else if (prop.type !== expectedType) {
      errors.push(`Wrong type for "${name}": got "${prop.type}", expected "${expectedType}"`);
    }
  }

  if (errors.length > 0) {
    throw new Error(`Preflight failed: ${errors.join("; ")}`);
  }
}
