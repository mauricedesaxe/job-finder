Title: Principal AI Engineer

URL Source: https://boards.greenhouse.io/exactera/jobs/7740360003

Markdown Content:
**Exactera**has offices in**New York City, Tarrytown NY, San Diego, CA, London,**and **Argentina.**

### The Role

As Principal AI Engineer, you will own the inference and intelligence layer of the Exactera platform. You will build the substrate that agentic workflows run on: the domain knowledge graph and structured representations of expert reasoning, the hybrid retrieval that operates over them, model serving and LLM integration, the agent platform, the evaluation harness that captures expert judgment as ground truth, and the interfaces that product engineers compose into customer-facing workflows.

You report directly to the CTO and work closely with the data engineering team (close collaboration on data structure, entity resolution, and the contract between the lakehouse and the intelligence layer), product engineering (who consume your interfaces to build agentic workflows), product management (who set product direction and prioritize the workflows the platform supports), and domain experts in tax advisory (who provide the ground truth and judgment the AI systems are evaluated against — and whose decisions you will help capture as first-class data).

This is an individual contributor role with architectural authority over the AI/ML platform. You will set technical direction, make build-versus-buy decisions, and provide direction to other senior engineers working on platform components. You will have meaningful input on stack evolution and are expected to evaluate the stack against real workloads and propose changes when warranted.

### What You Will Build

We have made initial choices you will inherit and refine. The data platform runs on Databricks with Unity Catalog for governance. We use MLflow for experiment tracking and model lifecycle. Our LLM integrations use Anthropic and OpenAI APIs. MCP is our current pattern for exposing capabilities to agentic workflows, with production gateways already in service. We are on AWS, with Terraform for infrastructure-as-code. Exact tool experience matters less than having strong, defensible opinions about the categories.

### Knowledge and Reasoning Substrate

This is the headline of the role. Tax-advisory work is relationship-heavy: companies own subsidiaries, subsidiaries transact, transactions map to jurisdictions, jurisdictions have regulatory frameworks, and comparable selections are justified against multi-dimensional functional profiles. A vector store can suggest candidates; it cannot defend a selection under audit. The substrate you build is what makes the rest of the platform compliance-grade.

*   **Domain ontology and knowledge graph.**Design and operate the typed graph of tax-domain entities and relationships — companies, transactions, jurisdictions, segments, functional profiles, comparability factors, expert decisions, and reports — with relationships reified (e.g., comparable-to as an edge carrying which dimensions, who weighted them, in which report, with what outcome). Stack choice is yours.
*   **Entity resolution and relationship extraction at scale.**Pipelines that resolve entities across 34,000 reports, SEC/EDGAR filings, and customer data into a canonical, versioned representation. Relationship extraction from free text into structured edges. Reconciliation against curated reference data (NAICS, ownership filings).
*   **Expert judgment as first-class data.**Capture practitioner decisions — selections, rejections, weightings, and the reasoning behind them — as structured entities attached to the graph, versioned and queryable. This is Exactera's proprietary moat encoded.
*   **Versioned graph snapshots.**The graph evolves; audit defense requires being able to reconstruct exactly what it looked like at any point in time. Design the versioning and snapshot strategy.

### Hybrid Retrieval

Retrieval is three modes — graph traversal, vector search, and structured queries — and a planner that decides which combination to use per task. "Find comparable companies for this intercompany loan" is a different retrieval shape than "summarize prior treatment of intercompany IP licensing in EMEA." The retrieval layer makes the right combination of moves automatically.

