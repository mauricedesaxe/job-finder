import { loadJobFinderConfig } from "../src/config";
import {
  createPromptTagClient,
  moveProductionAtomically,
  resolvePromptCommit,
} from "../src/services/promptAdmin";
import { PROMPT_NAMES } from "../src/services/promptRegistry";

const config = loadJobFinderConfig();

const release = process.argv[2];
if (!release || !/^release-\d{4}-\d{2}-\d{2}-\d+$/.test(release)) throw new Error("Usage: bun scripts/rollback-prompts.ts release-YYYY-MM-DD-N");
const adminConfig = { endpoint: config.langsmithEndpoint, apiKey: config.langsmithApiKey };
const targets = new Map();
const previous = new Map();
for (const name of PROMPT_NAMES) {
  const [target, production] = await Promise.all([
    resolvePromptCommit({ config: adminConfig, name, ref: release }),
    resolvePromptCommit({ config: adminConfig, name, ref: "production" }),
  ]);
  targets.set(name, target.id);
  previous.set(name, production.id);
}
await moveProductionAtomically({ client: createPromptTagClient(adminConfig), targets, previous });
