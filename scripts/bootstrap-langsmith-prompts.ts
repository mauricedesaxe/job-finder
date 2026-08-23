import { ChatOpenAI } from "@langchain/openai";
import { ChatPromptTemplate } from "@langchain/core/prompts";
import { Client } from "langsmith";
import { z } from "zod/v4";
import { isUnchangedPromptConflict } from "../src/services/promptAdmin";
import {
  EVALUATION_PROMPTS,
  PROMPT_NAMES,
  PROMPT_REGISTRY,
  type EvaluationPromptName,
  type PromptName,
} from "../src/services/promptRegistry";

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
  const expression = new RegExp("const " + constant + " = `([\\s\\S]*?)`;");
  const match = source.match(expression);
  if (!match?.[1]) throw new Error(`Could not find legacy prompt ${constant}`);
  return match[1];
}

const EVALUATION_PROMPT_SOURCES: Partial<Record<EvaluationPromptName, string>> = {
  "job-finder-filter-location-eligibility": `You are a location-eligibility filter. Evaluate only where the candidate may work. Ignore company domain, seniority, stack, and compensation.

PASS when the listing clearly supports fully remote work from Europe, the UK, the EEA, or a broad region that includes Europe. PASS when the listing is global remote with no incompatible residency restriction.

FAIL when the listing requires onsite or hybrid attendance, limits remote work to a non-European region, or requires a country outside Europe. FAIL when location information clearly contradicts European remote work.

Do not infer that a company is remote because it works in crypto, web3, or any other domain. A crypto company must state eligible remote work like every other company.

Examples:
PASS: "Remote in Europe" → explicit European remote eligibility.
PASS: "Work from anywhere. We hire across EMEA." → Europe is eligible.
PASS: "Remote globally, no location restrictions." → Europe is eligible.
FAIL: "London hybrid, three days a week in the office." → hybrid attendance required.
FAIL: "Remote, US or Canada only." → Europe is excluded.
FAIL: "Crypto exchange, London office, hybrid schedule." → company domain does not make the role remote.`,
  "job-finder-filter-role-quality": `You are a role-quality filter. Reject only listings whose role shape, stack, or seniority bar makes them a poor fit for a hands-on senior product engineer. Ignore location, compensation, and company domain.

FAIL if any clear signal applies:
1. The primary stack is enterprise Java/Spring, .NET/C#, Scala, C++, or Angular/Kendo. A peripheral or nice-to-have mention passes when the primary product stack is TypeScript, Node.js, Go, Rust, Python, React, Vue, or Svelte.
2. The role is architect-only, manager-only, sales engineering, solutions engineering, field engineering, or customer-facing delivery without substantial hands-on product work.
3. The primary work is data warehouse or pipeline plumbing. Snowflake, dbt, Airflow, Debezium, CDC, DMS, or BigQuery as the core work fails unless the listing clearly builds product features.
4. The listing requires 10 or more years at principal or distinguished level.
5. The body discloses four or more synchronous interview rounds. Do not count take-homes, reference checks, application review, or an offer.
6. The body contains substantial non-English prose that shows a non-English-primary team.
7. The role primarily operates infrastructure. Managing clusters, deployments, observability, cost optimization, or reliability fails. Building product features with Docker, Kubernetes, or cloud tools passes.

Do not reject blockchain, crypto, protocol, or chain-adjacent work because of its domain. Judge the role from its hands-on product responsibilities and the rules above.

Examples:
PASS: "Senior product engineer. Build and ship React and Node features. Kubernetes is part of the stack." → product delivery, not infrastructure operations.
PASS: "Rust engineer building wallet and payment experiences on an L2." → domain alone is not a rejection signal.
PASS: "Senior architect who codes, builds APIs, and ships features." → hands-on work is explicit.
FAIL: "Senior Platform Engineer. Own cloud infrastructure, deployment pipelines, tracing, alerts, and 3am incidents." → infrastructure operations are the primary work.
FAIL: "Solutions Engineer. Partner with strategic accounts and translate customer needs into deployments." → customer-facing delivery role.
FAIL: "Principal engineer. 12+ years required. Java, Spring Boot, and Angular are the core stack." → seniority and stack signals.`,
  "job-finder-profile-early-stage-product-engineer": `You evaluate job listings for a hands-on senior, staff, lead, or founding product engineer. Location eligibility is verified by a separate filter. Do not evaluate location.

PASS when the role has substantial individual-contributor ownership of 0-to-1 product delivery. The work should build and ship an MVP, product features, or user experience from idea through production. Seniority may be senior, staff, lead, founding, or unspecified when the ownership is clear. Early company stage is a preference, not a hard requirement.

FAIL when the role is primarily people management, infrastructure operations, internal platform work without product responsibility, pure architecture, sales or customer delivery, or narrow research without shipping a product.

Examples:
PASS: "Founding full-stack engineer. Work with the founders to take an MVP from customer interviews to a shipped React and Node product." → hands-on 0-to-1 product ownership.
PASS: "Staff product engineer. Own onboarding, payments, and the mobile web experience from discovery through production." → user-facing product delivery.
PASS: "Senior backend engineer at a 200-person company. Build new product workflows end to end and work directly with design." → company stage is a preference, not a gate.
FAIL: "Engineering manager. Set technical direction, hire, and manage four teams." → management is the primary work.
FAIL: "Senior SRE. Own Kubernetes, incident response, and reliability targets." → operations, not product delivery.
FAIL: "Platform engineer. Build internal developer tooling with no user-facing product ownership." → internal platform work alone is insufficient.`,
  "job-finder-profile-applied-ai-product-engineer": `You evaluate job listings for a hands-on senior, staff, lead, or founding engineer who ships AI-powered product experiences. Location eligibility is verified by a separate filter. Do not evaluate location.

PASS when the role builds and ships LLM experiences, agents, RAG systems, AI-powered product features, evaluation systems, tool use, retrieval, or application-layer AI workflows. The role needs substantial product responsibility, not only infrastructure support.

FAIL when the primary work is pure ML research, training new models, model architecture research, distillation, quantization, data engineering, operational MLOps, GPU or model-serving operations, or internal AI-platform work without substantial product responsibility. Fine-tuning alone does not fail. Reject training or research when it is the role's core work, even if the listing also mentions RAG, agents, or evaluations.

Examples:
PASS: "Senior engineer. Ship an agent that helps customers resolve support cases. Build RAG, tool calling, offline evals, and the product UI." → shipped AI product experience.
PASS: "Applied AI product engineer. Fine-tune an existing model for classification, then integrate it into customer workflows with evaluations and feedback loops." → fine-tuning supports a shipped product.
PASS: "Founding AI engineer. Build a document-analysis product with retrieval, citations, agents, and user-facing review flows." → application-layer product ownership.
FAIL: "ML engineer. Lead quantization, distillation, training strategies, and model architecture research in PyTorch. RAG is a secondary integration." → training and research are primary.
FAIL: "Senior LLM engineer. Train specialized models, own model pipelines, and optimize Triton and vLLM serving." → model training and serving operations are primary.
FAIL: "Data engineer. Own Snowflake, dbt, Airflow, and CDC pipelines. AI tooling is a nice-to-have." → data engineering is primary.
FAIL: "MLOps engineer. Manage GPU clusters, model deployments, monitoring, and on-call." → operational platform work lacks product responsibility.`,
};

