import { describe, expect, spyOn, test } from "bun:test";
import {
  createNotionClient,
  withNonRetryingNotionCreate,
  withNotionResilience,
} from "../notion/client";

function notionError(status: number): Error {
  return Object.assign(new Error(`notion ${status}`), { status });
}

describe("withNotionResilience", () => {
  test("retries Notion 429s and then resolves", async () => {
    const sleep = spyOn(Bun, "sleep").mockImplementation(() => Promise.resolve());
    let calls = 0;
    const result = await withNotionResilience(async () => {
      calls += 1;
      if (calls < 3) throw notionError(429);
      return "ok";
    });
    expect(result).toBe("ok");
    expect(calls).toBe(3);
    sleep.mockRestore();
  });

  test("invokes page creation once on a retryable error", async () => {
    const sleep = spyOn(Bun, "sleep").mockImplementation(() => Promise.resolve());
    let calls = 0;
    const run = withNonRetryingNotionCreate(async () => {
      calls += 1;
      throw notionError(429);
    });

    await expect(run).rejects.toMatchObject({ status: 429 });
    expect(calls).toBe(1);
    expect(sleep).not.toHaveBeenCalled();
    sleep.mockRestore();
  });

  test("does not retry a non-retryable Notion error", async () => {
    let calls = 0;
    const run = withNotionResilience(async () => {
      calls += 1;
      throw notionError(400);
    });
    await expect(run).rejects.toMatchObject({ status: 400 });
    expect(calls).toBe(1);
  });
});

describe("createNotionClient", () => {
  test("uses the runtime fetch implementation", async () => {
    const runtimeFetch = spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ object: "database", id: "database-id" }),
    );

    try {
      const client = createNotionClient("token");
      await client.databases.retrieve({ database_id: "database-id" });

      expect(runtimeFetch).toHaveBeenCalledTimes(1);
    } finally {
      runtimeFetch.mockRestore();
    }
  });
});
