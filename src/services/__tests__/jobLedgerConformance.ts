import type {
  CompanyExclusion,
  JobLedger,
  PendingNotionProjection,
  PendingReviewProjection,
  ProcessedJob,
  SelectiveNotionImportStats,
} from "../jobLedger";

interface JobLedgerConformanceResult {
  exactJob: ProcessedJob | null;
  exactMiss: ProcessedJob | null;
  clearedRawUrl: boolean;
  updatedJob: ProcessedJob | null;
  titles: string[];
  urlLessTitles: string[];
  exclusion: CompanyExclusion | null;
  importedStats: SelectiveNotionImportStats;
  importedExclusion: CompanyExclusion | null;
  legacyNotionExclusion: CompanyExclusion | null;
  hasMigration: boolean;
  missingSourceKeyError: string;
  pendingBeforeComplete: PendingNotionProjection[];
  pendingAfterOrdinaryUpsert: PendingNotionProjection[];
  pendingAfterComplete: PendingNotionProjection[];
  duplicatePending: PendingNotionProjection[];
  pendingReviewBeforeComplete: PendingReviewProjection[];
  pendingReviewAfterComplete: PendingReviewProjection[];
  projectionKinds: {
    none: "none";
    notion: "notion";
    notionAndReview: "notion-and-review";
  };
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
  urlLessTitles: [],
  exclusion: {
    company: "  Acme   Labs ",
    excludedAt: "2026-08-22T10:00:00.000Z",
  },
  importedStats: {
    blockedCompanies: 2,
    recentApplications: 1,
  },
  importedExclusion: {
    company: "Imported Block",
    excludedAt: "2026-08-22T13:00:00.000Z",
  },
  legacyNotionExclusion: null,
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
  pendingReviewBeforeComplete: [
    {
      sourceKey: "source:pending",
      traceId: "trace-pending",
      createdAt: "2026-08-22T14:00:00.000Z",
    },
  ],
  pendingReviewAfterComplete: [],
  projectionKinds: {
    none: "none",
    notion: "notion",
    notionAndReview: "notion-and-review",
  },
};

export async function runJobLedgerConformanceScenario(
  ledger: JobLedger,
): Promise<JobLedgerConformanceResult> {
  const noProjections = await ledger.recordProcessedJob({
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
    company: "Legacy Notion Exclusion",
    excludedAt: "2026-08-22T11:00:00.000Z",
    sourceKey: "notion:block-1",
  });
  await ledger.excludeCompany({
    company: "ACME LABS",
    excludedAt: "2026-08-22T12:00:00.000Z",
  });

  await ledger.migrateNotionCompanyState({
    states: [{ kind: "blocked", company: "Stale Import" }],
    importedAt: "2026-08-22T12:00:00.000Z",
  });
  const importedStats = await ledger.migrateNotionCompanyState({
    states: [
      { kind: "blocked", company: "Acme Labs" },
      { kind: "blocked", company: "Imported Block" },
      {
        kind: "recent-application",
        company: "Recent Company",
        applicationDate: "2026-08-20",
      },
    ],
    importedAt: "2026-08-22T13:00:00.000Z",
  });

  await ledger.markMigration("notion-company-state-import-v2", "2026-08-22T12:00:00.000Z");
  await ledger.markMigration("notion-company-state-import-v2", "2026-08-22T13:00:00.000Z");

  const notionOnly = await ledger.recordProcessedJob({
    sourceKey: "source:notion-only",
    rawUrl: "https://jobs.example.com/notion-only",
    company: "Projection Co",
    title: "Projection Engineer",
    outcome: "rejected",
    projections: {
      kind: "notion",
      notion: {
        job: {
          title: "Projection Engineer",
          company: "Projection Co",
          url: "https://jobs.example.com/notion-only",
          source: "Example",
          keywordsMatched: ["engineer"],
          datePosted: null,
          dateScraped: "2026-08-22",
          description: "Notion-only projection",
          location: "Remote",
          profile: "Backend",
        },
        status: "Auto-Rejected",
        createdAt: "2026-08-22T13:30:00.000Z",
      },
    },
  });
  await ledger.markNotionProjectionComplete("source:notion-only");

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
  const notionAndReview = await ledger.recordProcessedJob({
    sourceKey: pendingProjection.sourceKey,
    rawUrl: pendingProjection.job.url,
    company: pendingProjection.job.company,
    title: pendingProjection.job.title,
    outcome: "inserted",
    projections: {
      kind: "notion-and-review",
      notion: {
        job: pendingProjection.job,
        status: pendingProjection.status,
        createdAt: pendingProjection.createdAt,
      },
      review: {
        traceId: "trace-pending",
        createdAt: pendingProjection.createdAt,
      },
    },
  });
  const pendingReviewBeforeComplete = await ledger.listPendingReviewProjections();
  const pendingBeforeComplete = await ledger.nextPendingNotionProjectionBatch();
  await ledger.recordProcessedJob({
    sourceKey: pendingProjection.sourceKey,
    rawUrl: pendingProjection.job.url,
    company: pendingProjection.job.company,
    title: "Updated Pending Engineer",
    outcome: "inserted",
  });
  const pendingAfterOrdinaryUpsert = await ledger.nextPendingNotionProjectionBatch();
  await ledger.markNotionProjectionComplete(pendingProjection.sourceKey);
  const pendingAfterComplete = await ledger.nextPendingNotionProjectionBatch();
  await ledger.markReviewProjectionComplete(pendingProjection.sourceKey);
  const pendingReviewAfterComplete = await ledger.listPendingReviewProjections();
  await ledger.recordProcessedJob({
    sourceKey: "source:duplicate-pending",
    rawUrl: "https://jobs.example.com/duplicate-pending",
    company: "Pending Co",
    title: "Duplicate Pending Engineer",
    outcome: "duplicated",
  });
  const duplicatePending = await ledger.nextPendingNotionProjectionBatch();

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

  if (noProjections.kind !== "none") throw new Error("Expected no projections");
  if (notionOnly.kind !== "notion") throw new Error("Expected a Notion projection");
  if (notionAndReview.kind !== "notion-and-review") {
    throw new Error("Expected Notion and review projections");
  }

  return {
    exactJob,
    exactMiss,
    clearedRawUrl,
    updatedJob: await ledger.findByRawUrl("https://jobs.example.com/new"),
    titles: await ledger.titlesForCompany(" acme labs "),
    urlLessTitles: await ledger.titlesForCompany("acme"),
    exclusion: await ledger.findCompanyExclusion("ACME LABS"),
    importedStats,
    importedExclusion: await ledger.findCompanyExclusion("imported block"),
    legacyNotionExclusion: await ledger.findCompanyExclusion("legacy notion exclusion"),
    hasMigration: await ledger.hasMigration("notion-company-state-import-v2"),
    missingSourceKeyError,
    pendingBeforeComplete,
    pendingAfterOrdinaryUpsert,
    pendingAfterComplete,
    duplicatePending,
    pendingReviewBeforeComplete,
    pendingReviewAfterComplete,
    projectionKinds: {
      none: noProjections.kind,
      notion: notionOnly.kind,
      notionAndReview: notionAndReview.kind,
    },
  };
}
