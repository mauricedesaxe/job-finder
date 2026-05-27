import { describe, expect, test } from "bun:test";
import { daysAgo, monthsAgo } from "../dates";

describe("monthsAgo", () => {
  test("subtracts calendar months from the given date", () => {
    const from = new Date("2026-05-27T00:00:00.000Z");
    expect(monthsAgo(6, from).toISOString().split("T")[0]).toBe("2025-11-27");
  });

  test("does not mutate the input date", () => {
    const from = new Date("2026-05-27T00:00:00.000Z");
    monthsAgo(6, from);
    expect(from.toISOString().split("T")[0]).toBe("2026-05-27");
  });
});

describe("daysAgo", () => {
  test("subtracts days from the given date", () => {
    const from = new Date("2026-05-27T00:00:00.000Z");
    expect(daysAgo(30, from).toISOString().split("T")[0]).toBe("2026-04-27");
  });

  test("does not mutate the input date", () => {
    const from = new Date("2026-05-27T00:00:00.000Z");
    daysAgo(30, from);
    expect(from.toISOString().split("T")[0]).toBe("2026-05-27");
  });
});
