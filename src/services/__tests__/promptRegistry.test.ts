import { describe, expect, test } from "bun:test";
import { EVALUATION_PROMPTS, PROMPT_NAMES, PROMPT_REGISTRY } from "../promptRegistry";

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

  test("contains only the active evaluation profiles", () => {
    expect(EVALUATION_PROMPTS).toContain("job-finder-profile-early-stage-product-engineer");
    expect(EVALUATION_PROMPTS).toContain("job-finder-profile-applied-ai-product-engineer");
    expect(EVALUATION_PROMPTS).not.toContain("job-finder-profile-crypto-web3-ts");
    expect(EVALUATION_PROMPTS).not.toContain("job-finder-profile-fintech-trading-infra-ts");
    expect(EVALUATION_PROMPTS).not.toContain("job-finder-profile-senior-fullstack-react");
    expect(EVALUATION_PROMPTS).not.toContain("job-finder-profile-ai-engineering");
    expect(Object.keys(PROMPT_REGISTRY)).toEqual([...PROMPT_NAMES]);
  });
});
