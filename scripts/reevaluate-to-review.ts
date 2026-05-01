/**
 * Re-evaluate the current Notion "To Review" pile with the latest eval
 * pipeline (structuralFilter + filters + profiles) and mark rejects as
 * Auto-Rejected. Use after improving prompts to clean up false positives
 * that landed in the pile under older prompts.
 *
 * Run with: bun scripts/reevaluate-to-review.ts [--dry-run] [--limit N]
 *
 *   --dry-run  evaluate but do not update Notion
 *   --limit N  only process the first N entries (useful for testing)
 *
 * Idempotent: jobs already in "Auto-Rejected" or other terminal states are
 * skipped. Re-running is safe.
 */

import { Client } from "@notionhq/client";
import { atsApiRateLimiter, atsApiSemaphore, Semaphore } from "../src/concurrency";
import { evaluateJob, type JobEvaluation } from "../src/pipeline/evaluate";
import { parseJobDetails, scrapeJobPage } from "../src/pipeline/scrape";
import { structuralFilter } from "../src/pipeline/structuralFilter";
import {
  atsStructuralFilter,
  clearAshbyCache,
  fetchAtsData,
  formatAtsBlock,
} from "../src/services/ats";
import { updateJobStatus } from "../src/services/notion";
import type { JobListing } from "../src/types";

const DRY_RUN = process.argv.includes("--dry-run");
const limitArg = process.argv.find((a) => a.startsWith("--limit"));
const LIMIT = limitArg ? Number.parseInt(limitArg.split("=")[1] ?? process.argv[process.argv.indexOf(limitArg) + 1] ?? "", 10) : Number.POSITIVE_INFINITY;

const FIXTURE_CONCURRENCY = 8;

const token = process.env.NOTION_TOKEN;
const databaseId = process.env.NOTION_DATABASE_ID;
const jinaApiKey = process.env.JINA_API_KEY;
const openrouterApiKey = process.env.OPENROUTER_API_KEY;
const llmModel = process.env.LLM_MODEL ?? "google/gemini-2.5-flash";
if (!token || !databaseId || !jinaApiKey || !openrouterApiKey) {
  console.error("Missing one of: NOTION_TOKEN, NOTION_DATABASE_ID, JINA_API_KEY, OPENROUTER_API_KEY");
  process.exit(1);
}

const notion = new Client({ auth: token });

type ToReview = { id: string; title: string; company: string; url: string };

async function fetchToReview(): Promise<ToReview[]> {
  const out: ToReview[] = [];
  let cursor: string | undefined;
  do {
    const resp = await notion.databases.query({
      database_id: databaseId!,
      filter: { property: "Status", select: { equals: "To Review" } },
      sorts: [{ property: "Date Scraped", direction: "descending" }],
      start_cursor: cursor,
      page_size: 100,
    });
    for (const page of resp.results) {
      if (!("properties" in page)) continue;
      const p = page.properties;
      const text = (prop: any): string => {
        if (!prop) return "";
        if (prop.type === "title") return prop.title.map((t: any) => t.plain_text).join("");
        if (prop.type === "rich_text") return prop.rich_text.map((t: any) => t.plain_text).join("");
        if (prop.type === "url") return prop.url ?? "";
        return "";
      };
      const url = text(p["URL"]);
      if (!url) continue;
      out.push({
        id: page.id,
        title: text(p["Job Title"]),
        company: text(p["Company"]),
        url,
      });
    }
    cursor = resp.has_more ? (resp.next_cursor ?? undefined) : undefined;
  } while (cursor);
  return out;
}

