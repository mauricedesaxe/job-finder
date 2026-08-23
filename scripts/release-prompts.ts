import { z } from "zod/v4";
import {
  createPromptTagClient,
  isMissingPromptReference,
  moveProductionAtomically,
  resolvePromptCommit,
  resolveProductionHistory,
} from "../src/services/promptAdmin";
import { PROMPT_NAMES, type PromptName } from "../src/services/promptRegistry";

const PromptReleaseConfigSchema = z.object({
  langsmithApiKey: z.string().min(1, "LANGSMITH_API_KEY is required"),
  langsmithEndpoint: z.string().url().default("https://eu.api.smith.langchain.com"),
});
const promptReleaseConfig = PromptReleaseConfigSchema.parse({
  langsmithApiKey: process.env.LANGSMITH_API_KEY,
  langsmithEndpoint: process.env.LANGSMITH_ENDPOINT,
});

const release = process.argv[2];
const bootstrap = process.argv.includes("--bootstrap");
if (!release || !/^release-\d{4}-\d{2}-\d{2}-\d+$/.test(release))
  throw new Error("Usage: bun scripts/release-prompts.ts release-YYYY-MM-DD-N");
const adminConfig = {
  endpoint: promptReleaseConfig.langsmithEndpoint,
  apiKey: promptReleaseConfig.langsmithApiKey,
};
const candidates = new Map<PromptName, string>();
const previous = new Map<PromptName, string>();
async function resolvePreviousProduction(name: PromptName): Promise<string | undefined> {
  try {
    return (await resolvePromptCommit({ config: adminConfig, name, ref: "production" })).id;
  } catch (error) {
    if (isMissingPromptReference(error)) return undefined;
    throw error;
  }
}
for (const name of PROMPT_NAMES) {
  const [candidate, production] = await Promise.all([
    resolvePromptCommit({ config: adminConfig, name, ref: "latest" }),
    resolvePreviousProduction(name),
  ]);
  candidates.set(name, candidate.id);
  if (production) previous.set(name, production);
}
const tagClient = createPromptTagClient(adminConfig);
resolveProductionHistory({ targets: candidates, previous, bootstrap });
for (const [name, commitId] of candidates) await tagClient.moveTag({ name, tag: release, commitId });
await moveProductionAtomically({ client: tagClient, targets: candidates, previous, bootstrap });