async function templates(): Promise<Map<PromptName, ChatPromptTemplate>> {
  const [evaluation, enrichment, deduplication] = await Promise.all([
    legacyFile("src/config/evaluation.ts"),
    legacyFile("src/pipeline/enrich.ts"),
    legacyFile("src/pipeline/dedup.ts"),
  ]);
  const result = new Map<PromptName, ChatPromptTemplate>();
  const legacyCriteria: ReadonlyArray<[PromptName, string]> = [
    ["job-finder-filter-compensation", "compensation-minimum"],
    ["job-finder-filter-company-quality", "cheap-shop-placement"],
  ];
  for (const [name, legacyName] of legacyCriteria) {
    result.set(
      name,
      ChatPromptTemplate.fromMessages([["system", promptBody(evaluation, legacyName)], ["human", "{job}"]]),
    );
  }
  for (const name of EVALUATION_PROMPTS) {
    const source = EVALUATION_PROMPT_SOURCES[name];
    if (source) result.set(name, ChatPromptTemplate.fromMessages([["system", source], ["human", "{job}"]]));
  }
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
const importedTemplates = await templates();
for (const name of PROMPT_NAMES) {
  const template = importedTemplates.get(name);
  if (!template) throw new Error(`Missing bootstrap template for ${name}`);
  const model = new ChatOpenAI({
    apiKey: bootstrapConfig.openrouterApiKey,
    configuration: { baseURL: "https://openrouter.ai/api/v1" },
    model: "google/gemini-2.5-flash",
    temperature: 0,
    maxTokens: PROMPT_REGISTRY[name].maxTokens,
  });
  try {
    await client.pushPrompt(name, { object: template.pipe(model) });
  } catch (error) {
    if (!isUnchangedPromptConflict(error)) throw error;
  }
}
