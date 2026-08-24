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
