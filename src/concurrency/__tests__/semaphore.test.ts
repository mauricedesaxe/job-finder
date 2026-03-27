import { test, expect, describe } from "bun:test";
import { Semaphore } from "../semaphore";

describe("Semaphore", () => {
  test("allows up to limit concurrent runs", async () => {
    const sem = new Semaphore(2);
    let active = 0;
    let maxActive = 0;

    const task = () =>
      sem.run(async () => {
        active++;
        maxActive = Math.max(maxActive, active);
        await Bun.sleep(10);
        active--;
      });

    await Promise.allSettled([task(), task(), task(), task()]);
    expect(maxActive).toBe(2);
    expect(active).toBe(0);
  });

  test("processes all tasks", async () => {
    const sem = new Semaphore(2);
    const results: number[] = [];

    const tasks = [1, 2, 3, 4, 5].map((n) =>
      sem.run(async () => {
        results.push(n);
        return n;
      }),
    );

    const values = await Promise.all(tasks);
    expect(values).toEqual([1, 2, 3, 4, 5]);
    expect(results).toHaveLength(5);
  });

  test("releases on error", async () => {
    const sem = new Semaphore(1);

    try {
      await sem.run(async () => {
        throw new Error("fail");
      });
    } catch {}

    // Should still be able to acquire after error
    const result = await sem.run(async () => "ok");
    expect(result).toBe("ok");
  });
});
