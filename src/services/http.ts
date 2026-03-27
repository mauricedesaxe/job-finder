import { withRetry } from "../concurrency/retry";

export async function fetchWithRetry(
  url: string,
  options: RequestInit,
  maxRetries = 3,
): Promise<Response> {
  return withRetry(
    async () => {
      const res = await fetch(url, options);
      if (!res.ok) {
        const err = Object.assign(new Error(`HTTP ${res.status}: ${url}`), {
          status: res.status,
        });
        throw err;
      }
      return res;
    },
    {
      maxRetries,
      baseDelayMs: 1000,
      shouldRetry: (err) => {
        const status =
          err && typeof err === "object" && "status" in err
            ? (err as { status: number }).status
            : undefined;
        return status === 429 || status === 500 || status === 503;
      },
      onRetry: (attempt) => {
        console.log(`  ⏳ Retrying request (attempt ${attempt})...`);
      },
    },
  );
}
