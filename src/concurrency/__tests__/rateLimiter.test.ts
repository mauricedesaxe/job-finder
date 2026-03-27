import { test, expect, describe } from "bun:test";
import { RateLimiter } from "../rateLimiter";

describe("RateLimiter", () => {
  test("allows burst up to max", async () => {
    const limiter = new RateLimiter(10, 3);
    const results: number[] = [];

    // 3 burst calls should be near-instant
    const start = Date.now();
    await Promise.all([
      limiter.run(async () => results.push(1)),
      limiter.run(async () => results.push(2)),
      limiter.run(async () => results.push(3)),
    ]);
    const elapsed = Date.now() - start;

    expect(results).toHaveLength(3);
    expect(elapsed).toBeLessThan(200);
  });

  test("throttles beyond burst", async () => {
    const limiter = new RateLimiter(10, 1); // 1 token burst, 10/s refill
    const results: number[] = [];

    // First call instant, second must wait for refill
    const start = Date.now();
    await limiter.run(async () => results.push(1));
    await limiter.run(async () => results.push(2));
    const elapsed = Date.now() - start;

    expect(results).toEqual([1, 2]);
    // Second call should wait ~100ms (1 token / 10 per sec)
    expect(elapsed).toBeGreaterThanOrEqual(50);
  });

  test("run returns the function result", async () => {
    const limiter = new RateLimiter(10, 3);
    const result = await limiter.run(async () => 42);
    expect(result).toBe(42);
  });
});
