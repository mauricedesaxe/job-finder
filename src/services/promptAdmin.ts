import { z } from "zod/v4";
import type { PromptName } from "./promptRegistry";

const UnchangedPromptMessage = "Nothing to commit: prompt has not changed since latest commit";

export function isUnchangedPromptConflict(error: unknown): boolean {
  return (
    error instanceof Error &&
    error.message.includes("409") &&
    error.message.includes(UnchangedPromptMessage)
  );
}

export function isMissingPromptReference(error: unknown): boolean {
  return error instanceof Error && error.message.includes(": 404");
}

export interface PromptAdminConfig {
  apiKey: string;
  endpoint: string;
}
export interface PromptTagClient {
  moveTag(input: { name: PromptName; tag: string; commitId: string }): Promise<void>;
  deleteTag(input: { name: PromptName; tag: string }): Promise<void>;
}

const CommitSchema = z.object({ id: z.string().optional(), commit_hash: z.string() });
const CommitResponseSchema = z.union([CommitSchema, z.object({ commit: CommitSchema })]);
const CommitListSchema = z.object({ commits: z.array(CommitSchema) });

export async function resolvePromptCommit(input: {
  config: PromptAdminConfig;
  name: PromptName;
  ref: string;
}): Promise<{ id: string; hash: string }> {
  const response = await fetch(`${input.config.endpoint}/commits/-/${input.name}/${input.ref}`, {
    headers: { "x-api-key": input.config.apiKey },
  });
  if (!response.ok)
    throw new Error(`Could not resolve ${input.ref} for ${input.name}: ${response.status}`);
  const parsed = CommitResponseSchema.parse(await response.json());
  const commit = "commit" in parsed ? parsed.commit : parsed;
  if (commit.id) return { id: commit.id, hash: commit.commit_hash };

  const commitsResponse = await fetch(
    `${input.config.endpoint}/commits/-/${input.name}?limit=100`,
    {
      headers: { "x-api-key": input.config.apiKey },
    },
  );
  if (!commitsResponse.ok)
    throw new Error(`Could not list commits for ${input.name}: ${commitsResponse.status}`);
  const matchingCommit = CommitListSchema.parse(await commitsResponse.json()).commits.find(
    (candidate) => candidate.commit_hash === commit.commit_hash,
  );
  if (!matchingCommit?.id) throw new Error(`Could not resolve commit ID for ${input.name}`);
  return { id: matchingCommit.id, hash: matchingCommit.commit_hash };
}

export type ProductionHistory =
  | { kind: "first-release" }
  | { kind: "established" }
  | { kind: "bootstrap" };

export function resolveProductionHistory(input: {
  targets: ReadonlyMap<PromptName, string>;
  previous: ReadonlyMap<PromptName, string>;
  bootstrap?: boolean;
}): ProductionHistory {
  if (input.previous.size === 0) return { kind: "first-release" };
  const missingPriorTags = [...input.targets.keys()].filter((name) => !input.previous.has(name));
  if (missingPriorTags.length > 0 || input.previous.size !== input.targets.size)
    return input.bootstrap
      ? { kind: "bootstrap" }
      : (() => {
          throw new Error("Cannot promote with partial prior production history");
        })();
  return { kind: "established" };
}

export function createPromptTagClient(config: PromptAdminConfig): PromptTagClient {
  return {
    async moveTag({ name, tag, commitId }) {
      const response = await fetch(`${config.endpoint}/repos/-/${name}/tags`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-api-key": config.apiKey },
        body: JSON.stringify({ tag_name: tag, commit_id: commitId }),
      });
      const body = await response.text();
      if (response.status === 409 && body.includes("already exists on commit")) return;
      if (!response.ok)
        throw new Error(`Could not move ${tag} for ${name}: ${response.status} ${body}`);
    },
    async deleteTag({ name, tag }) {
      const response = await fetch(
        `${config.endpoint}/repos/-/${name}/tags/${encodeURIComponent(tag)}`,
        {
          method: "DELETE",
          headers: { "x-api-key": config.apiKey },
        },
      );
      if (!response.ok) throw new Error(`Could not delete ${tag} for ${name}: ${response.status}`);
    },
  };
}

export async function moveProductionAtomically(input: {
  client: PromptTagClient;
  targets: ReadonlyMap<PromptName, string>;
  previous: ReadonlyMap<PromptName, string>;
  bootstrap?: boolean;
}): Promise<void> {
  const history = resolveProductionHistory(input);
  const attempted: PromptName[] = [];
  try {
    for (const [name, commitId] of input.targets) {
      attempted.push(name);
      await input.client.moveTag({ name, tag: "production", commitId });
    }
  } catch (error) {
    const restores =
      history.kind === "first-release"
        ? attempted.map((name) => input.client.deleteTag({ name, tag: "production" }))
        : attempted.map(async (name) => {
            const commitId = input.previous.get(name);
            if (commitId) {
              await input.client.moveTag({ name, tag: "production", commitId });
            } else {
              await input.client.deleteTag({ name, tag: "production" });
            }
          });
    const settled = await Promise.allSettled(restores);
    const failedRestores = settled.filter((result) => result.status === "rejected");
    if (failedRestores.length)
      throw new Error(
        `Production tag move failed and ${failedRestores.length} compensation move(s) failed`,
        { cause: error },
      );
    throw error;
  }
}
