import { expect, test } from "bun:test";
import { parseCliConfig, parseJobFinderConfig } from "../schema";

const applicationEnvironment = {
  NOTION_DATABASE_ID: "database",
  NOTION_TOKEN: "notion",
  JINA_API_KEY: "jina",
  OPENROUTER_API_KEY: "openrouter",
  LANGSMITH_API_KEY: "langsmith",
};

test("parses Worker application config without a CLI ledger path", () => {
  const config = parseJobFinderConfig(applicationEnvironment);
  expect(config.notionDatabaseId).toBe("database");
  expect("jobLedgerPath" in config).toBe(false);
});

test("requires the SQLite path only at the CLI boundary", () => {
  expect(() => parseCliConfig(applicationEnvironment)).toThrow();
  expect(
    parseCliConfig({ ...applicationEnvironment, JOB_LEDGER_PATH: "data/job-ledger.sqlite" })
      .jobLedgerPath,
  ).toBe("data/job-ledger.sqlite");
});

test("accepts only Pino log levels", () => {
  expect(parseJobFinderConfig({ ...applicationEnvironment, LOG_LEVEL: "trace" }).logLevel).toBe(
    "trace",
  );
  expect(() => parseJobFinderConfig({ ...applicationEnvironment, LOG_LEVEL: "verbose" })).toThrow();
});
