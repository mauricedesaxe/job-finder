import type { AIMessage } from "@langchain/core/messages";
import type { Runnable } from "@langchain/core/runnables";
import { pull } from "langchain/hub/node";
import { Client } from "langsmith";
import { traceable } from "langsmith/traceable";

export const LOCATION_ELIGIBILITY_PROMPT_NAME = "job-finder-filter-location-eligibility";
export const LOCATION_ELIGIBILITY_PROMPT_REF = `${LOCATION_ELIGIBILITY_PROMPT_NAME}:production`;

export type LocationEligibilityRunnable = Runnable<{ job: string }, AIMessage>;

export interface LangSmithConfig {
  apiKey: string;
  endpoint: string;
  project: string;
  openrouterApiKey: string;
}

export interface TraceOptions {
  name: string;
  runType?: "chain" | "llm";
  metadata?: Record<string, unknown>;
  /** Metadata resolved after the traced work completes (outcome, retries, ...). */
  finalMeta?: () => Record<string, unknown>;
  /** Attach model identity so LangSmith can price cost from token usage. */
  model?: { name: string; temperature?: number };
}

export interface TraceResult<T> {
  data: T;
  usage?: { input: number; output: number; total: number };
}

interface TraceConfigState {
  client: Client;
  project: string;
  locationEligibilityCommitHash: string;
  locationEligibilityRunnable: LocationEligibilityRunnable;
}

export interface LangSmithDependencies {
  resolveLocationEligibilityCommit?: (input: {
    client: Client;
    promptRef: string;
  }) => Promise<string>;
  pullLocationEligibilityPrompt?: (input: {
    client: Client;
    apiKey: string;
    endpoint: string;
    openrouterApiKey: string;
    promptRef: string;
  }) => Promise<LocationEligibilityRunnable>;
}

let state: TraceConfigState | undefined;

async function resolveLocationEligibilityCommit(input: {
  client: Client;
  promptRef: string;
}): Promise<string> {
  const prompt = await input.client.pullPromptCommit(input.promptRef);
  return prompt.commit_hash;
}

async function pullLocationEligibilityPrompt(input: {
  client: Client;
  apiKey: string;
  endpoint: string;
  openrouterApiKey: string;
  promptRef: string;
}): Promise<LocationEligibilityRunnable> {
  return pull<LocationEligibilityRunnable>(input.promptRef, {
    client: input.client,
    apiKey: input.apiKey,
    apiUrl: input.endpoint,
    includeModel: true,
    secrets: { OPENAI_API_KEY: input.openrouterApiKey },
    secretsFromEnv: false,
  });
}

export async function initLangSmith(
  cfg: LangSmithConfig,
  deps: LangSmithDependencies = {},
): Promise<void> {
  const client = new Client({ apiUrl: cfg.endpoint, apiKey: cfg.apiKey });
  const resolveCommit = deps.resolveLocationEligibilityCommit ?? resolveLocationEligibilityCommit;
  const loadPrompt = deps.pullLocationEligibilityPrompt ?? pullLocationEligibilityPrompt;
  const locationEligibilityCommitHash = await resolveCommit({
    client,
    promptRef: LOCATION_ELIGIBILITY_PROMPT_REF,
  });
  const locationEligibilityRunnable = await loadPrompt({
    client,
    apiKey: cfg.apiKey,
    endpoint: cfg.endpoint,
    openrouterApiKey: cfg.openrouterApiKey,
    promptRef: `${LOCATION_ELIGIBILITY_PROMPT_NAME}:${locationEligibilityCommitHash}`,
  });

  state = {
    client,
    project: cfg.project,
    locationEligibilityCommitHash,
    locationEligibilityRunnable,
  };
}

export function getLocationEligibilityRunnable(): LocationEligibilityRunnable {
  if (!state) throw new Error("LangSmith is not initialized");
  return state.locationEligibilityRunnable;
}

export function getLocationEligibilityCommitHash(): string {
  if (!state) throw new Error("LangSmith is not initialized");
  return state.locationEligibilityCommitHash;
}

export function isTracingEnabled(): boolean {
  return state !== undefined;
}

export function shutdownLangSmith(): void {
  state?.client.cleanup();
  state = undefined;
}

function attachUsage<T>(
  data: T,
  usage: { input: number; output: number; total: number } | undefined,
): Record<string, unknown> {
  if (typeof data === "object" && data !== null) {
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
  }
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

/**
 * Run `fn` inside a LangSmith span named `opts.name`, nesting under any active
 * span. Token usage returned by `fn` is attached to the span so it rolls up to
 * the root. A no-op (calls `fn`, returns `.data`) until `initLangSmith` runs,
 * so the pipeline stays testable without a LangSmith workspace.
 */
export async function traced<T>(opts: TraceOptions, fn: () => Promise<TraceResult<T>>): Promise<T> {
  const { name, runType, metadata, finalMeta, model } = opts;
  if (!state) {
    const result = await fn();
    return result.data;
  }

  let result: TraceResult<T> | undefined;
  const wrapped = traceable(
    async (): Promise<Record<string, unknown>> => {
      result = await fn();
      return attachUsage(result.data, result.usage);
    },
    {
      name,
      run_type: runType ?? "chain",
      client: state.client,
      project_name: state.project,
      tracingEnabled: true,
      metadata,
      getInvocationParams: model
        ? () => ({
            ls_model_type: "chat",
            ls_model_name: model.name,
            ls_temperature: model.temperature,
            ls_provider: "openai",
          })
        : undefined,
      on_end: finalMeta
        ? (runTree) => {
            if (runTree) {
              runTree.extra.metadata = { ...runTree.extra.metadata, ...finalMeta() };
            }
          }
        : undefined,
    },
  );

  await wrapped();
  if (result === undefined) {
    throw new Error("tracing returned without a result");
  }
  return result.data;
}

/** Drain every pending trace batch so runs reach LangSmith before exit. */
export async function flushPending(): Promise<void> {
  if (!state) return;
  await state.client.flush();
}
