import { describe, expect, spyOn, test } from "bun:test";
import { withNotionResilience } from "../notion/client";

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
