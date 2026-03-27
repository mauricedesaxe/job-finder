import type { Client } from "@notionhq/client";
import { JOB_STATUSES } from "./types";

const EXPECTED_PROPERTIES: Record<string, string> = {
  "Job Title": "title",
  Company: "rich_text",
  URL: "url",
  Source: "select",
  Keywords: "multi_select",
  "Date Scraped": "date",
  "Date Posted": "date",
  Location: "rich_text",
  Status: "select",
  "Application Date": "date",
};

export async function runPreflight(
  notion: Client,
  databaseId: string,
): Promise<void> {
  console.log("Running preflight checks...\n");

  // Retrieve database schema (also validates token + database access)
  const db = await notion.databases.retrieve({ database_id: databaseId });

  // Validate properties exist with correct types
  const errors: string[] = [];

  for (const [name, expectedType] of Object.entries(EXPECTED_PROPERTIES)) {
    const prop = db.properties[name];
    if (!prop) {
      errors.push(`Missing property: "${name}" (expected type: ${expectedType})`);
    } else if (prop.type !== expectedType) {
      errors.push(
        `Wrong type for "${name}": got "${prop.type}", expected "${expectedType}"`,
      );
    }
  }

  if (errors.length > 0) {
    console.error("Preflight failed — database schema errors:");
    for (const error of errors) {
      console.error(`  ✗ ${error}`);
    }
    process.exit(1);
  }

  // Validate status options
  const statusProp = db.properties.Status;
  if (statusProp.type !== "select") return; // already validated above

  const existingOptions = new Set(
    statusProp.select.options.map((o) => o.name),
  );
  const missingStatuses = JOB_STATUSES.filter((s) => !existingOptions.has(s));

  if (missingStatuses.length > 0) {
    console.log(
      `Creating missing status options: ${missingStatuses.join(", ")}`,
    );

    await notion.databases.update({
      database_id: databaseId,
      properties: {
        Status: {
          select: {
            options: [
              ...statusProp.select.options,
              ...missingStatuses.map((name) => ({ name })),
            ],
          },
        },
      },
    });
  }

  console.log("Preflight checks passed.\n");
}
