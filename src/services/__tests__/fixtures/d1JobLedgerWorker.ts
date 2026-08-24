import { ZodError } from "zod/v4";
import { createD1JobLedger } from "../../d1JobLedger";
import { runJobLedgerConformanceScenario } from "../jobLedgerConformance";

interface TestD1Statement {
  bind(...values: (string | null)[]): TestD1Statement;
  first(): Promise<unknown>;
  all(): Promise<unknown>;
  run(): Promise<unknown>;
}

interface TestEnvironment {
  JOB_LEDGER: {
    prepare(query: string): TestD1Statement;
    batch(statements: TestD1Statement[]): Promise<unknown>;
  };
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
        .bind("notion-job-ledger-backfill-v1")
        .first();

      return Response.json({
        result,
        migration,
      });
    }

    if (path === "/atomic-projection") {
      await environment.JOB_LEDGER.prepare(
        `CREATE TRIGGER reject_pending_projection
         BEFORE INSERT ON pending_notion_projections
         BEGIN
           SELECT RAISE(ABORT, 'projection write failed');
         END`,
      ).run();

      let rejected = false;
      try {
        await ledger.recordProcessedJob({
          sourceKey: "source:atomic",
          rawUrl: "https://jobs.example.com/atomic",
          company: "Atomic Co",
          title: "Atomic Engineer",
          outcome: "inserted",
          pendingNotionProjection: {
            job: {
              title: "Atomic Engineer",
              company: "Atomic Co",
              url: "https://jobs.example.com/atomic",
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
        });
      } catch {
        rejected = true;
      }

      const processed = await environment.JOB_LEDGER.prepare(
        "SELECT source_key FROM processed_jobs WHERE source_key = ?",
      )
        .bind("source:atomic")
        .first();
      await environment.JOB_LEDGER.prepare("DROP TRIGGER reject_pending_projection").run();
      return Response.json({ rejected, rolledBack: processed === null });
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
        await ledger.listPendingNotionProjections();
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
