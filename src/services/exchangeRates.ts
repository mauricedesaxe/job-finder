import { logger } from "../logger";

const log = logger.child({ component: "exchangeRates" });

export type ExchangeRates = Record<string, number>;

/** Fallback rates (approximate USD equivalents) used when the API is unreachable. */
export const DEFAULT_RATES: ExchangeRates = {
  EUR: 1.1,
  GBP: 1.27,
};

/**
 * Fetch live exchange rates from Frankfurter API.
 * Returns rates as "1 CURRENCY ≈ X USD" (inverted from the API's USD→currency format).
 * Falls back to DEFAULT_RATES on failure.
 */
export async function fetchExchangeRates(): Promise<ExchangeRates> {
  try {
    const res = await fetch("https://api.frankfurter.app/latest?from=USD");
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }

    const data = (await res.json()) as { rates: Record<string, number> };
    const rates: ExchangeRates = {};

    for (const [currency, perUsd] of Object.entries(data.rates)) {
      if (perUsd > 0) {
        rates[currency] = Math.round((1 / perUsd) * 100) / 100;
      }
    }

    log.info({ rates }, "fetched exchange rates");
    return rates;
  } catch (err) {
    log.warn({ err }, "failed to fetch exchange rates, using defaults");
    return DEFAULT_RATES;
  }
}
