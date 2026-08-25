import { afterAll, beforeAll, expect, test } from "bun:test";
import { resolve } from "node:path";
import { createTestHarness, type TestHarness, type WorkerHandle } from "wrangler";
import { z } from "zod/v4";
import { sleep } from "../concurrency/sleep";

interface RuntimeProofEnvironment {
  JOB_FINDER_WORKFLOW: Workflow<{ mode: { kind: "scrape" | "reconcile" } }>;
  PRODUCTION_WORKFLOW: Workflow<{ mode: { kind: "scrape" | "reconcile" } }>;
  RUNTIME_PROOF_WORKFLOW: Workflow<{ runId: string }>;
}

const RuntimeProofResultSchema = z.object({
  runId: z.string(),
  one: z.literal(1),
});

let harness: TestHarness;
let worker: WorkerHandle<RuntimeProofEnvironment>;

beforeAll(async () => {
  harness = createTestHarness({
    root: resolve(import.meta.dir, "../.."),
    workers: [
      { configPath: resolve(import.meta.dir, "fixtures/cloudflare-workflow.wrangler.jsonc") },
    ],
  });
  await harness.listen();
  worker = harness.getWorker<RuntimeProofEnvironment>();
});

afterAll(async () => {
  await harness.close();
});

test("loads the production Worker import graph for health", async () => {
  const response = await worker.fetch("/health");
  expect(response.status).toBe(200);
  const body: unknown = await response.json();
  expect(z.object({ status: z.literal("ok") }).parse(body)).toEqual({ status: "ok" });
}, 15_000);

test("runs a real Workflow step in workerd", async () => {
  const runId = "runtime-proof-1";
  const environment = await worker.getEnv();
  await environment.RUNTIME_PROOF_WORKFLOW.create({ id: runId, params: { runId } });
  const instance = await environment.RUNTIME_PROOF_WORKFLOW.get(runId);
  expect(instance.id).toBe(runId);

  const status = await waitForTerminalStatus(instance);
  expect(status.status).toBe("complete");
  expect(RuntimeProofResultSchema.parse(status.output)).toEqual({ runId, one: 1 });
}, 15_000);

test("reuses a real Workflow instance for a repeated manual run ID", async () => {
  const request = () =>
    worker.fetch("/runs", {
      method: "POST",
      headers: { authorization: "Bearer test-secret", "content-type": "application/json" },
      body: JSON.stringify({ runId: "manual-retry", mode: { kind: "reconcile" } }),
    });

  const first = await request();
  const retry = await request();
  expect(first.status).toBe(202);
  expect(retry.status).toBe(202);
  expect(await first.json()).toEqual({ runId: "manual-retry" });
  expect(await retry.json()).toEqual({ runId: "manual-retry" });
}, 15_000);

test("executes the production Workflow failure path in workerd", async () => {
  const runId = "production-failure-proof";
  const environment = await worker.getEnv();
  await environment.PRODUCTION_WORKFLOW.create({
    id: runId,
    params: { mode: { kind: "reconcile" } },
  });

  const instance = await environment.PRODUCTION_WORKFLOW.get(runId);
  const status = await waitForTerminalStatus(instance);
  expect(status.status).toBe("errored");
  expect(status.error?.message).toContain('"notionDatabaseId"');
}, 15_000);

async function waitForTerminalStatus(instance: WorkflowInstance): Promise<InstanceStatus> {
  for (let attempt = 0; attempt < 100; attempt++) {
    const status = await instance.status();
    if (["complete", "errored", "terminated"].includes(status.status)) return status;
    await sleep(25);
  }
  throw new Error("Workflow runtime proof did not finish");
}
