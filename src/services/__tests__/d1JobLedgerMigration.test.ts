import { expect, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { z } from "zod/v4";

const projectRoot = resolve(import.meta.dir, "../../..");
const wranglerPath = join(projectRoot, "node_modules/wrangler/bin/wrangler.js");
const migrationDirectory = join(projectRoot, "migrations");

const WranglerResultSchema = z.array(
  z.object({
    results: z.array(z.unknown()),
    success: z.literal(true),
    meta: z.unknown(),
  }),
);

const JobLedgerProofSchema = z.object({
  exact_source_key: z.string(),
  exact_miss_count: z.number(),
  normalized_titles: z.string(),
  upsert_first_processed_at: z.string(),
  upsert_last_processed_at: z.string(),
  upsert_outcome: z.string(),
  upsert_trace_id: z.string(),
  url_less_source_key: z.string(),
  url_less_count: z.number(),
  exclusion_company: z.string(),
  exclusion_excluded_at: z.string(),
  exclusion_source_key: z.string(),
  migration_count: z.number(),
  migration_completed_at: z.string(),
  notion_source_rows: z.number(),
  notion_urls: z.number(),
  notion_company_title_pairs: z.number(),
  notion_url_less_rows: z.number(),
  notion_exclusions: z.number(),
});

const proofSql = `
INSERT INTO processed_jobs (
  source_key, raw_url, company, normalized_company, title, normalized_title,
  outcome, first_processed_at, last_processed_at, trace_id
) VALUES (
  'url:https://jobs.example.com/role?id=1',
  'https://jobs.example.com/role?id=1',
  'ACME Labs',
  'acme labs',
  'Engineer',
  'engineer',
  'rejected',
  '2026-08-22T10:00:00.000Z',
  '2026-08-22T10:00:00.000Z',
  NULL
);

INSERT INTO processed_jobs (
  source_key, raw_url, company, normalized_company, title, normalized_title,
  outcome, first_processed_at, last_processed_at, trace_id
) VALUES (
  'url:https://jobs.example.com/role?id=1',
  'https://jobs.example.com/role?id=1',
  'Acme Labs',
  'acme labs',
  'Senior Engineer',
  'senior engineer',
  'inserted',
  '2026-08-22T11:00:00.000Z',
  '2026-08-22T11:00:00.000Z',
  'trace-2'
) ON CONFLICT(source_key) DO UPDATE SET
  raw_url = excluded.raw_url,
  company = excluded.company,
  normalized_company = excluded.normalized_company,
  title = excluded.title,
  normalized_title = excluded.normalized_title,
  outcome = excluded.outcome,
  last_processed_at = excluded.last_processed_at,
  trace_id = excluded.trace_id;

INSERT INTO processed_jobs VALUES (
  'url:https://jobs.example.com/2',
  'https://jobs.example.com/2',
  'Acme Labs',
  'acme labs',
  'Product Engineer',
  'product engineer',
  'rejected',
  '2026-08-22T10:00:00.000Z',
  '2026-08-22T10:00:00.000Z',
  NULL
);

INSERT INTO processed_jobs VALUES (
  'url:https://jobs.example.com/duplicate',
  'https://jobs.example.com/duplicate',
  'Acme Labs',
  'acme labs',
  'Duplicate Engineer',
  'duplicate engineer',
  'duplicated',
  '2026-08-22T10:00:00.000Z',
  '2026-08-22T10:00:00.000Z',
  NULL
);

INSERT INTO processed_jobs VALUES (
  'notion:page-123',
  NULL,
  'Acme',
  'acme',
  'Legacy Engineer',
  'legacy engineer',
  'historical',
  '2026-08-01T10:00:00.000Z',
  '2026-08-01T10:00:00.000Z',
  NULL
);

INSERT INTO processed_jobs VALUES (
  'notion:page-456',
  'https://jobs.example.com/notion',
  'ACME',
  'acme',
  'Legacy Engineer',
  'legacy engineer',
  'historical',
  '2026-08-01T11:00:00.000Z',
  '2026-08-01T11:00:00.000Z',
  NULL
);

INSERT INTO company_exclusions (
  normalized_company, company, excluded_at, source_key
) VALUES (
  'acme labs',
  '  Acme   Labs ',
  '2026-08-22T10:00:00.000Z',
  NULL
);

INSERT INTO company_exclusions (
  normalized_company, company, excluded_at, source_key
) VALUES (
  'acme labs',
  'acme labs',
  '2026-08-22T11:00:00.000Z',
  'notion:block-1'
) ON CONFLICT(normalized_company) DO UPDATE SET
  source_key = COALESCE(excluded.source_key, company_exclusions.source_key);

INSERT INTO job_ledger_migrations (name, completed_at)
VALUES ('notion-job-ledger-backfill-v1', '2026-08-22T12:00:00.000Z')
ON CONFLICT(name) DO UPDATE SET completed_at = excluded.completed_at;

INSERT INTO job_ledger_migrations (name, completed_at)
VALUES ('notion-job-ledger-backfill-v1', '2026-08-22T13:00:00.000Z')
ON CONFLICT(name) DO UPDATE SET completed_at = excluded.completed_at;

SELECT
  (SELECT source_key FROM processed_jobs
    WHERE raw_url = 'https://jobs.example.com/role?id=1') AS exact_source_key,
  (SELECT COUNT(*) FROM processed_jobs
    WHERE raw_url = 'https://jobs.example.com/role?id=01') AS exact_miss_count,
  (SELECT GROUP_CONCAT(title, '|') FROM (
    SELECT DISTINCT title, normalized_title
    FROM processed_jobs
    WHERE normalized_company = 'acme labs' AND outcome <> 'duplicated'
    ORDER BY normalized_title, title
  )) AS normalized_titles,
  (SELECT first_processed_at FROM processed_jobs
    WHERE source_key = 'url:https://jobs.example.com/role?id=1') AS upsert_first_processed_at,
  (SELECT last_processed_at FROM processed_jobs
    WHERE source_key = 'url:https://jobs.example.com/role?id=1') AS upsert_last_processed_at,
  (SELECT outcome FROM processed_jobs
    WHERE source_key = 'url:https://jobs.example.com/role?id=1') AS upsert_outcome,
  (SELECT trace_id FROM processed_jobs
    WHERE source_key = 'url:https://jobs.example.com/role?id=1') AS upsert_trace_id,
  (SELECT source_key FROM processed_jobs
    WHERE source_key = 'notion:page-123') AS url_less_source_key,
  (SELECT COUNT(*) FROM processed_jobs
    WHERE source_key = 'notion:page-123' AND raw_url IS NULL) AS url_less_count,
  (SELECT company FROM company_exclusions
    WHERE normalized_company = 'acme labs') AS exclusion_company,
  (SELECT excluded_at FROM company_exclusions
    WHERE normalized_company = 'acme labs') AS exclusion_excluded_at,
  (SELECT source_key FROM company_exclusions
    WHERE normalized_company = 'acme labs') AS exclusion_source_key,
  (SELECT COUNT(*) FROM job_ledger_migrations
    WHERE name = 'notion-job-ledger-backfill-v1') AS migration_count,
  (SELECT completed_at FROM job_ledger_migrations
    WHERE name = 'notion-job-ledger-backfill-v1') AS migration_completed_at,
  (SELECT COUNT(*) FROM processed_jobs
    WHERE source_key LIKE 'notion:%') AS notion_source_rows,
  (SELECT COUNT(DISTINCT raw_url) FROM processed_jobs
    WHERE source_key LIKE 'notion:%' AND raw_url IS NOT NULL) AS notion_urls,
  (SELECT COUNT(*) FROM (
    SELECT normalized_company, normalized_title
    FROM processed_jobs
    WHERE source_key LIKE 'notion:%'
    GROUP BY normalized_company, normalized_title
  )) AS notion_company_title_pairs,
  (SELECT COUNT(*) FROM processed_jobs
    WHERE source_key LIKE 'notion:%' AND raw_url IS NULL) AS notion_url_less_rows,
  (SELECT COUNT(*) FROM company_exclusions
    WHERE source_key LIKE 'notion:%') AS notion_exclusions;
`;

test("preserves job ledger behavior in local D1", async () => {
  const temporaryDirectory = await mkdtemp(join(tmpdir(), "job-ledger-d1-"));
  const configPath = join(temporaryDirectory, "wrangler.jsonc");
  const proofPath = join(temporaryDirectory, "proof.sql");
  const persistencePath = join(temporaryDirectory, "state");

  try {
    await Bun.write(
      configPath,
      JSON.stringify({
        name: "job-ledger-local-proof",
        compatibility_date: "2026-08-24",
        d1_databases: [
          {
            binding: "JOB_LEDGER",
            database_name: "job-ledger-proof",
            database_id: "00000000-0000-0000-0000-000000000000",
            migrations_dir: migrationDirectory,
          },
        ],
      }),
    );
    await Bun.write(proofPath, proofSql);

    await runWrangler([
      "d1",
      "migrations",
      "apply",
      "job-ledger-proof",
      "--config",
      configPath,
      "--local",
      "--persist-to",
      persistencePath,
    ]);
    await runWrangler([
      "d1",
      "migrations",
      "apply",
      "job-ledger-proof",
      "--config",
      configPath,
      "--local",
      "--persist-to",
      persistencePath,
    ]);

    const output = await runWrangler([
      "d1",
      "execute",
      "job-ledger-proof",
      "--config",
      configPath,
      "--local",
      "--persist-to",
      persistencePath,
      "--file",
      proofPath,
      "--json",
    ]);
    const batches = parseWranglerResults(output);
    const finalBatch = batches.at(-1);
    const finalRow = finalBatch?.results.at(0);
    const result = JobLedgerProofSchema.parse(finalRow);

    expect(result).toEqual({
      exact_source_key: "url:https://jobs.example.com/role?id=1",
      exact_miss_count: 0,
      normalized_titles: "Product Engineer|Senior Engineer",
      upsert_first_processed_at: "2026-08-22T10:00:00.000Z",
      upsert_last_processed_at: "2026-08-22T11:00:00.000Z",
      upsert_outcome: "inserted",
      upsert_trace_id: "trace-2",
      url_less_source_key: "notion:page-123",
      url_less_count: 1,
      exclusion_company: "  Acme   Labs ",
      exclusion_excluded_at: "2026-08-22T10:00:00.000Z",
      exclusion_source_key: "notion:block-1",
      migration_count: 1,
      migration_completed_at: "2026-08-22T13:00:00.000Z",
      notion_source_rows: 2,
      notion_urls: 1,
      notion_company_title_pairs: 1,
      notion_url_less_rows: 1,
      notion_exclusions: 1,
    });
  } finally {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
}, 15_000);

async function runWrangler(arguments_: string[]): Promise<string> {
  const command = Bun.spawn([process.execPath, wranglerPath, ...arguments_], {
    cwd: projectRoot,
    env: { ...process.env, CI: "true" },
    stdout: "pipe",
    stderr: "pipe",
  });
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(command.stdout).text(),
    new Response(command.stderr).text(),
    command.exited,
  ]);

  if (exitCode !== 0) {
    throw new Error(`Wrangler failed with exit code ${exitCode}: ${stderr || stdout}`);
  }

  return stdout;
}

function parseWranglerResults(output: string): z.infer<typeof WranglerResultSchema> {
  const parsed: unknown = JSON.parse(output);
  return WranglerResultSchema.parse(parsed);
}