async function evaluateOne(item: ToReview): Promise<{ pass: boolean; reason: string; stage: string; atsSource: string | null }> {
  // Re-fetch markdown via Jina so we evaluate against the same input
  // shape the production pipeline sees.
  const markdown = await scrapeJobPage(item.url, { jinaBaseUrl: "https://r.jina.ai", jinaApiKey: jinaApiKey! });
  const job: JobListing = parseJobDetails(markdown, item.url, "");
  // Do NOT overwrite job.title / job.company with the Notion-stored values:
  // those are post-enrich (the enrich LLM step rewrites them after evaluation).
  // The production pipeline runs the eval against the raw extraction, so we
  // must feed the same input to reproduce its verdict — overriding here can
  // flip borderline LLM verdicts (e.g. "swaprail" → PASS but "SwapRail" →
  // FAIL on the location filter, due to capitalisation-driven hallucination).
  // The script's report output uses the Notion-stored item.title/item.company
  // for human readability, which is independent of what the LLM sees.

  // ATS-native enrichment — same step as processUrl. Prepends structured
  // location/workplaceType/country signals to the body before LLM eval.
  const atsData = await atsApiSemaphore.run(() =>
    atsApiRateLimiter.run(() => fetchAtsData(item.url, { title: job.title })),
  );
  if (atsData) {
    job.description = `${formatAtsBlock(atsData)}\n\n${job.description}`;
  }

  const atsSource = atsData?.source ?? null;

  const atsCheck = atsStructuralFilter(atsData);
  if (!atsCheck.pass) {
    return { pass: false, reason: atsCheck.reason, stage: "ats", atsSource };
  }

  const structural = structuralFilter(job);
  if (!structural.pass) {
    return { pass: false, reason: structural.reason, stage: "structural", atsSource };
  }

  const evaluation: JobEvaluation = await evaluateJob(job, openrouterApiKey!, {
    temperature: 0,
    model: llmModel,
  });
  return {
    pass: evaluation.pass,
    reason: evaluation.reason,
    stage: evaluation.pass ? "passed" : "llm-eval",
    atsSource,
  };
}

async function main() {
  // Reset per-run ATS caches (Ashby returns whole-org listings).
  clearAshbyCache();

  const all = await fetchToReview();
  const items = Number.isFinite(LIMIT) ? all.slice(0, LIMIT) : all;
  console.log(`Found ${all.length} To Review entries${items.length < all.length ? ` (processing first ${items.length})` : ""}`);
  console.log(`Mode: ${DRY_RUN ? "DRY RUN (no Notion writes)" : "LIVE — will mark rejects as Auto-Rejected"}\n`);

  const sem = new Semaphore(FIXTURE_CONCURRENCY);
  const verdicts: Array<{ item: ToReview; pass: boolean; reason: string; stage: string; atsSource: string | null; error?: string }> = [];
  let completed = 0;

  await Promise.all(
    items.map((item) =>
      sem.run(async () => {
        try {
          const v = await evaluateOne(item);
          verdicts.push({ item, ...v });
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          verdicts.push({ item, pass: true, reason: "errored", stage: "error", atsSource: null, error: message });
        }
        completed++;
        if (completed % 10 === 0 || completed === items.length) {
          console.error(`  progress: ${completed}/${items.length}`);
        }
      }),
    ),
  );

  const rejects = verdicts.filter((v) => !v.pass);
  const passes = verdicts.filter((v) => v.pass);
  const errors = verdicts.filter((v) => v.error);

  const atsEnriched = verdicts.filter((v) => v.atsSource !== null);
  const atsRejects = rejects.filter((v) => v.atsSource !== null);

  console.log(`\n=== Summary ===`);
  console.log(`  Total: ${verdicts.length}`);
  console.log(`  Passes (would stay in To Review): ${passes.length}`);
  console.log(`  Rejects (would be marked Auto-Rejected): ${rejects.length}`);
  console.log(`  Errors during eval: ${errors.length}`);
  console.log(`  ATS-enriched: ${atsEnriched.length} (rejects: ${atsRejects.length})`);

  if (rejects.length > 0) {
    console.log(`\n=== Reject candidates ===`);
    for (const r of rejects) {
      const ats = r.atsSource ? ` [ats:${r.atsSource}]` : "";
      console.log(`\n[${r.stage}]${ats} ${r.item.title} @ ${r.item.company}`);
      console.log(`  ${r.item.url}`);
      console.log(`  reason: ${r.reason}`);
    }
  }

  if (DRY_RUN) {
    console.log(`\n(dry run — no Notion writes)`);
    return;
  }

  if (rejects.length === 0) {
    console.log(`\nNo changes needed.`);
    return;
  }

  console.log(`\nUpdating ${rejects.length} jobs to Auto-Rejected...`);
  let updated = 0;
  for (const r of rejects) {
    try {
      await updateJobStatus(notion, r.item.id, "Auto-Rejected");
      updated++;
      if (updated % 10 === 0) console.error(`  updated: ${updated}/${rejects.length}`);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.error(`  failed to update ${r.item.id}: ${message}`);
    }
  }
  console.log(`\nDone. Updated ${updated}/${rejects.length} jobs to Auto-Rejected.`);
}

await main();
