import { z } from "zod/v4";
import type { JobFinderRunLock } from "./services/runLock";

export const WorkflowRunIdSchema = z
  .string()
  .min(1)
  .max(100)
  .regex(/^[a-zA-Z0-9_][a-zA-Z0-9-_]*$/);

export const JobFinderRunModeSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("scrape") }),
  z.object({ kind: z.literal("reconcile") }),
  z.object({ kind: z.literal("backfill") }),
]);

export const WorkflowPayloadSchema = z.object({
  mode: JobFinderRunModeSchema,
});

export type WorkflowPayload = z.infer<typeof WorkflowPayloadSchema>;

const ManualRunSchema = z.object({
  runId: WorkflowRunIdSchema.optional(),
  mode: JobFinderRunModeSchema.default({ kind: "scrape" }),
});

const WorkflowStatusSchema = z.object({
  status: z.enum([
    "queued",
    "running",
    "paused",
    "errored",
    "terminated",
    "complete",
    "waiting",
    "waitingForPause",
    "unknown",
  ]),
  output: z.unknown().optional(),
  error: z.unknown().optional(),
});

const RunCreatedResponseSchema = z.object({ runId: WorkflowRunIdSchema });
const RunStatusResponseSchema = WorkflowStatusSchema.extend({ runId: WorkflowRunIdSchema });
const UnlockResponseSchema = z.object({
  runId: WorkflowRunIdSchema,
  status: z.literal("unlocked"),
});
const ErrorResponseSchema = z.object({ error: z.string() });
const HealthResponseSchema = z.object({ status: z.literal("ok") });

const TERMINAL_WORKFLOW_STATUSES = new Set(["complete", "errored", "terminated"]);

export interface WorkerRequestEnvironment {
  workflow: {
    startOrReuse(options: { id: string; params: WorkflowPayload }): Promise<void>;
    status(id: string): Promise<unknown>;
  };
  runLock: Pick<JobFinderRunLock, "release">;
  manualTriggerSecret: string;
}

export interface LockedJobDependencies {
  runLock: JobFinderRunLock;
  execute(): Promise<void>;
  flushLangSmith(): Promise<void>;
  shutdownLangSmith(): void;
  reportFailure(error: unknown): Promise<void>;
  recordCleanupFailure(error: unknown): void;
  now(): string;
}

export class RunLockContendedError extends Error {
  constructor(
    readonly workflowInstanceId: string,
    readonly acquiredAt: string,
  ) {
    super(`Job finder is locked by Workflow ${workflowInstanceId} since ${acquiredAt}`);
    this.name = "RunLockContendedError";
  }
}

export async function runLockedJob(
  workflowInstanceId: string,
  dependencies: LockedJobDependencies,
): Promise<void> {
  const acquisition = await dependencies.runLock.acquire(workflowInstanceId, dependencies.now());
  if (acquisition.kind === "contended") {
    throw new RunLockContendedError(acquisition.workflowInstanceId, acquisition.acquiredAt);
  }

  let cleanupStarted = false;
  const cleanup = async (): Promise<void> => {
    cleanupStarted = true;
    try {
      await dependencies.flushLangSmith();
    } finally {
      dependencies.shutdownLangSmith();
    }
  };

  try {
    await dependencies.execute();
    await cleanup();
    if (!(await dependencies.runLock.release(workflowInstanceId))) {
      throw new Error(`Workflow ${workflowInstanceId} lost its run lock`);
    }
  } catch (error) {
    if (!cleanupStarted) {
      try {
        await cleanup();
      } catch (cleanupError) {
        dependencies.recordCleanupFailure(cleanupError);
      }
    }
    try {
      await dependencies.reportFailure(error);
    } catch (reportError) {
      dependencies.recordCleanupFailure(reportError);
    }
    throw error;
  }
}

export async function handleWorkerRequest(
  request: Request,
  environment: WorkerRequestEnvironment,
): Promise<Response> {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/health") {
    return json(HealthResponseSchema.parse({ status: "ok" }));
  }

  const route = parseRunRoute(url.pathname);
  if (!route) return errorResponse("not found", 404);
  if (!isAuthorized(request, environment.manualTriggerSecret)) {
    return errorResponse("unauthorized", 401);
  }

  if (request.method === "POST" && route.kind === "collection") {
    const input = await parseRequestBody(request);
    if (!input) return errorResponse("invalid request", 400);
    const requestedRunId = input.runId ?? crypto.randomUUID();
    await environment.workflow.startOrReuse({
      id: requestedRunId,
      params: { mode: input.mode },
    });
    return json(RunCreatedResponseSchema.parse({ runId: requestedRunId }), 202);
  }

  if (request.method === "GET" && route.kind === "instance") {
    const status = WorkflowStatusSchema.parse(await environment.workflow.status(route.runId));
    return json(RunStatusResponseSchema.parse({ runId: route.runId, ...status }));
  }

  if (request.method === "POST" && route.kind === "unlock") {
    const status = WorkflowStatusSchema.parse(await environment.workflow.status(route.runId));
    if (!TERMINAL_WORKFLOW_STATUSES.has(status.status)) {
      return errorResponse("workflow is not terminal", 409);
    }
    if (!(await environment.runLock.release(route.runId))) {
      return errorResponse("run lock is not owned by this workflow", 409);
    }
    return json(UnlockResponseSchema.parse({ runId: route.runId, status: "unlocked" }));
  }

  return errorResponse("not found", 404);
}

type RunRoute =
  | { kind: "collection" }
  | { kind: "instance"; runId: string }
  | { kind: "unlock"; runId: string };

function parseRunRoute(pathname: string): RunRoute | null {
  if (pathname === "/runs") return { kind: "collection" };
  const match = /^\/runs\/([^/]+)(\/unlock)?$/.exec(pathname);
  if (!match?.[1]) return null;
  try {
    const runId = WorkflowRunIdSchema.safeParse(decodeURIComponent(match[1]));
    if (!runId.success) return null;
    return match[2]
      ? { kind: "unlock", runId: runId.data }
      : { kind: "instance", runId: runId.data };
  } catch {
    return null;
  }
}

async function parseRequestBody(request: Request): Promise<z.infer<typeof ManualRunSchema> | null> {
  try {
    const body: unknown = await request.json();
    const result = ManualRunSchema.safeParse(body);
    return result.success ? result.data : null;
  } catch {
    return null;
  }
}

function isAuthorized(request: Request, secret: string): boolean {
  const parsedSecret = z.string().min(1).parse(secret);
  return request.headers.get("authorization") === `Bearer ${parsedSecret}`;
}

function errorResponse(error: string, status: number): Response {
  return json(ErrorResponseSchema.parse({ error }), status);
}

function json(value: unknown, status = 200): Response {
  return Response.json(value, { status });
}
