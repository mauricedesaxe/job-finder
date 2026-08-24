import { expect, test } from "bun:test";
import { parseCliConfig, parseContainerRuntimeConfig, parseJobFinderConfig } from "../schema";

const environment = {
  NOTION_DATABASE_ID: "database",
  NOTION_TOKEN: "notion-token",
  JINA_API_KEY: "jina-key",
  OPENROUTER_API_KEY: "openrouter-key",
  LANGSMITH_API_KEY: "langsmith-key",
  JOB_LEDGER_PATH: "/tmp/job-ledger.sqlite",
};

test("keeps the ledger path outside application config", () => {
  const application = parseJobFinderConfig(environment);
  const cli = parseCliConfig(environment);

  expect("jobLedgerPath" in application).toBe(false);
  expect(cli.jobLedgerPath).toBe("/tmp/job-ledger.sqlite");
});

test("requires the ledger path only at the CLI boundary", () => {
  expect(() => parseJobFinderConfig({ ...environment, JOB_LEDGER_PATH: undefined })).not.toThrow();
  expect(() => parseCliConfig({ ...environment, JOB_LEDGER_PATH: undefined })).toThrow();
});

test("parses the Container port at its boundary", () => {
  expect(parseContainerRuntimeConfig({ ...environment, PORT: "9090" }).port).toBe(9090);
  expect(() => parseContainerRuntimeConfig({ ...environment, PORT: "invalid" })).toThrow();
});
