import { expect, test } from "bun:test";
import { z } from "zod/v4";
import {
  createRunCoordinator,
  handleWorkerRequest,
  type WorkerRequestEnvironment,
} from "../cloudflareRun";
import { createContainerRequestHandler } from "../container";

const runOne = { runId: "run-1", mode: { kind: "scrape" as const } };
const runTwo = { runId: "run-2", mode: { kind: "reconcile" as const } };

test("coalesces an active run and serializes distinct runs", async () => {
  const firstGate = Promise.withResolvers<void>();
  const secondGate = Promise.withResolvers<void>();
  const started: string[] = [];
  const coordinator = createRunCoordinator(async ({ runId }) => {
    started.push(runId);
    await (runId === runOne.runId ? firstGate.promise : secondGate.promise);
  });

  const first = coordinator.run(runOne);
  const duplicate = coordinator.run(runOne);
  const second = coordinator.run(runTwo);
  expect(duplicate).toBe(first);
  await Bun.sleep(0);
  expect(started).toEqual(["run-1"]);

  firstGate.resolve();
  await first;
  await Promise.resolve();
  expect(started).toEqual(["run-1", "run-2"]);
  secondGate.resolve();
  await second;
});

test("rejects malformed Container run requests", async () => {
  const handler = createContainerRequestHandler({
    coordinator: createRunCoordinator(async () => undefined),
  });
  const response = await handler(
    new Request("http://container/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ runId: "bad id", mode: { kind: "scrape" } }),
    }),
  );

  expect(response.status).toBe(400);
});

test("protects and parses manual run requests", async () => {
  const created: unknown[] = [];
  const environment: WorkerRequestEnvironment = {
    manualTriggerSecret: "secret",
    workflow: {
      async create(options) {
        created.push(options);
        return { id: options.id };
      },
      async status() {
        return { status: "running" };
      },
    },
  };

  const unauthorized = await handleWorkerRequest(
    new Request("https://worker.example/runs", { method: "POST", body: "{}" }),
    environment,
  );
  expect(unauthorized.status).toBe(401);

  const malformed = await handleWorkerRequest(
    new Request("https://worker.example/runs", {
      method: "POST",
      headers: { authorization: "Bearer secret", "content-type": "application/json" },
      body: JSON.stringify({ mode: { kind: "invalid" } }),
    }),
    environment,
  );
  expect(malformed.status).toBe(400);

  const accepted = await handleWorkerRequest(
    new Request("https://worker.example/runs", {
      method: "POST",
      headers: { authorization: "Bearer secret", "content-type": "application/json" },
      body: JSON.stringify({ runId: "manual-1", mode: { kind: "reconcile" } }),
    }),
    environment,
  );
  expect(accepted.status).toBe(202);
  expect(created).toEqual([{ id: "manual-1", params: { mode: { kind: "reconcile" } } }]);
});

test("returns parsed Workflow status", async () => {
  const environment: WorkerRequestEnvironment = {
    manualTriggerSecret: "secret",
    workflow: {
      async create(options) {
        return { id: options.id };
      },
      async status(id) {
        return { status: "complete", output: { runId: id } };
      },
    },
  };
  const response = await handleWorkerRequest(
    new Request("https://worker.example/runs/manual-1", {
      headers: { authorization: "Bearer secret" },
    }),
    environment,
  );
  expect(response.status).toBe(200);
  const body: unknown = await response.json();
  expect(z.record(z.string(), z.unknown()).parse(body)).toEqual({
    runId: "manual-1",
    status: "complete",
    output: { runId: "manual-1" },
  });
});
