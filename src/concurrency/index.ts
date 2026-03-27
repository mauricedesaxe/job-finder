import { Semaphore } from "./semaphore";
import { RateLimiter } from "./rateLimiter";
import { CircuitBreaker } from "./circuitBreaker";

export { Semaphore } from "./semaphore";
export { RateLimiter } from "./rateLimiter";
export { CircuitBreaker, CircuitBreakerOpenError } from "./circuitBreaker";
export {
  withRetry,
  isRetryableJina,
  isRetryableAnthropic,
  isRetryableNotion,
} from "./retry";
export type { RetryOptions } from "./retry";

// Shared service limiters — all concurrent work goes through these

export const jinaSearchSemaphore = new Semaphore(5);
export const jinaReaderSemaphore = new Semaphore(8);
export const anthropicSemaphore = new Semaphore(10);
export const notionRateLimiter = new RateLimiter(3, 3); // 3 req/s, burst 3

export const jinaBreaker = new CircuitBreaker(5, 30_000);
export const anthropicBreaker = new CircuitBreaker(5, 30_000);
export const notionBreaker = new CircuitBreaker(5, 60_000);
