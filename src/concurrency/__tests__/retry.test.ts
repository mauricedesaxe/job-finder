import { describe, expect, test } from "bun:test";
import { isRetryableJina, isRetryableLLM, isRetryableNotion, withRetry } from "../retry";

describe("withRetry", () => {
  test("returns on first success", async () => {
    let calls = 0;
    const result = await withRetry(async () => {
      calls++;
      return "ok";
    });
    expect(result).toBe("ok");
    expect(calls).toBe(1);
  });

  test("retries on retryable error", async () => {
    let calls = 0;
    const result = await withRetry(
      async () => {
        calls++;
        if (calls < 3) throw { status: 429 };
        return "ok";
      },
      {
        maxRetries: 3,
        baseDelayMs: 1,
        shouldRetry: (err) =>
          !!(
            err &&
            typeof err === "object" &&
            "status" in err &&
            (err as { status: number }).status === 429
          ),
      },
    );
    expect(result).toBe("ok");
    expect(calls).toBe(3);
  });

  test("throws after exhausting retries", async () => {
    let calls = 0;
    await expect(
      withRetry(
        async () => {
          calls++;
          throw { status: 429 };
        },
        {
          maxRetries: 2,
          baseDelayMs: 1,
          shouldRetry: () => true,
        },
      ),
    ).rejects.toEqual({ status: 429 });
    expect(calls).toBe(3); // initial + 2 retries
  });

  test("does not retry non-retryable errors", async () => {
    let calls = 0;
    await expect(
      withRetry(
        async () => {
          calls++;
          throw new Error("permanent");
        },
        { shouldRetry: () => false },
      ),
    ).rejects.toThrow("permanent");
    expect(calls).toBe(1);
  });

  test("calls onRetry callback", async () => {
    const retries: number[] = [];
    await withRetry(
      async () => {
        if (retries.length < 2) throw { status: 429 };
        return "ok";
      },
      {
        maxRetries: 3,
        baseDelayMs: 1,
        shouldRetry: () => true,
        onRetry: (attempt) => retries.push(attempt),
      },
    );
    expect(retries).toEqual([1, 2]);
  });
});

describe("shouldRetry predicates", () => {
  test("isRetryableJina", () => {
    expect(isRetryableJina({ status: 429 })).toBe(true);
    expect(isRetryableJina({ status: 500 })).toBe(true);
    expect(isRetryableJina({ status: 503 })).toBe(true);
    expect(isRetryableJina({ status: 404 })).toBe(false);
    expect(isRetryableJina(new Error("network"))).toBe(false);
  });

  test("isRetryableLLM", () => {
    expect(isRetryableLLM({ status: 429 })).toBe(true);
    expect(isRetryableLLM({ status: 500 })).toBe(true);
    expect(isRetryableLLM({ status: 502 })).toBe(true);
    expect(isRetryableLLM({ status: 503 })).toBe(true);
    expect(isRetryableLLM({ status: 400 })).toBe(false);
    expect(isRetryableLLM({ status: 529 })).toBe(false);
  });

  test("isRetryableNotion", () => {
    expect(isRetryableNotion({ status: 429 })).toBe(true);
    expect(isRetryableNotion({ status: 502 })).toBe(true);
    expect(isRetryableNotion({ status: 503 })).toBe(true);
    expect(isRetryableNotion({ status: 401 })).toBe(false);
  });
});
