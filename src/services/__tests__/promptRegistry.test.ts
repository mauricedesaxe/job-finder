import { describe, expect, test } from "bun:test";
import { PROMPT_REGISTRY } from "../promptRegistry";

describe("prompt registry", () => {
  test("preserves the former output limits for imported prompts", () => {
    expect(PROMPT_REGISTRY["job-finder-enrichment"].maxTokens).toBe(1024);
    expect(PROMPT_REGISTRY["job-finder-title-deduplication"].maxTokens).toBe(128);

    for (const [name, prompt] of Object.entries(PROMPT_REGISTRY)) {
      if (name !== "job-finder-enrichment" && name !== "job-finder-title-deduplication") {
        expect(prompt.maxTokens).toBe(256);
      }
    }
  });
});
