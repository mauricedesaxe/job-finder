import { config } from "../src/config";
import { logger } from "../src/logger";
import { createJobLedger } from "../src/services/jobLedger";
import { backfillJobLedger } from "../src/services/notionLedgerBackfill";
import { createNotionClient } from "../src/services/notion";

const log = logger.child({ component: "backfill-job-ledger" });

async function main(): Promise<void> {
  const ledger = createJobLedger(config.jobLedgerPath);
  try {
    const result = await backfillJobLedger({
      client: createNotionClient(config.notionToken),
      databaseId: config.notionDatabaseId,
      ledger,
    });
    log.info(result, "Notion job ledger backfill complete");
  } finally {
    ledger.close();
  }
}

main().catch((err) => {
  log.fatal({ err }, "Notion job ledger backfill failed");
  process.exit(1);
});