*   **The full RAG pipeline,**as one retrieval mode within the larger system: chunking strategies, embedding generation, index management, retrieval optimization, and context assembly for LLM consumption. Embedding pipelines for heterogeneous data, with index maintenance as source data and the graph evolve.
*   **Graph-enhanced retrieval.**Graph traversal for relationship-aware lookups, graph-guided chunking that respects entity boundaries, graph context assembly that pulls in related entities and prior precedents alongside narrative text.
*   **Structured retrieval.**First-class structured queries (jurisdiction, year, industry code, transaction value range) as a peer mode, not an afterthought.
*   **Retrieval planning and orchestration.**The component that chooses, for a given task or sub-task, which retrieval modes to use, in what order, and how to fuse results. This is its own non-trivial design problem.
*   **Retrieval feedback loops.**Every retrieved-and-used result is evidence; the substrate compounds over time.

### Reasoning, Memory, and Agent Platform

Exactera's products operate in regulated, high-stakes domains where AI outputs have to be defensible to tax authorities. The bar is compliance-grade AI: systems where errors carry real consequences and outputs have to hold up under audit. You will build the platform that lets agentic workflows operate at this standard:

*   **Agent memory infrastructure:**short-term (conversation context), long-term (the graph itself), and episodic (per-customer history). The patterns that let agents operate across sessions without losing state or reasoning.
*   **Tool access and permissioning:**the layer that gives agents controlled, auditable access to data and capabilities, with constraints appropriate to the action being taken. Integrates with the platform's existing tenant and grant model.
*   **Determinism and audit trail, end-to-end.**Versioned graph snapshots, versioned indexes, versioned prompts, versioned model snapshots, versioned expert-judgment datasets — all coherent enough to replay exactly what the system saw when it made a specific decision. This is not just logging; it is replayability for audit defense.
*   **Observability:**tracing, logging, and monitoring for agent execution, with the ability to reconstruct why an agent made a specific decision.

### Evaluation and Expert Judgment as Ground Truth

Evaluation is a pillar, not a sub-bullet. For compliance-grade AI, it is how you safely change anything in the stack, how you measure whether the system is actually replicating expert judgment, and how you defend outputs after the fact.

*   **Ground-truth capture.**Design the system that turns practitioner decisions into versioned, queryable evaluation data — labels, rationales, weights, outcomes, dissents.
*   **System-level evaluation.**Frameworks that exercise retrieval quality (precision/recall against expert-labeled relevant sets), decision quality (does the system pick what the expert picked, and if not, why?), justification quality (does the rationale survive expert review?), and end-to-end (does the synthesized output match a defensible expert report?).
*   **Regression detection in CI**and in shadow against production, with the operational discipline to act on it.

### Model Serving and Inference Infrastructure

*   **Production model serving:**real-time inference endpoints for classification, extraction, and decision-support models, with latency, cost, and reliability SLAs.
*   **Experiment tracking, model registry, and deployment lifecycle**in production environments.
*   **Structured extraction at scale,**as a first-class workload: extracting entities, transactions, comparable sets, and reasoning patterns from semi-structured and unstructured documents into the knowledge substrate above.

### LLM Integration and Cost Management

*   **Production patterns for LLM API integration:**cost optimization, token management, prompt caching, rate limiting, fallback routing, and observability.
*   **Cost models that scale sublinearly with corpus growth.**Decisions about what to embed vs. what to leave for full-text vs. what to materialize as graph state, evaluated by both query latency and ongoing cost.
*   **MCP servers and similar interfaces**that expose Exactera's data and AI capabilities to agentic workflows. We already operate three production MCP gateways with custom OAuth handling; expect to work at that depth.

### Interfaces for Product Engineering

*   **Domain-meaningful API abstractions**(searchComparables, assessComparability, findPrecedents, proposeFunctionalAnalysis) that let product engineers compose agentic workflows without rebuilding retrieval, inference, memory, tool access, or evaluation primitives.
*   **SDKs in Python and Node,**with documentation, usage examples, and sensible defaults so product engineers can build agent orchestration without worrying about the underlying platform.
*   **Cost visibility, usage monitoring, and onboarding patterns**for teams adopting the platform.

### Business Problems You Will Solve

Unlocking trapped expert reasoning at scale

