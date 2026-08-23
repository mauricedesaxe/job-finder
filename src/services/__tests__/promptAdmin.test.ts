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
      deleteTag: async () => {},
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
  test("removes production tags created during a failed first release", async () => {
    const calls: string[] = [];
    const client: PromptTagClient = {
      moveTag: async ({ name, commitId }) => {
        calls.push(`move:${name}:${commitId}`);
        if (name === second) throw new Error("network");
      },
      deleteTag: async ({ name, tag }) => {
        calls.push(`delete:${name}:${tag}`);
      },
    };
    await expect(
      moveProductionAtomically({
        client,
        targets: new Map([
          [first, "new-first"],
          [second, "new-second"],
        ]),
        previous: new Map(),
      }),
    ).rejects.toThrow("network");
    expect(calls).toEqual([
      `move:${first}:new-first`,
      `move:${second}:new-second`,
      `delete:${first}:production`,
      `delete:${second}:production`,
    ]);
  });
  test("rejects mixed production history before moving a tag", async () => {
    const calls: string[] = [];
    const client: PromptTagClient = {
      moveTag: async ({ name }) => {
        calls.push(`move:${name}`);
      },
      deleteTag: async ({ name }) => {
        calls.push(`delete:${name}`);
      },
    };
    await expect(
      moveProductionAtomically({
        client,
        targets: new Map([
          [first, "new-first"],
          [second, "new-second"],
        ]),
        previous: new Map([[first, "old-first"]]),
      }),
    ).rejects.toThrow("partial prior production history");
    expect(calls).toEqual([]);
  });
});
