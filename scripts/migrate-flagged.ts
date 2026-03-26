/**
 * One-off migration: convert existing "Flagged" jobs to "Company Applied".
 *
 * Run with: bun scripts/migrate-flagged.ts
 */

import { Client } from "@notionhq/client";

const token = process.env.NOTION_TOKEN;
const databaseId = process.env.NOTION_DATABASE_ID;

if (!token || !databaseId) {
  console.error("Missing NOTION_TOKEN or NOTION_DATABASE_ID");
  process.exit(1);
}

const notion = new Client({ auth: token });
let cursor: string | undefined;
let updated = 0;

do {
  const response = await notion.databases.query({
    database_id: databaseId,
    filter: {
      property: "Status",
      select: { equals: "Flagged" },
    },
    start_cursor: cursor,
  });

  for (const page of response.results) {
    await notion.pages.update({
      page_id: page.id,
      properties: {
        Status: { select: { name: "Company Applied" } },
      },
    });
    updated++;
    console.log(`  Updated ${page.id}`);
  }

  cursor = response.has_more ? (response.next_cursor ?? undefined) : undefined;
} while (cursor);

console.log(`\nDone. Migrated ${updated} job(s) from "Flagged" to "Company Applied".`);
