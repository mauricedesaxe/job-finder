import { describe, expect, test } from "bun:test";
import { z } from "zod/v4";
import {
  handleWorkerRequest,
  type LockedJobDependencies,
  runLockedJob,
  type WorkerRequestEnvironment,
  WorkflowPayloadSchema,
} from "../cloudflareRun";
import type { JobFinderRunLock, RunLockAcquisition } from "../services/runLock";

const authHeaders = { authorization: "Bearer secret", "content-type": "application/json" };

describe("Worker run routes", () => {
  test("serves health without run authorization", async () => {
    const response = await handleWorkerRequest(
      new Request("https://worker.example/health"),
      requestEnvironment(),
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
  });

  test("returns not found for unknown and unsupported routes", async () => {
    const environment = requestEnvironment();
    const unknown = await handleWorkerRequest(
      new Request("https://worker.example/unknown", { headers: authHeaders }),
      environment,
    );
    const unsupportedMethod = await handleWorkerRequest(
      new Request("https://worker.example/runs/manual-1", {
        method: "DELETE",
        headers: authHeaders,
      }),
      environment,
    );
    const malformedRunId = await handleWorkerRequest(
      new Request("https://worker.example/runs/%25", { headers: authHeaders }),
      environment,
    );

    expect(unknown.status).toBe(404);
    expect(unsupportedMethod.status).toBe(404);
    expect(malformedRunId.status).toBe(404);
  });

  test("protects and parses run creation", async () => {
    const created: unknown[] = [];
    const environment = requestEnvironment({
      async startOrReuse(options) {
        created.push(options);
      },
    });

    const unauthorized = await handleWorkerRequest(
      new Request("https://worker.example/runs", { method: "POST", body: "{}" }),
      environment,
    );
    expect(unauthorized.status).toBe(401);

    const malformed = await handleWorkerRequest(
      new Request("https://worker.example/runs", {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ mode: { kind: "invalid" } }),
      }),
      environment,
    );
    expect(malformed.status).toBe(400);

    const accepted = await handleWorkerRequest(
      new Request("https://worker.example/runs", {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ runId: "manual-1", mode: { kind: "reconcile" } }),
      }),
      environment,
    );
    expect(accepted.status).toBe(202);
    expect(created).toEqual([{ id: "manual-1", params: { mode: { kind: "reconcile" } } }]);

    const backfill = await handleWorkerRequest(
      new Request("https://worker.example/runs", {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ runId: "backfill-1", mode: { kind: "backfill" } }),
      }),
      environment,
    );
    expect(backfill.status).toBe(202);
    expect(created.at(-1)).toEqual({ id: "backfill-1", params: { mode: { kind: "backfill" } } });
  });

  test("returns the requested ID when createBatch finds an existing Workflow", async () => {
    const existing = new Set<string>();
    let creations = 0;
    const environment = requestEnvironment({
      async startOrReuse(options) {
        if (!existing.has(options.id)) {
          existing.add(options.id);
          creations++;
        }
      },
    });
    const request = () =>
      new Request("https://worker.example/runs", {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ runId: "manual-retry", mode: { kind: "scrape" } }),
      });

    const first = await handleWorkerRequest(request(), environment);
    const retry = await handleWorkerRequest(request(), environment);

    expect(first.status).toBe(202);
    expect(retry.status).toBe(202);
    const firstBody: unknown = await first.json();
    const retryBody: unknown = await retry.json();
    const responseSchema = z.object({ runId: z.literal("manual-retry") });
    expect(responseSchema.parse(firstBody)).toEqual({ runId: "manual-retry" });
    expect(responseSchema.parse(retryBody)).toEqual({ runId: "manual-retry" });
    expect(creations).toBe(1);
  });

  test("returns a validated Workflow status", async () => {
    const response = await handleWorkerRequest(
      new Request("https://worker.example/runs/manual-1", { headers: authHeaders }),
      requestEnvironment({
        async status() {
          return { status: "complete", output: { ok: true } };
        },
      }),
    );

    expect(response.status).toBe(200);
    expect(z.record(z.string(), z.unknown()).parse(await response.json())).toEqual({
      runId: "manual-1",
      status: "complete",
      output: { ok: true },
    });
  });

  test("rejects malformed Workflow status responses", async () => {
    await expect(
      handleWorkerRequest(
        new Request("https://worker.example/runs/manual-1", { headers: authHeaders }),
        requestEnvironment({
          async status() {
            return { status: "not-real" };
          },
        }),
      ),
    ).rejects.toBeInstanceOf(z.ZodError);
  });

  test("unlocks only a terminal named Workflow", async () => {
    const released: string[] = [];
    const running = await handleWorkerRequest(
      new Request("https://worker.example/runs/manual-1/unlock", {
        method: "POST",
        headers: authHeaders,
      }),
      requestEnvironment({
        async status() {
          return { status: "running" };
        },
        async release(id) {
          released.push(id);
          return true;
        },
      }),
    );
    expect(running.status).toBe(409);
    expect(released).toEqual([]);

    const terminal = await handleWorkerRequest(
      new Request("https://worker.example/runs/manual-1/unlock", {
        method: "POST",
        headers: authHeaders,
      }),
      requestEnvironment({
        async status() {
          return { status: "errored" };
        },
        async release(id) {
          released.push(id);
          return true;
        },
      }),
    );
    expect(terminal.status).toBe(200);
    expect(released).toEqual(["manual-1"]);
  });

  test("keeps a terminal Workflow locked when it does not own the lock", async () => {
    const response = await handleWorkerRequest(
      new Request("https://worker.example/runs/manual-1/unlock", {
        method: "POST",
        headers: authHeaders,
      }),
      requestEnvironment({
        async status() {
          return { status: "complete" };
        },
        async release() {
          return false;
        },
      }),
    );

    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "run lock is not owned by this workflow" });
  });
});

