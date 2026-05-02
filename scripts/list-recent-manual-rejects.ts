/**
 * Lists the most recent manually-rejected jobs (Status=Rejected with
 * Profile/Location filled — i.e. eval marked them pass but Alex rejected
 * after reviewing) that are not yet covered by an existing fixture URL.
 *
 * Distinct from Status=Auto-Rejected, where the pipeline rejected on its own
 * without Alex ever seeing the job. These manual rejects are confirmed false
 * positives — every one is a high-signal `reject/` fixture candidate.
 *
 * Usage: bun scripts/list-recent-manual-rejects.ts [N]   (default N = 20)
 */

import { readdirSync, readFileSync } from "node:fs";
import { Client } from "@notionhq/client";

const token = process.env.NOTION_TOKEN!;
const databaseId = process.env.NOTION_DATABASE_ID!;
const notion = new Client({ auth: token });

const limitArg = Number.parseInt(process.argv[2] ?? "", 10);
const LIMIT = Number.isFinite(limitArg) && limitArg > 0 ? limitArg : 20;

const FIX_DIRS = [
  "src/pipeline/__integration__/fixtures/evaluate/pass",
  "src/pipeline/__integration__/fixtures/evaluate/reject",
];

const fixtureUrls = new Set<string>();
for (const dir of FIX_DIRS) {
  for (const f of readdirSync(dir, { recursive: true }) as string[]) {
    if (!f.endsWith(".md")) continue;
    const c = readFileSync(`${dir}/${f}`, "utf8");
    const m = c.match(/^URL Source:\s*(.+)$/m);
    if (m?.[1]) fixtureUrls.add(m[1].trim());
  }
}

const text = (prop: any): string => {
  if (!prop) return "";
  if (prop.type === "title") return prop.title.map((t: any) => t.plain_text).join("");
  if (prop.type === "rich_text") return prop.rich_text.map((t: any) => t.plain_text).join("");
  if (prop.type === "url") return prop.url ?? "";
  if (prop.type === "select") return prop.select?.name ?? "";
  if (prop.type === "multi_select") return prop.multi_select.map((o: any) => o.name).join(", ");
  if (prop.type === "date") return prop.date?.start ?? "";
  return "";
};

type Row = {
  title: string;
  company: string;
  profile: string;
  location: string;
  url: string;
  scraped: string;
};

const collected: Row[] = [];
let scanned = 0;
let cursor: string | undefined;

while (collected.length < LIMIT) {
  const resp = await notion.databases.query({
    database_id: databaseId,
    filter: { property: "Status", select: { equals: "Rejected" } },
    sorts: [{ property: "Date Scraped", direction: "descending" }],
    start_cursor: cursor,
    page_size: 100,
  });
  for (const page of resp.results) {
    if (!("properties" in page)) continue;
    scanned++;
    const p = page.properties;
    const row: Row = {
      title: text(p["Job Title"]),
      company: text(p["Company"]),
      profile: text(p["Profile"]),
      location: text(p["Location"]),
      url: text(p["URL"]),
      scraped: text(p["Date Scraped"]),
    };
    const isUserRejected = Boolean(row.profile || row.location);
    if (!isUserRejected) continue;
    if (!row.url || fixtureUrls.has(row.url)) continue;
    collected.push(row);
    if (collected.length >= LIMIT) break;
  }
  if (!resp.has_more) break;
  cursor = resp.next_cursor ?? undefined;
}

console.log(`Scanned ${scanned} Rejected entries. Showing ${collected.length} user-rejected without a fixture.`);
console.log(`(user-rejected = Profile/Location filled — eval said pass, Alex rejected after review)`);
console.log();

for (const [i, r] of collected.entries()) {
  console.log(`${(i + 1).toString().padStart(3, " ")}. [${r.scraped}] ${r.title} @ ${r.company}`);
  console.log(`     profile: ${r.profile} | loc: ${r.location}`);
  console.log(`     ${r.url}`);
}
