import { logger } from "../logger";
import type { ScrapeStats } from "../pipeline/processUrl";
import type { ReconcileStats } from "../pipeline/reconcile";
import type { TokenSummary } from "./tokenTracker";

const log = logger.child({ component: "slack" });

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

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function buildRunReportBlocks(
  stats: ScrapeStats,
  reconcileStats: ReconcileStats,
  search: SearchMeta,
  durationMs: number,
  tokens?: TokenSummary,
): object[] {
  const blocks: object[] = [
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
  ];

  if (tokens && tokens.total.calls > 0) {
    const { byStage, total, estimatedCost } = tokens;
    blocks.push(
      { type: "divider" },
      {
        type: "section",
        text: { type: "mrkdwn", text: "*Token Usage*" },
      },
      {
        type: "section",
        fields: [
          {
            type: "mrkdwn",
            text: `*Evaluation:* ${formatTokens(byStage.evaluation.input)} in / ${formatTokens(byStage.evaluation.output)} out (${byStage.evaluation.calls} calls)`,
          },
          {
            type: "mrkdwn",
            text: `*Enrichment:* ${formatTokens(byStage.enrichment.input)} in / ${formatTokens(byStage.enrichment.output)} out (${byStage.enrichment.calls} calls)`,
          },
          {
            type: "mrkdwn",
            text: `*Dedup:* ${formatTokens(byStage.dedup.input)} in / ${formatTokens(byStage.dedup.output)} out (${byStage.dedup.calls} calls)`,
          },
          {
            type: "mrkdwn",
            text: `*Total:* ${formatTokens(total.input)} in / ${formatTokens(total.output)} out · *$${estimatedCost.toFixed(4)}*`,
          },
        ],
      },
    );
  }

  blocks.push(
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
  );

  return blocks;
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
  tokens?: TokenSummary,
): Promise<void> {
  try {
    await postToSlack(webhookUrl, {
      attachments: [
        {
          color: getColor(stats),
          blocks: buildRunReportBlocks(stats, reconcileStats, search, durationMs, tokens),
        },
      ],
    });
    log.info("slack run report sent");
  } catch (err) {
    log.warn({ err }, "failed to send slack run report");
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
    log.info("slack error alert sent");
  } catch (err) {
    log.warn({ err }, "failed to send slack error alert");
  }
}
