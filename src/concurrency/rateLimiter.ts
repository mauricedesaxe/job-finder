export class RateLimiter {
  private tokens: number;
  private lastRefill: number;

  constructor(
    private readonly tokensPerSecond: number,
    private readonly maxBurst: number,
  ) {
    this.tokens = maxBurst;
    this.lastRefill = Date.now();
  }

  private refill(): void {
    const now = Date.now();
    const elapsed = (now - this.lastRefill) / 1000;
    this.tokens = Math.min(this.maxBurst, this.tokens + elapsed * this.tokensPerSecond);
    this.lastRefill = now;
  }

  async acquire(): Promise<void> {
    this.refill();
    if (this.tokens >= 1) {
      this.tokens--;
      return;
    }
    const waitMs = ((1 - this.tokens) / this.tokensPerSecond) * 1000;
    await Bun.sleep(waitMs);
    this.refill();
    this.tokens--;
  }

  async run<T>(fn: () => Promise<T>): Promise<T> {
    await this.acquire();
    return fn();
  }
}
