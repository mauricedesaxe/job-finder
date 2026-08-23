import { describe, expect, test } from "bun:test";
import {
  isUnchangedPromptConflict,
  moveProductionAtomically,
  type PromptTagClient,
} from "../promptAdmin";
import type { PromptName } from "../promptRegistry";

const first = "job-finder-filter-location-eligibility" as PromptName;
const second = "job-finder-enrichment" as PromptName;
describe("prompt administration", () => {
  test("accepts only the known unchanged prompt conflict", () => {
    expect(
      isUnchangedPromptConflict(
        new Error("409 Nothing to commit: prompt has not changed since latest commit"),
      ),
    ).toBe(true);
    expect(isUnchangedPromptConflict(new Error("409 conflict"))).toBe(false);
  });
  test("restores every moved production tag when promotion fails", async () => {
    const moves: string[] = [];
    const client: PromptTagClient = {
      moveTag: async ({ name, commitId }) => {
        moves.push(`${name}:${commitId}`);
        if (name === second && commitId === "new-second") throw new Error("network");
      },
    };
    await expect(
      moveProductionAtomically({
        client,
        targets: new Map([
          [first, "new-first"],
          [second, "new-second"],
        ]),
        previous: new Map([
          [first, "old-first"],
          [second, "old-second"],
        ]),
      }),
    ).rejects.toThrow("network");
    expect(moves).toEqual([
      `${first}:new-first`,
      `${second}:new-second`,
      `${first}:old-first`,
      `${second}:old-second`,
    ]);
  });
});
