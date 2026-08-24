import { WorkflowEntrypoint, type WorkflowEvent, type WorkflowStep } from "cloudflare:workers";
import { NonRetryableError } from "cloudflare:workflows";
import { Container } from "@cloudflare/containers";
import { z } from "zod/v4";
import { handleWorkerRequest, JobFinderRunRequestSchema } from "./cloudflareRun";
import { parseJobFinderConfig } from "./config";
import { createD1JobLedger } from "./services/d1JobLedger";
import { handleJobLedgerRpc } from "./services/jobLedgerRpc";

const CONTAINER_NAME = "job-finder";
const CONTAINER_PORT = 8080;

const WorkflowParamsSchema = z.object({
  mode: JobFinderRunRequestSchema.shape.mode,
});
const CompleteRunResponseSchema = z.object({
  runId: JobFinderRunRequestSchema.shape.runId,
  status: z.literal("complete"),
});

declare global {
  namespace Cloudflare {
    interface Env {
      MANUAL_TRIGGER_SECRET: string;
      NOTION_DATABASE_ID: string;
      NOTION_TOKEN: string;
      JINA_API_KEY: string;
      OPENROUTER_API_KEY: string;
      LLM_MODEL?: string;
      LANGSMITH_API_KEY: string;
      LANGSMITH_ENDPOINT?: string;
      LANGSMITH_PROJECT?: string;
      SLACK_WEBHOOK_URL?: string;
    }
  }
}

export class JobFinderContainer extends Container<Cloudflare.Env> {
  override defaultPort = CONTAINER_PORT;
  override sleepAfter = "10m";
  override pingEndpoint = "localhost/health";
  override envVars = applicationEnvironment(this.env);

  static override outboundByHost = {
    "job-ledger.internal": (request: Request, environment: Cloudflare.Env) =>
      handleJobLedgerRpc(request, createD1JobLedger(environment.JOB_LEDGER)),
  };
}

export class JobFinderWorkflow extends WorkflowEntrypoint<
  Cloudflare.Env,
  z.input<typeof WorkflowParamsSchema>
> {
  override async run(
    event: WorkflowEvent<z.input<typeof WorkflowParamsSchema>>,
    step: WorkflowStep,
  ): Promise<{ runId: string }> {
    const payload = event.schedule
      ? { mode: { kind: "scrape" as const } }
      : WorkflowParamsSchema.parse(event.payload);
    const runRequest = JobFinderRunRequestSchema.parse({
      runId: event.instanceId,
      mode: payload.mode,
    });

    console.info({ component: "workflow", runId: runRequest.runId }, "run dispatched");
    try {
      await step.do(
        "run job finder",
        {
          retries: { limit: 2, delay: "5 minutes", backoff: "exponential" },
          timeout: "30 minutes",
        },
        async () => {
          const container = this.env.JOB_FINDER.getByName(CONTAINER_NAME);
          const response = await container.fetch("http://container/runs", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(runRequest),
          });
          if (!response.ok) {
            throw new NonRetryableError(`Container run failed with status ${response.status}`);
          }
          try {
            const body: unknown = await response.json();
            CompleteRunResponseSchema.parse(body);
          } catch {
            throw new NonRetryableError("Container returned an invalid run response");
          }
        },
      );
    } catch (error) {
      console.error({ component: "workflow", err: error, runId: runRequest.runId }, "run failed");
      throw error;
    }
    console.info({ component: "workflow", runId: runRequest.runId }, "run completed");
    return { runId: runRequest.runId };
  }
}

function applicationEnvironment(environment: Cloudflare.Env): Record<string, string> {
  const config = parseJobFinderConfig(environmentValues(environment));
  return {
    NODE_ENV: "production",
    LOG_LEVEL: "info",
    NOTION_DATABASE_ID: config.notionDatabaseId,
    NOTION_TOKEN: config.notionToken,
    JINA_API_KEY: config.jinaApiKey,
    OPENROUTER_API_KEY: config.openrouterApiKey,
    LLM_MODEL: config.llmModel,
    LANGSMITH_API_KEY: config.langsmithApiKey,
    LANGSMITH_ENDPOINT: config.langsmithEndpoint,
    LANGSMITH_PROJECT: config.langsmithProject,
    ENABLE_ATS_ENRICHMENT: String(config.enableAtsEnrichment),
    ...(config.slackWebhookUrl ? { SLACK_WEBHOOK_URL: config.slackWebhookUrl } : {}),
  };
}

function environmentValues(
  environment: Cloudflare.Env,
): Readonly<Record<string, string | undefined>> {
  return {
    NOTION_DATABASE_ID: environment.NOTION_DATABASE_ID,
    NOTION_TOKEN: environment.NOTION_TOKEN,
    JINA_API_KEY: environment.JINA_API_KEY,
    OPENROUTER_API_KEY: environment.OPENROUTER_API_KEY,
    LLM_MODEL: environment.LLM_MODEL,
    LANGSMITH_API_KEY: environment.LANGSMITH_API_KEY,
    LANGSMITH_ENDPOINT: environment.LANGSMITH_ENDPOINT,
    LANGSMITH_PROJECT: environment.LANGSMITH_PROJECT,
    SLACK_WEBHOOK_URL: environment.SLACK_WEBHOOK_URL,
    ENABLE_ATS_ENRICHMENT: environment.ENABLE_ATS_ENRICHMENT,
  };
}

export { ContainerProxy } from "@cloudflare/containers";

export default {
  fetch(request, environment) {
    return handleWorkerRequest(request, {
      manualTriggerSecret: environment.MANUAL_TRIGGER_SECRET,
      workflow: {
        create: (options) => environment.JOB_FINDER_WORKFLOW.create(options),
        async status(id) {
          const instance = await environment.JOB_FINDER_WORKFLOW.get(id);
          return instance.status();
        },
      },
    });
  },
} satisfies ExportedHandler<Cloudflare.Env>;
