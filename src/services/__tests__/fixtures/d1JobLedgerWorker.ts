import { ZodError } from "zod/v4";
import type { D1DatabaseBinding } from "../../d1";
import { createD1JobLedger } from "../../d1JobLedger";
import { isJobLedgerReadyForScrape } from "../../notionLedgerInitialization";
import { createD1JobFinderRunLock } from "../../runLock";
import { runJobLedgerConformanceScenario } from "../jobLedgerConformance";

interface TestEnvironment {
  JOB_LEDGER: D1DatabaseBinding;
}

export default {
  async fetch(request: Request, environment: TestEnvironment): Promise<Response> {
    const path = new URL(request.url).pathname;
    const ledger = createD1JobLedger(environment.JOB_LEDGER);

    if (path === "/scenario") {
      const result = await runJobLedgerConformanceScenario(ledger);
      const migration = await environment.JOB_LEDGER.prepare(
        "SELECT COUNT(*) AS count, completed_at FROM job_ledger_migrations WHERE name = ?",
      )
        .bind("notion-company-state-import-v2")
        .first();

      return Response.json({
        result,
        migration,
      });
    }

    if (path === "/ready") {
      return Response.json({ ready: await isJobLedgerReadyForScrape(ledger) });
    }

    if (path === "/run-lock") {
      const lock = createD1JobFinderRunLock(environment.JOB_LEDGER);
      const acquired = await lock.acquire("run-1", "2026-08-24T22:00:00.000Z");
      const contended = await lock.acquire("run-2", "2026-08-24T22:01:00.000Z");
      const wrongRelease = await lock.release("run-2");
      const released = await lock.release("run-1");
      const reacquired = await lock.acquire("run-2", "2026-08-24T22:02:00.000Z");
      await lock.release("run-2");
      return Response.json({ acquired, contended, wrongRelease, released, reacquired });
    }

    if (path === "/run-lock-concurrent") {
      const lock = createD1JobFinderRunLock(environment.JOB_LEDGER);
      const results = await Promise.all([
        lock.acquire("concurrent-1", "2026-08-24T22:10:00.000Z"),
        lock.acquire("concurrent-2", "2026-08-24T22:10:01.000Z"),
      ]);
      const owner = results.find((result) => result.kind === "acquired");
      if (!owner) throw new Error("Concurrent acquisition returned no owner");
      await lock.release(owner.workflowInstanceId);
      return Response.json({ results });
    }

    if (path === "/run-lock-same-owner") {
      const lock = createD1JobFinderRunLock(environment.JOB_LEDGER);
      const first = await lock.acquire("same-owner", "2026-08-24T22:20:00.000Z");
      const second = await lock.acquire("same-owner", "2026-08-24T22:21:00.000Z");
      await lock.release("same-owner");
      return Response.json({ first, second });
    }

    if (path === "/atomic-projection/notion") {
      return Response.json(await atomicProjectionFailure(environment, ledger, "notion"));
    }

    if (path === "/atomic-projection/review") {
      return Response.json(await atomicProjectionFailure(environment, ledger, "review"));
    }

    if (path === "/atomic-import-replacement") {
      return Response.json(await atomicImportReplacementFailure(environment, ledger));
    }

    if (path === "/malformed-projection") {
      await ledger.recordProcessedJob({
        sourceKey: "source:malformed-projection",
        rawUrl: "https://jobs.example.com/malformed-projection",
        company: "Acme",
        title: "Engineer",
        outcome: "inserted",
      });
      await environment.JOB_LEDGER.prepare(
        `INSERT INTO pending_notion_projections (source_key, job_json, status, created_at)
         VALUES (?, ?, ?, ?)`,
      )
        .bind(
          "source:malformed-projection",
          JSON.stringify({ title: "Incomplete" }),
          "To Review",
          "2026-08-24T10:00:00.000Z",
        )
        .run();

      try {
        await ledger.nextPendingNotionProjectionBatch();
        return Response.json({ rejected: false });
      } catch (error) {
        return Response.json({ rejected: error instanceof ZodError });
      }
    }

    if (path === "/malformed-outcome") {
      await environment.JOB_LEDGER.prepare(
        `INSERT INTO processed_jobs (
          source_key, raw_url, company, normalized_company, title, normalized_title,
          outcome, first_processed_at, last_processed_at, trace_id
        ) VALUES (
          'source:malformed', 'https://jobs.example.com/malformed', 'Acme', 'acme',
          'Engineer', 'engineer', 'not-an-outcome',
          '2026-08-22T10:00:00.000Z', '2026-08-22T10:00:00.000Z', NULL
        )`,
      ).run();

      try {
        await ledger.findByRawUrl("https://jobs.example.com/malformed");
        return Response.json({ rejected: false });
      } catch (error) {
        return Response.json({ rejected: error instanceof ZodError });
      }
    }

    return new Response("Not found", { status: 404 });
  },
};

