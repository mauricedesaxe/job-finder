import { z } from "zod/v4";

export const JobFinderRunRequestSchema = z.object({
  runId: z
    .string()
    .min(1)
    .max(100)
    .regex(/^[a-zA-Z0-9_][a-zA-Z0-9-_]*$/),
  mode: z.discriminatedUnion("kind", [
    z.object({ kind: z.literal("scrape") }),
    z.object({ kind: z.literal("reconcile") }),
  ]),
});

export type JobFinderRunRequest = z.infer<typeof JobFinderRunRequestSchema>;

export interface RunCoordinator {
  run(request: JobFinderRunRequest): Promise<void>;
}

export function createRunCoordinator(
  execute: (request: JobFinderRunRequest) => Promise<void>,
): RunCoordinator {
  const activeByRunId = new Map<string, Promise<void>>();
  let queue = Promise.resolve();

  return {
    run(request) {
      const active = activeByRunId.get(request.runId);
      if (active) return active;

      const run = queue.catch(() => undefined).then(() => execute(request));
      activeByRunId.set(request.runId, run);
      queue = run;
      const clearActiveRun = () => {
        if (activeByRunId.get(request.runId) === run) activeByRunId.delete(request.runId);
      };
      void run.then(clearActiveRun, clearActiveRun);
      return run;
    },
  };
}

const ManualRunSchema = z.object({
  runId: JobFinderRunRequestSchema.shape.runId.optional(),
  mode: JobFinderRunRequestSchema.shape.mode.default({ kind: "scrape" }),
});

export interface WorkerRequestEnvironment {
  workflow: {
    create(options: {
      id: string;
      params: { mode: JobFinderRunRequest["mode"] };
    }): Promise<{ id: string }>;
    status(id: string): Promise<unknown>;
  };
  manualTriggerSecret: string;
}

export async function handleWorkerRequest(
  request: Request,
  environment: WorkerRequestEnvironment,
): Promise<Response> {
  const url = new URL(request.url);
  const runId = runIdFromPath(url.pathname);
  const isRunRoute = url.pathname === "/runs" || runId !== null;
  if (!isRunRoute) return Response.json({ error: "not found" }, { status: 404 });
  if (!isAuthorized(request, environment.manualTriggerSecret)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }

  if (request.method === "POST" && url.pathname === "/runs") {
    let input: z.infer<typeof ManualRunSchema>;
    try {
      const body: unknown = await request.json();
      input = ManualRunSchema.parse(body);
    } catch {
      return Response.json({ error: "invalid request" }, { status: 400 });
    }
    const requestRunId = input.runId ?? crypto.randomUUID();
    const instance = await environment.workflow.create({
      id: requestRunId,
      params: { mode: input.mode },
    });
    console.info({ component: "worker", runId: instance.id }, "manual run created");
    return Response.json({ runId: instance.id }, { status: 202 });
  }

  if (request.method === "GET" && runId !== null) {
    const status = z
      .record(z.string(), z.unknown())
      .parse(await environment.workflow.status(runId));
    return Response.json({ runId, ...status });
  }

  return Response.json({ error: "not found" }, { status: 404 });
}

function runIdFromPath(pathname: string): string | null {
  const match = /^\/runs\/([^/]+)$/.exec(pathname);
  if (!match?.[1]) return null;
  try {
    const parsed = JobFinderRunRequestSchema.shape.runId.safeParse(decodeURIComponent(match[1]));
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

function isAuthorized(request: Request, secret: string): boolean {
  return request.headers.get("authorization") === `Bearer ${z.string().min(1).parse(secret)}`;
}
