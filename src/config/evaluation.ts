import { DEFAULT_RATES, type ExchangeRates } from "../services/exchangeRates";
import type { EvaluationPromptName } from "../services/promptRegistry";

export interface EvaluationCriteria {
  name: string;
  promptName?: EvaluationPromptName;
  rates?: string;
}
export type EvaluationProfile = EvaluationCriteria;
export type EvaluationFilter = EvaluationCriteria;
export type LocalEvaluationCriteria = EvaluationCriteria;

export const EVALUATION_PROFILES: EvaluationProfile[] = [
  { name: "crypto-web3-ts", promptName: "job-finder-profile-crypto-web3-ts" },
  { name: "fintech-trading-infra-ts", promptName: "job-finder-profile-fintech-trading-infra-ts" },
  { name: "senior-fullstack-react", promptName: "job-finder-profile-senior-fullstack-react" },
  { name: "ai-engineering", promptName: "job-finder-profile-ai-engineering" },
];
const PROMPT_CURRENCIES = [
  "EUR",
  "GBP",
  "CHF",
  "CAD",
  "AUD",
  "PLN",
  "SEK",
  "NOK",
  "DKK",
  "CZK",
  "SGD",
  "ILS",
];
function compensationRates(rates: ExchangeRates): string {
  return PROMPT_CURRENCIES.filter((currency) => currency in rates)
    .map((currency) => `1 ${currency} ≈ ${(rates[currency] as number).toFixed(2)} USD`)
    .join(", ");
}
export function getEvaluationFilters(rates: ExchangeRates = DEFAULT_RATES): EvaluationFilter[] {
  return [
    { name: "remote-europe-eligible", promptName: "job-finder-filter-location-eligibility" },
    {
      name: "compensation-minimum",
      promptName: "job-finder-filter-compensation",
      rates: compensationRates(rates),
    },
    { name: "role-quality", promptName: "job-finder-filter-role-quality" },
    { name: "cheap-shop-placement", promptName: "job-finder-filter-company-quality" },
  ];
}
