export const SEARCH_KEYWORDS = [
  "senior backend engineer crypto",
  "senior fullstack engineer web3",
  "senior typescript engineer blockchain",
  "lead backend engineer defi",
  "senior software engineer defi",
  "senior software engineer web3",
  "typescript engineer crypto",
  "node.js engineer blockchain",
  "protocol engineer",
  "senior engineer solana",
  "senior engineer ethereum",
  "fullstack engineer defi",
  "backend engineer blockchain infrastructure",

  // Fintech trading infra
  "backend engineer trading platform",
  "backend engineer market data",
  "senior engineer fintech",
  "real-time systems engineer fintech",
  "backend engineer quantitative finance",
];

export const SEARCH_DOMAINS = [
  "jobs.ashbyhq.com",
  "jobs.lever.co",
  "boards.greenhouse.io",
];

export interface EvaluationProfile {
  name: string;
  prompt: string;
}

export const EVALUATION_PROFILES: EvaluationProfile[] = [
  {
    name: "crypto-web3",
    prompt: `You evaluate job listings for a senior/lead fullstack TypeScript developer focused on crypto/web3, based in Europe.

A job PASSES if ALL of these are true:
1. The role is remote-friendly OR available to European timezones (CET/EET). Reject if explicitly US-only, on-site only, or requires a specific non-European location.
2. The role is senior or lead level (or doesn't specify level, which is acceptable).
3. The role is relevant: software engineering involving TypeScript, Node.js, fullstack, or backend. Crypto/web3/blockchain context preferred but general senior TS roles at crypto companies also pass.

A job FAILS if ANY of these are true:
- Explicitly requires on-site presence
- Explicitly restricted to US/Asia timezones only with no European overlap
- Junior or internship level
- Non-engineering role (marketing, design, sales, HR, etc.)
- Completely unrelated tech stack with no TypeScript/JavaScript involvement
- Strictly frontend role (e.g. UI engineer, frontend engineer, design systems) with no backend or fullstack component
- MEV extraction or arbitrage bot development
- HFT or ultra-low-latency systems requiring C++/C`,
  },
  {
    name: "fintech-trading-infra",
    prompt: `You evaluate job listings for a senior backend engineer specializing in real-time trading systems and financial data infrastructure, based in Europe.
A job PASSES if ALL of these are true:
1. The role is remote-friendly OR available to European timezones (CET/EET). Reject if explicitly US-only, on-site only, or requires a specific non-European location.
2. The role is senior level or doesn't specify level.
3. The role involves application-layer trading infrastructure: real-time data pipelines, market data systems, trading platforms, or financial backend services. TypeScript, Node.js, Go, or Rust stack preferred. NOT raw latency optimization or hardware-level performance work.
A job FAILS if ANY of these are true:
- Explicitly requires on-site presence
- Explicitly restricted to US/Asia timezones only with no European overlap
- Junior or internship level
- Non-engineering role
- Generic CRUD/SaaS backend with no real-time or financial data component
- HFT (high-frequency trading) or ultra-low-latency systems requiring C++/C
- MEV extraction or arbitrage bot development
- Strictly frontend role with no backend or infrastructure component
- Primarily C++, C, or low-level systems programming — must involve TypeScript, Node.js, Go, or Rust`,
  },
];
