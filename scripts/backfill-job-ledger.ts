import { loadCliConfig } from "../src/config";

import { logger } from "../src/logger";
import { createSqliteJobLedger } from "../src/services/sqliteJobLedger";
import { initializeJobLedgerFromNotion } from "../src/services/notionLedgerInitialization";
import { createNotionClient } from "../src/services/notion";

const config = loadCliConfig();
const log = logger.child({ component: "backfill-job-ledger" });

async function main(): Promise<void> {
  const ledger = createSqliteJobLedger(config.jobLedgerPath);
  try {
    const result = await initializeJobLedgerFromNotion({
      client: createNotionClient(config.notionToken),
      databaseId: config.notionDatabaseId,
      ledger,
    });
    log.info(result, "Notion job ledger initialization complete");
  } finally {
    await ledger.close();
  }
}

main().catch((err) => {
  log.fatal({ err }, "Notion job ledger initialization failed");
  process.exit(1);
});
