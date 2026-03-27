import { describe, expect, test } from "bun:test";
import { checkFuzzyDuplicate, type DedupResult } from "../dedup";

describe("dedup module exports", () => {
  test("exports checkFuzzyDuplicate function", () => {
    expect(typeof checkFuzzyDuplicate).toBe("function");
  });

  test("DedupResult interface shape is correct", () => {
    const result: DedupResult = { isDuplicate: true, matchedTitle: "test" };
    expect(result.isDuplicate).toBe(true);
    expect(result.matchedTitle).toBe("test");
  });
});

describe("checkFuzzyDuplicate short-circuit", () => {
  test("returns not duplicate for empty existing titles", async () => {
    const result = await checkFuzzyDuplicate("Senior Engineer", [], "fake-key");
    expect(result.isDuplicate).toBe(false);
  });

  test("detects exact case-insensitive match without LLM", async () => {
    const result = await checkFuzzyDuplicate(
      "Senior Backend Engineer",
      ["senior backend engineer", "Staff Frontend Engineer"],
      "fake-key",
    );
    expect(result.isDuplicate).toBe(true);
    expect(result.matchedTitle).toBe("senior backend engineer");
  });

  test("detects match with surrounding whitespace", async () => {
    const result = await checkFuzzyDuplicate(
      "  Senior Backend Engineer  ",
      ["Senior Backend Engineer"],
      "fake-key",
    );
    expect(result.isDuplicate).toBe(true);
  });
});
