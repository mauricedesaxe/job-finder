import { config } from "./config";
import {
  createNotionClient,
  checkRecentApplication,
  queryJobsByStatus,
  queryJobsByStatusAndCompany,
  updateJobStatus,
} from "./notion";

function validateConfig() {
  const missing: string[] = [];
  if (!config.notionToken) missing.push("NOTION_TOKEN");
  if (!config.notionDatabaseId) missing.push("NOTION_DATABASE_ID");

  if (missing.length > 0) {
    console.error(`Missing env vars: ${missing.join(", ")}`);
    process.exit(1);
  }
}

async function main() {
  validateConfig();

  const notion = createNotionClient(config.notionToken);
  const stats = { unflagged: 0, propagated: 0 };

  // Pass 1: Unflag stale flags
  console.log("Pass 1: Unflagging stale flags...\n");

  const flaggedJobs = await queryJobsByStatus(notion, config.notionDatabaseId, "Flagged");
  const flaggedCompanies = new Map<string, string[]>();

  for (const job of flaggedJobs) {
    const ids = flaggedCompanies.get(job.company) ?? [];
    ids.push(job.id);
    flaggedCompanies.set(job.company, ids);
  }

  for (const [company, pageIds] of flaggedCompanies) {
    const recency = await checkRecentApplication(notion, config.notionDatabaseId, company);

    if (!recency.exists) {
      console.log(`  Unflagging ${pageIds.length} job(s) from "${company}" (no recent application)`);
      for (const id of pageIds) {
        await updateJobStatus(notion, id, "To Review");
        stats.unflagged++;
      }
    }
  }

  // Pass 2: Propagate missing flags
  console.log("\nPass 2: Propagating missing flags...\n");

  const stillFlagged = await queryJobsByStatus(notion, config.notionDatabaseId, "Flagged");
  const stillFlaggedCompanies = new Set(stillFlagged.map((j) => j.company));

  for (const company of stillFlaggedCompanies) {
    const unflaggedJobs = await queryJobsByStatusAndCompany(
      notion,
      config.notionDatabaseId,
      "To Review",
      company,
    );

    if (unflaggedJobs.length > 0) {
      console.log(`  Flagging ${unflaggedJobs.length} job(s) from "${company}"`);
      for (const job of unflaggedJobs) {
        await updateJobStatus(notion, job.id, "Flagged");
        stats.propagated++;
      }
    }
  }

  console.log("\n--- Summary ---");
  console.log(`Unflagged: ${stats.unflagged}`);
  console.log(`Propagated: ${stats.propagated}`);
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
