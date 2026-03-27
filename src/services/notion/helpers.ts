export interface RichTextItem {
  plain_text: string;
}

export function extractRichText(items: RichTextItem[]): string {
  return items.map((t) => t.plain_text).join("");
}

export function toDateString(date: Date): string {
  return date.toISOString().split("T")[0] ?? "";
}
