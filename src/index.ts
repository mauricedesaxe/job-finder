import { loadCliConfig } from "./config";
import { jobFinderRunMode, runJobFinder } from "./jobFinder";
import { configureLogger, logger } from "./logger";
import { flushPending } from "./services/langsmith";
import { sendFatalError } from "./services/slack";
import { createSqliteJobLedger } from "./services/sqliteJobLedger";

const log = logger.child({ component: "main" });
const config = loadCliConfig();
configureLogger(config.logLevel);
const mode = jobFinderRunMode(process.argv);

async function main(): Promise<void> {
  const ledger = createSqliteJobLedger(config.jobLedgerPath);
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
