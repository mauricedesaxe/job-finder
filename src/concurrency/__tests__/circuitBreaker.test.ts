import { test, expect, describe } from "bun:test";
import { CircuitBreaker, CircuitBreakerOpenError } from "../circuitBreaker";

describe("CircuitBreaker", () => {
  test("passes through when closed", async () => {
    const cb = new CircuitBreaker(3, 1000);
    const result = await cb.run(async () => "ok");
    expect(result).toBe("ok");
  });

  test("opens after failure threshold", async () => {
    const cb = new CircuitBreaker(3, 1000);

    for (let i = 0; i < 3; i++) {
      try {
        await cb.run(async () => {
          throw new Error("fail");
        });
      } catch {}
    }

    // Circuit should now be open
    await expect(cb.run(async () => "ok")).rejects.toBeInstanceOf(
      CircuitBreakerOpenError,
    );
  });

  test("resets on success", async () => {
    const cb = new CircuitBreaker(3, 1000);

    // 2 failures (below threshold)
    for (let i = 0; i < 2; i++) {
      try {
        await cb.run(async () => {
          throw new Error("fail");
        });
      } catch {}
    }

    // Success resets counter
    await cb.run(async () => "ok");

    // 2 more failures should not open (counter was reset)
    for (let i = 0; i < 2; i++) {
      try {
        await cb.run(async () => {
          throw new Error("fail");
        });
      } catch {}
    }

    const result = await cb.run(async () => "still works");
    expect(result).toBe("still works");
  });

  test("transitions to half-open after cooldown", async () => {
    const cb = new CircuitBreaker(2, 50); // 50ms cooldown

    // Open the circuit
    for (let i = 0; i < 2; i++) {
      try {
        await cb.run(async () => {
          throw new Error("fail");
        });
      } catch {}
    }

    // Should be open
    await expect(cb.run(async () => "ok")).rejects.toBeInstanceOf(
      CircuitBreakerOpenError,
    );

    // Wait for cooldown
    await Bun.sleep(60);

    // Should allow one attempt (half-open)
    const result = await cb.run(async () => "recovered");
    expect(result).toBe("recovered");
  });

  test("re-opens if half-open attempt fails", async () => {
    const cb = new CircuitBreaker(2, 50);

    // Open the circuit
    for (let i = 0; i < 2; i++) {
      try {
        await cb.run(async () => {
          throw new Error("fail");
        });
      } catch {}
    }

    await Bun.sleep(60);

    // Half-open attempt fails
    try {
      await cb.run(async () => {
        throw new Error("still failing");
      });
    } catch {}

    // Should be open again
    await expect(cb.run(async () => "ok")).rejects.toBeInstanceOf(
      CircuitBreakerOpenError,
    );
  });
});
