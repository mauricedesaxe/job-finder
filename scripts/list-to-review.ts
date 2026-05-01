import { readdirSync, readFileSync } from "node:fs";
import { Client } from "@notionhq/client";

const token = process.env.NOTION_TOKEN!;
const databaseId = process.env.NOTION_DATABASE_ID!;
const notion = new Client({ auth: token });

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

const all: Array<{title: string; company: string; profile: string; location: string; url: string; scraped: string}> = [];
let cursor: string | undefined;
do {
  const resp = await notion.databases.query({
    database_id: databaseId,
    filter: { property: "Status", select: { equals: "To Review" } },
    sorts: [{ property: "Date Scraped", direction: "descending" }],
    start_cursor: cursor,
    page_size: 100,
  });
  for (const page of resp.results) {
    if (!("properties" in page)) continue;
    const p = page.properties;
    all.push({
      title: text(p["Job Title"]),
      company: text(p["Company"]),
      profile: text(p["Profile"]),
      location: text(p["Location"]),
      url: text(p["URL"]),
      scraped: text(p["Date Scraped"]),
    });
  }
  cursor = resp.has_more ? resp.next_cursor ?? undefined : undefined;
} while (cursor);

const newOnes = all.filter((r) => r.url && !fixtureUrls.has(r.url));

console.log(`Total To Review: ${all.length}`);
console.log(`Already fixtures: ${all.length - newOnes.length}`);
console.log(`New (no fixture yet): ${newOnes.length}`);
console.log();

for (const [i, r] of newOnes.entries()) {
  console.log(`${(i + 1).toString().padStart(3, " ")}. [${r.scraped}] ${r.title} @ ${r.company}`);
  console.log(`     profile: ${r.profile} | loc: ${r.location}`);
  console.log(`     ${r.url}`);
}
