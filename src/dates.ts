/** Returns a Date `months` calendar-months before `from` (defaults to now). */
export function monthsAgo(months: number, from: Date = new Date()): Date {
  const d = new Date(from);
  const originalDayOfMonth = d.getUTCDate();
  d.setUTCDate(1);
  d.setUTCMonth(d.getUTCMonth() - months);
  const lastDayOfTargetMonth = new Date(
    Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0),
  ).getUTCDate();
  d.setUTCDate(Math.min(originalDayOfMonth, lastDayOfTargetMonth));
  return d;
}

/** Returns a Date `days` before `from` (defaults to now). */
export function daysAgo(days: number, from: Date = new Date()): Date {
  const d = new Date(from);
  d.setDate(d.getDate() - days);
  return d;
}
