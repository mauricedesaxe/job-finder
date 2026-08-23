import { ChatOpenAI } from "@langchain/openai";
import { ChatPromptTemplate } from "@langchain/core/prompts";
import { Client } from "langsmith";
import { z } from "zod/v4";
import { isUnchangedPromptConflict } from "../src/services/promptAdmin";
import { PROMPT_NAMES, type PromptName } from "../src/services/promptRegistry";

const legacyRef = "a702deef57526a024df4b4e29ab4279ecddbaaac";
const BootstrapConfigSchema = z.object({
  langsmithApiKey: z.string().min(1, "LANGSMITH_API_KEY is required"),
  langsmithEndpoint: z.string().url().default("https://eu.api.smith.langchain.com"),
  openrouterApiKey: z.string().min(1, "OPENROUTER_API_KEY is required"),
});
const TenantSchema = z.object({ id: z.string().uuid() });

const bootstrapConfig = BootstrapConfigSchema.parse({
  langsmithApiKey: process.env.LANGSMITH_API_KEY,
  langsmithEndpoint: process.env.LANGSMITH_ENDPOINT,
  openrouterApiKey: process.env.OPENROUTER_API_KEY,
});

async function currentTenantId(): Promise<string> {
  const response = await fetch(`${bootstrapConfig.langsmithEndpoint}/settings`, {
    headers: { "x-api-key": bootstrapConfig.langsmithApiKey },
  });
  if (!response.ok) throw new Error(`Could not resolve current LangSmith tenant: ${response.status}`);
  return TenantSchema.parse(await response.json()).id;
}

async function legacyFile(path: string): Promise<string> {
  const process = Bun.spawn(["git", "show", `${legacyRef}:${path}`], { stdout: "pipe", stderr: "pipe" });
  const [stdout, stderr, code] = await Promise.all([new Response(process.stdout).text(), new Response(process.stderr).text(), process.exited]);
  if (code !== 0) throw new Error(`Could not read legacy prompt source: ${stderr}`);
  return stdout;
}

function promptBody(source: string, name: string): string {
  const expression = new RegExp('name: "' + name + '"[\\s\\S]*?prompt: `([\\s\\S]*?)`');
  const match = source.match(expression);
  if (!match?.[1]) throw new Error(`Could not find legacy prompt ${name}`);
  return match[1].replace("${rateLines}", "{rates}");
}

function constantBody(source: string, constant: string): string {
  const expression = new RegExp('const '+ constant + ' = `([\\s\\S]*?)`;');
  const match = source.match(expression);
  if (!match?.[1]) throw new Error(`Could not find legacy prompt ${constant}`);
  return match[1];
}

async function templates(): Promise<Map<PromptName, ChatPromptTemplate>> {
  const [evaluation, enrichment, deduplication] = await Promise.all([legacyFile("src/config/evaluation.ts"), legacyFile("src/pipeline/enrich.ts"), legacyFile("src/pipeline/dedup.ts")]);
  const result = new Map<PromptName, ChatPromptTemplate>();
  const criteria: ReadonlyArray<[PromptName, string]> = [
    ["job-finder-filter-location-eligibility", "remote-europe-eligible"],
    ["job-finder-filter-compensation", "compensation-minimum"],
    ["job-finder-filter-role-quality", "role-quality"],
    ["job-finder-filter-company-quality", "cheap-shop-placement"],
    ["job-finder-profile-crypto-web3-ts", "crypto-web3-ts"],
    ["job-finder-profile-fintech-trading-infra-ts", "fintech-trading-infra-ts"],
    ["job-finder-profile-senior-fullstack-react", "senior-fullstack-react"],
    ["job-finder-profile-ai-engineering", "ai-engineering"],
  ];
  for (const [name, legacyName] of criteria) result.set(name, ChatPromptTemplate.fromMessages([["system", promptBody(evaluation, legacyName)], ["human", "{job}"]]));
  result.set("job-finder-enrichment", ChatPromptTemplate.fromMessages([["system", constantBody(enrichment, "ENRICH_PROMPT")], ["human", "{job}"]]));
  const dedupPrompt = deduplication.match(/content: `([\s\S]*?)`,/)?.[1];
  if (!dedupPrompt) throw new Error("Could not find legacy title-deduplication prompt");
  result.set("job-finder-title-deduplication", ChatPromptTemplate.fromMessages([["system", dedupPrompt], ["human", 'New title: "{newTitle}"\n\nExisting titles at the same company:\n{existingTitles}\n\nIs the new title a duplicate of any existing title?']]));
  return result;
}

const client = new Client({
  apiUrl: bootstrapConfig.langsmithEndpoint,
  apiKey: bootstrapConfig.langsmithApiKey,
  workspaceId: await currentTenantId(),
});
const model = new ChatOpenAI({ apiKey: bootstrapConfig.openrouterApiKey, configuration: { baseURL: "https://openrouter.ai/api/v1" }, model: "google/gemini-2.5-flash", temperature: 0, maxTokens: 256 });
const importedTemplates = await templates();
for (const name of PROMPT_NAMES) {
  const template = importedTemplates.get(name);
  if (!template) throw new Error(`Missing bootstrap template for ${name}`);
  try {
    await client.pushPrompt(name, { object: template.pipe(model) });
  } catch (error) {
    if (!isUnchangedPromptConflict(error)) throw error;
  }
}
