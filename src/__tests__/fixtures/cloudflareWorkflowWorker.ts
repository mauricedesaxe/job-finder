import { WorkflowEntrypoint, type WorkflowEvent, type WorkflowStep } from "cloudflare:workers";
import { z } from "zod/v4";
import productionWorker from "../../cloudflare";
import { sleep } from "../../concurrency/sleep";
import { logger, withRunLogContext } from "../../logger";

const log = logger.child({ component: "workflow-runtime-proof" });
const RuntimeProofPayloadSchema = z.object({ runId: z.string().min(1) });
const RuntimeProofResultSchema = z.object({
  runId: z.string().min(1),
  one: z.literal(1),
});

interface RuntimeProofEnvironment {
  JOB_LEDGER: D1Database;
}

export class RuntimeProofWorkflow extends WorkflowEntrypoint<
  RuntimeProofEnvironment,
  z.input<typeof RuntimeProofPayloadSchema>
> {
  override async run(
    event: WorkflowEvent<z.input<typeof RuntimeProofPayloadSchema>>,
    step: WorkflowStep,
  ): Promise<z.infer<typeof RuntimeProofResultSchema>> {
    const payload = RuntimeProofPayloadSchema.parse(event.payload);
    return step.do(
      "prove Worker runtime",
      { retries: { limit: 0, delay: "1 second" }, timeout: "1 minute" },
      () =>
        withRunLogContext(payload.runId, async () => {
          await sleep(1);
          log.info("runtime proof step");
          const row = z
            .object({ one: z.literal(1) })
            .parse(await this.env.JOB_LEDGER.prepare("SELECT 1 AS one").first());
          return RuntimeProofResultSchema.parse({ runId: payload.runId, one: row.one });
        }),
    );
  }
}

export { JobFinderWorkflow } from "../../cloudflare";
export default productionWorker;
