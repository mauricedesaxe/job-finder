import type { ReconcileStats } from "../pipeline/reconcile";

export interface ScrapeStats {
  inserted: number;
  skipped: number;
  companyApplied: number;
  rejected: number;
  archived: number;
  duplicated: number;
  errored: number;
}

export interface SearchMeta {
  urlCount: number;
  searchErrors: number;
}

function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes === 0) return `${remainingSeconds}s`;
  return `${minutes}m ${remainingSeconds}s`;
}

function buildRunReportBlocks(
  stats: ScrapeStats,
  reconcileStats: ReconcileStats,
  search: SearchMeta,
  durationMs: number,
): object[] {
  return [
    {
      type: "header",
      text: { type: "plain_text", text: "📊 Scrape Complete" },
    },
    {
      type: "section",
      text: {
        type: "mrkdwn",
        text: "*Results*",
      },
    },
    {
      type: "section",
      fields: [
        { type: "mrkdwn", text: `*Inserted:* ${stats.inserted}` },
        { type: "mrkdwn", text: `*Rejected:* ${stats.rejected}` },
        { type: "mrkdwn", text: `*Duplicated:* ${stats.duplicated}` },
        { type: "mrkdwn", text: `*Archived:* ${stats.archived}` },
        { type: "mrkdwn", text: `*Company Applied:* ${stats.companyApplied}` },
        { type: "mrkdwn", text: `*Skipped:* ${stats.skipped}` },
        { type: "mrkdwn", text: `*Errored:* ${stats.errored}` },
      ],
    },
    { type: "divider" },
    {
      type: "section",
      text: {
        type: "mrkdwn",
        text: "*Reconciliation*",
      },
    },
    {
      type: "section",
      fields: [
        { type: "mrkdwn", text: `*Auto-Applied:* ${reconcileStats.applied}` },
        { type: "mrkdwn", text: `*Unstaled:* ${reconcileStats.unstaled}` },
        {
          type: "mrkdwn",
          text: `*Company Applied:* ${reconcileStats.companyApplied}`,
        },
        { type: "mrkdwn", text: `*Archived:* ${reconcileStats.archived}` },
      ],
    },
    { type: "divider" },
    {
      type: "context",
      elements: [
        {
          type: "mrkdwn",
          text: `🔍 ${search.urlCount} unique URLs · ${search.searchErrors} search errors · ⏱️ ${formatDuration(durationMs)}`,
        },
      ],
    },
  ];
}

function getColor(stats: ScrapeStats): string {
  if (stats.errored > 0) return "#f59e0b"; // orange
  return "#22c55e"; // green
}

async function postToSlack(webhookUrl: string, payload: object): Promise<void> {
  const response = await fetch(webhookUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Slack webhook failed (${response.status}): ${text}`);
  }
}

export async function sendRunReport(
  webhookUrl: string,
  stats: ScrapeStats,
  reconcileStats: ReconcileStats,
  search: SearchMeta,
  durationMs: number,
): Promise<void> {
  try {
    await postToSlack(webhookUrl, {
      attachments: [
        {
          color: getColor(stats),
          blocks: buildRunReportBlocks(stats, reconcileStats, search, durationMs),
        },
      ],
    });
    console.log("  ✓ Slack run report sent");
  } catch (err) {
    console.warn(`  ⚠ Failed to send Slack alert: ${err instanceof Error ? err.message : err}`);
  }
}

export async function sendFatalError(webhookUrl: string, error: unknown): Promise<void> {
  const message = error instanceof Error ? error.message : String(error);
  const stack =
    error instanceof Error && error.stack ? error.stack.slice(0, 500) : "No stack trace";

  try {
    await postToSlack(webhookUrl, {
      attachments: [
        {
          color: "#ef4444", // red
          blocks: [
            {
              type: "header",
              text: { type: "plain_text", text: "🚨 Scrape Failed" },
            },
            {
              type: "section",
              text: {
                type: "mrkdwn",
                text: `*Error:* ${message}`,
              },
            },
            {
              type: "section",
              text: {
                type: "mrkdwn",
                text: `\`\`\`${stack}\`\`\``,
              },
            },
          ],
        },
      ],
    });
    console.log("  ✓ Slack error alert sent");
  } catch (err) {
    console.warn(
      `  ⚠ Failed to send Slack error alert: ${err instanceof Error ? err.message : err}`,
    );
  }
}
