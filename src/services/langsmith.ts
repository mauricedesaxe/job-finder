import type { AIMessage } from "@langchain/core/messages";
import { type Runnable, RunnableBinding, type RunnableConfig } from "@langchain/core/runnables";
import { pull } from "langchain/hub/node";
import { Client } from "langsmith";
import { traceable } from "langsmith/traceable";
import { z } from "zod/v4";
import { withRetry } from "../concurrency";
import type { ReviewSnapshot } from "../review";
import { PROMPT_NAMES, type PromptName, promptRef } from "./promptRegistry";
import { createReviewQueue } from "./reviewQueue";

export interface LangSmithConfig {
  apiKey: string;
  endpoint: string;
  project: string;
  openrouterApiKey: string;
}

export interface PromptTool<T> {
  name: string;
  description: string;
  schema: z.ZodType<T>;
}

export interface TraceOptions {
  name: string;
  runType?: "chain" | "llm";
  metadata?: Record<string, unknown>;
  finalMeta?: () => Record<string, unknown>;
  model?: { name: string; temperature?: number };
}

export interface TraceContext {
  requireAccepted(): Promise<string>;
}

export interface TraceResult<T> {
  data: T;
  usage?: { input: number; output: number; total: number };
}

type PromptRunnable = Runnable<Record<string, string>, AIMessage>;

interface ReleaseTag {
  name: string;
  commitHash: string;
}

interface ResolvedPromptVersion {
  releaseTags: ReleaseTag[];
}

interface ResolvedPrompt {
  commitHash: string;
  releaseTag: string;
  runnable: PromptRunnable;
}

interface TraceConfigState {
  client: Client;
  project: string;
  prompts: Map<PromptName, ResolvedPrompt>;
  reviewQueue: ReturnType<typeof createReviewQueue>;
}

export interface LangSmithDependencies {
  client?: Client;
  resolvePrompt?: (input: {
    client: Client;
    config: LangSmithConfig;
    name: PromptName;
  }) => Promise<ResolvedPromptVersion>;
  pullPrompt?: (input: {
    client: Client;
    config: LangSmithConfig;
    name: PromptName;
    commitHash: string;
  }) => Promise<PromptRunnable>;
}

let state: TraceConfigState | undefined;

const ReleaseTagsSchema = z.array(z.object({ tag_name: z.string(), commit_hash: z.string() }));

async function resolvePrompt(input: {
  client: Client;
  config: LangSmithConfig;
  name: PromptName;
}): Promise<ResolvedPromptVersion> {
  const response = await fetch(`${input.config.endpoint}/repos/-/${input.name}/tags`, {
    headers: { "x-api-key": input.config.apiKey },
  });
  if (!response.ok)
    throw new Error(`Could not read LangSmith tags for ${input.name}: ${response.status}`);
  const tags = ReleaseTagsSchema.parse(await response.json());
  const releaseTags = tags
    .filter((tag) => /^release-\d{4}-\d{2}-\d{2}-\d+$/.test(tag.tag_name))
    .map((tag) => ({ name: tag.tag_name, commitHash: tag.commit_hash }));
  if (!releaseTags.length) throw new Error(`Prompt ${input.name} has no immutable release tag`);
  return { releaseTags };
}

async function pullPrompt(input: {
  client: Client;
  config: LangSmithConfig;
  name: PromptName;
  commitHash: string;
}): Promise<PromptRunnable> {
  return pull<PromptRunnable>(promptRef(input.name, input.commitHash), {
    client: input.client,
    apiKey: input.config.apiKey,
    apiUrl: input.config.endpoint,
    includeModel: true,
    secrets: { OPENAI_API_KEY: input.config.openrouterApiKey },
    secretsFromEnv: false,
  });
}

export async function initLangSmith(
  cfg: LangSmithConfig,
  deps: LangSmithDependencies = {},
): Promise<void> {
  const client = deps.client ?? new Client({ apiUrl: cfg.endpoint, apiKey: cfg.apiKey });
  const resolve = deps.resolvePrompt ?? resolvePrompt;
  const load = deps.pullPrompt ?? pullPrompt;
  const versions = await Promise.all(
    PROMPT_NAMES.map(async (name) => {
      const version = await resolve({ client, config: cfg, name });
      return [name, version] as const;
    }),
  );
  const commonReleaseTags = versions.reduce(
    (common, [, version]) => {
      for (const releaseTag of common) {
        if (!version.releaseTags.some((tag) => tag.name === releaseTag)) {
          common.delete(releaseTag);
        }
      }
      return common;
    },
    new Set(versions[0]?.[1].releaseTags.map((tag) => tag.name)),
  );
  const releaseTag = [...commonReleaseTags].sort().at(-1);
  if (!releaseTag) throw new Error("Production prompts have mixed releases");
  const resolved = await Promise.all(
    versions.map(async ([name, version]) => {
      const release = version.releaseTags.find((tag) => tag.name === releaseTag);
      if (!release) throw new Error(`Prompt ${name} is missing release ${releaseTag}`);
      const runnable = await load({ client, config: cfg, name, commitHash: release.commitHash });
      return [name, { commitHash: release.commitHash, releaseTag, runnable }] as const;
    }),
  );
  state = {
    client,
    project: cfg.project,
    prompts: new Map(resolved),
    reviewQueue: createReviewQueue(client),
  };
}

