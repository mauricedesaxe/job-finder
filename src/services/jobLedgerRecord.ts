import type {
  ExcludeCompanyInput,
  PendingJobProjections,
  ProcessedJobOutcome,
  RecordProcessedJobInput,
} from "./jobLedger";

interface ProcessedJobWriteRecord {
  sourceKey: string;
  rawUrl: string | null;
  company: string;
  normalizedCompany: string;
  title: string;
  normalizedTitle: string;
  outcome: ProcessedJobOutcome;
  firstProcessedAt: string;
  lastProcessedAt: string;
  traceId: string | null;
}

interface CompanyExclusionWriteRecord {
  normalizedCompany: string;
  company: string;
  excludedAt: string;
  sourceKey: string | null;
}

export function createProcessedJobWriteRecord(
  input: RecordProcessedJobInput,
): ProcessedJobWriteRecord {
  const processedAt = input.processedAt ?? new Date().toISOString();
  const sourceKey = input.sourceKey ?? sourceKeyForRawUrl(input.rawUrl);

  return {
    sourceKey,
    rawUrl: input.rawUrl ?? null,
    company: input.company,
    normalizedCompany: normalizeJobLedgerText(input.company),
    title: input.title,
    normalizedTitle: normalizeJobLedgerText(input.title),
    outcome: input.outcome,
    firstProcessedAt: processedAt,
    lastProcessedAt: processedAt,
    traceId: input.traceId ?? null,
  };
}

export function createPendingJobProjections(
  sourceKey: string,
  input: RecordProcessedJobInput["projections"],
): PendingJobProjections {
  if (!input) return { kind: "none" };
  switch (input.kind) {
    case "notion":
      return { kind: "notion", notion: { sourceKey, ...input.notion } };
    case "notion-and-review":
      return {
        kind: "notion-and-review",
        notion: { sourceKey, ...input.notion },
        review: { sourceKey, ...input.review },
      };
  }
}

export function createCompanyExclusionWriteRecord(
  input: ExcludeCompanyInput,
): CompanyExclusionWriteRecord {
  return {
    normalizedCompany: normalizeJobLedgerText(input.company),
    company: input.company,
    excludedAt: input.excludedAt ?? new Date().toISOString(),
    sourceKey: input.sourceKey ?? null,
  };
}

export function processedJobWriteValues(
  record: ProcessedJobWriteRecord,
): [
  string,
  string | null,
  string,
  string,
  string,
  string,
  ProcessedJobOutcome,
  string,
  string,
  string | null,
] {
  return [
    record.sourceKey,
    record.rawUrl,
    record.company,
    record.normalizedCompany,
    record.title,
    record.normalizedTitle,
    record.outcome,
    record.firstProcessedAt,
    record.lastProcessedAt,
    record.traceId,
  ];
}

export function companyExclusionWriteValues(
  record: CompanyExclusionWriteRecord,
): [string, string, string, string | null] {
  return [record.normalizedCompany, record.company, record.excludedAt, record.sourceKey];
}

export function normalizeJobLedgerText(value: string): string {
  return value.trim().replace(/\s+/g, " ").toLowerCase();
}

function sourceKeyForRawUrl(rawUrl: string | undefined): string {
  if (!rawUrl) {
    throw new Error("A source key is required when a processed job has no URL");
  }

  return `url:${rawUrl}`;
}
