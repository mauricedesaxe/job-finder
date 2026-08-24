import { expect, test } from "bun:test";
import { assertNotionSchema } from "../preflight";

test("rejects missing Notion properties with their expected types", () => {
  expect(() => assertNotionSchema({})).toThrow(
    'Missing property: "Job Title" (expected type: title)',
  );
});

test("rejects a Notion property with the wrong type", () => {
  expect(() => assertNotionSchema({ "Job Title": { type: "rich_text" } })).toThrow(
    'Wrong type for "Job Title": got "rich_text", expected "title"',
  );
});
