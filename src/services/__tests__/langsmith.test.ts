import { afterEach, describe, expect, test } from "bun:test";
import { AIMessage } from "@langchain/core/messages";
import { RunnableLambda } from "@langchain/core/runnables";
import {
  getPromptCommitHash,
  getPromptReleaseTag,
  initLangSmith,
  shutdownLangSmith,
} from "../langsmith";
import { PROMPT_NAMES } from "../promptRegistry";

describe("LangSmith prompt resolution", () => {
  afterEach(shutdownLangSmith);
  test("freezes every production prompt at one release", async () => {
    await initLangSmith(
      {
        apiKey: "key",
        endpoint: "https://example.test",
        project: "test",
        openrouterApiKey: "router",
      },
      {
        resolvePrompt: async ({ name }) => ({
          commitHash: `${name}-commit`,
          releaseTags: ["release-2026-08-23-1"],
        }),
        pullPrompt: async () => RunnableLambda.from(async () => new AIMessage({ content: "" })),
      },
    );
    expect(getPromptReleaseTag()).toBe("release-2026-08-23-1");
    expect(getPromptCommitHash(PROMPT_NAMES[0])).toBe(`${PROMPT_NAMES[0]}-commit`);
  });
  test("rejects a mixed production release before work starts", async () => {
    await expect(
      initLangSmith(
        {
          apiKey: "key",
          endpoint: "https://example.test",
          project: "test",
          openrouterApiKey: "router",
        },
        {
          resolvePrompt: async ({ name }) => ({
            commitHash: `${name}-commit`,
            releaseTags: [
              name === PROMPT_NAMES[0] ? "release-2026-08-23-1" : "release-2026-08-22-1",
            ],
          }),
          pullPrompt: async () => RunnableLambda.from(async () => new AIMessage({ content: "" })),
        },
      ),
    ).rejects.toThrow("mixed releases");
  });

  test("selects the common release when unchanged prompts retain older tags", async () => {
    await initLangSmith(
      {
        apiKey: "key",
        endpoint: "https://example.test",
        project: "test",
        openrouterApiKey: "router",
      },
      {
        resolvePrompt: async ({ name }) => ({
          commitHash: `${name}-commit`,
          releaseTags:
            name === PROMPT_NAMES[0]
              ? ["release-2026-08-22-1", "release-2026-08-23-1"]
              : ["release-2026-08-23-1"],
        }),
        pullPrompt: async () => RunnableLambda.from(async () => new AIMessage({ content: "" })),
      },
    );

    expect(getPromptReleaseTag()).toBe("release-2026-08-23-1");
  });
});
