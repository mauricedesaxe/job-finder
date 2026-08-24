import type {
  CompanyExclusion,
  JobLedger,
  NotionBackfillStats,
  PendingNotionProjection,
  ProcessedJob,
} from "../jobLedger";

interface JobLedgerConformanceResult {
  exactJob: ProcessedJob | null;
  exactMiss: ProcessedJob | null;
  clearedRawUrl: boolean;
  updatedJob: ProcessedJob | null;
  titles: string[];
  urlLessTitles: string[];
  exclusion: CompanyExclusion | null;
  notionStats: NotionBackfillStats;
  hasMigration: boolean;
  missingSourceKeyError: string;
  pendingBeforeComplete: PendingNotionProjection[];
  pendingAfterOrdinaryUpsert: PendingNotionProjection[];
  pendingAfterComplete: PendingNotionProjection[];
  duplicatePending: PendingNotionProjection[];
}

export const JOB_LEDGER_CONFORMANCE_RESULT: JobLedgerConformanceResult = {
  exactJob: {
    sourceKey: "url:https://jobs.example.com/role?id=1",
    rawUrl: "https://jobs.example.com/role?id=1",
    company: "Exact Co",
    title: "Exact Engineer",
    outcome: "inserted",
    firstProcessedAt: "2026-08-22T09:00:00.000Z",
    lastProcessedAt: "2026-08-22T09:00:00.000Z",
    traceId: "trace-exact",
  },
  exactMiss: null,
  clearedRawUrl: true,
  updatedJob: {
    sourceKey: "source:mutable",
    rawUrl: "https://jobs.example.com/new",
    company: "Acme Labs",
    title: "Senior Engineer",
    outcome: "archived",
    firstProcessedAt: "2026-08-22T10:00:00.000Z",
    lastProcessedAt: "2026-08-22T12:00:00.000Z",
    traceId: null,
  },
  titles: ["Alpha Engineer", "alpha engineer", "Product Engineer", "Senior Engineer"],
  urlLessTitles: ["Legacy Engineer"],
  exclusion: {
    company: "  Acme   Labs ",
    excludedAt: "2026-08-22T10:00:00.000Z",
  },
  notionStats: {
    sourceRows: 2,
    urls: 1,
    companyTitlePairs: 1,
    urlLessRows: 1,
    exclusions: 1,
  },
  hasMigration: true,
  missingSourceKeyError: "A source key is required when a processed job has no URL",
  pendingBeforeComplete: [
    {
      sourceKey: "source:pending",
      job: {
        title: "Pending Engineer",
        company: "Pending Co",
        url: "https://jobs.example.com/pending",
        source: "Example",
        keywordsMatched: ["engineer"],
        datePosted: null,
        dateScraped: "2026-08-22",
        description: "Pending projection",
        location: "Remote",
        profile: "Backend",
      },
      status: "To Review",
      createdAt: "2026-08-22T14:00:00.000Z",
    },
  ],
  pendingAfterOrdinaryUpsert: [
    {
      sourceKey: "source:pending",
      job: {
        title: "Pending Engineer",
        company: "Pending Co",
        url: "https://jobs.example.com/pending",
        source: "Example",
        keywordsMatched: ["engineer"],
        datePosted: null,
        dateScraped: "2026-08-22",
        description: "Pending projection",
        location: "Remote",
        profile: "Backend",
      },
      status: "To Review",
      createdAt: "2026-08-22T14:00:00.000Z",
    },
  ],
  pendingAfterComplete: [],
  duplicatePending: [],
};

