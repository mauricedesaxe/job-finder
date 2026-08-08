import { afterEach, describe, expect, test } from "bun:test";
import { flushPending, initLangSmith, shutdownLangSmith, traced } from "../langsmith.ts";

function enableTracing() {
  initLangSmith({
    apiKey: "dummy",
    endpoint: "https://127.0.0.1:9",
    project: "test",
  });
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

  test("flushPending resolves when uninitialized", async () => {
    await expect(flushPending()).resolves.toBeUndefined();
  });

  test("returns the caller's data, not the traced output, when tracing is enabled", async () => {
    enableTracing();
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
    enableTracing();
    await expect(
      traced({ name: "evaluate", runType: "llm" }, async () => {
        throw new Error("boom");
      }),
    ).rejects.toThrow("boom");
  });
});
