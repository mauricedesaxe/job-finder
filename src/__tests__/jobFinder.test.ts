import { expect, test } from "bun:test";
import { jobFinderRunMode } from "../jobFinder";

test("selects a scrape run by default", () => {
  expect(jobFinderRunMode(["bun", "src/index.ts"])).toEqual({ kind: "scrape" });
});

test("selects a reconcile run from the CLI flag", () => {
  expect(jobFinderRunMode(["bun", "src/index.ts", "--reconcile-only"])).toEqual({
    kind: "reconcile",
  });
});
