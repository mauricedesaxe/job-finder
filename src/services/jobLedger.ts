export const PROCESSED_JOB_OUTCOMES = [
  "historical",
  "inserted",
  "rejected",
  "duplicated",
  "companyApplied",
  "archived",
] as const;

export type ProcessedJobOutcome = (typeof PROCESSED_JOB_OUTCOMES)[number];

export interface ProcessedJob {
  sourceKey: string;
  rawUrl: string | null;
  company: string;
  title: string;
  outcome: ProcessedJobOutcome;
  firstProcessedAt: string;
  lastProcessedAt: string;
  traceId: string | null;
}

export interface CompanyExclusion {
  company: string;
  excludedAt: string;
}

export interface RecordProcessedJobInput {
  rawUrl?: string;
  sourceKey?: string;
  company: string;
  title: string;
  outcome: ProcessedJobOutcome;
  processedAt?: string;
  traceId?: string;
}

export interface ExcludeCompanyInput {
  company: string;
  excludedAt?: string;
  sourceKey?: string;
}

export interface NotionBackfillStats {
  sourceRows: number;
  urls: number;
  companyTitlePairs: number;
  urlLessRows: number;
  exclusions: number;
}

export interface JobLedger {
  findByRawUrl(rawUrl: string): Promise<ProcessedJob | null>;
  titlesForCompany(company: string): Promise<string[]>;
  findCompanyExclusion(company: string): Promise<CompanyExclusion | null>;
  recordProcessedJob(input: RecordProcessedJobInput): Promise<void>;
  excludeCompany(input: ExcludeCompanyInput): Promise<void>;
  notionBackfillStats(): Promise<NotionBackfillStats>;
  markMigration(name: string, completedAt: string): Promise<void>;
  hasMigration(name: string): Promise<boolean>;
}
