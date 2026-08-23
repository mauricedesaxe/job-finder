import { afterEach, describe, expect, test } from "bun:test";
import { AIMessage } from "@langchain/core/messages";
import { RunnableLambda } from "@langchain/core/runnables";
import {
  flushPending,
  getLocationEligibilityCommitHash,
  getLocationEligibilityRunnable,
  initLangSmith,
  LOCATION_ELIGIBILITY_PROMPT_REF,
  shutdownLangSmith,
  traced,
} from "../langsmith.ts";

const locationRunnable = RunnableLambda.from(
  async () => new AIMessage({ content: "location eligibility" }),
);

async function enableTracing() {
  await initLangSmith(
    {
      apiKey: "dummy",
      endpoint: "https://127.0.0.1:9",
      project: "test",
      openrouterApiKey: "openrouter-dummy",
    },
    {
      resolveLocationEligibilityCommit: async () => "immutable-commit",
      pullLocationEligibilityPrompt: async () => locationRunnable,
    },
  );
}

describe("langsmith adapter", () => {
  afterEach(() => {
    shutdownLangSmith();
  });

  test("returns the traced data when tracing is uninitialized", async () => {
    const result = await traced({ name: "evaluate", runType: "llm" }, async () => ({
      data: { pass: true },
      usage: { input: 5, output: 2, total: 7 },
    }));
    expect(result).toEqual({ pass: true });
  });

  test("returns a scalar value untouched when tracing is uninitialized", async () => {
    const result = await traced({ name: "process_job", runType: "chain" }, async () => ({
      data: "inserted",
      usage: { input: 1, output: 1, total: 2 },
    }));
    expect(result).toBe("inserted");
  });

  test("resolves production once and loads its immutable location runnable", async () => {
    let resolveCount = 0;
    let resolvedPromptRef: string | undefined;
    let received: { endpoint: string; openrouterApiKey: string; promptRef: string } | undefined;
    await initLangSmith(
      {
        apiKey: "langsmith-dummy",
        endpoint: "https://eu.api.smith.langchain.com",
        project: "test",
        openrouterApiKey: "openrouter-dummy",
      },
      {
        resolveLocationEligibilityCommit: async ({ promptRef }) => {
          resolveCount++;
          resolvedPromptRef = promptRef;
          return "immutable-commit";
        },
        pullLocationEligibilityPrompt: async (input) => {
          received = input;
          return locationRunnable;
        },
      },
    );

    expect(LOCATION_ELIGIBILITY_PROMPT_REF).toBe(
      "job-finder-filter-location-eligibility:production",
    );
    expect(resolveCount).toBe(1);
    expect(resolvedPromptRef).toBe(LOCATION_ELIGIBILITY_PROMPT_REF);
    expect(received).toMatchObject({
      endpoint: "https://eu.api.smith.langchain.com",
      promptRef: "job-finder-filter-location-eligibility:immutable-commit",
      openrouterApiKey: "openrouter-dummy",
    });
    expect(getLocationEligibilityRunnable()).toBe(locationRunnable);
    expect(getLocationEligibilityCommitHash()).toBe("immutable-commit");
  });

  test("flushPending resolves when uninitialized", async () => {
    await expect(flushPending()).resolves.toBeUndefined();
  });

  test("returns the caller's data, not the traced output, when tracing is enabled", async () => {
    await enableTracing();
    const scalar = await traced({ name: "process_job", runType: "chain" }, async () => ({
      data: "rejected",
      usage: { input: 1, output: 1, total: 2 },
    }));
    expect(scalar).toBe("rejected");

    const objectData = await traced({ name: "evaluate", runType: "llm" }, async () => ({
      data: { pass: false },
      usage: { input: 1, output: 1, total: 2 },
    }));
    expect(objectData).toEqual({ pass: false });
    expect("usage_metadata" in (objectData as object)).toBe(false);
  });

  test("propagates a throwing fn when tracing is enabled", async () => {
    await enableTracing();
    await expect(
      traced({ name: "evaluate", runType: "llm" }, async () => {
        throw new Error("boom");
      }),
    ).rejects.toThrow("boom");
  });
});
