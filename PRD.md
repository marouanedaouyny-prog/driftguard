# Security-Aware Refactoring Assistant for data pipelines

## Vision

Developer-first refactoring assistant that intelligently reduces complexity in data pipelines codebases while detecting vulnerabilities as it rewrites — instant, self-hosted, free tier for CI.

## Problem

Refactoring large data pipelines codebases is risky and slow, and reviews miss the security issues the rewrite touches.

## Solution

Developer-first refactoring assistant that intelligently reduces complexity in data pipelines codebases while detecting vulnerabilities as it rewrites — instant, self-hosted, free tier for CI.

## Target Audience

software developers

## Value Proposition

Solves Refactoring large data pipelines codebases is risky and slow, and reviews miss the security issues the rewrite touches. for software developers with Developer-first refactoring assistant that intelligently reduces complexity in data pipelines codebases while detecting vulnerabilities as it rewrites — instant, self-hosted, free tier for CI.

## Background & Evidence

- Opportunity type: technical_debt_reduction+missing_security_automation
- Finding: [['test suite present (+20)', 'documentation present (+15)', 'CI configuration present (+15)', 'README with 293 lines (+10)', 'license present (+10)', '72 TODO/FIXME markers (-20)'], '']
- Problem observed: Refactoring large data pipelines codebases is risky and slow, and reviews miss the security issues the rewrite touches.
- Source signal: ['maintainability', 'security']
- Target audience signal: developers
- Product domain: data pipelines

## Source Repositories

- godror/godror — ⭐596 — GO DRiver for ORacle DB
- godror/godror — ⭐596 — GO DRiver for ORacle DB

## Features

- Pipeline parsing & introspection
- Data lineage tracking across stages
- Schema drift detection with diffs
- Dry-run transformations with preview output
- Failure isolation & retry policies
- Metrics/observability hooks per stage
- Local LLM suggestions via Ollama
- Free tier for CI / small teams

## MVP Scope

- Pipeline parsing & introspection
- Data lineage tracking across stages
- Schema drift detection with diffs
- CLI interface
- SQLite persistence
- Basic documentation

## Non-MVP Scope

- Web UI / dashboard
- Team collaboration features
- Advanced analytics
- Enterprise SSO
- Plugin marketplace

## User Flows

- Point tool at pipeline definition -> parse -> stage map with lineage
- Run dry-run on changed schema -> diff preview -> approve execution
- Failure in stage -> isolated retry -> metrics hook records outcome

## Architecture

Modular monolith with clear separation: CLI layer -> Core engine (data pipelines analysis logic) -> Storage (SQLite) -> Optional LLM enrichment (Ollama). Plugin interface for extensibility; every operation is idempotent and recorded in an audit trail.

## Technology Stack

Python 3.11+ (stdlib), SQLite + DuckDB for analytics, optional local LLM (Ollama), Docker for packaging

## Database

SQLite (embedded) / DuckDB (analytics)

## APIs

- CLI as primary interface
- REST API (optional, stdlib http.server or FastAPI)

## Integrations

- GitHub Actions
- GitLab CI
- Docker

## Security

No external secrets required; runs locally; optional read-only GitHub token. Subprocesses run with a cleaned environment and sandboxed paths; all logs are redacted.

## Deployment

Single binary (PyInstaller) or Docker image; docs on GitHub Pages; CI via GitHub Actions; optional self-hosted server mode for data pipelines teams.

## Zero-Cost Strategy

- Default: SQLite + local models + GitHub Actions + free hosting

## Zero-Cost Analysis

feasible: True; estimated infra: $0/month — green flags: runs locally

## Monetization

Model: freemium + pro ($10-50/user/mo) — Distribution: direct sales + content — Recurring revenue potential: 92/100

## Competitive Landscape

Real analysis in `docs/STRATEGY_AND_MARKET.md` (replaces heuristic filler). Summary:

- The empty quadrant is real: no tool today refactors pipeline code (dbt/Airflow/Spark) while verifying the rewrite didn't introduce security regressions. Closest partial solutions: sqlfluff (style lint only), dbt tests/dbt_project_evaluator (validation, no security semantics), SonarQube (generic SAST, no dbt Jinja/`ref()` semantics), Sourcery (generic AI refactoring, cloud-based, not pipeline-aware), Datafold (data diffing from $799/mo, no code-level security), dbt Copilot (codegen, no verification), AI assistants (rewrite without a seatbelt).
- Competition for attention is HIGH (incumbents are funded); competition in our exact quadrant is zero. Differentiation is real but narrow — it is the combination (pipeline semantics × security × verified refactors × local-first × free), not any single feature.
- Recommended wedge: security-in-rewrite for dbt teams (local-first CLI + CI gate); schema drift is a feature inside the wedge, not the wedge itself.

## Roadmap

- Phase 1: Pipeline parser + lineage model
- Phase 2: Dry-run engine + schema drift detection
- Phase 3: Retry/isolation policies + observability hooks
- Phase 4: Managed pipeline catalog + team collaboration

## Risks

Full 5-item register with mitigations in `docs/STRATEGY_AND_MARKET.md` §8. Top 3:

1. Scope creep into a "full refactoring engine" before the security-gate wedge is adopted (fatal) — mitigate by MVP = security-gate-first, conservative verified refactors only.
2. Incumbent bundles the feature (dbt Copilot security, Sourcery SQL rules, SonarQube dbt pack) — mitigate by adapter-agnostic breadth (Airflow/Spark), local-first trust, open rules, community velocity.
3. Security false positives/negatives destroy trust — mitigate by conservative rules, evidence-cited findings, per-rule tests, no auto-block in v1.

## Assumptions

- Primary users are developers
- Opportunity is validated in the data pipelines space
- Target users comfortable with CLI
- Problem is real and recurring
- Zero-cost stack is sufficient for MVP
- Community will contribute patterns

## Success Criteria

Real, measurable KPIs (6-month targets; full table in `docs/STRATEGY_AND_MARKET.md` §6.3):

- 700+ GitHub stars (floor 150 at month 2 — below that, wedge/demo is wrong)
- 2,000+ PyPI downloads/month and 100+ weekly active CLI users (opt-in telemetry only)
- 30+ contributors, ≥10 external with merged PRs, 10+ community-contributed rules
- 20+ production deployments reported; 2 conference/meetup talks delivered
- Zero critical security issues in the tool itself
- 2–3 paid teams at month 6 (adoption is the goal, not revenue)
- Heuristic scores (market ≥ 88 etc.) are removed from decision-making — see strategy doc §6.3

## Implementation Phases

1. Phase 0 — Skeleton: project layout, CLI entry, SQLite schema, CI pipeline
2. Phase 1 — Core engine: pipeline parsing & introspection
3. Phase 2 — Extend: data lineage tracking across stages
4. Phase 3 — Polish: schema drift detection with diffs
5. Phase 4 — Optional Ollama LLM enrichment
6. Phase 5 — Docs, examples, launch on GitHub with CI/CD

