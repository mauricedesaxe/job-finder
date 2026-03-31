export interface EvaluationCriteria {
  name: string;
  prompt: string;
}

export interface EvaluationProfile extends EvaluationCriteria {}
export interface EvaluationFilter extends EvaluationCriteria {}

export const EVALUATION_PROFILES: EvaluationProfile[] = [
  {
    name: "crypto-web3-ts",
    prompt: `You evaluate job listings for a senior/lead backend or fullstack developer focused on crypto/web3. Location eligibility is already verified — do not evaluate it.
A job PASSES if ALL of these are true:
1. The role is senior or lead level (or doesn't specify level, which is acceptable).
2. The role is in or serves the crypto/web3/blockchain space. This includes: crypto-native companies, agencies/studios/consultancies whose specific role or department works on crypto/trading/DeFi projects, or roles that explicitly mention crypto/blockchain/trading platforms as the work domain.
3. The role is compatible with a polyglot engineer who works primarily in TypeScript/Node.js but also knows Go, Rust, and other languages. PASS if ANY of these stack conditions are true:
   a. TS/Node.js/JavaScript is listed anywhere in the stack (primary, secondary, or "nice to have")
   b. The listing mentions Go, Rust, or Java for backend — the candidate is multilingual and can work in these
   c. The tech stack is NOT specified in the listing — PASS this criterion. Many crypto companies are language-flexible and decide during interviews. Do NOT fail a job just because it doesn't mention a specific programming language.
   d. Fullstack role with React or TS frontend, even if backend uses Go, Rust, or another language
   IMPORTANT: Only FAIL on stack if the role explicitly requires a niche language the candidate cannot use (e.g., Haskell, Erlang) with zero overlap with TS/Go/Rust/Java.
A job FAILS if ANY of these are true:
- Junior or internship level
- Non-engineering role (marketing, design, sales, HR, etc.)
- The role is exclusively for a Solidity/smart-contract auditor with no application-layer engineering
- Strictly frontend role with no backend or fullstack component
- MEV extraction or arbitrage bot development
- HFT or ultra-low-latency systems (matching engines, FPGA, kernel-level networking, C++/C performance-critical systems)

Examples:
PASS: Crypto company, senior backend, TypeScript/Node.js stack → all criteria met
PASS: Web3 protocol, senior backend, stack not specified → crypto + senior + stack-flexible ✓
PASS: DeFi company, fullstack with React frontend + Go backend → fullstack with TS-compatible frontend ✓
PASS: Blockchain company, Solana engineer, requires Node.js + Rust → multilingual stack includes Node.js ✓
PASS: Crypto agency/studio building trading platforms, React + Go → crypto project + TS-compatible ✓
PASS: Cross-chain protocol, senior backend, stack not specified, "experience with smart contract development" as qualification → backend infra role with blockchain integration, NOT a smart-contract-only auditor ✓
FAIL: Blockchain company hiring Solidity auditor only → no application-layer engineering
FAIL: Crypto exchange, C++ matching engine engineer → HFT/ultra-low-latency
FAIL: Web3 company, marketing manager → non-engineering role`,
  },
  {
    name: "fintech-trading-infra-ts",
    prompt: `You evaluate job listings for a senior backend or fullstack engineer working on trading systems, financial data, or real-time financial infrastructure. Location eligibility is already verified — do not evaluate it.
A job PASSES if ALL of these are true:
1. The role is senior or lead level (or doesn't specify level, which is acceptable).
2. The role involves trading infrastructure, real-time data pipelines, market data systems, trading platforms, DeFi trading, crypto trading platforms, or financial backend services.
3. The role is compatible with a polyglot engineer (TS/Node.js, Go, Rust, Java). PASS if: TS/Node.js is in the stack, or Go/Rust/Java is listed, or stack is not specified, or fullstack with React/TS frontend.
A job FAILS if ANY of these are true:
- Junior or internship level
- Non-engineering role
- Generic CRUD/SaaS backend with no real-time or financial data component
- HFT or ultra-low-latency systems (matching engines, FPGA, kernel-level networking, C++/C performance-critical)
- MEV extraction or arbitrage bot development
- Strictly frontend role with no backend or infrastructure component

Examples:
PASS: Crypto trading platform, senior fullstack, React + Go → trading infra + polyglot ✓
PASS: Fintech company, senior backend, real-time market data, TypeScript → trading infra + TS ✓
PASS: Agency building trading platforms for clients, senior fullstack, React + Go → trading infra ✓
FAIL: C++ matching engine engineer at hedge fund → HFT/ultra-low-latency
FAIL: Generic fintech CRUD API, no real-time component → no trading/real-time element`,
  },
  {
    name: "senior-fullstack-react",
    prompt: `You evaluate job listings for a senior fullstack engineer with strong React/TypeScript frontend skills. Location eligibility is already verified — do not evaluate it.
A job PASSES if ALL of these are true:
1. The role is senior or lead level (or doesn't specify level, which is acceptable).
2. The role is fullstack — involves BOTH frontend and backend work.
3. The frontend stack includes React, Next.js, or TypeScript/JavaScript UI work.
4. The backend can be any language (Node.js, Go, Rust, Python, Java — the candidate is polyglot).
A job FAILS if ANY of these are true:
- Junior or internship level
- Non-engineering role
- Strictly backend role with no frontend component
- Strictly frontend role with no backend component
- HFT or ultra-low-latency systems (matching engines, FPGA, C++/C performance-critical)
- The frontend is NOT React/TypeScript (e.g., Angular-only, Swift/iOS, Android/Kotlin)

Examples:
PASS: Agency, senior fullstack, React + Go, building trading platforms → fullstack + React ✓
PASS: Startup, senior fullstack, React + Node.js → fullstack + React ✓
PASS: Crypto company, senior full stack engineer, React Native + Go → fullstack + React ecosystem ✓
FAIL: Senior backend engineer, Go, no frontend → no frontend component
FAIL: Senior iOS developer, Swift → not React/TS frontend`,
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
FAIL if the body says "X days/month in office", "occasional on-site", or describes a hybrid arrangement in the body text.
FAIL if "option to work remotely" implies on-site is the default arrangement.
DO NOT FAIL just because a header/tag says "Hybrid" — that alone is not evidence. You need the body to confirm hybrid requirements.
FAIL if it says "remote" but means local-remote to a specific city (e.g., "Remote - San Francisco").
Note: annual or bi-annual team retreats/offsites are acceptable and do NOT count as hybrid.

STEP 3 — Can someone in Romania/Europe work this role?
As with step 2, prioritize the job description body over structured metadata fields when they conflict. Country lists in metadata headers are often incomplete or reflect hiring entity locations, not actual eligibility restrictions.
If the body says "worldwide", "employees worldwide", "anywhere", or "global" — PASS immediately, regardless of any country list in metadata headers. Country lists in metadata often reflect where the company has legal entities, not actual hiring restrictions.
FAIL if the body explicitly restricts remote work to non-European regions (e.g., "US only", "APAC only") with no broader worldwide/global language.
FAIL if it requires work authorization in a non-EU country.
PASS if no geographic restriction, or if it includes Romania, Europe, EU, EMEA, EET, CET, "worldwide", "anywhere", or includes any EU country (suggesting EU eligibility).

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
PASS: Header says "The Netherlands (remote)" but body says "this role is not office-based, candidate can be in any EMEA country" → body overrides header, remote ✓, EMEA ✓
PASS: Location metadata lists "Canada; Portugal; UK; USA" but body says "remote-first organization with employees worldwide" → body says worldwide, metadata country list is just where they have entities, not a restriction ✓`,
  },
];
