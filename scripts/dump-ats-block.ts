/**
 * One-off helper: print the formatAtsBlock output for one or more URLs.
 * Used to build ATS-aware integration fixtures with the exact block shape
 * the production pipeline prepends.
 *
 * Run: bun scripts/dump-ats-block.ts <url> [<url>...]
 */
import { fetchAtsData, formatAtsBlock } from "../src/services/ats";

const urls = process.argv.slice(2);
if (urls.length === 0) {
  console.error("usage: bun scripts/dump-ats-block.ts <url> [<url>...]");
  process.exit(1);
}

for (const url of urls) {
  const data = await fetchAtsData(url);
  console.log(`=== ${url} ===`);
  if (!data) {
    console.log("(no ATS data)");
    continue;
  }
  console.log(formatAtsBlock(data));
  console.log();
}
