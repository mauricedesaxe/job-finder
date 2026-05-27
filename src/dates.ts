/** Returns a Date `months` calendar-months before `from` (defaults to now). */
export function monthsAgo(months: number, from: Date = new Date()): Date {
  const d = new Date(from);
  d.setMonth(d.getMonth() - months);
  return d;
}

/** Returns a Date `days` before `from` (defaults to now). */
export function daysAgo(days: number, from: Date = new Date()): Date {
  const d = new Date(from);
  d.setDate(d.getDate() - days);
  return d;
}
