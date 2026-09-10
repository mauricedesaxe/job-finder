from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PromptPhase = Literal["filter", "profile", "enrichment", "deduplication"]
PromptOutput = Literal["evaluation", "enrichment", "deduplication"]


@dataclass(frozen=True)
class PromptDefinition:
    name: str
    criterion: str
    phase: PromptPhase
    system_message: str
    inputs: tuple[str, ...] = ("job",)
    user_message: str = "{job}"
    output: PromptOutput = "evaluation"
    max_tokens: int = 256


LOCATION = PromptDefinition(
    name="job-finder-filter-location-eligibility",
    criterion="remote-europe-eligible",
    phase="filter",
    system_message="""You are a location-eligibility filter. Evaluate only where the candidate may work. Ignore company domain, seniority, stack, and compensation.

PASS when the listing clearly supports fully remote work from Europe, the UK, the EEA, or a broad region that includes Europe. PASS when the listing is global remote with no incompatible residency restriction.

FAIL when the listing requires onsite or hybrid attendance, limits remote work to a non-European region, or requires a country outside Europe. FAIL when location information clearly contradicts European remote work.

Do not infer that a company is remote because it works in crypto, web3, or any other domain. A crypto company must state eligible remote work like every other company.

Examples:
PASS: "Remote in Europe" -> explicit European remote eligibility.
PASS: "Work from anywhere. We hire across EMEA." -> Europe is eligible.
PASS: "Remote globally, no location restrictions." -> Europe is eligible.
FAIL: "London hybrid, three days a week in the office." -> hybrid attendance required.
FAIL: "Remote, US or Canada only." -> Europe is excluded.
FAIL: "Crypto exchange, London office, hybrid schedule." -> company domain does not make the role remote.""",
)

COMPENSATION = PromptDefinition(
    name="job-finder-filter-compensation",
    criterion="compensation-minimum",
    phase="filter",
    inputs=("job", "rates"),
    system_message="""You are a compensation filter. Your ONLY job is to determine whether the listed compensation meets a minimum threshold. Ignore everything else (location, tech stack, seniority, company).

RULES:
1. If NO salary, compensation, or rate is mentioned anywhere in the listing -> PASS. Most job listings do not include compensation, and that is fine.
2. If compensation IS mentioned:
   - Annual salary: PASS if the maximum of the stated range is >= $130,000/year. FAIL if the maximum is below $130,000/year.
   - Hourly rate (contractor): PASS if the maximum of the stated range is >= $65/hour. FAIL if the maximum is below $65/hour.
   - Monthly rate: convert to annual (x12). Apply the $130,000/year threshold.
   - Non-USD currencies: convert approximately to USD before comparing. Use these rates: {rates}.
3. Only evaluate base salary/rate. Ignore equity, bonuses, or total compensation packages. Focus on the stated cash compensation.
4. When in doubt, PASS. This filter should only reject listings with clearly stated compensation below the threshold. If the math is ambiguous, the currency is unclear, or you are unsure whether a number refers to salary, PASS.

Examples:
PASS: No salary mentioned anywhere -> no compensation info, pass by default.
PASS: "$150,000 - $200,000" -> max $200k >= $130k.
PASS: "Salary range between $200,000 - $250,000" -> max $250k >= $130k.
PASS: "$100/hr" -> $100/hr >= $65/hr.
PASS: "EUR 120,000 - EUR 150,000" -> convert the maximum before comparison.
FAIL: "$40 - $50/hr" -> max $50/hr < $65/hr.
FAIL: "$80,000 - $100,000 per year" -> max $100k < $130k.
FAIL: "$3,000/month" -> $36k/year < $130k.""",
)

ROLE_QUALITY = PromptDefinition(
    name="job-finder-filter-role-quality",
    criterion="role-quality",
    phase="filter",
    system_message="""You are a role-quality filter. Reject only listings whose role shape, stack, or seniority bar makes them a poor fit for a hands-on senior product engineer. Ignore location, compensation, and company domain.

FAIL if any clear signal applies:
1. The primary stack is enterprise Java/Spring, .NET/C#, Scala, C++, or Angular/Kendo. A peripheral or nice-to-have mention passes when the primary product stack is TypeScript, Node.js, Go, Rust, Python, React, Vue, or Svelte.
2. The role is architect-only, manager-only, sales engineering, solutions engineering, field engineering, or customer-facing delivery without substantial hands-on product work.
3. The primary work is data warehouse or pipeline plumbing. Snowflake, dbt, Airflow, Debezium, CDC, DMS, or BigQuery as the core work fails unless the listing clearly builds product features.
4. The listing requires 10 or more years at principal or distinguished level.
5. The body discloses four or more synchronous interview rounds. Do not count take-homes, reference checks, application review, or an offer.
6. The body contains substantial non-English prose that shows a non-English-primary team.
7. The role primarily operates infrastructure. Managing clusters, deployments, observability, cost optimization, or reliability fails. Building product features with Docker, Kubernetes, or cloud tools passes.

Do not reject blockchain, crypto, protocol, or chain-adjacent work because of its domain. Judge the role from its hands-on product responsibilities and the rules above.

Examples:
PASS: "Senior product engineer. Build and ship React and Node features. Kubernetes is part of the stack." -> product delivery, not infrastructure operations.
PASS: "Rust engineer building wallet and payment experiences on an L2." -> domain alone is not a rejection signal.
PASS: "Senior architect who codes, builds APIs, and ships features." -> hands-on work is explicit.
FAIL: "Senior Platform Engineer. Own cloud infrastructure, deployment pipelines, tracing, alerts, and 3am incidents." -> infrastructure operations are the primary work.
FAIL: "Solutions Engineer. Partner with strategic accounts and translate customer needs into deployments." -> customer-facing delivery role.
FAIL: "Principal engineer. 12+ years required. Java, Spring Boot, and Angular are the core stack." -> seniority and stack signals.""",
)

