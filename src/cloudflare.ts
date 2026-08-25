import { WorkflowEntrypoint, type WorkflowEvent, type WorkflowStep } from "cloudflare:workers";
import { z } from "zod/v4";
import {
  handleWorkerRequest,
  runLockedJob,
  WorkflowPayloadSchema,
  WorkflowRunIdSchema,
} from "./cloudflareRun";
import { type JobFinderEnvironment, parseJobFinderConfig } from "./config";
import { runJobFinder } from "./jobFinder";
import { configureLogger, logger, withRunLogContext } from "./logger";
import { createD1JobLedger } from "./services/d1JobLedger";
import { flushPending, shutdownLangSmith } from "./services/langsmith";
import { createD1JobFinderRunLock } from "./services/runLock";
import { sendFatalError } from "./services/slack";

const log = logger.child({ component: "workflow" });
const WorkflowResultSchema = z.object({ runId: WorkflowRunIdSchema });

declare global {
  namespace Cloudflare {
    interface Env extends JobFinderEnvironment {
      JOB_LEDGER: D1Database;
      JOB_FINDER_WORKFLOW: Workflow;
      MANUAL_TRIGGER_SECRET: string;
    }
  }
}

export class JobFinderWorkflow extends WorkflowEntrypoint<
  Cloudflare.Env,
  z.input<typeof WorkflowPayloadSchema>
> {
  override async run(
    event: WorkflowEvent<z.input<typeof WorkflowPayloadSchema>>,
    step: WorkflowStep,
  ): Promise<z.infer<typeof WorkflowResultSchema>> {
    const runId = WorkflowRunIdSchema.parse(event.instanceId);
    return withRunLogContext(runId, async () => {
      try {
        const payload = event.schedule
          ? WorkflowPayloadSchema.parse({ mode: { kind: "scrape" } })
          : WorkflowPayloadSchema.parse(event.payload);
        const config = parseJobFinderConfig(this.env);
        configureLogger(config.logLevel);
        log.info({ mode: payload.mode.kind }, "run started");
        await step.do(
          "run job finder",
          { retries: { limit: 0, delay: "1 second" }, timeout: "30 minutes" },
          async () => {
            const ledger = createD1JobLedger(this.env.JOB_LEDGER);
            await runLockedJob(runId, {
              runLock: createD1JobFinderRunLock(this.env.JOB_LEDGER),
              execute: () => runJobFinder({ mode: payload.mode, ledger, config }),
              flushLangSmith: flushPending,
              shutdownLangSmith,
              reportFailure: async (error) => {
                if (config.slackWebhookUrl) await sendFatalError(config.slackWebhookUrl, error);
              },
              recordCleanupFailure: (error) => {
                log.error({ err: error }, "run cleanup failed");
              },
              now: () => new Date().toISOString(),
            });
          },
        );
      } catch (error) {
        log.error({ err: error }, "run failed");
        throw error;
      }
      log.info("run completed");
      return WorkflowResultSchema.parse({ runId });
    });
  }
}

export default {
  fetch(request, environment) {
    return handleWorkerRequest(request, {
      manualTriggerSecret: environment.MANUAL_TRIGGER_SECRET,
      runLock: createD1JobFinderRunLock(environment.JOB_LEDGER),
      workflow: {
        async startOrReuse(options) {
          try {
            await environment.JOB_FINDER_WORKFLOW.create(options);
          } catch (creationError) {
            try {
              const instance = await environment.JOB_FINDER_WORKFLOW.get(options.id);
              await instance.status();
            } catch {
              throw creationError;
            }
          }
        },
        async status(id) {
          const instance = await environment.JOB_FINDER_WORKFLOW.get(id);
          return instance.status();
        },
      },
    });
  },
} satisfies ExportedHandler<Cloudflare.Env>;