export async function runJobLedgerConformanceScenario(
  ledger: JobLedger,
): Promise<JobLedgerConformanceResult> {
  await ledger.recordProcessedJob({
    rawUrl: "https://jobs.example.com/role?id=1",
    company: "Exact Co",
    title: "Exact Engineer",
    outcome: "inserted",
    processedAt: "2026-08-22T09:00:00.000Z",
    traceId: "trace-exact",
  });
  const exactJob = await ledger.findByRawUrl("https://jobs.example.com/role?id=1");
  const exactMiss = await ledger.findByRawUrl("https://jobs.example.com/role?id=01");

  await ledger.recordProcessedJob({
    sourceKey: "source:mutable",
    rawUrl: "https://jobs.example.com/old",
    company: "  ACME   Labs ",
    title: "Engineer",
    outcome: "rejected",
    processedAt: "2026-08-22T10:00:00.000Z",
    traceId: "trace-old",
  });
  await ledger.recordProcessedJob({
    sourceKey: "source:mutable",
    company: "Acme Labs",
    title: "Staff Engineer",
    outcome: "inserted",
    processedAt: "2026-08-22T11:00:00.000Z",
  });
  const clearedRawUrl = (await ledger.findByRawUrl("https://jobs.example.com/old")) === null;
  await ledger.recordProcessedJob({
    sourceKey: "source:mutable",
    rawUrl: "https://jobs.example.com/new",
    company: "Acme Labs",
    title: "Senior Engineer",
    outcome: "archived",
    processedAt: "2026-08-22T12:00:00.000Z",
  });

  await ledger.recordProcessedJob({
    sourceKey: "source:product-1",
    rawUrl: "https://jobs.example.com/product-1",
    company: "Acme Labs",
    title: "Product Engineer",
    outcome: "rejected",
  });
  await ledger.recordProcessedJob({
    sourceKey: "source:product-2",
    rawUrl: "https://jobs.example.com/product-2",
    company: "ACME LABS",
    title: "Product Engineer",
    outcome: "inserted",
  });
  await ledger.recordProcessedJob({
    sourceKey: "source:duplicate",
    rawUrl: "https://jobs.example.com/duplicate",
    company: "Acme Labs",
    title: "Duplicate Engineer",
    outcome: "duplicated",
  });
  await ledger.recordProcessedJob({
    sourceKey: "source:alpha-upper",
    rawUrl: "https://jobs.example.com/alpha-upper",
    company: "Acme Labs",
    title: "Alpha Engineer",
    outcome: "inserted",
  });
  await ledger.recordProcessedJob({
    sourceKey: "source:alpha-lower",
    rawUrl: "https://jobs.example.com/alpha-lower",
    company: "Acme Labs",
    title: "alpha engineer",
    outcome: "inserted",
  });

  await ledger.recordProcessedJob({
    sourceKey: "notion:page-1",
    company: "Acme",
    title: "Legacy Engineer",
    outcome: "historical",
    processedAt: "2026-08-01T10:00:00.000Z",
  });
  await ledger.recordProcessedJob({
    sourceKey: "notion:page-2",
    rawUrl: "https://jobs.example.com/notion",
    company: "ACME",
    title: "Legacy Engineer",
    outcome: "historical",
    processedAt: "2026-08-01T11:00:00.000Z",
  });

  await ledger.excludeCompany({
    company: "  Acme   Labs ",
    excludedAt: "2026-08-22T10:00:00.000Z",
  });
  await ledger.excludeCompany({
    company: "acme labs",
    excludedAt: "2026-08-22T11:00:00.000Z",
    sourceKey: "notion:block-1",
  });
  await ledger.excludeCompany({
    company: "ACME LABS",
    excludedAt: "2026-08-22T12:00:00.000Z",
  });

  await ledger.markMigration("notion-job-ledger-backfill-v1", "2026-08-22T12:00:00.000Z");
  await ledger.markMigration("notion-job-ledger-backfill-v1", "2026-08-22T13:00:00.000Z");

  const pendingProjection: PendingNotionProjection = {
    sourceKey: "source:pending",
    job: {
      title: "Pending Engineer",
      company: "Pending Co",
      url: "https://jobs.example.com/pending",
      source: "Example",
      keywordsMatched: ["engineer"],
      datePosted: null,
      dateScraped: "2026-08-22",
      description: "Pending projection",
      location: "Remote",
      profile: "Backend",
    },
    status: "To Review",
    createdAt: "2026-08-22T14:00:00.000Z",
  };
  await ledger.recordProcessedJob({
    sourceKey: pendingProjection.sourceKey,
    rawUrl: pendingProjection.job.url,
    company: pendingProjection.job.company,
    title: pendingProjection.job.title,
    outcome: "inserted",
    pendingNotionProjection: {
      job: pendingProjection.job,
      status: pendingProjection.status,
      createdAt: pendingProjection.createdAt,
    },
  });
  const pendingBeforeComplete = await ledger.listPendingNotionProjections();
  await ledger.recordProcessedJob({
    sourceKey: pendingProjection.sourceKey,
    rawUrl: pendingProjection.job.url,
    company: pendingProjection.job.company,
    title: "Updated Pending Engineer",
    outcome: "inserted",
  });
  const pendingAfterOrdinaryUpsert = await ledger.listPendingNotionProjections();
  await ledger.markNotionProjectionComplete(pendingProjection.sourceKey);
  const pendingAfterComplete = await ledger.listPendingNotionProjections();
  await ledger.recordProcessedJob({
    sourceKey: "source:duplicate-pending",
    rawUrl: "https://jobs.example.com/duplicate-pending",
    company: "Pending Co",
    title: "Duplicate Pending Engineer",
    outcome: "duplicated",
  });
  const duplicatePending = await ledger.listPendingNotionProjections();

  let missingSourceKeyError = "";
  try {
    await ledger.recordProcessedJob({
      company: "Missing Source",
      title: "Engineer",
      outcome: "rejected",
    });
  } catch (error) {
    missingSourceKeyError = error instanceof Error ? error.message : String(error);
  }

  return {
    exactJob,
    exactMiss,
    clearedRawUrl,
    updatedJob: await ledger.findByRawUrl("https://jobs.example.com/new"),
    titles: await ledger.titlesForCompany(" acme labs "),
    urlLessTitles: await ledger.titlesForCompany("acme"),
    exclusion: await ledger.findCompanyExclusion("ACME LABS"),
    notionStats: await ledger.notionBackfillStats(),
    hasMigration: await ledger.hasMigration("notion-job-ledger-backfill-v1"),
    missingSourceKeyError,
    pendingBeforeComplete,
    pendingAfterOrdinaryUpsert,
    pendingAfterComplete,
    duplicatePending,
  };
}
