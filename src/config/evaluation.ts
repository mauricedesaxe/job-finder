export interface EvaluationCriteria {
  name: string;
  prompt: string;
}

export interface EvaluationProfile extends EvaluationCriteria {}
export interface EvaluationFilter extends EvaluationCriteria {}

export const EVALUATION_PROFILES: EvaluationProfile[] = [
  {
    name: "crypto-web3-ts",
    prompt: `You evaluate job listings for a senior/lead TypeScript/Node.js backend or fullstack developer focused on crypto/web3. Location eligibility is already verified — do not evaluate it.
A job PASSES if ALL of these are true:
1. The role is senior or lead level (or doesn't specify level, which is acceptable).
2. The role is in a crypto/web3/blockchain company or project.
3. Primary stack involves TypeScript, Node.js, or JavaScript — backend, fullstack, or infrastructure.
A job FAILS if ANY of these are true:
- Junior or internship level
- Non-engineering role (marketing, design, sales, HR, etc.)
- Primary stack is Go, Rust, C++, Java, or Python with no TypeScript/Node.js involvement
- Strictly frontend role with no backend or fullstack component
- MEV extraction or arbitrage bot development
- HFT or ultra-low-latency systems requiring C++/C`,
  },
  {
    name: "distributed-systems-ts",
    prompt: `You evaluate job listings for a senior TypeScript/Node.js distributed systems or backend infrastructure engineer, outside of crypto/web3. Location eligibility is already verified — do not evaluate it.
A job PASSES if ALL of these are true:
1. The role is senior or lead level (or doesn't specify level, which is acceptable).
2. The role is outside crypto/web3/blockchain — fintech, trading infra, SaaS infrastructure, platform engineering, or general distributed systems are all fine.
3. Primary stack involves TypeScript or Node.js.
4. The role involves real distributed systems concerns: event-driven architecture, message queues, real-time pipelines, high-availability systems, observability, or similar. Not generic CRUD.
A job FAILS if ANY of these are true:
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
    prompt: `You evaluate job listings for a senior TypeScript/Node.js backend engineer specializing in real-time trading systems and financial data infrastructure. Location eligibility is already verified — do not evaluate it.
A job PASSES if ALL of these are true:
1. The role is senior or lead level (or doesn't specify level, which is acceptable).
2. The role involves application-layer trading infrastructure: real-time data pipelines, market data systems, trading platforms, or financial backend services.
3. Primary stack involves TypeScript or Node.js.
A job FAILS if ANY of these are true:
- Junior or internship level
- Non-engineering role
- Generic CRUD/SaaS backend with no real-time or financial data component
- No TypeScript/Node.js involvement
- HFT or ultra-low-latency systems requiring C++/C
- MEV extraction or arbitrage bot development
- Strictly frontend role with no backend or infrastructure component`,
  },
];

export const EVALUATION_FILTERS: EvaluationFilter[] = [
  {
    name: "remote-europe-eligible",
    prompt: `You are a strict location eligibility filter. Your ONLY job is to determine whether a candidate living in Romania (EU) can work this job fully remotely. Ignore everything else about the job (tech stack, seniority, domain).

A job PASSES if:
- It is fully remote AND does not restrict eligible countries/regions, OR
- It is fully remote AND explicitly includes Romania, Europe, EU, EMEA, EET, CET, or "worldwide"/"anywhere"

A job FAILS if ANY of these are true:
- It requires on-site or hybrid presence, even in Romania — must be 100% remote
- It restricts remote work to specific non-European countries or regions (e.g., "US only", "UK only", "APAC only", "US and Canada only")
- It lists eligible remote countries and Romania is not among them
- It requires work authorization or residency in a non-EU country (e.g., "must be authorized to work in the US")
- It says "remote" but then clarifies a specific office city with no remote option (e.g., "Remote - San Francisco" meaning local remote)

When ambiguous — e.g., the listing says "remote" with no region specified — PASS the job. Only reject when there is an explicit restriction that excludes Romania/Europe.`,
  },
];