Our 34,000 TP reports and curated SEC data contain expert reasoning, comparable selections, and fact patterns that practitioners currently access by memory or manual search. The knowledge substrate and hybrid retrieval you build turn that reasoning into a searchable, defensible, re-applicable asset.

Powering agentic workflows that replace manual work

Practitioners spend the majority of their time on tasks AI can automate: comparable company selection, document classification, data extraction, and report generation. The inference and agent platform you build is what runs those workflows in production.

Making AI hold up under audit

Tax advisory outputs have to survive scrutiny from tax authorities. The graph versioning, evaluation harness, observability, and determinism infrastructure you build is what allows us to deploy AI in regulated workflows and reconstruct, two years later, exactly why the system made a specific call.

Enabling product engineers to build agentic workflows

Without clean primitives, SDKs, and domain-meaningful patterns, product engineers cannot build agent orchestration on top of ML infrastructure. You will provide the interfaces that let them compose retrieval, reasoning, agent memory, and tool access into the workflows that automate practitioner work.

### Required Experience

*   10+ years in software engineering, with depth in at least two of the following accumulated _before_ AI/ML became your primary focus: information retrieval and search infrastructure, knowledge representation, distributed systems, data-intensive applications, or platform engineering.
*   5+ years in ML engineering, ML infrastructure, or AI platform development.
*   Production experience with knowledge graphs or structured reasoning systems alongside vector search and LLMs. You have built or significantly extended a knowledge graph in a production setting: schema design, entity resolution, graph query languages (Cypher, Gremlin, SPARQL, or equivalent), and graph-aware retrieval. You have an opinion on when relationships matter more than embeddings, and how to combine both.
*   Hybrid retrieval design. Production experience composing multiple retrieval modes — graph, vector, structured — into a coherent retrieval layer, including the orchestration of which mode to use when.
*   RAG architecture and LLM integration in production: cost optimization, retrieval quality measurement, regression detection, reliability under load.
*   AI infrastructure for document-heavy workflows: extraction, classification, and analysis of semi-structured and unstructured documents into structured representations.
*   Model serving infrastructure in production: real-time inference endpoints with latency, cost, and reliability SLAs. Experiment tracking, model registry, and deployment lifecycle tooling.
*   Evaluation framework design for retrieval and LLM-powered systems: retrieval quality metrics, decision quality measurement against expert ground truth, answer accuracy, and regression detection in CI. You have designed eval harnesses that capture domain-expert judgment as versioned data, not just static benchmarks.
*   Developer-facing APIs, SDKs, or platform services consumed by other engineering teams.
*   Strong Python engineering. Production Node/TypeScript experience is a plus given the platform team's existing services.
*   MCP at depth, or equivalent production patterns for exposing AI capabilities to agentic workflows. Familiarity with the realities at scale: per-server tool limits, OAuth and auth-context propagation, structured tool output, observability.
*   Willingness to develop working fluency in the domain: transfer pricing comparability factors, jurisdictional regulatory frameworks, and the structure of an audit-defensible analysis. You will not be the tax expert, but you will be the engineer who knows enough to design correctly.
*   Ability to design systems from first principles in ambiguous, early-stage environments.
*   Strong written and verbal communication. You write documentation product engineers actually use, and engage directly with technical and business stakeholders.
*   Hands-on use of AI development tools (Claude Code, Cursor, Replit, V0, etc.) in your engineering workflow.
*   US-based. Hybrid preferred (San Diego, Seattle, or NY), open to remote for the right candidate.

### Preferred Experience

*   **Domain experience:**financial data, tax advisory, legal, scientific research, or other enterprise compliance domains where outputs must be defensible.
*   **Knowledge representation in legal or regulatory contexts:**case law graphs, regulatory ontologies, contract analysis systems, or similar domains where structured reasoning is the differentiator.
*   **Data governance and compliance frameworks**for regulated industries.
*   **Open-source contributions**to retrieval, knowledge graph, or LLM infrastructure projects.
