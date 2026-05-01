import { DEFAULT_RATES, type ExchangeRates } from "../services/exchangeRates";

export interface EvaluationCriteria {
  name: string;
  prompt: string;
}

export interface EvaluationProfile extends EvaluationCriteria {}
export interface EvaluationFilter extends EvaluationCriteria {}

export const EVALUATION_PROFILES: EvaluationProfile[] = [
  {
    name: "crypto-web3-ts",
    prompt: `You evaluate job listings for a senior/lead backend or fullstack developer focused on crypto/web3. Location eligibility is already verified by a separate filter — do NOT evaluate it. Do not reject based on timezone requirements, office location, or geographic restrictions.
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
    prompt: `You evaluate job listings for a senior backend or fullstack engineer working on trading systems, financial data, or real-time financial infrastructure. Location eligibility is already verified by a separate filter — do NOT evaluate it. Do not reject based on timezone requirements, office location, or geographic restrictions.
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
    prompt: `You evaluate job listings for a senior fullstack engineer with strong React/TypeScript frontend skills. Location eligibility is already verified by a separate filter — do NOT evaluate it. Do not reject based on timezone requirements, office location, or geographic restrictions.
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
  {
    name: "ai-engineering",
    prompt: `You evaluate job listings for a senior backend or fullstack engineer building AI-powered products. Location eligibility is already verified by a separate filter — do NOT evaluate it. Do not reject based on timezone requirements, office location, or geographic restrictions.
A job PASSES if ALL of these are true:
1. The role is senior or lead level (or doesn't specify level, which is acceptable).
2. The role involves building AI-powered products or infrastructure at the application layer: RAG pipelines, LLM integrations, AI agents, AI-powered features, vector databases, prompt engineering infrastructure, or similar. The work is about integrating and deploying AI capabilities into products, not training models from scratch.
3. The role is compatible with a polyglot engineer (TS/Node.js, Go, Rust, Python, Java). PASS if: any of these languages are in the stack, or stack is not specified.
A job FAILS if ANY of these are true:
- Junior or internship level
- Non-engineering role
- Pure ML research or model training role (PhD required, writing papers, training foundation models)
- Data science or analytics role with no engineering component
- DevOps, SRE, or operational MLOps roles where AI is a secondary concern (deploying LLMs to clusters, managing GPU pools, on-call for AI infrastructure, model-serving cluster ops). The role must be engineering — not operating infrastructure. Note: building AI Platform / Agent Platform engineering (model routing, evaluation frameworks, agent runtimes, RAG infrastructure, tool-calling abstractions, context management) IS in scope and PASSES — those are foundational engineering for AI products, not ops.
- Deep data engineering roles where the primary job is data pipeline infrastructure with AI tooling only as a nice-to-have. Look for "Data Engineer" in the title plus must-have skills dominated by data-warehouse tooling (Snowflake, dbt, Airflow, Debezium, CDC pipelines, DMS) without AI/LLM/agent work as the primary function. Note: event streaming (Kafka) appearing in the stack is fine when the role is AI-product-leaning.
- Strictly frontend role
- HFT or ultra-low-latency systems (matching engines, FPGA, C++/C performance-critical)

Examples:
PASS: Startup, senior backend, building RAG pipeline for document search, Python + TypeScript → AI product engineering ✓
PASS: Company, senior fullstack, integrating LLMs into existing product, React + Node.js → AI-powered product ✓
PASS: AI company, senior engineer, building AI agents and tool-use infrastructure → AI application layer ✓
PASS: Senior AI Engineer, event-driven architecture with Kafka, building RAG pipelines → AI product engineering with event streaming ✓
PASS: Senior SWE, AI Platform — model routing, agent architecture, context management, evaluation frameworks for product teams → AI Platform engineering, foundational to AI products ✓
FAIL: ML researcher, PhD required, training large language models → pure ML research, not application engineering
FAIL: Senior LLM Engineer, fine-tuning and distillation, model architectures, Triton/vLLM inference servers → model-training/research primary, not application engineering
FAIL: Data analyst, building dashboards with AI insights → analytics, not engineering
FAIL: Senior DevOps Engineer managing LLM deployments and AI infrastructure → DevOps/infra primary, not product engineering
FAIL: Senior Staff Data Engineer, dbt + Snowflake + Airflow + Debezium for wealth-mgmt warehouse → deep data engineering primary, AI is nice-to-have only`,
  },
];

const REMOTE_FILTER: EvaluationFilter = {
  name: "remote-europe-eligible",
  prompt: `You are a strict location eligibility filter. Your ONLY job is to determine whether a candidate living in Romania (EU) can work this job fully remotely. Ignore everything else (tech stack, seniority, compensation).

# Reading the input

A listing may begin with a "## ATS Structured Data" block. When present, it is employer-set metadata from the ATS (Ashby, Lever, Greenhouse) and is the source of truth for the fields it carries:

- "Workplace type" is authoritative. Hybrid means hybrid; you may only override it if the body explicitly contradicts ("100% remote", "fully remote regardless of location"). Body silence does NOT override Hybrid. (OnSite is filtered upstream — you should not see it; if you ever do, FAIL.)
- "All listed locations" reflects where the employer is actively hiring. Use it to judge geographic eligibility per the rules below.
- "Primary location" is often the HQ city — do not over-index on it as a hiring restriction.
- "Country (HQ)" is the headquartering country, not necessarily the only hiring country.

When there is no ATS block (e.g., Workable listings), rely entirely on the body.

# STEP 1 — Is the role remote?

PASS the remote signal when:
- The ATS block has Workplace type=Remote, or the body indicates remote ("Remote", "Work from anywhere", "Distributed team", "100% remote", "Fully remote", "Remote - Europe", etc.).
- The company is clearly crypto/web3/blockchain — these operate remote-by-default UNLESS there is an explicit on-site signal (named office, requires local work authorization, "on-site"/"in-office" stated). The crypto exception does NOT override an ATS Workplace type=Hybrid signal — Hybrid still requires a body-explicit override.

FAIL the remote signal when:
- ATS Workplace type=Hybrid AND the body does not explicitly contradict it. Body silence is NOT contradiction.
- The body describes regular in-person attendance (weekly/monthly/quarterly), "X days/month in office", "occasional on-site", or any hybrid arrangement.
- "Option to work remotely" framing implies on-site is the default.
- "Remote" but means local-remote to a specific city (e.g., "Remote - San Francisco").
- No remote signal anywhere AND not crypto.

Annual or bi-annual offsites/retreats are acceptable and do NOT count as hybrid.

# STEP 2 — Can someone in Romania/Europe work this role?

Apply the first rule that fits, in order:

A. Body says "worldwide", "employees worldwide", "anywhere", or "global" → PASS, regardless of any ATS country list. Country lists often reflect legal entities, not restrictions.

B. Body explicitly restricts to non-EU regions ("US only", "APAC only", "must be authorized to work in [non-EU]") → FAIL.

C. CHEAP-COUNTRY SKEW. The ATS lists multiple locations and a meaningful share of them are non-EU low-comp markets (India, Pakistan, Egypt, Philippines, Bangladesh, Indonesia, Vietnam, Serbia, Georgia, Armenia, etc.) → FAIL. The presence of a token EU country (e.g., Spain) does NOT rescue a cheap-country-skewed listing — the company is plausibly hiring at cheap-market rates.

D. ATS lists multiple locations including any EU country (Spain, Portugal, Germany, Romania, Ireland, Greece, Netherlands, etc.) AND no cheap-country skew → PASS. The company hires across the EU and Romania need not be in the list explicitly.

E. ATS lists multiple locations, all non-EU, none of which is EU-eligible (e.g., US, Canada, Mexico, Brazil) → FAIL.

F. ATS lists a single non-EU country (US, Canada, Singapore, etc.) AND the body is silent on geo eligibility → FAIL. The single-country signal is a hiring restriction.

G. ATS lists only EU countries, or a single EU country → PASS.

H. No ATS block, body says nothing about geo, non-crypto company → FAIL.

I. Body says "UK-based or Europe with UK-hours overlap", "EMEA", "EET/CET" → PASS. Europe includes Romania.

J. Body says "US business hours" or "US East Coast hours" without excluding Europeans → PASS. Romania (EET, UTC+2) overlaps US East Coast morning.

# Examples (worked decisions across the rules above)

[Step 1 — remote signal]
PASS: "We are a fully remote team distributed across Europe." → body remote ✓, EU eligible ✓
PASS: "Remote (Worldwide)" → body remote ✓, body worldwide → rule A ✓
PASS: "DeFi protocol, our team works from anywhere." → crypto exception ✓, anywhere ✓
PASS: Crypto company, no location info mentioned → crypto exception ✓
FAIL: Crypto exchange, "prioritising applicants who have a current right to work in Hong Kong" → explicit on-site signal overrides crypto exception
PASS: "A supportive remote environment. Two annual in-person team meet-ups." → remote ✓, annual offsites OK
FAIL: "A highly flexible remote work policy, 2 days at the office per month" → 2 days/month in office = hybrid
FAIL: "Full-time position in Prague. Option to work remotely." → on-site default, remote optional
FAIL: No location or remote info mentioned, non-crypto company → no remote signal

[ATS Workplace type interactions]
FAIL: ATS Workplace type=Hybrid, locations="San Francisco, New York", body describes role/stack but says nothing about workplace arrangement → Hybrid + body silent → rule on Step 1 FAIL
PASS: ATS Workplace type=Hybrid, country=Spain, body says "Work 100% remotely, with the option to use our offices in Málaga or Barcelona if you're nearby" → body explicitly contradicts Hybrid ✓
PASS: ATS Workplace type=Remote, locations include Portugal/Spain/UK/Ireland alongside non-EU markets, body says "remote-first" → rule D, EU members in list ✓

[Country / cheap-country handling]
FAIL: ATS Workplace type=Remote, country=United States, locations=[Remote], body discusses role/stack but is silent on geo → rule F, single non-EU country + body silent
PASS: ATS Workplace type=Remote, country=United States, body says "we hire globally regardless of location, employees in 25+ countries" → rule A, body says global
FAIL: ATS Workplace type=Remote, locations="United States, Canada, Mexico, Brazil", body silent on geo → rule E, all non-EU
FAIL: ATS Workplace type=Remote, locations="India, Pakistan, Egypt, Philippines, Spain", body says "fully remote", country=India → rule C, cheap-country skew with token EU country
PASS: ATS Workplace type=Remote, locations="Spain" only → rule G, single EU country
PASS: ATS Workplace type=Remote, locations="Spain, Portugal, Germany" → rule G, all EU
FAIL: "Remote - US only" → rule B, explicit non-EU restriction
FAIL: "Dublin, Ireland — Hybrid" → hybrid + Ireland-only

[Body overrides ATS metadata]
PASS: Header says "USA and Global (Hybrid)" but body says "team members all over the world" → rule A, body says worldwide
PASS: Header says "The Netherlands (remote)" but body says "this role is not office-based, candidate can be in any EMEA country" → body explicit EMEA ✓
PASS: Location metadata "Canada; Portugal; UK; USA" + body "remote-first organization with employees worldwide" → rule A, body worldwide

[Hours / region phrasing]
PASS: "UK-based or Europe with significant UK hours overlap" → Europe includes Romania ✓
PASS: "Work around U.S. business hours" or "US East Coast hours" → Romania (EET) overlaps US East morning ✓`,
};

/** Currencies to include in the compensation filter prompt (when available in rates). */
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

function buildCompensationFilter(rates: ExchangeRates): EvaluationFilter {
  const rateLines = PROMPT_CURRENCIES.filter((c) => c in rates)
    .map((c) => `1 ${c} ≈ ${(rates[c] as number).toFixed(2)} USD`)
    .join(", ");

  return {
    name: "compensation-minimum",
    prompt: `You are a compensation filter. Your ONLY job is to determine whether the listed compensation meets a minimum threshold. Ignore everything else (location, tech stack, seniority, company).

RULES:
1. If NO salary, compensation, or rate is mentioned anywhere in the listing → PASS. Most job listings do not include compensation, and that is fine.
2. If compensation IS mentioned:
   - Annual salary: PASS if the maximum of the stated range is ≥ $130,000/year. FAIL if the maximum is below $130,000/year.
   - Hourly rate (contractor): PASS if the maximum of the stated range is ≥ $65/hour. FAIL if the maximum is below $65/hour.
   - Monthly rate: convert to annual (×12). Apply the $130,000/year threshold.
   - Non-USD currencies: convert approximately to USD before comparing. Use these rates: ${rateLines}.
3. Only evaluate base salary/rate. Ignore equity, bonuses, or total compensation packages — focus on the stated cash compensation.
4. When in doubt, PASS. This filter should only reject listings with clearly stated compensation below the threshold. If the math is ambiguous, the currency is unclear, or you're unsure whether a number refers to salary, PASS.

Examples:
PASS: No salary mentioned anywhere → no compensation info, pass by default ✓
PASS: "$150,000 - $200,000" → max $200k ≥ $130k ✓
PASS: "Salary range between $200,000 - $250,000" → max $250k ≥ $130k ✓
PASS: "$100/hr" → $100/hr ≥ $65/hr ✓
PASS: "€120,000 - €150,000" → max €150k ≈ $165k ≥ $130k ✓
FAIL: "$40 - $50/hr" → max $50/hr < $65/hr
FAIL: "$80,000 - $100,000 per year" → max $100k < $130k
FAIL: "$3,000/month" → $36k/year < $130k`,
  };
}

const ROLE_QUALITY_FILTER: EvaluationFilter = {
  name: "role-quality",
  prompt: `You are a role-quality filter. Your ONLY job is to detect listings whose role shape, stack, or seniority bar makes them a poor fit for a senior backend-leaning fullstack engineer. Ignore location, compensation, and domain — those are evaluated by other filters and profiles.

The candidate is a polyglot whose primary stack is TypeScript/Node.js, with strong working knowledge of Go, Rust, and Python. They have ~6 years of experience. They prefer hands-on senior or lead roles.

FAIL if ANY of these apply:

1. ENTERPRISE STACK — the must-have/primary backend stack is dominated by enterprise languages with no modern alternative:
   - "Spring Boot", "Spring Cloud", "Java/Spring", "JPA" required as the backend stack
   - ".NET", "ASP.NET", "ASP.NET Core", "C#" required as the backend stack
   - "Scala" required as the primary backend
   - "C++" in must-have backend skills
   - Backend language list contains ONLY enterprise languages (e.g., "Java, C#, or Scala") with no TS/Node/Go/Rust/Python as primary alternatives
   PASS if Java/.NET/C#/Scala appears only as nice-to-have, secondary, or as one of many options that includes TS/Node/Go/Rust/Python.

2. PURE ARCHITECT, NO IC — the role is purely architectural with no individual-contributor / hands-on coding work:
   - Title contains "Architect" AND responsibilities are vision/strategy/leadership only
   - No "build", "code", "ship", "develop", "implement", or "write" appear as primary actions
   - Reads as "drive technical strategy", "lead architecture", "long-term planning" without concrete delivery
   PASS if the role is "Architect/Lead" but the responsibilities include hands-on coding, building features, or shipping production code.

3. SOLUTIONS / FDE / FIELD ENGINEER — primarily customer-facing technical:
   - Title contains "Solutions Engineer", "Forward Deployed Engineer", "Customer Engineer", "Field Engineer", "Sales Engineer"
   - Body emphasizes "work directly with customers", "strategic accounts", "voice of the developer community", "translate customer needs"
   PASS if the title is normal Software/Backend/Fullstack engineer even if customer collaboration is mentioned in passing.

4. PURE DATA ENGINEERING — primary work is data plumbing, not product engineering:
   - Title contains "Data Engineer" AND must-have skills are dominated by data-warehouse / pipeline tooling
   - Required skills focus on Snowflake, dbt, Airflow, Debezium, CDC pipelines, DMS, OpenFlow, BigQuery
   - AI/LLM tooling is only mentioned as nice-to-have or "intelligent insights"
   PASS if the role builds AI products and uses some data tooling along the way (Kafka for event streaming is fine).

5. TOO-SENIOR BAR — explicit "10+ years" or "12+ years" requirement at Principal/Distinguished level. Roles asking for 5-9 years pass.

6. INTERVIEW PROCESS DISCLOSED AS 4+ SYNCHRONOUS ROUNDS — the listing publishes a multi-stage process with 4 or more synchronous interview rounds (recruiter screen, behavioral, technical, system-design, culture-fit, hiring-manager call, etc.). DO NOT count: take-home assignments, async coding tests, reference checks, offers, application reviews — these are not interview rounds. PASS if 1-3 synchronous rounds are described, or if no process is disclosed.

7. NON-ENGLISH BODY — substantial non-English text leaks into the description (Russian, Chinese, Arabic, etc.) that isn't a translated UI element or stated language requirement. A single mid-sentence non-English token strongly suggests a non-English-primary team.

When in doubt, PASS. This filter should only reject listings where one of the above signals is clearly present in the body. Borderline cases stay in the pile for the user to decide.

Examples:

PASS: "Stack: TypeScript, React, Go, Python" → modern polyglot, no enterprise BS ✓
PASS: "Backend: Node.js, Python; familiarity with Java is a plus" → Java is plus, not must-have ✓
PASS: "5+ years software engineering, building RAG pipelines with Python" → standard senior bar, AI-product role ✓
PASS: "Senior Architect, Blockchain & DeFi — hands-on, dig deep into the code" → architect + IC content ✓
PASS: "Senior Software Engineer; some customer collaboration" → not primarily FDE ✓
FAIL: "Must have: Java 11+, Spring Boot, Spring Cloud" → enterprise Java/Spring stack ✓ enterprise
FAIL: "10+ years experience; production with Java, Golang, or C++" → 10+ bar AND enterprise language alternatives without modern primary ✓
FAIL: "Software Engineer, Solutions — be the technical partner for top crypto teams" → Solutions Engineer customer-facing role
FAIL: "Senior Staff Data Engineer; dbt, Snowflake, Airflow, Debezium primary" → pure data engineering, AI not central
FAIL: "Senior Backend Developer (Node.js); ... оптимизация под нагрузкой ..." → Russian text in otherwise-English body, non-English-primary team
FAIL: "Interview Process: 1) Talent screen 2) Behavioral 3) Two 90-min technicals 4) Culture-add" → 4+ synchronous rounds disclosed
PASS: "Process: 1) Initial screening 2) Live pairing session 3) Founder call 4) Offer" → "Offer" is not an interview round, only 3 synchronous rounds ✓
PASS: "Process: phone screen, take-home assignment, on-site loop, reference check" → take-home and reference check don't count as synchronous rounds ✓
FAIL: "Senior Software Engineer; design, drive strategy, define vision; lead architecture across teams; promote best practices" → architect-only framing with no IC content
PASS: "Senior Engineer; design and build production systems; ship features end-to-end; mentor juniors" → leadership + IC ✓`,
};

export function getEvaluationFilters(rates?: ExchangeRates): EvaluationFilter[] {
  return [REMOTE_FILTER, buildCompensationFilter(rates ?? DEFAULT_RATES), ROLE_QUALITY_FILTER];
}
