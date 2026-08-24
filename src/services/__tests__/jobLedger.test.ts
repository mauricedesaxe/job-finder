import { Database } from "bun:sqlite";
import { afterEach, describe, expect, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createJobLedger, type JobLedger } from "../jobLedger.ts";

interface SchemaRow {
  type: string;
  name: string;
  tableName: string;
  sql: string | null;
}

const schemaQuery = `
  SELECT type, name, tbl_name AS tableName, sql
  FROM sqlite_schema
  WHERE name NOT LIKE 'sqlite_%'
  ORDER BY type, name
`;

describe("job ledger", () => {
  let ledger: JobLedger | undefined;

  afterEach(async () => {
    await ledger?.close();
    ledger = undefined;
  });

  test("finds a processed job by its exact raw URL", async () => {
    ledger = createJobLedger(":memory:");
    await ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/role?id=1",
      company: "Acme",
      title: "Senior Engineer",
      outcome: "inserted",
      processedAt: "2026-08-22T10:00:00.000Z",
      traceId: "trace-1",
    });

    expect(await ledger.findByRawUrl("https://jobs.example.com/role?id=1")).toEqual({
      sourceKey: "url:https://jobs.example.com/role?id=1",
      rawUrl: "https://jobs.example.com/role?id=1",
      company: "Acme",
      title: "Senior Engineer",
      outcome: "inserted",
      firstProcessedAt: "2026-08-22T10:00:00.000Z",
      lastProcessedAt: "2026-08-22T10:00:00.000Z",
      traceId: "trace-1",
    });
    expect(await ledger.findByRawUrl("https://jobs.example.com/role?id=01")).toBeNull();
  });

  test("returns titles by normalized company", async () => {
    ledger = createJobLedger(":memory:");
    await ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/1",
      company: "  ACME   Labs ",
      title: "Senior Engineer",
      outcome: "inserted",
    });
    await ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/2",
      company: "Acme Labs",
      title: "Product Engineer",
      outcome: "rejected",
    });

    expect(await ledger.titlesForCompany("acme labs")).toEqual([
      "Product Engineer",
      "Senior Engineer",
    ]);
  });

  test("excludes duplicate outcomes from title candidates", async () => {
    ledger = createJobLedger(":memory:");
    await ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/duplicate",
      company: "Acme",
      title: "Duplicate Engineer",
      outcome: "duplicated",
    });

    expect(await ledger.titlesForCompany("Acme")).toEqual([]);
  });

  test("updates an existing source key instead of creating another record", async () => {
    ledger = createJobLedger(":memory:");
    await ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/1",
      company: "Acme",
      title: "Engineer",
      outcome: "rejected",
      processedAt: "2026-08-22T10:00:00.000Z",
    });
    await ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/1",
      company: "Acme",
      title: "Senior Engineer",
      outcome: "inserted",
      processedAt: "2026-08-22T11:00:00.000Z",
      traceId: "trace-2",
    });

    expect(await ledger.findByRawUrl("https://jobs.example.com/1")).toMatchObject({
      title: "Senior Engineer",
      outcome: "inserted",
      firstProcessedAt: "2026-08-22T10:00:00.000Z",
      lastProcessedAt: "2026-08-22T11:00:00.000Z",
      traceId: "trace-2",
    });
    expect(await ledger.titlesForCompany("Acme")).toEqual(["Senior Engineer"]);
  });

  test("keeps URL-less records under their source key", async () => {
    ledger = createJobLedger(":memory:");
    await ledger.recordProcessedJob({
      sourceKey: "notion:page-123",
      company: "Acme",
      title: "Legacy Engineer",
      outcome: "archived",
    });

    expect(await ledger.findByRawUrl("https://jobs.example.com/missing")).toBeNull();
    expect(await ledger.titlesForCompany("Acme")).toEqual(["Legacy Engineer"]);
  });

  test("records company exclusions once and finds them by normalized company", async () => {
    ledger = createJobLedger(":memory:");
    await ledger.excludeCompany({
      company: "  Acme   Labs ",
      excludedAt: "2026-08-22T10:00:00.000Z",
    });
    await ledger.excludeCompany({
      company: "acme labs",
      excludedAt: "2026-08-22T11:00:00.000Z",
    });

    expect(await ledger.findCompanyExclusion("ACME LABS")).toEqual({
      company: "  Acme   Labs ",
      excludedAt: "2026-08-22T10:00:00.000Z",
    });
  });

  test("keeps the SQLite schema aligned with the D1 migration", async () => {
    const directory = await mkdtemp(join(tmpdir(), "job-ledger-schema-"));
    const runtimePath = join(directory, "runtime.sqlite");
    const migrationDatabase = new Database(":memory:");

    ledger = createJobLedger(runtimePath);
    await ledger.close();
    ledger = undefined;

    const reopenedRuntimeDatabase = new Database(runtimePath, { readonly: true });
    try {
      const migrationPath = resolve(import.meta.dir, "../../../migrations/0001_job_ledger.sql");
      migrationDatabase.exec(await Bun.file(migrationPath).text());

      expect(readSchema(migrationDatabase)).toEqual(readSchema(reopenedRuntimeDatabase));
    } finally {
      reopenedRuntimeDatabase.close();
      migrationDatabase.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});

function readSchema(database: Database): SchemaRow[] {
  return database
    .query<SchemaRow, []>(schemaQuery)
    .all()
    .map((row) => ({
      ...row,
      sql: row.sql?.replace(/\s+/g, " ").trim() ?? null,
    }));
}