function configuredPrompt(name: PromptName): ResolvedPrompt {
  const prompt = state?.prompts.get(name);
  if (!prompt) throw new Error("LangSmith is not initialized");
  return prompt;
}

export function getPromptCommitHash(name: PromptName): string {
  return configuredPrompt(name).commitHash;
}

export function getPromptReleaseTag(): string {
  const prompt = state?.prompts.values().next().value as ResolvedPrompt | undefined;
  if (!prompt) throw new Error("LangSmith is not initialized");
  return prompt.releaseTag;
}

export async function enqueueReviewSnapshot(snapshot: ReviewSnapshot): Promise<void> {
  if (!state) throw new Error("LangSmith is not initialized");
  await state.client.flush();
  await state.reviewQueue.enqueue(snapshot);
}

export async function invokePrompt<T>(input: {
  name: PromptName;
  values: Record<string, string>;
  tool: PromptTool<T>;
}): Promise<TraceResult<T>> {
  const prompt = configuredPrompt(input.name);
  interface ToolOptions extends RunnableConfig {
    tools: Array<{
      type: "function";
      function: { name: string; description: string; parameters: object };
    }>;
    tool_choice: { type: "function"; function: { name: string } };
  }

  const options: ToolOptions = {
    tools: [
      {
        type: "function",
        function: {
          name: input.tool.name,
          description: input.tool.description,
          parameters: z.toJSONSchema(input.tool.schema),
        },
      },
    ],
    tool_choice: { type: "function", function: { name: input.tool.name } },
  };
  const response = await new RunnableBinding({
    bound: prompt.runnable,
    config: {},
    kwargs: options,
  }).invoke(input.values);
  const toolCall = response.tool_calls?.[0];
  if (!toolCall || toolCall.name !== input.tool.name)
    throw new Error(`Prompt ${input.name} returned no ${input.tool.name} tool call`);
  const usage = response.usage_metadata;
  return {
    data: input.tool.schema.parse(toolCall.args),
    usage: usage
      ? { input: usage.input_tokens, output: usage.output_tokens, total: usage.total_tokens }
      : undefined,
  };
}

export function isTracingEnabled(): boolean {
  return state !== undefined;
}
export function shutdownLangSmith(): void {
  state?.client.cleanup();
  state = undefined;
}

function attachUsage<T>(data: T, usage: TraceResult<T>["usage"]): Record<string, unknown> {
  if (typeof data === "object" && data !== null)
    return usage
      ? {
          ...(data as Record<string, unknown>),
          usage_metadata: {
            input_tokens: usage.input,
            output_tokens: usage.output,
            total_tokens: usage.total,
          },
        }
      : (data as Record<string, unknown>);
  return usage
    ? {
        output: data,
        usage_metadata: {
          input_tokens: usage.input,
          output_tokens: usage.output,
          total_tokens: usage.total,
        },
      }
    : { output: data };
}

export async function traced<T>(
  opts: TraceOptions,
  fn: (context: TraceContext) => Promise<TraceResult<T>>,
): Promise<T> {
  if (!state) {
    return (
      await fn({
        requireAccepted: async () => {
          throw new Error("LangSmith is not initialized");
        },
      })
    ).data;
  }
  const configured = state;
  let result: TraceResult<T> | undefined;
  let traceId: string | undefined;
  const wrapped = traceable(
    async (): Promise<Record<string, unknown>> => {
      result = await fn({
        requireAccepted: async () => {
          const startedTraceId = traceId;
          if (!startedTraceId) throw new Error("LangSmith trace did not start");
          await configured.client.flush();
          await withRetry(() => configured.client.readRun(startedTraceId), {
            maxRetries: 2,
            baseDelayMs: 100,
            shouldRetry: isMissingTrace,
          });
          return startedTraceId;
        },
      });
      return attachUsage(result.data, result.usage);
    },
    {
      name: opts.name,
      run_type: opts.runType ?? "chain",
      client: configured.client,
      project_name: configured.project,
      tracingEnabled: true,
      metadata: opts.metadata,
      on_start: (runTree) => {
        if (runTree) traceId = runTree.id;
      },
      getInvocationParams: opts.model
        ? () => ({
            ls_model_type: "chat",
            ls_model_name: opts.model?.name ?? "",
            ls_temperature: opts.model?.temperature,
            ls_provider: "openai",
          })
        : undefined,
      on_end: opts.finalMeta
        ? (runTree) => {
            if (runTree)
              runTree.extra.metadata = { ...runTree.extra.metadata, ...opts.finalMeta?.() };
          }
        : undefined,
    },
  );
  await wrapped();
  if (!result) throw new Error("tracing returned without a result");
  return result.data;
}

function isMissingTrace(error: unknown): boolean {
  return error instanceof Error && "status" in error && error.status === 404;
}

export async function flushPending(): Promise<void> {
  if (state) await state.client.flush();
}
