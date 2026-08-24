export function normalizeJobLedgerText(value: string): string {
  return value.trim().replace(/\s+/g, " ").toLowerCase();
}
