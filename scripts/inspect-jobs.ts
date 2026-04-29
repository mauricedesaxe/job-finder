/**
 * Diagnostic: dump recent jobs from Notion grouped by Status.
 *
 * Run with: bun scripts/inspect-jobs.ts [Status1] [Status2] ...
 * Defaults to: "To Review" "Rejected" "Skipped" "Applied"
 *
 * Used to spot-check eval pipeline output against manual review decisions.
 */

import { Client } from "@notionhq/client";

const token = process.env.NOTION_TOKEN;
const databaseId = process.env.NOTION_DATABASE_ID;

if (!token || !databaseId) {
  console.error("Missing NOTION_TOKEN or NOTION_DATABASE_ID");
  process.exit(1);
}

const notion = new Client({ auth: token });

type Row = {
  id: string;
  title: string;
  company: string;
  status: string;
  profile: string;
  keywords: string[];
  location: string;
  url: string;
  scraped: string;
};

async function fetchByStatus(status: string, limit = 15): Promise<Row[]> {
  const response = await notion.databases.query({
    database_id: databaseId!,
    filter: { property: "Status", select: { equals: status } },
    sorts: [{ property: "Date Scraped", direction: "descending" }],
    page_size: limit,
  });

  const rows: Row[] = [];
  for (const page of response.results) {
    if (!("properties" in page)) continue;
    const p = page.properties;
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
    rows.push({
      id: page.id,
      title: text(p["Job Title"]),
      company: text(p["Company"]),
      status: text(p["Status"]),
      profile: text(p["Profile"]),
      keywords: text(p["Keywords"]).split(",").map((s) => s.trim()).filter(Boolean),
      location: text(p["Location"]),
      url: text(p["URL"]),
      scraped: text(p["Date Scraped"]),
    });
  }
  return rows;
}

const statuses = process.argv.slice(2);
const targets = statuses.length > 0 ? statuses : ["To Review", "Rejected", "Skipped", "Applied"];

for (const s of targets) {
  const rows = await fetchByStatus(s, 15);
  console.log(`\n=== ${s} (${rows.length}) ===`);
  for (const r of rows) {
    console.log(`\n[${r.scraped}] ${r.title} @ ${r.company}`);
    console.log(`  profile: ${r.profile}  |  location: ${r.location}`);
    console.log(`  keywords: ${r.keywords.join(", ")}`);
    console.log(`  url: ${r.url}`);
  }
}
