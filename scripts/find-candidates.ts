import { readdirSync, readFileSync } from "node:fs";
import { Client } from "@notionhq/client";

const token = process.env.NOTION_TOKEN;
const databaseId = process.env.NOTION_DATABASE_ID;
if (!token || !databaseId) throw new Error("missing env");
const notion = new Client({ auth: token });

const FIX_DIRS = [
  "src/pipeline/__integration__/fixtures/evaluate/pass",
  "src/pipeline/__integration__/fixtures/evaluate/reject",
];

const fixtureUrls = new Set<string>();
for (const dir of FIX_DIRS) {
  for (const f of readdirSync(dir)) {
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

async function fetchByStatus(status: string, limit = 60) {
  const out: Array<{
    title: string;
    company: string;
    profile: string;
    location: string;
    url: string;
    scraped: string;
  }> = [];
  let cursor: string | undefined;
  while (out.length < limit) {
    const resp = await notion.databases.query({
      database_id: databaseId!,
      filter: { property: "Status", select: { equals: status } },
      sorts: [{ property: "Date Scraped", direction: "descending" }],
      start_cursor: cursor,
      page_size: 50,
    });
    for (const page of resp.results) {
      if (!("properties" in page)) continue;
      const p = page.properties;
      out.push({
        title: text(p["Job Title"]),
        company: text(p["Company"]),
        profile: text(p["Profile"]),
        location: text(p["Location"]),
        url: text(p["URL"]),
        scraped: text(p["Date Scraped"]),
      });
      if (out.length >= limit) break;
    }
    if (!resp.has_more) break;
    cursor = resp.next_cursor ?? undefined;
  }
  return out;
}

const rejected = await fetchByStatus("Rejected", 200);
const skipped = await fetchByStatus("Skipped", 60);

const userRej = rejected.filter((r) => r.profile || r.location);
const autoRej = rejected.filter((r) => !r.profile && !r.location);
const userRejNew = userRej.filter((r) => r.url && !fixtureUrls.has(r.url));
const autoRejNew = autoRej.filter((r) => r.url && !fixtureUrls.has(r.url));
const skippedNew = skipped.filter((r) => r.url && !fixtureUrls.has(r.url));

console.log(`\n=== Pile breakdown ===`);
console.log(`  Rejected pulled: ${rejected.length}`);
console.log(`    user-rejected (Profile/Location filled): ${userRej.length}, new: ${userRejNew.length}`);
console.log(`    auto-rejected legacy (Profile/Location empty): ${autoRej.length}, new: ${autoRejNew.length}`);
console.log(`  Skipped pulled: ${skipped.length}, new: ${skippedNew.length}`);
console.log(`  Existing fixture URLs: ${fixtureUrls.size}`);

if (userRejNew.length > 0) {
  console.log(`\n=== USER-REJECTED (false positives — highest signal) ===`);
  for (const r of userRejNew) {
    console.log(`\n[${r.scraped}] ${r.title} @ ${r.company}`);
    console.log(`  profile: ${r.profile}  |  location: ${r.location}`);
    console.log(`  url: ${r.url}`);
  }
}

if (skippedNew.length > 0) {
  console.log(`\n=== SKIPPED (you looked, chose not to apply) ===`);
  for (const r of skippedNew) {
    console.log(`\n[${r.scraped}] ${r.title} @ ${r.company}`);
    console.log(`  profile: ${r.profile}  |  location: ${r.location}`);
    console.log(`  url: ${r.url}`);
  }
}

console.log(`\n=== AUTO-REJECTED (true negatives — confirm pipeline still rejects) — first 30 ===`);
for (const r of autoRejNew.slice(0, 30)) {
  console.log(`\n[${r.scraped}] ${r.title} @ ${r.company}`);
  console.log(`  url: ${r.url}`);
}
