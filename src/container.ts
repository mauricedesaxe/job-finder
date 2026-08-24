import { ZodError } from "zod/v4";
import {
  createRunCoordinator,
  type JobFinderRunRequest,
  JobFinderRunRequestSchema,
  type RunCoordinator,
} from "./cloudflareRun";
import { loadContainerRuntimeConfig } from "./config";
import { runJobFinder } from "./jobFinder";
import { logger, withRunLogContext } from "./logger";
import { createHttpJobLedger } from "./services/jobLedgerRpc";

interface ContainerHandlerOptions {
  coordinator: RunCoordinator;
}

function startContainer(): void {
  const runtime = loadContainerRuntimeConfig();
  const ledger = createHttpJobLedger();
  const coordinator = createRunCoordinator(async ({ runId, mode }) => {
    const log = logger.child({ component: "container-run", runId });
    log.info({ mode: mode.kind }, "run started");
    try {
      await withRunLogContext(runId, () => runJobFinder({ mode, ledger, config: runtime.config }));
      log.info({ mode: mode.kind }, "run completed");
    } catch (error) {
      log.error({ err: error, mode: mode.kind }, "run failed");
      throw error;
    }
  });

  Bun.serve({
    port: runtime.port,
    fetch: createContainerRequestHandler({ coordinator }),
  });
}

export function createContainerRequestHandler({ coordinator }: ContainerHandlerOptions) {
  return async function handle(request: Request): Promise<Response> {
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path === "/health") {
      return Response.json({ status: "ok" });
    }
    if (request.method !== "POST" || path !== "/runs") {
      return Response.json({ error: "not found" }, { status: 404 });
    }

    let runRequest: JobFinderRunRequest;
    try {
      const body: unknown = await request.json();
      runRequest = JobFinderRunRequestSchema.parse(body);
    } catch (error) {
      if (error instanceof ZodError || error instanceof SyntaxError) {
        return Response.json({ error: "invalid request" }, { status: 400 });
      }
      throw error;
    }

    await coordinator.run(runRequest);
    return Response.json({ runId: runRequest.runId, status: "complete" });
  };
}

if (import.meta.main) startContainer();
