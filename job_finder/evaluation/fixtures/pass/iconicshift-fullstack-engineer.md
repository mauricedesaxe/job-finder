# Fullstack Product Engineer @ IconicShift

*   [IconicShift](https://iconicshift.co.uk/)
*   [](https://jobs.ashbyhq.com/iconicshift)

# Fullstack Product Engineer

## Location

United Kingdom

## Employment Type

Contract

## Location Type

Remote

## Department

Product

## Deadline to Apply

March 27, 2026 at 5:00 PM UTC

[Overview](https://jobs.ashbyhq.com/iconicshift/41a4ec81-9d92-41e4-b79c-d806dad5541f)[Application](https://jobs.ashbyhq.com/iconicshift/41a4ec81-9d92-41e4-b79c-d806dad5541f/application)

# About IconicShift

IconicShift AI is building a platform that turns founder intent into usable strategic outputs — clear strategies, investor-grade pitches, and practical roadmaps.

It is based on the experience of Mike Harris, founding CEO of First Direct (the world's first telephone bank), founding CEO of Egg (the world's first internet bank), and CEO of Mercury Communications (that created some of the UK's first mobile and cable networks).

Most founders don't fail because they lack ideas or options. They fail because their thinking drifts and they can't afford the experienced support they need. Their story changes, priorities multiply, and the business loses its single direction. Most AI approaches just help them fail faster by accelerating rushed decisions.

IconicShift works by creating a single source of strategic truth, where every decision is tracked and held to account. It refuses to move forward without explicit acceptance of core strategic bets, ensuring founders confront the real trade-offs they face. And then gives founders the tools, processes, and structures they need to succeed.

We're pre-funding, building the production foundation. Small team, real conviction, interesting problems.

## The project

We're building the core of the platform: a 10-step coaching flow where founders move from unstructured input through AI-generated pitches, explicit challenge, and strategic planning. The system is a governed state machine with LLM integration at its core. It deliberately introduces friction — it refuses to let founders skip past uncomfortable decisions or bypass strategic commitments. The platform treats AI as a powerful but unreliable tool that requires governance, cost tracking, and human oversight.

The codebase is essentially greenfield with solid foundations already in place: authentication, CI/CD, structured logging, feature flags, monitoring, and a well-modelled PostgreSQL schema. You'd be stepping into a clean, well-structured project where you can help shape the architecture.

# What you'd work on

We have clear milestones and a well-maintained backlog. Likely initial workstreams include:

*   **LLM integration pipeline** — building the routing layer that connects the platform to frontier models (OpenAI, Anthropic) via Vercel AI SDK, with streaming responses, cost tracking, and governance

*   **Core coaching flow** — implementing the step-by-step journey where founders provide input, receive AI-generated pitches, face explicit challenges, and make binding strategic decisions

*   **Database modelling and state management** — translating domain concepts into a normalised PostgreSQL schema that encodes business rules and enforces sequencing

You'd take ownership of discrete chunks of work end-to-end. There are requirements from the product and business, but you have genuine creative freedom in how you implement them. We want people who raise their hand, make suggestions, and ask questions — not people who silently take tickets and implement exactly what's written.

# The stack

*   **React Router v7** (Remix lineage) with TypeScript

*   **PostgreSQL** with Drizzle ORM

*   **Tailwind CSS** with shadcn/ui components

*   **Vercel AI SDK** for LLM integration (OpenAI, Anthropic)

*   **BetterAuth** for authentication

*   **GitHub** with linear history, commit signing, and CI/CD via GitHub Actions, deploying to **Render**

*   **Sentry** with OpenTelemetry for monitoring

*   **Vitest** and **Playwright** for testing

*   **pnpm workspaces** for monorepo structure

*   **Devenv/Nix** for development environment (Docker available as alternative)

# Hard requirements

These are non-negotiable. If you don't meet them, we won't be able to consider your application.

*   **Production experience with PostgreSQL.** You need to understand normalised schema design, foreign keys, indexes, migrations, and the trade-offs between different approaches (e.g. UUIDs vs serials as primary keys). If you're primarily a MongoDB or Firebase developer, this isn't the right role.

*   **Production experience with React and server-side rendering patterns.** We use React Router v7 (Remix lineage) with loaders, actions, and nested routing. You don't need prior React Router v7 experience specifically, but you must understand SSR and data loading patterns, and be ready to work with loaders and actions from day one. If your instinct is to add REST API endpoints alongside the framework, this isn't the right role.

*   **Strong TypeScript skills.** We avoid stringly-typed code. Types are documentation. You should be comfortable with discriminated unions, generics, and using the type system to prevent bugs rather than just satisfying the compiler.

*   **API integration experience.** You'll be building the LLM routing layer that integrates with multiple providers. You need solid experience designing and consuming APIs, handling streaming responses, managing error states, and architecting systems with clear separation of concerns.

*   **Exceptional English, written and spoken.** This is a remote team doing complex technical work. Written communication is how we coordinate — your commit messages, PR descriptions, and Slack messages need to be clear, precise, and professional. We also work through video calls discussing technical architecture, and you need to be able to communicate complex ideas clearly in conversation. Regional accents are welcome; clarity is essential.

*   **Discerning use of language models.** We have company Anthropic and OpenAI accounts, Claude Code, and skills that encode our processes — from landing pull requests to scaffolding from Figma designs. We're a small team with ambitious goals, and LLMs are a key enabler for doing more with less. But we need someone with enough production experience to know when a model is producing unmaintainable code, impose the right guardrails, and distinguish a plausible answer from a correct one. The tool is powerful; we want someone with the judgement to wield it well.

## **Nice to have**

*   Experience with LLM integration, streaming responses, or AI product development

*   Familiarity with observability and governance patterns (OpenTelemetry, structured logging)

*   Experience with Drizzle ORM or similar TypeScript ORMs

*   Familiarity with state machine patterns or domain-driven design

# How we work

Fully remote. We communicate via Slack and video calls. Design work lives in Figma. All work is tracked in Linear. Code goes through GitHub with full CI/CD — land a pull request and it's live on Render.

We embrace the heart of Agile — collaborate, deliver, reflect, improve — without the ceremony. No planning poker. No two-week rituals for the sake of it. We size work relative to previously delivered tasks and forecast based on measured velocity. Lightweight process that actually helps rather than getting in the way.

A few principles we take seriously:

*   **Transparency by default.** Project communication happens in shared channels. When we onboard someone new, they should be able to read back through the channels and get up to speed without anyone replaying conversations.

*   **Small leaps, committed landings.** When we commit to a piece of work, the scope is agreed and the designs are understood. While you're implementing, the target doesn't move. If something needs to change, it goes into the next batch.

*   **Ownership, not delegation.** Each piece of work has a single owner who drives it to completion. That means talking to Matt about requirements, Laura about design, James about technical approach. We don't want everything routing through a single bottleneck.

*   **Over-communication.** We pride ourselves on making sure everyone has the context they need. Async progress updates (progress, problems, priorities) replace stand-up meetings. If you're blocked, say so early. If you disagree with an approach, raise it. If you spot a better way, suggest it.

*   **Considerate coding.** We think about the impact of our actions on the rest of the team. We have database triggers that set updated_at timestamps automatically, and tests that check every table with an updated_at column has the trigger present. We recognise we're fallible and use the tools available to prevent mistakes — for ourselves and for each other.

*   **Productivity over presence.** We care about what you ship, not when you're online. Flexible hours, asynchronous by default, with enough overlap for meaningful collaboration during UK working hours.

*   **Treat your process like your product.** We ship a version of how we work, get feedback, and iterate. Retrospectives are lightweight but genuine. If something isn't working, we change it.

We've built skills and automation into our workflow using language models. When you want to land a pull request, there's a skill for that. When you start work on a Figma design, there's a skill for that. When you're being onboarded, there's a skill that walks you through getting your environment set up correctly. We use LLMs to encode our processes and accelerate ourselves — not just for writing code.

Our most recent developer shipped to production in her first week. We'd expect the same from you.

# The team

*   **Matt** — Founder, product lead. Owns the coaching methodology and product vision.

*   **Mike Harris** — Founder. The methodology and strategic experience behind the platform.

*   **Tariq** — Founder.

*   **James** — Fractional CTO. Architecture, technical leadership, code review, day-to-day collaboration. You'd work closely with James.

*   **Laura** — UX/Product Designer. Owns the user journeys and interaction design in Figma.

*   **Christa** — Developer. Working on feature implementation.

# This probably isn't for you if…

*   You're an agency or recruiter

*   You rotate staff on and off projects

*   You need detailed specifications for every task

*   You can't overlap significantly with UK working hours

# Engagement details

1.   **Start:**Early to mid-March 2026

2.   **Duration:**Through to July initially, with strong potential to continue

3.   **Commitment:**3–5 days per week (flexible based on fit)

4.   **Location:**UK based, or Europe with exceptional English and significant UK hours overlap

5.   **Rate:**Depends on experience and location

6.   **Contract:**Direct with IconicShift (no intermediaries)

# Interview process

1.   Initial screening based on your application

2.   90-minute live pairing session with the CTO — we'll hack on a real problem together using a cutdown version of our real production stack. This is the interview that matters. Come prepared: understand React Router v7 loaders and actions, be ready to model a PostgreSQL schema, and bring your normal development setup including any AI tools you use.

3.   Founder call

4.   Offer

[Apply for this Job](https://jobs.ashbyhq.com/iconicshift/41a4ec81-9d92-41e4-b79c-d806dad5541f/application)

This site is protected by reCAPTCHA and the Google[Privacy Policy](https://policies.google.com/privacy) and[Terms of Service](https://policies.google.com/terms) apply.

[Powered by](https://www.ashbyhq.com/)
[Privacy Policy](https://www.ashbyhq.com/privacy)[Security](https://www.ashbyhq.com/security)[Vulnerability Disclosure](https://www.ashbyhq.com/disclosure)
