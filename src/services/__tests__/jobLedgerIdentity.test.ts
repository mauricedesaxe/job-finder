import { expect, test } from "bun:test";
import { normalizeJobLedgerText } from "../jobLedgerIdentity";

test("normalizes ledger identities without the host locale", () => {
  expect(normalizeJobLedgerText(" I\u0130 ")).toBe("ii\u0307");
});
