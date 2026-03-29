export const SEARCH_KEYWORDS = [
  // Crypto/Web3 - TS/Node
  "senior backend engineer crypto",
  "senior fullstack engineer web3",
  "senior typescript engineer blockchain",
  "lead backend engineer defi",
  "senior software engineer defi",
  "senior software engineer web3",
  "typescript engineer crypto",
  "node.js engineer blockchain",
  "senior engineer solana",
  "senior engineer ethereum",
  "fullstack engineer defi",
  "backend engineer blockchain infrastructure",
  "distributed systems engineer crypto",
  "distributed systems engineer web3",
  // Non-crypto distributed systems - TS/Node
  "distributed systems engineer typescript",
  "distributed systems engineer node.js",
  "backend engineer real-time systems typescript",
  "senior backend engineer event-driven typescript",
  "senior backend engineer message queue",
  // Fintech/trading infra - TS/Node
  "backend engineer trading platform typescript",
  "backend engineer market data",
  "senior engineer fintech typescript",
  "real-time systems engineer fintech",
  "backend engineer quantitative finance",
];

export const SEARCH_DOMAINS = [
  "jobs.ashbyhq.com",
  "jobs.lever.co",
  "boards.greenhouse.io",
  "apply.workable.com",
];

export interface EvaluationCriteria {
  name: string;
  prompt: string;
}

export interface EvaluationProfile extends EvaluationCriteria {}
export interface EvaluationFilter extends EvaluationCriteria {}

export const EVALUATION_PROFILES: EvaluationProfile[] = [
  {
    name: "crypto-web3-ts",
    prompt: `You evaluate job listings for a senior/lead TypeScript/Node.js backend or fullstack developer focused on crypto/web3, based in Europe.
A job PASSES if ALL of these are true:
1. The role is remote-friendly OR available to European timezones (CET/EET). Reject if explicitly US-only, on-site only, or requires a specific non-European location.
2. The role is senior or lead level (or doesn't specify level, which is acceptable).
3. The role is in a crypto/web3/blockchain company or project.
4. Primary stack involves TypeScript, Node.js, or JavaScript — backend, fullstack, or infrastructure.
A job FAILS if ANY of these are true:
- Explicitly requires on-site presence
- Explicitly restricted to US/Asia timezones only with no European overlap
- Junior or internship level
- Non-engineering role (marketing, design, sales, HR, etc.)
- Primary stack is Go, Rust, C++, Java, or Python with no TypeScript/Node.js involvement
- Strictly frontend role with no backend or fullstack component
- MEV extraction or arbitrage bot development
- HFT or ultra-low-latency systems requiring C++/C`,
  },
  {
    name: "distributed-systems-ts",
    prompt: `You evaluate job listings for a senior TypeScript/Node.js distributed systems or backend infrastructure engineer, outside of crypto/web3, based in Europe.
A job PASSES if ALL of these are true:
1. The role is remote-friendly OR available to European timezones (CET/EET). Reject if explicitly US-only, on-site only, or requires a specific non-European location.
2. The role is senior or lead level (or doesn't specify level, which is acceptable).
3. The role is outside crypto/web3/blockchain — fintech, trading infra, SaaS infrastructure, platform engineering, or general distributed systems are all fine.
4. Primary stack involves TypeScript or Node.js.
5. The role involves real distributed systems concerns: event-driven architecture, message queues, real-time pipelines, high-availability systems, observability, or similar. Not generic CRUD.
A job FAILS if ANY of these are true:
- Explicitly requires on-site presence
- Explicitly restricted to US/Asia timezones only with no European overlap
- Junior or internship level
- Non-engineering role
- Crypto/web3/blockchain company or project
- Generic CRUD/SaaS backend with no distributed systems component
- No TypeScript/Node.js involvement
- Strictly frontend role
- HFT or ultra-low-latency systems`,
  },
  {
    name: "fintech-trading-infra-ts",
    prompt: `You evaluate job listings for a senior TypeScript/Node.js backend engineer specializing in real-time trading systems and financial data infrastructure, based in Europe.
A job PASSES if ALL of these are true:
1. The role is remote-friendly OR available to European timezones (CET/EET). Reject if explicitly US-only, on-site only, or requires a specific non-European location.
2. The role is senior or lead level (or doesn't specify level, which is acceptable).
3. The role involves application-layer trading infrastructure: real-time data pipelines, market data systems, trading platforms, or financial backend services.
4. Primary stack involves TypeScript or Node.js.
A job FAILS if ANY of these are true:
- Explicitly requires on-site presence
- Explicitly restricted to US/Asia timezones only with no European overlap
- Junior or internship level
- Non-engineering role
- Generic CRUD/SaaS backend with no real-time or financial data component
- No TypeScript/Node.js involvement
- HFT or ultra-low-latency systems requiring C++/C
- MEV extraction or arbitrage bot development
- Strictly frontend role with no backend or infrastructure component`,
  },
];

export const EVALUATION_FILTERS: EvaluationFilter[] = [];
