export interface RetryOptions {
  maxRetries?: number;
  baseDelayMs?: number;
  shouldRetry?: (err: unknown) => boolean;
  onRetry?: (attempt: number, err: unknown) => void;
}

function getErrorStatus(err: unknown): number | undefined {
  if (err && typeof err === "object" && "status" in err) {
    return (err as { status: number }).status;
  }
  return undefined;
}

export const isRetryableJina = (err: unknown): boolean => {
  const status = getErrorStatus(err);
  return status === 429 || status === 500 || status === 503;
};

export const isRetryableAnthropic = (err: unknown): boolean => {
  const status = getErrorStatus(err);
  return status === 429 || status === 529 || status === 500;
};

export const isRetryableNotion = (err: unknown): boolean => {
  const status = getErrorStatus(err);
  return status === 429 || status === 502 || status === 503;
};

export async function withRetry<T>(
  fn: () => Promise<T>,
  options: RetryOptions = {},
): Promise<T> {
  const {
    maxRetries = 3,
    baseDelayMs = 1000,
    shouldRetry = () => false,
    onRetry,
  } = options;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (err) {
      if (attempt < maxRetries && shouldRetry(err)) {
        const delay = baseDelayMs * 2 ** attempt;
        onRetry?.(attempt + 1, err);
        await Bun.sleep(delay);
        continue;
      }
      throw err;
    }
  }

  // Unreachable, but TypeScript needs it
  throw new Error("withRetry: exhausted retries");
}
