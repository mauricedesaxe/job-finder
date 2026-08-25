import { expect, test } from "bun:test";
import { sleep } from "../sleep";

test("resolves through the portable timer", async () => {
  const start = Date.now();
  await sleep(5);
  expect(Date.now() - start).toBeGreaterThanOrEqual(4);
});