async function atomicProjectionFailure(
  environment: TestEnvironment,
  ledger: ReturnType<typeof createD1JobLedger>,
  failedProjection: "notion" | "review",
) {
  const table =
    failedProjection === "notion" ? "pending_notion_projections" : "pending_review_projections";
  await environment.JOB_LEDGER.prepare(
    `CREATE TRIGGER reject_${failedProjection}_projection
     BEFORE INSERT ON ${table}
     BEGIN
       SELECT RAISE(ABORT, '${failedProjection} projection write failed');
     END`,
  ).run();

  const sourceKey = `source:atomic-${failedProjection}`;
  let rejected = false;
  try {
    await ledger.recordProcessedJob({
      sourceKey,
      rawUrl: `https://jobs.example.com/atomic-${failedProjection}`,
      company: "Atomic Co",
      title: "Atomic Engineer",
      outcome: "inserted",
      projections: {
        kind: "notion-and-review",
        notion: {
          job: {
            title: "Atomic Engineer",
            company: "Atomic Co",
            url: `https://jobs.example.com/atomic-${failedProjection}`,
            source: "Example",
            keywordsMatched: ["engineer"],
            datePosted: null,
            dateScraped: "2026-08-24",
            description: "Atomic projection",
            location: "Remote",
            profile: "Backend",
          },
          status: "To Review",
          createdAt: "2026-08-24T10:00:00.000Z",
        },
        review: {
          traceId: `trace-atomic-${failedProjection}`,
          createdAt: "2026-08-24T10:00:00.000Z",
        },
      },
    });
  } catch {
    rejected = true;
  }

  const counts = await Promise.all(
    ["processed_jobs", "pending_notion_projections", "pending_review_projections"].map((name) =>
      environment.JOB_LEDGER.prepare(`SELECT COUNT(*) AS count FROM ${name} WHERE source_key = ?`)
        .bind(sourceKey)
        .first(),
    ),
  );
  await environment.JOB_LEDGER.prepare(`DROP TRIGGER reject_${failedProjection}_projection`).run();
  return { rejected, counts };
}

async function atomicImportReplacementFailure(
  environment: TestEnvironment,
  ledger: ReturnType<typeof createD1JobLedger>,
) {
  await ledger.replaceImportedNotionCompanyState([
    {
      kind: "blocked",
      company: "Existing Atomic Import",
      sourceKey: "notion:atomic-import-existing",
      importedAt: "2026-08-25T12:00:00.000Z",
    },
  ]);
  await ledger.recordProcessedJob({
    sourceKey: "notion:atomic-legacy-job",
    rawUrl: "https://jobs.example.com/atomic-legacy",
    company: "Atomic Legacy Job",
    title: "Legacy Engineer",
    outcome: "historical",
    processedAt: "2026-08-25T12:00:00.000Z",
  });
  await ledger.excludeCompany({
    company: "Atomic Legacy Exclusion",
    excludedAt: "2026-08-25T12:00:00.000Z",
    sourceKey: "notion:atomic-legacy-exclusion",
  });
  await ledger.excludeCompany({
    company: "Atomic Runtime Exclusion",
    excludedAt: "2026-08-25T12:00:00.000Z",
  });
  await environment.JOB_LEDGER.prepare(
    `CREATE TRIGGER reject_imported_state_replacement
     BEFORE INSERT ON imported_notion_company_state
     BEGIN
       SELECT RAISE(ABORT, 'imported state replacement failed');
     END`,
  ).run();

  let rejected = false;
  try {
    await ledger.replaceImportedNotionCompanyState([
      {
        kind: "blocked",
        company: "Replacement Atomic Import",
        sourceKey: "notion:atomic-import-replacement",
        importedAt: "2026-08-25T13:00:00.000Z",
      },
    ]);
  } catch {
    rejected = true;
  }

  const result = {
    rejected,
    importedStateSurvived: (await ledger.findCompanyExclusion("existing atomic import")) !== null,
    legacyJobSurvived:
      (await ledger.findByRawUrl("https://jobs.example.com/atomic-legacy")) !== null,
    legacyExclusionSurvived:
      (await ledger.findCompanyExclusion("atomic legacy exclusion")) !== null,
    runtimeExclusionSurvived:
      (await ledger.findCompanyExclusion("atomic runtime exclusion")) !== null,
  };
  await environment.JOB_LEDGER.prepare("DROP TRIGGER reject_imported_state_replacement").run();
  await ledger.replaceImportedNotionCompanyState([]);
  return result;
}
