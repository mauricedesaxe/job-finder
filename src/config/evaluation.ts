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
    prompt: `You are a strict location eligibility filter. Your ONLY job is to determine whether a candidate living in Romania (EU) can work this job fully remotely. Ignore everything else (tech stack, seniority, compensation).

STEP 1 — Does the listing explicitly indicate the role is remote?
Look for a clear signal: "Remote", "Work from anywhere", "Distributed team", "100% remote", "Fully remote", location listed as "Remote", "Remote - Europe", etc.
If NO remote signal exists → FAIL. Do not infer remote from silence.
Exception: crypto/web3/blockchain companies commonly operate fully remote. If the company is clearly in crypto/web3, you may PASS even without an explicit remote mention — UNLESS the listing contains an explicit on-site signal (e.g., requires local work authorization, names a specific office, or says "on-site"/"in-office"). In that case, treat it like any other company and FAIL.

STEP 2 — Is it truly 100% remote with zero required in-person days?
IMPORTANT: Job board platforms (Greenhouse, Ashby, Lever, Workable) often have structured metadata fields (location headers, tags) that say "Hybrid" or list a single city. These labels are frequently inaccurate and MUST NOT be trusted on their own. The word "Hybrid" in a header/tag does NOT count as evidence of hybrid work if the job description body does not describe any in-person requirements. Always base your decision on the actual job description body text. Only classify a role as hybrid if the body explicitly describes regular in-person attendance.
FAIL if the job description body describes regular in-person attendance (weekly, monthly, quarterly).
FAIL if the body says "X days/month in office", "occasional on-site", or describes a hybrid arrangement.
FAIL if "option to work remotely" implies on-site is the default arrangement.
FAIL if it says "remote" but means local-remote to a specific city (e.g., "Remote - San Francisco").
Note: annual or bi-annual team retreats/offsites are acceptable and do NOT count as hybrid.

STEP 3 — Can someone in Romania/Europe work this role?
FAIL if remote work is restricted to non-European regions (e.g., "US only", "APAC only").
FAIL if it lists eligible countries and Romania/EU is not included.
FAIL if it requires work authorization in a non-EU country.
PASS if no geographic restriction, or if it includes Romania, Europe, EU, EMEA, EET, CET, "worldwide", or "anywhere".

Examples:

PASS: "We are a fully remote team distributed across Europe." → remote ✓, fully remote ✓, Europe ✓
PASS: "Remote (Worldwide)" → remote ✓, fully remote ✓, worldwide ✓
PASS: "DeFi protocol, our team works from anywhere." → crypto + remote signal ✓, fully remote ✓, anywhere ✓
PASS: Crypto company, no location info mentioned → crypto exception ✓
FAIL: Crypto exchange, "prioritising applicants who have a current right to work in Hong Kong" → crypto but explicit on-site signal overrides exception
PASS: "A supportive remote environment. Two annual in-person team meet-ups." → remote ✓, annual offsites are fine ✓
PASS: "Remote" with no region mentioned → remote ✓, no in-person req ✓, no restriction ✓
FAIL: "A highly flexible remote work policy, 2 days at the office per month" → 2 days/month in office = regular hybrid attendance
FAIL: "Full-time position in Prague. Option to work remotely." → on-site is default, remote is just an option
FAIL: No location or remote info mentioned, non-crypto company → no remote signal at all
FAIL: "Remote - US only" → restricted to US
FAIL: "Dublin, Ireland — Hybrid" → hybrid, and Ireland-only
PASS: Header says "USA and Global (Hybrid)" but body says "team members all over the world" → body overrides misleading header metadata, remote ✓, global ✓
PASS: Header says "The Netherlands (remote)" but body says "this role is not office-based, candidate can be in any EMEA country" → body overrides header, remote ✓, EMEA ✓`,
  },
];
