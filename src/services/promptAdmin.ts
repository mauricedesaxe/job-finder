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

export interface PromptAdminConfig {
  apiKey: string;
  endpoint: string;
}
export interface PromptTagClient {
  moveTag(input: { name: PromptName; tag: string; commitId: string }): Promise<void>;
}

const CommitSchema = z.object({ id: z.string(), commit_hash: z.string() });

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
  const commit = CommitSchema.parse(await response.json());
  return { id: commit.id, hash: commit.commit_hash };
}

export function createPromptTagClient(config: PromptAdminConfig): PromptTagClient {
  return {
    async moveTag({ name, tag, commitId }) {
      const response = await fetch(`${config.endpoint}/repos/-/${name}/tags`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-api-key": config.apiKey },
        body: JSON.stringify({ tag_name: tag, commit_id: commitId }),
      });
      if (!response.ok) throw new Error(`Could not move ${tag} for ${name}: ${response.status}`);
    },
  };
}

export async function moveProductionAtomically(input: {
  client: PromptTagClient;
  targets: ReadonlyMap<PromptName, string>;
  previous: ReadonlyMap<PromptName, string>;
}): Promise<void> {
  const attempted: PromptName[] = [];
  try {
    for (const [name, commitId] of input.targets) {
      attempted.push(name);
      await input.client.moveTag({ name, tag: "production", commitId });
    }
  } catch (error) {
    const restores = attempted.map(async (name) => {
      const commitId = input.previous.get(name);
      if (!commitId) throw new Error(`Missing prior production commit for ${name}`);
      await input.client.moveTag({ name, tag: "production", commitId });
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
