import { config } from "./config";
import { jobFinderRunMode, runJobFinder } from "./jobFinder";
import { logger } from "./logger";
import { createJobLedger } from "./services/jobLedger";
import { flushPending } from "./services/langsmith";
import { sendFatalError } from "./services/slack";

const log = logger.child({ component: "main" });
const mode = jobFinderRunMode(process.argv);

async function main(): Promise<void> {
  const ledger = createJobLedger(config.jobLedgerPath);
  try {
    await runJobFinder({ mode, ledger, config });
  } finally {
    await ledger.close();
  }
}

main().catch(async (err) => {
  log.fatal({ err }, "fatal error");
  await flushPending();
  if (config.slackWebhookUrl) {
    await sendFatalError(config.slackWebhookUrl, err);
  }
  process.exit(1);
});
