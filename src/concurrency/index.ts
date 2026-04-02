import { CircuitBreaker } from "./circuitBreaker";
import { RateLimiter } from "./rateLimiter";
import { Semaphore } from "./semaphore";

export { CircuitBreaker, CircuitBreakerOpenError } from "./circuitBreaker";
export { RateLimiter } from "./rateLimiter";
export type { RetryOptions } from "./retry";
export {
  isRetryableJina,
  isRetryableLLM,
  isRetryableNotion,
  withRetry,
} from "./retry";
export { Semaphore } from "./semaphore";

// Shared service limiters — all concurrent work goes through these

export const jinaSearchSemaphore = new Semaphore(5);
export const jinaReaderSemaphore = new Semaphore(8);
export const llmSemaphore = new Semaphore(10);
export const notionRateLimiter = new RateLimiter(3, 3); // 3 req/s, burst 3

export const jinaBreaker = new CircuitBreaker(5, 30_000);
export const llmBreaker = new CircuitBreaker(5, 30_000);
export const notionBreaker = new CircuitBreaker(5, 60_000);
