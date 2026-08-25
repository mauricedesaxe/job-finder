import { afterAll, beforeAll, expect, test } from "bun:test";
import { resolve } from "node:path";
import { createTestHarness, type TestHarness, type WorkerHandle } from "wrangler";
import { z } from "zod/v4";
import { JOB_STATUSES } from "../../types";
import { createD1JobLedger } from "../d1JobLedger";
import { PROCESSED_JOB_OUTCOMES } from "../jobLedger";
import { JOB_LEDGER_CONFORMANCE_RESULT } from "./jobLedgerConformance";

const projectRoot = resolve(import.meta.dir, "../../..");
const configPath = resolve(import.meta.dir, "fixtures/d1-job-ledger.wrangler.jsonc");

const PendingProjectionSchema = z.object({
  sourceKey: z.string(),
  job: z.object({
    title: z.string(),
    company: z.string(),
    url: z.string(),
    source: z.string(),
    keywordsMatched: z.array(z.string()),
    datePosted: z.string().nullable(),
    dateScraped: z.string(),
    description: z.string(),
    location: z.string(),
    profile: z.string(),
  }),
  status: z.enum(JOB_STATUSES),
  createdAt: z.string(),
});

const PendingReviewProjectionSchema = z.object({
  sourceKey: z.string(),
  traceId: z.string(),
  createdAt: z.string(),
});

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
  pendingBeforeComplete: z.array(PendingProjectionSchema),
  pendingAfterOrdinaryUpsert: z.array(PendingProjectionSchema),
  pendingAfterComplete: z.array(PendingProjectionSchema),
  duplicatePending: z.array(PendingProjectionSchema),
  pendingReviewBeforeComplete: z.array(PendingReviewProjectionSchema),
  pendingReviewAfterComplete: z.array(PendingReviewProjectionSchema),
  projectionKinds: z.object({
    none: z.literal("none"),
    notion: z.literal("notion"),
    notionAndReview: z.literal("notion-and-review"),
  }),
});

const ScenarioResponseSchema = z.object({
  result: ConformanceResultSchema,
  migration: z.object({
    count: z.number(),
    completed_at: z.string(),
  }),
});

const MalformedResponseSchema = z.object({ rejected: z.boolean() });
const CountRowSchema = z.object({ count: z.number() });
const AtomicResponseSchema = z.object({
  rejected: z.literal(true),
  counts: z.tuple([CountRowSchema, CountRowSchema, CountRowSchema]),
});
const AcquiredLockSchema = z.object({
  kind: z.literal("acquired"),
  workflowInstanceId: z.string(),
  acquiredAt: z.string(),
});
const ContendedLockSchema = z.object({
  kind: z.literal("contended"),
  workflowInstanceId: z.string(),
  acquiredAt: z.string(),
});
const RunLockResponseSchema = z.object({
  acquired: AcquiredLockSchema,
  contended: ContendedLockSchema,
  wrongRelease: z.literal(false),
  released: z.literal(true),
  reacquired: AcquiredLockSchema,
});
const ConcurrentRunLockResponseSchema = z.object({
  results: z
    .array(z.discriminatedUnion("kind", [AcquiredLockSchema, ContendedLockSchema]))
    .length(2),
});
const SameOwnerRunLockResponseSchema = z.object({
  first: AcquiredLockSchema,
  second: AcquiredLockSchema,
});

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

test("acquires, contends, and releases the singleton D1 run lock", async () => {
  const response = await worker.fetch("/run-lock");
  expect(response.status).toBe(200);
  const body: unknown = await response.json();
  expect(RunLockResponseSchema.parse(body)).toEqual({
    acquired: {
      kind: "acquired",
      workflowInstanceId: "run-1",
      acquiredAt: "2026-08-24T22:00:00.000Z",
    },
    contended: {
      kind: "contended",
      workflowInstanceId: "run-1",
      acquiredAt: "2026-08-24T22:00:00.000Z",
    },
    wrongRelease: false,
    released: true,
    reacquired: {
      kind: "acquired",
      workflowInstanceId: "run-2",
      acquiredAt: "2026-08-24T22:02:00.000Z",
    },
  });
}, 15_000);

test("returns one durable owner for concurrent D1 run-lock acquisition", async () => {
  const response = await worker.fetch("/run-lock-concurrent");
  expect(response.status).toBe(200);
  const body: unknown = await response.json();
  const { results } = ConcurrentRunLockResponseSchema.parse(body);
  const acquired = results.filter((result) => result.kind === "acquired");
  const contended = results.filter((result) => result.kind === "contended");
  expect(acquired).toHaveLength(1);
  expect(contended).toHaveLength(1);
  expect(contended[0]?.workflowInstanceId).toBe(acquired[0]?.workflowInstanceId);
  expect(contended[0]?.acquiredAt).toBe(acquired[0]?.acquiredAt);
}, 15_000);

test("reacquires the D1 run lock idempotently for the same owner", async () => {
  const response = await worker.fetch("/run-lock-same-owner");
  expect(response.status).toBe(200);
  const body: unknown = await response.json();
  expect(SameOwnerRunLockResponseSchema.parse(body)).toEqual({
    first: {
      kind: "acquired",
      workflowInstanceId: "same-owner",
      acquiredAt: "2026-08-24T22:20:00.000Z",
    },
    second: {
      kind: "acquired",
      workflowInstanceId: "same-owner",
      acquiredAt: "2026-08-24T22:20:00.000Z",
    },
  });
}, 15_000);

for (const failedProjection of ["notion", "review"] as const) {
  test(`rolls back all D1 writes when the ${failedProjection} outbox insert fails`, async () => {
    const response = await worker.fetch(`/atomic-projection/${failedProjection}`);
    expect(response.status).toBe(200);
    const body: unknown = await response.json();
    expect(AtomicResponseSchema.parse(body)).toEqual({
      rejected: true,
      counts: [{ count: 0 }, { count: 0 }, { count: 0 }],
    });
  }, 15_000);
}

test("rejects malformed stored outcomes at the D1 boundary", async () => {
  const response = await worker.fetch("/malformed-outcome");
  expect(response.status).toBe(200);
  const body: unknown = await response.json();
  expect(MalformedResponseSchema.parse(body)).toEqual({ rejected: true });
}, 15_000);

test("rejects malformed stored projections at the D1 boundary", async () => {
  const response = await worker.fetch("/malformed-projection");
  expect(response.status).toBe(200);
  const body: unknown = await response.json();
  expect(MalformedResponseSchema.parse(body)).toEqual({ rejected: true });
}, 15_000);

test("rejects D1 writes that do not report success", async () => {
  const ledger = createD1JobLedger({
    async batch() {
      return [{ success: false }, { success: true }];
    },
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