test("rejects malformed Workflow payloads", () => {
  expect(() => WorkflowPayloadSchema.parse({ mode: { kind: "invalid" } })).toThrow();
});

describe("Workflow cleanup", () => {
  test("flushes and shuts down before a successful lock release", async () => {
    const events: string[] = [];
    await runLockedJob("run-1", lockedJobDependencies(events));
    expect(events).toEqual(["acquire", "execute", "flush", "shutdown", "release"]);
  });

  test("retains the lock and cleans up after a failed run", async () => {
    const events: string[] = [];
    const dependencies = lockedJobDependencies(events);
    dependencies.execute = async () => {
      events.push("execute");
      throw new Error("run failed");
    };

    await expect(runLockedJob("run-1", dependencies)).rejects.toThrow("run failed");
    expect(events).toEqual(["acquire", "execute", "flush", "shutdown", "report"]);
  });

  test("preserves the run error when failure cleanup also fails", async () => {
    const events: string[] = [];
    const dependencies = lockedJobDependencies(events);
    dependencies.execute = async () => {
      throw new Error("run failed");
    };
    dependencies.flushLangSmith = async () => {
      throw new Error("flush failed");
    };

    await expect(runLockedJob("run-1", dependencies)).rejects.toThrow("run failed");
    expect(events).toEqual(["acquire", "shutdown", "cleanup-error", "report"]);
  });
});

function requestEnvironment({
  startOrReuse = async () => {},
  status = async () => ({ status: "running" }),
  release = async () => true,
}: {
  startOrReuse?: WorkerRequestEnvironment["workflow"]["startOrReuse"];
  status?: WorkerRequestEnvironment["workflow"]["status"];
  release?: WorkerRequestEnvironment["runLock"]["release"];
} = {}): WorkerRequestEnvironment {
  return {
    manualTriggerSecret: "secret",
    workflow: { startOrReuse, status },
    runLock: { release },
  };
}

function lockedJobDependencies(events: string[]): LockedJobDependencies {
  const runLock: JobFinderRunLock = {
    async acquire(): Promise<RunLockAcquisition> {
      events.push("acquire");
      return {
        kind: "acquired",
        workflowInstanceId: "run-1",
        acquiredAt: "2026-08-24T22:00:00.000Z",
      };
    },
    async release() {
      events.push("release");
      return true;
    },
  };
  return {
    runLock,
    async execute() {
      events.push("execute");
    },
    async flushLangSmith() {
      events.push("flush");
    },
    shutdownLangSmith() {
      events.push("shutdown");
    },
    async reportFailure() {
      events.push("report");
    },
    recordCleanupFailure() {
      events.push("cleanup-error");
    },
    now: () => "2026-08-24T22:00:00.000Z",
  };
}
