import { describe, expect, test } from "bun:test";
import { EVALUATION_FILTERS } from "../../config/evaluation";
import type { JobListing } from "../../types";
import { evaluateSingle } from "../evaluate";

const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY as string;
const remoteFilter = EVALUATION_FILTERS.find(
  (f) => f.name === "remote-europe-eligible",
) as (typeof EVALUATION_FILTERS)[number];

function fixtureJob(
  fixtureDir: string,
  fixtureName: string,
  overrides?: Partial<JobListing>,
): {
  job: JobListing;
  load: () => Promise<JobListing>;
} {
  const job: JobListing = {
    title: fixtureName,
    company: fixtureName,
    url: `https://example.com/${fixtureName}`,
    source: "ashbyhq",
    keywordsMatched: ["test"],
    datePosted: null,
    dateScraped: "2026-03-30",
    description: "", // loaded async
    location: "",
    profile: "",
    ...overrides,
  };

  return {
    job,
    async load() {
      const path = `${import.meta.dir}/fixtures/${fixtureDir}/${fixtureName}.md`;
      job.description = await Bun.file(path).text();
      return job;
    },
  };
}

describe("remote-europe-eligible filter (integration)", () => {
  // Expected FAIL — hybrid, 2 days/month in Paris office
  test("yubo-hybrid → FAIL", async () => {
    const { load } = fixtureJob("remote/reject", "yubo-hybrid", {
      company: "yubo",
      url: "https://jobs.ashbyhq.com/yubo/82425f52-aaf8-46fd-a8b5-03eb22430978",
    });
    const job = await load();
    const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY);
    expect(result.pass).toBe(false);
  }, 30_000);

  // Expected FAIL — on-site default in Prague/Brno, "option to work remotely"
  test("apify-onsite-default → FAIL", async () => {
    const { load } = fixtureJob("remote/reject", "apify-onsite-default", {
      company: "apify",
      url: "https://jobs.ashbyhq.com/apify/a6293104-d98f-4317-8ba7-bd7a0e785950",
    });
    const job = await load();
    const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY);
    expect(result.pass).toBe(false);
  }, 30_000);

  // Expected FAIL — Dublin/Hybrid in page metadata (stripped by Jina), no remote signal in body
  test("protex-no-location → FAIL", async () => {
    const { load } = fixtureJob("remote/reject", "protex-no-location", {
      company: "Protex AI",
      url: "https://jobs.ashbyhq.com/Protex%20AI/f2e221a5-dbe5-437a-ba11-a3eec1151728",
    });
    const job = await load();
    const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY);
    expect(result.pass).toBe(false);
  }, 30_000);

  // Expected PASS — crypto/web3, "supportive remote environment", annual offsites only
  test("0x-remote-crypto → PASS", async () => {
    const { load } = fixtureJob("remote/pass", "0x-remote-crypto", {
      company: "0x",
      url: "https://jobs.ashbyhq.com/0x/1b8b57e9-e054-442d-a663-86f21724fc8a",
    });
    const job = await load();
    const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY);
    expect(result.pass).toBe(true);
  }, 30_000);

  // Expected PASS — remote, multiple EU countries listed, crypto
  test("fuel-labs-remote-eu → PASS", async () => {
    const { load } = fixtureJob("remote/pass", "fuel-labs-remote-eu", {
      company: "Fuel Labs",
      url: "https://jobs.lever.co/fuellabs/c0c2c414-46c4-4c74-8a4a-4ae8bc9af3ba",
      source: "lever",
    });
    const job = await load();
    const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY);
    expect(result.pass).toBe(true);
  }, 30_000);

  // Expected PASS — crypto company (relay protocol), no explicit location but crypto exception
  test("relay-protocol-crypto → PASS", async () => {
    const { load } = fixtureJob("remote/pass", "relay-protocol-crypto", {
      company: "Relay Protocol",
      url: "https://jobs.ashbyhq.com/relayprotocol/d0b0692b-eb03-45d0-8491-56ef4d2cbf38",
    });
    const job = await load();
    const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY);
    expect(result.pass).toBe(true);
  }, 30_000);
});