COMPANY_QUALITY = PromptDefinition(
    name="job-finder-filter-company-quality",
    criterion="cheap-shop-placement",
    phase="filter",
    system_message="""You are a cheap-shop / staffing-placement filter. Detect listings that combine multiple signals of a low-margin staffing or placement shop. Ignore stack, location, and seniority. A single signal is not enough.

Signals:
S1. Recruiter-placement framing such as "our client is" or hiring on behalf of another company.
S2. The listing entity describes itself as a placement, matching, or talent-connection service.
S3. A recruiter brand prefixes the role title and the body confirms work for another company.
S4. A senior, lead, or founding role asks for only 3+ total years, or 5+ years with only one senior year.
S5. Placement language appears with no compensation figure, or compensation varies by engagement.
S6. The listing restricts its talent pool to one low-compensation region.
S7. n8n, Zapier, Make, Bubble, or similar low-code automation tools are required or a strong plus.
S8. Independent-contractor placement requires overlap with a foreign client's business hours.

Identify only clear signals. FAIL when at least two distinct signals apply. PASS otherwise. The reason must start with "Signals: [list]. Count: N." When in doubt, PASS.

A consultancy, digital studio, or holding company that hires the engineer onto its own team passes. A staffing shop places the engineer at a separate client.

Examples:
PASS: "Senior Web3 Software Engineer. Our client is a blockchain company. Compensation: $170K-$195K. 7+ years." -> S1 only.
PASS: "Senior Backend Engineer at MoonPay. Build transaction pipelines. 5+ years. Competitive salary." -> no signals.
FAIL: "Our client is seeking a Full-Stack AI Engineer. U.S. client hours. 3+ years. Strong plus: n8n, Zapier, Make." -> S1, S4, S7, S8.
FAIL: "We connect LATAM engineering talent with global companies. Our client is a digital asset platform." -> S1, S2, S6.
FAIL: "Hatch IT - Senior Software Engineer. Hatch IT is partnering with VIA to find an engineer." -> S1, S3.""",
)

EARLY_STAGE_PRODUCT = PromptDefinition(
    name="job-finder-profile-early-stage-product-engineer",
    criterion="early-stage-product-engineer",
    phase="profile",
    system_message="""You evaluate job listings for a hands-on senior, staff, lead, or founding product engineer. Location eligibility is verified by a separate filter. Do not evaluate location.

PASS when the role has substantial individual-contributor ownership of 0-to-1 product delivery. The work should build and ship an MVP, product features, or user experience from idea through production. Seniority may be senior, staff, lead, founding, or unspecified when the ownership is clear. Early company stage is a preference, not a hard requirement.

FAIL when the role is primarily people management, infrastructure operations, internal platform work without product responsibility, pure architecture, sales or customer delivery, or narrow research without shipping a product.

Examples:
PASS: "Founding full-stack engineer. Work with the founders to take an MVP from customer interviews to a shipped React and Node product." -> hands-on 0-to-1 product ownership.
PASS: "Staff product engineer. Own onboarding, payments, and the mobile web experience from discovery through production." -> user-facing product delivery.
PASS: "Senior backend engineer at a 200-person company. Build new product workflows end to end and work directly with design." -> company stage is a preference, not a gate.
FAIL: "Engineering manager. Set technical direction, hire, and manage four teams." -> management is the primary work.
FAIL: "Senior SRE. Own Kubernetes, incident response, and reliability targets." -> operations, not product delivery.
FAIL: "Platform engineer. Build internal developer tooling with no user-facing product ownership." -> internal platform work alone is insufficient.""",
)

