import { expect, test } from "bun:test";
import {
  createCompanyExclusionWriteRecord,
  createProcessedJobWriteRecord,
  normalizeJobLedgerText,
} from "../jobLedgerRecord";

test("normalizes ledger identities without the host locale", () => {
  expect(normalizeJobLedgerText(" I\u0130 ")).toBe("ii\u0307");
});

test("creates stable processed job write records", () => {
  const record = createProcessedJobWriteRecord({
    rawUrl: "https://jobs.example.com/role?id=1",
    company: "  ACME   Labs ",
    title: " Senior   Engineer ",
    outcome: "inserted",
  });

  expect(record).toMatchObject({
    sourceKey: "url:https://jobs.example.com/role?id=1",
    rawUrl: "https://jobs.example.com/role?id=1",
    normalizedCompany: "acme labs",
    normalizedTitle: "senior engineer",
    traceId: null,
  });
  expect(record.firstProcessedAt).toBe(record.lastProcessedAt);
  expect(Number.isNaN(Date.parse(record.firstProcessedAt))).toBe(false);
});

test("converts optional exclusion fields to stored values", () => {
  expect(
    createCompanyExclusionWriteRecord({
      company: "Acme",
      excludedAt: "2026-08-22T10:00:00.000Z",
    }),
  ).toEqual({
    normalizedCompany: "acme",
    company: "Acme",
    excludedAt: "2026-08-22T10:00:00.000Z",
    sourceKey: null,
  });
});
