import { afterEach, describe, expect, spyOn, test } from "bun:test";
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

  test("resolves release tags from the repository tag array", async () => {
    const fetch = spyOn(globalThis, "fetch");
    for (const _ of PROMPT_NAMES) {
      fetch.mockResolvedValueOnce(
        new Response(
          JSON.stringify([{ tag_name: "release-2026-08-23-1", commit_hash: "release-commit" }]),
        ),
      );
    }

    try {
      await initLangSmith(
        {
          apiKey: "key",
          endpoint: "https://example.test",
          project: "test",
          openrouterApiKey: "router",
        },
        {
          pullPrompt: async () => RunnableLambda.from(async () => new AIMessage({ content: "" })),
        },
      );

      expect(getPromptReleaseTag()).toBe("release-2026-08-23-1");
      expect(getPromptCommitHash(PROMPT_NAMES[0])).toBe("release-commit");
    } finally {
      fetch.mockRestore();
    }
  });

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
          releaseTags: [{ name: "release-2026-08-23-1", commitHash: `${name}-commit` }],
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
            releaseTags: [
              {
                name: name === PROMPT_NAMES[0] ? "release-2026-08-23-1" : "release-2026-08-22-1",
                commitHash: `${name}-commit`,
              },
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
          releaseTags:
            name === PROMPT_NAMES[0]
              ? [
                  { name: "release-2026-08-22-1", commitHash: `${name}-old-commit` },
                  { name: "release-2026-08-23-1", commitHash: `${name}-commit` },
                ]
              : [{ name: "release-2026-08-23-1", commitHash: `${name}-commit` }],
        }),
        pullPrompt: async () => RunnableLambda.from(async () => new AIMessage({ content: "" })),
      },
    );

    expect(getPromptReleaseTag()).toBe("release-2026-08-23-1");
  });
});
