export interface RichTextItem {
  plain_text: string;
}

export function extractRichText(items: RichTextItem[]): string {
  return items.map((t) => t.plain_text).join("");
}

export function toDateString(date: Date): string {
  return date.toISOString().split("T")[0] ?? "";
}

/** The job-identity fields the pipeline reads off a Notion page. */
export interface PageIdentity {
  url: string;
  company: string;
  title: string;
  status: string;
  appDate: string | null;
}

interface PageProperties {
  URL?: { type?: string; url?: string | null };
  Company?: { type?: string; rich_text?: RichTextItem[] };
  "Job Title"?: { type?: string; title?: RichTextItem[] };
  Status?: { type?: string; select?: { name?: string } | null };
  "Application Date"?: { type?: string; date?: { start?: string | null } | null };
}

export function extractPageIdentity(
  page: { properties?: PageProperties } | undefined,
): PageIdentity {
  const props = page?.properties ?? {};

  const urlProp = props.URL;
  const rawUrl = urlProp?.type === "url" ? urlProp.url : null;
  const url = typeof rawUrl === "string" ? rawUrl : "";

  const companyProp = props.Company;
  const company =
    companyProp?.type === "rich_text" ? extractRichText(companyProp.rich_text ?? []) : "";

  const titleProp = props["Job Title"];
  const title = titleProp?.type === "title" ? extractRichText(titleProp.title ?? []) : "";

  const statusProp = props.Status;
  const selectVal = statusProp?.type === "select" ? statusProp.select : null;
  const status = selectVal && "name" in selectVal ? (selectVal.name ?? "") : "";

  const appDateProp = props["Application Date"];
  const appDate = appDateProp?.type === "date" ? (appDateProp.date?.start ?? null) : null;

  return { url, company, title, status, appDate };
}