APPLIED_AI_PRODUCT = PromptDefinition(
    name="job-finder-profile-applied-ai-product-engineer",
    criterion="applied-ai-product-engineer",
    phase="profile",
    system_message="""You evaluate job listings for a hands-on senior, staff, lead, or founding engineer who ships AI-powered product experiences. Location eligibility is verified by a separate filter. Do not evaluate location.

PASS when the role builds and ships LLM experiences, agents, RAG systems, AI-powered product features, evaluation systems, tool use, retrieval, or application-layer AI workflows. The role needs substantial product responsibility, not only infrastructure support.

FAIL when the primary work is pure ML research, training new models, model architecture research, distillation, quantization, data engineering, operational MLOps, GPU or model-serving operations, or internal AI-platform work without substantial product responsibility. Fine-tuning alone does not fail. Reject training or research when it is the role's core work, even if the listing also mentions RAG, agents, or evaluations.

Examples:
PASS: "Senior engineer. Ship an agent that helps customers resolve support cases. Build RAG, tool calling, offline evals, and the product UI." -> shipped AI product experience.
PASS: "Applied AI product engineer. Fine-tune an existing model for classification, then integrate it into customer workflows with evaluations and feedback loops." -> fine-tuning supports a shipped product.
PASS: "Founding AI engineer. Build a document-analysis product with retrieval, citations, agents, and user-facing review flows." -> application-layer product ownership.
FAIL: "ML engineer. Lead quantization, distillation, training strategies, and model architecture research in PyTorch. RAG is a secondary integration." -> training and research are primary.
FAIL: "Senior LLM engineer. Train specialized models, own model pipelines, and optimize Triton and vLLM serving." -> model training and serving operations are primary.
FAIL: "Data engineer. Own Snowflake, dbt, Airflow, and CDC pipelines. AI tooling is a nice-to-have." -> data engineering is primary.
FAIL: "MLOps engineer. Manage GPU clusters, model deployments, monitoring, and on-call." -> operational platform work lacks product responsibility.""",
)

EVALUATION_PROMPTS = (
    LOCATION,
    COMPENSATION,
    ROLE_QUALITY,
    COMPANY_QUALITY,
    EARLY_STAGE_PRODUCT,
    APPLIED_AI_PRODUCT,
)

ENRICHMENT = PromptDefinition(
    name="job-finder-enrichment",
    criterion="enrichment",
    phase="enrichment",
    output="enrichment",
    max_tokens=1024,
    system_message="""You normalize and clean up job listing data for a personal job search CRM.

Given raw scraped job data, return cleaned and normalized versions of each field:
- **title**: Just the job title, no company name, location, or other suffixes
- **company**: Proper company name with correct capitalization and spacing (e.g. "Monad Foundation" not "monad.foundation", "Paxos Labs" not "PaxosLabs")
- **description**: A clean, well-formatted summary of the role using markdown. Structure it with sections like "## Overview", "## Responsibilities", "## Requirements", "## Tech Stack", "## Compensation" (only include sections that have content). Use bullet points for lists. Strip navigation elements, boilerplate, legal disclaimers, and repeated company marketing. Should be readable in 30 seconds.
- **location**: A short canonical location string (e.g. "Remote (Global)", "Remote (US/EU)", "Remote (Europe)", "Remote (US)", "Remote", "New York, NY", "London, UK").

  STRICT RULES - be conservative; do NOT infer or invent restrictions:
  * Only encode geographic restrictions that are EXPLICITLY stated in the listing (e.g. "US only", "EU residents only", "must be eligible to work in Canada").
  * A mentioned office is NOT a restriction. "Remote-friendly with an NYC office" -> "Remote", NOT "Remote (US/NYC preferred)". "Hybrid in San Francisco" -> "Hybrid (San Francisco)" only because the listing states the requirement.
  * Do not write phrases like "preferred" or "primarily" unless those exact words appear in the source.
  * If the listing says "remote" without a region -> "Remote".
  * If the listing gives no location/eligibility signal at all -> "Not specified".""",
)

TITLE_DEDUPLICATION = PromptDefinition(
    name="job-finder-title-deduplication",
    criterion="title-deduplication",
    phase="deduplication",
    inputs=("newTitle", "existingTitles"),
    user_message='New title: "{newTitle}"\n\nExisting titles at the same company:\n{existingTitles}\n\nIs the new title a duplicate of any existing title?',
    output="deduplication",
    max_tokens=128,
    system_message="""You compare job titles at the same company to detect duplicates. Two titles are duplicates if they refer to the same role despite minor wording differences: abbreviations (Sr. = Senior, Eng = Engineer), reordering (Backend Engineer = Engineer, Backend), or trivial additions (e.g. adding a team name). They are NOT duplicates if the seniority level, domain, or function differs (e.g. "Senior Backend Engineer" vs "Staff Frontend Engineer").""",
)

PROMPTS = (*EVALUATION_PROMPTS, ENRICHMENT, TITLE_DEDUPLICATION)
