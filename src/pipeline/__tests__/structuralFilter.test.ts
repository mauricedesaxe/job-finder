import { describe, expect, test } from "bun:test";
import type { JobListing } from "../../types";
import { structuralFilter } from "../structuralFilter";

const baseJob: JobListing = {
  title: "Senior Backend Engineer",
  company: "Acme",
  url: "https://jobs.ashbyhq.com/acme/some-id",
  source: "ashbyhq",
  keywordsMatched: ["test"],
  datePosted: null,
  dateScraped: "2026-04-29",
  description: "...",
  location: "",
  profile: "",
};

describe("structuralFilter", () => {
  test("rejects Toptal listings", () => {
    const result = structuralFilter({
      ...baseJob,
      url: "https://jobs.lever.co/toptal/5e236599-9746-4e95-94c5-f405138dcbd7",
    });
    expect(result.pass).toBe(false);
    expect(result.reason).toContain("Aggregator");
  });

  test("rejects Jobgether listings", () => {
    const result = structuralFilter({
      ...baseJob,
      url: "https://jobs.lever.co/jobgether/634306c8-853d-4737-b74c-fbc4652cbaa1",
    });
    expect(result.pass).toBe(false);
    expect(result.reason).toContain("Aggregator");
  });

  test("rejects 'General Application' titles", () => {
    const result = structuralFilter({ ...baseJob, title: "General Application" });
    expect(result.pass).toBe(false);
    expect(result.reason).toContain("Generic");
  });

  test("rejects talent-pool titles", () => {
    expect(structuralFilter({ ...baseJob, title: "Talent Pool" }).pass).toBe(false);
    expect(structuralFilter({ ...baseJob, title: "Talent Community" }).pass).toBe(false);
    expect(structuralFilter({ ...baseJob, title: "Future Opportunities" }).pass).toBe(false);
    expect(structuralFilter({ ...baseJob, title: "Open Application" }).pass).toBe(false);
    expect(structuralFilter({ ...baseJob, title: "Join Our Talent Network" }).pass).toBe(false);
  });

  test("does not reject titles that merely contain the word 'general'", () => {
    expect(
      structuralFilter({ ...baseJob, title: "Senior Engineer, General AI Platform" }).pass,
    ).toBe(true);
  });

  test("passes ordinary direct-employer listings", () => {
    expect(structuralFilter(baseJob).pass).toBe(true);
    expect(
      structuralFilter({
        ...baseJob,
        url: "https://jobs.lever.co/morpho/abc",
      }).pass,
    ).toBe(true);
    expect(
      structuralFilter({
        ...baseJob,
        url: "https://boards.greenhouse.io/chainlinklabs/jobs/123",
      }).pass,
    ).toBe(true);
  });
});
