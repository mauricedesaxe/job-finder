export async function fetchWithRetry(
  url: string,
  options: RequestInit,
  maxRetries = 3,
): Promise<Response> {
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    const res = await fetch(url, options);
    if (res.ok) return res;

    if (res.status === 429 && attempt < maxRetries - 1) {
      const delay = 1000 * 2 ** attempt; // 1s, 2s, 4s
      console.log(`  ⏳ Rate limited, retrying in ${delay / 1000}s...`);
      await Bun.sleep(delay);
      continue;
    }

    throw new Error(`HTTP request failed (${res.status}): ${url}`);
  }

  throw new Error(`HTTP request failed after ${maxRetries} retries: ${url}`);
}
