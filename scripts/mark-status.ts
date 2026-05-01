// Move one or more Notion job pages to a given status.
//
// Usage:
//   bun scripts/mark-status.ts <status> <url> [url...]
//
// Example:
//   bun scripts/mark-status.ts Rejected \
//     https://jobs.lever.co/example/abc-123 \
//     https://jobs.ashbyhq.com/example/def-456
//
// Useful as a companion to /walk-to-review: once verdicts are saved as
// fixtures, use this to reflect them in Notion (To Review → Rejected /
// Applied / Skipped, etc).
import { Client } from "@notionhq/client";
import { JOB_STATUSES, type JobStatus } from "../src/types";
import { updateJobStatus } from "../src/services/notion/mutations";

const token = process.env.NOTION_TOKEN;
const databaseId = process.env.NOTION_DATABASE_ID;
if (!token || !databaseId) {
  console.error("NOTION_TOKEN and NOTION_DATABASE_ID must be set");
  process.exit(1);
}

async function findPageIdByUrl(
  client: Client,
  databaseId: string,
  url: string,
): Promise<string | null> {
  const response = await client.databases.query({
    database_id: databaseId,
    filter: { property: "URL", url: { equals: url } },
    page_size: 1,
  });
  return response.results[0]?.id ?? null;
}

const [statusArg, ...urls] = process.argv.slice(2);
if (!statusArg || urls.length === 0) {
  console.error("Usage: bun scripts/mark-status.ts <status> <url> [url...]");
  console.error(`Valid statuses: ${JOB_STATUSES.join(", ")}`);
  process.exit(1);
}

if (!(JOB_STATUSES as readonly string[]).includes(statusArg)) {
  console.error(`Invalid status "${statusArg}". Valid: ${JOB_STATUSES.join(", ")}`);
  process.exit(1);
}
const status = statusArg as JobStatus;

const notion = new Client({ auth: token });

let updated = 0;
let missing = 0;
for (const url of urls) {
  const pageId = await findPageIdByUrl(notion, databaseId, url);
  if (!pageId) {
    console.log(`MISS  ${url}`);
    missing++;
    continue;
  }
  await updateJobStatus(notion, pageId, status);
  console.log(`OK    ${url} → ${status}`);
  updated++;
}

console.log();
console.log(`Updated: ${updated} | Missing: ${missing}`);
