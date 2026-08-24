import { afterAll, beforeAll, expect, test } from "bun:test";
import { resolve } from "node:path";
import { createTestHarness, type TestHarness, type WorkerHandle } from "wrangler";
import { z } from "zod/v4";
import { createD1JobLedger } from "../d1JobLedger";
import { PROCESSED_JOB_OUTCOMES } from "../jobLedger";
import { JOB_LEDGER_CONFORMANCE_RESULT } from "./jobLedgerConformance";

const projectRoot = resolve(import.meta.dir, "../../..");
const configPath = resolve(import.meta.dir, "fixtures/d1-job-ledger.wrangler.jsonc");

const ProcessedJobSchema = z.object({
  sourceKey: z.string(),
  rawUrl: z.string().nullable(),
  company: z.string(),
  title: z.string(),
  outcome: z.enum(PROCESSED_JOB_OUTCOMES),
  firstProcessedAt: z.string(),
  lastProcessedAt: z.string(),
  traceId: z.string().nullable(),
});

const ConformanceResultSchema = z.object({
  exactJob: ProcessedJobSchema.nullable(),
  exactMiss: ProcessedJobSchema.nullable(),
  clearedRawUrl: z.boolean(),
  updatedJob: ProcessedJobSchema.nullable(),
  titles: z.array(z.string()),
  urlLessTitles: z.array(z.string()),
  exclusion: z.object({ company: z.string(), excludedAt: z.string() }).nullable(),
  notionStats: z.object({
    sourceRows: z.number(),
    urls: z.number(),
    companyTitlePairs: z.number(),
    urlLessRows: z.number(),
    exclusions: z.number(),
  }),
  hasMigration: z.boolean(),
  missingSourceKeyError: z.string(),
});

const ScenarioResponseSchema = z.object({
  result: ConformanceResultSchema,
  migration: z.object({
    count: z.number(),
    completed_at: z.string(),
  }),
});

const MalformedResponseSchema = z.object({ rejected: z.boolean() });

let harness: TestHarness;
let worker: WorkerHandle;

beforeAll(async () => {
  harness = createTestHarness({
    root: projectRoot,
    workers: [{ configPath }],
  });
  await harness.listen();
  worker = harness.getWorker();
  await worker.applyD1Migrations("JOB_LEDGER");
  await worker.applyD1Migrations("JOB_LEDGER");
});

afterAll(async () => {
  await harness.close();
});

test("runs the job ledger adapter in workerd", async () => {
  const response = await worker.fetch("/scenario");
  expect(response.status).toBe(200);
  const body: unknown = await response.json();
  const result = ScenarioResponseSchema.parse(body);

  expect(JOB_LEDGER_CONFORMANCE_RESULT).toEqual(result.result);
  expect(result.migration).toEqual({
    count: 1,
    completed_at: "2026-08-22T13:00:00.000Z",
  });
}, 15_000);

test("rejects malformed stored outcomes at the D1 boundary", async () => {
  const response = await worker.fetch("/malformed-outcome");
  expect(response.status).toBe(200);
  const body: unknown = await response.json();
  expect(MalformedResponseSchema.parse(body)).toEqual({ rejected: true });
}, 15_000);

test("rejects D1 writes that do not report success", async () => {
  const ledger = createD1JobLedger({
    prepare() {
      return {
        bind() {
          return this;
        },
        async first() {
          return null;
        },
        async all() {
          return { success: true, results: [] };
        },
        async run() {
          return { success: false };
        },
      };
    },
  });

  await expect(
    ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/write-failure",
      company: "Acme",
      title: "Engineer",
      outcome: "inserted",
    }),
  ).rejects.toBeInstanceOf(z.ZodError);
});
