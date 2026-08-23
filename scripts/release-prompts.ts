import { config } from "../src/config";
import {
  createPromptTagClient,
  moveProductionAtomically,
  resolvePromptCommit,
} from "../src/services/promptAdmin";
import { PROMPT_NAMES } from "../src/services/promptRegistry";

const release = process.argv[2];
if (!release || !/^release-\d{4}-\d{2}-\d{2}-\d+$/.test(release))
  throw new Error("Usage: bun scripts/release-prompts.ts release-YYYY-MM-DD-N");
const adminConfig = { endpoint: config.langsmithEndpoint, apiKey: config.langsmithApiKey };
const candidates = new Map();
const previous = new Map();
for (const name of PROMPT_NAMES) {
  const [candidate, production] = await Promise.all([
    resolvePromptCommit({ config: adminConfig, name, ref: "latest" }),
    resolvePromptCommit({ config: adminConfig, name, ref: "production" }),
  ]);
  candidates.set(name, candidate.id);
  previous.set(name, production.id);
}
const tagClient = createPromptTagClient(adminConfig);
for (const [name, commitId] of candidates) await tagClient.moveTag({ name, tag: release, commitId });
await moveProductionAtomically({ client: tagClient, targets: candidates, previous });
