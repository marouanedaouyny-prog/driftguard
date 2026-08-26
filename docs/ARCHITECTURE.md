# DriftGuard / Security-Aware Refactoring Assistant for Data Pipelines — Architecture & Implementation Plan

**Status:** Living document · Phase 3 (MVP complete), Phase 4 core landed (security scanner `core/security/` + `scan`; refactoring engine `core/refactor/` REF-001..006 + state machine/sessions/audit + `refactor` CLI; optional Ollama suggestion channel `llm/`, `--llm-suggestions`), and Phase 5 landed (trusted-code `--rules-dir` plugin loader `core/refactor/loader.py` + launch artifacts: SECURITY.md, CONTRIBUTING.md, CHANGELOG.md, Dockerfile, release workflow, docs site — version 0.5.0).
**Source of truth:** `PRD.md` (this document extends it; where this doc and the PRD conflict, the PRD wins for scope, this doc wins for how)
**Audience:** engineers implementing directly from this document

---

## 0. Executive summary

A modular-monolith CLI tool that makes refactoring data-pipeline codebases **safe and auditable**:

- **Parse & introspect** dbt-style SQL pipelines (the concrete Phase 1 target — decision in §4.1) into a typed intermediate representation (IR).
- **Track lineage** across stages and **detect schema drift** with a dry-run diff preview (the MVP wedge, already seeded in `driftguard/`).
- **Plan refactorings** as deterministic, risk-tiered rewrite candidates; **apply** them only from an approved plan file; **verify** the result by re-parsing, re-scanning, and re-checking drift.
- **Scan for security issues** (secrets, SQL injection, unsafe subprocess, credentials in connection strings) as an integral gate of the rewrite flow — the "detecting vulnerabilities as it rewrites" promise, made concrete.
- **Persist everything** in SQLite (WAL), with every state transition recorded in an append-only audit trail. Every operation is idempotent.
- **Optional Ollama** enrichment: suggestions only, never auto-applied. **Optional DuckDB** analytics backend. **$0 infra** by construction: Python 3.11+ stdlib core, GitHub Actions free runners, GitHub Pages docs, GHCR public images.

The core engine is an importable library (`driftguard.core.*`) with a thin CLI on top — this keeps a future stdlib `http.server` REST mode (PRD §APIs) trivial to add.

---

## 1. Design principles (derived from PRD constraints)

| # | Principle | PRD anchor |
|---|-----------|------------|
| P1 | **Stdlib-only core.** No third-party runtime deps. `sqlite3`, `ast`, `re`, `argparse`, `dataclasses`, `difflib`, `json`. Build tooling (setuptools, PyInstaller) is build-time only. | Tech stack: "Python 3.11+ (stdlib)" |
| P2 | **Zero-cost by default.** SQLite default, DuckDB optional, Ollama optional, CI/docs on free tiers. | Zero-Cost Strategy / Analysis |
| P3 | **Fail loud, never silently wrong.** A parser that cannot fully understand a construct must emit a structured diagnostic and skip conservatively — never produce a wrong IR that a rewrite could then apply. | Quality-over-quantity spirit; safety of a refactoring tool |
| P4 | **Semantics-preserving by construction.** A v1 rule may only make changes provably safe under the IR's model; anything uncertain is a *suggested* or *risky* rule behind a `--max-risk` gate. | "reduces complexity … while detecting vulnerabilities" |
| P5 | **Idempotent everything.** Same input + same plan ⇒ same output; applying a completed plan is a no-op. | PRD Architecture: "every operation is idempotent" |
| P6 | **Audit everything.** Every session transition is recorded; logs and artifacts are redacted. | PRD Architecture: "recorded in an audit trail"; PRD Security: "all logs are redacted" |
| P7 | **Sandboxed side effects.** Core does no subprocesses; the only subprocess (optional `git diff --stat`) runs with a cleaned env. | PRD Security |
| P8 | **LLM is a suggestion channel, not an author.** Ollama output enters the plan pipeline only through the same approval gate as human-authored plans. | "Local LLM suggestions via Ollama" |

---

## 2. High-level architecture

```
┌────────────────────────────────────────────────────────────────────┐
│ CLI layer  (driftguard.cli / __main__)                             │
│   argparse surface · exit-code contract (0/1/2) · output routing   │
│   text | markdown | JSON · redaction before print                  │
└───────────────┬────────────────────────────────────────────────────┘
                │ importable API (library boundary — no CLI coupling)
┌───────────────▼────────────────────────────────────────────────────┐
│ Core engine  (driftguard.core.*)                                   │
│                                                                     │
│  parser/      tokenizer → recursive-descent SQL parser → IR        │
│  ir/          typed model · JSON serialization · canonical sha256  │
│  lineage/     dependency graph · cycles · missing refs · topo sort │
│  drift/       schema diff · rename similarity · breaking rules     │
│  refactor/    rule registry · plan · apply (sourcemap edits) ·     │
│               verify                                               │
│  security/    scanners (secrets, SQLi, subprocess, conn strings) · │
│               redaction · suppression                              │
│  state/       workflow state machine · sessions                    │
└───────┬───────────────────────────────────────────────┬────────────┘
        │                                               │
┌───────▼──────────────┐              ┌──────────────────▼───────────────┐
│ Storage (driftguard. │              │ Optional enrichment              │
│ store)               │              │                                  │
│  SQLite (WAL) default│              │  llm/ollama.py — suggestions     │
│  audit trail         │              │  only, gated, offline-safe       │
│  DuckDB analytics    │              │  store/analytics.py — DuckDB     │
│  backend (optional)  │              │  query backend (optional)        │
└──────────────────────┘              └──────────────────────────────────┘
```

**Rule of the layering:** `core` never imports `cli` or `store` types into its domain logic (storage is injected); `cli` only wires; `store` only persists. This keeps the engine testable without a DB and keeps the audit trail complete at the boundary (the session/state layer writes audit rows, not the rules).

### 2.1 Module / directory layout

Current seed (Phase 0, flat — works, tests green):

```
driftguard/
  __init__.py  __main__.py  parser.py  lineage.py  drift.py  store.py  report.py
tests/  test_parser.py  test_lineage.py  test_drift.py  test_cli.py
examples/models/{staging, marts}/*.sql
```

Target layout (reorganization is a Phase 1 mechanical step; `python -m driftguard` and the flat test suite stay green throughout — see §7):

```
driftguard/
  __init__.py                # __version__ (single source), public API re-exports
  __main__.py                # thin shim → cli.main()
  cli.py                     # argparse surface, exit-code contract, output routing
  core/
    parser/
      __init__.py
      tokenizer.py           # SQL tokenizer preserving byte offsets (the sourcemap base)
      sql.py                 # recursive-descent parser: SELECT/CTE/CREATE subset → IR
      jinja.py               # static Jinja subset: extract ref('x') / source('s','t') / config(...)
      dialects/
        base.py              # Parser protocol (parse(project_root) -> Pipeline)
        dbt.py               # Phase 1 target: dbt project layout, ref()/source() resolution
    ir/
      model.py               # Stage, Pipeline, Column, RefEdge, Cte, CreateStmt, Span
      serialize.py           # IR ⇄ JSON (stable schema, versioned field)
      fingerprint.py         # canonical sha256 of a stage/pipeline/plan item
    lineage/
      graph.py               # build graph, consumers()/producers(), topological order
      cycles.py              # cycle detection (seeded logic moved here)
      refs.py                # unresolved-ref bookkeeping, source() resolution
    drift/
      detector.py            # drift detection (seeded logic moved here)
      similarity.py          # rename heuristic (SequenceMatcher ≥ threshold, configurable)
      diff.py                # unified diff preview for dry-run
    refactor/
      engine.py              # Rule protocol, registry, candidate model, risk tiers
      rules/
        __init__.py          # built-in rule registration
        drop_dead_cte.py     # REF-001
        dedupe_projection.py # REF-002
        inline_single_use_cte.py  # REF-003
        quote_normalize.py   # REF-004
        star_expand.py       # REF-005 (risky)
        dead_alias.py        # REF-006
      plan.py                # Plan / PlanItem JSON schema (dry-run output == apply input)
      apply.py               # sourcemap edits, .orig backups, --in-place / --out-dir
      verify.py              # post-apply: re-parse, re-scan, drift re-check
    security/
      __init__.py
      scanner.py             # orchestrates rules over IR + raw text; findings model
      findings.py            # Finding, severity (critical/high/medium/low), span, snippet
      redact.py              # secret scrubbing for logs/reports/plan files
      rules/
        secrets.py           # SEC-001 hardcoded secrets
        sql_injection.py     # SEC-002 string-interpolated SQL
        subprocess.py        # SEC-003 unsafe subprocess
        conn_strings.py      # SEC-004 credentials in connection strings/DSNs
        weak_auth_sql.py     # SEC-005 plaintext credentials in SQL DDL/DCL
    state/
      machine.py             # states, transitions, guards (pure)
      session.py             # session lifecycle, audit writes, recovery
  store/
    db.py                    # SQLite connection (WAL, busy_timeout, single owner), migrations
    schema.sql               # DDL (all tables, indexes)
    audit.py                 # append-only audit writer + reader
    analytics.py             # optional DuckDB backend; degrades to SQLite queries
  llm/
    ollama.py                # optional client (requests-free: urllib); offline-safe
    suggestions.py           # structured suggestion protocol (never auto-applies)
  report/
    text.py  markdown.py  json.py  diff.py   # report module split from seeded report.py
tests/
  test_parser.py  test_lineage.py  test_drift.py  test_cli.py ...   # flat (unittest discovery -s tests)
  golden/                    # data dir: tests/golden/<rule-id>/{input.sql, plan.json, expected.sql}
  security_corpus/           # data dir: positive/ (must flag) and negative/ (must stay clean)
  fixtures/                  # synthetic dbt projects for e2e
examples/models/...          # keep as living demo fixtures
docs/
  ARCHITECTURE.md  _config.yml  usage.md  security.md  CONTRIBUTING.md
scripts/
  build_binary.py            # PyInstaller wrapper
  build_docs.py              # static docs assembly (stdlib only)
pyproject.toml
Dockerfile
.github/workflows/{ci.yml, release.yml, docs.yml}
```

Notes:

- **Tests stay flat** (`tests/test_*.py`) — `python -m unittest discover -s tests` must keep working at every commit; `golden/` and `security_corpus/` are *data directories* consumed by harnesses, not test modules.
- **Package name stays `driftguard`** (matches seed, README, existing imports). The product name in `pyproject.toml` is `driftguard`; the marketing name from the PRD is used in docs.
- **Plugin seam (PRD "plugin interface for extensibility")**: the `Rule` protocol in `refactor/engine.py` *is* the plugin interface. Shipped: `--rules-dir` loader (`core/refactor/loader.py`) that imports user `.py` modules from a directory — documented as **trusted-code-only** (same trust level as the tool itself; loading arbitrary code is a deliberate, documented decision, not a silent one). Loader semantics: deterministic order (filenames sorted, module rules sorted by `id`), built-in id collisions rejected with a warning (no silent shadowing), invalid plugins (bad fields, import failure) warned and skipped — a bad plugin never breaks a run. `--rules-dir` is persisted on sessions (`sessions.rules_dir`) so `verify` re-analyzes with the same plugin set; `--rules` filters by id across built-ins and plugins.

---

## 3. Storage design (SQLite, WAL, migrations)

**Decision:** SQLite is the only persistence required by the MVP (PRD §Database). DuckDB appears only as an optional analytics query backend behind `store/analytics.py`; if `import duckdb` fails, the same queries run against SQLite (P1/P2). No schema is written by DuckDB.

Connection policy (from `store/db.py`): one connection owned by `Store`, WAL mode, `busy_timeout=5000`, `PRAGMA foreign_keys=ON`. Schema lives in `schema.sql`; migrations are ordered `ALTER`/`CREATE IF NOT EXISTS` scripts applied in a transaction and recorded in a `schema_version` table.

### 3.1 Tables

| Table | Purpose | Key columns |
|---|---|---|
| `runs` | one row per `driftguard <root>` invocation | id, root, checked_at, git_sha, stages, drifts, breaking, exit_code |
| `stages` | parsed stage snapshot per run | run_id, name, path, kind, fingerprint, ir_json |
| `lineage_edges` | producer→consumer edges per run | run_id, producer, consumer, kind (`ref`/`bare`/`source`), expected_columns_json |
| `drifts` | drift findings per run (seeded) | run_id, producer, consumer, added_json, removed_json, renamed_json, breaking |
| `sessions` | refactor workflow sessions | id, created_at, repo_fingerprint, state, plan_path, rule_ids, llm_used |
| `audit` | append-only transition log | id, session_id, ts, action, from_state, to_state, args_json (redacted), result_json, exit_code |
| `rewrites` | applied plan items | session_id, rule_id, stage, item_hash, fingerprint_before, fingerprint_after, applied_at |
| `scans` | security findings per scan | session_id/run_id, rule_id, severity, file, line, col, snippet_redacted, status (open/suppressed/resolved) |
| `schema_version` | migration bookkeeping | version, applied_at |

**Audit guarantees (§5.4):** rows are append-only (no UPDATE/DELETE in the writer API); every CLI operation that touches a session writes ≥ 1 row *in the same transaction* as the state change; `args_json` passes through `security.redact` before persist.

---

## 4. Core engine design

### 4.1 Pipeline parser — concrete Phase 1 decision: **dbt-style SQL projects**

**Decision (ADR-001):** Phase 1 parses **dbt-style SQL projects**: a directory tree of `*.sql` model files, `ref('name')` / `source('source','table')` calls, optional `{{ config(...) }}` and `CREATE TABLE/VIEW AS` wrappers, and `sources.yml` for source definitions. Airflow DAGs and generic multi-dialect SQL are **not** Phase 1 targets; they slot in later behind the `dialects.base.Parser` protocol.

**Justification:**

1. **The seed already targets it.** `driftguard/parser.py` handles `ref()`, bare `FROM`, `CREATE TABLE/VIEW`, projections — the dbt model shape is exactly what exists, tested, and demoed in `examples/models/`.
2. **SQL files are statically parseable without executing user code.** dbt models are data + templating; the *meaningful* graph (refs, columns) is recoverable from text. Airflow DAGs are arbitrary Python — introspecting them correctly means executing or fully analyzing Python, an order-of-magnitude harder problem (decorators, dynamic task factories, imports) that cannot be done safely in a stdlib-only, no-execution tool.
3. **dbt is the dominant transformation-layer framework** in the PRD's "data pipelines" domain; the CI-gate story (the MVP wedge) is dbt's native workflow (models in git, PR-gated CI).
4. **One dialect now, protocol later** beats a half-correct multi-dialect parser: every dialect added to a regex soup multiplies false positives (P3). A clean `Parser` protocol keeps the door open (Airflow via `ast`-based analysis, generic SQL via a stricter dialect) without contaminating v1.

**Parser architecture (ADR-002): hand-written tokenizer + recursive-descent parser** over a narrow, documented SQL subset. Not third-party (`sqlparse`, `tree-sitter` — violates P1), and not regex-on-text for anything structural (the seed's regex parser is the *seed*, not the *design*; it cannot carry source spans or support safe rewrites).

- `tokenizer.py`: tokenizes SQL (identifiers, quoted identifiers `"x"`/`` `x` ``, strings, numbers, operators, comments, parens) **preserving byte offsets** — this offset map is the *sourcemap* that makes `refactor/apply.py` edits precise and verifiable.
- `jinja.py`: a **static Jinja subset**. v1 understands `ref('x')`, `source('s','t')`, `config(key=value)`, and `{% raw %}` blocks; anything else (`{% for %}`, `var('x')`) is extracted as *unresolved template markers* — the IR records them so downstream rules can refuse to rewrite inside unknown template regions. Full Jinja rendering is explicitly out of scope (risk R2, §10).
- `sql.py`: recursive-descent grammar for: `SELECT` projections (with `AS` aliases, qualified columns, `*`), `FROM`/`JOIN` (incl. subqueries + CTEs), `WHERE`, `GROUP BY`, `ORDER BY`, `LIMIT`, `UNION [ALL]`, `CREATE TABLE/VIEW [schema.]name AS`, top-level `INSERT INTO ... SELECT`. Unknown constructs → structured diagnostic (`ParseWarning` with file/line/col/reason) and conservative skip of the affected scope, never a guessed IR (P3).
- `dialects/dbt.py`: project discovery (recursive `*.sql`, `dbt_project.yml` presence), `ref`/`source` resolution, `sources.yml` parse (YAML-free: a tiny key: value parser sufficient for `name:`/`schema:`/`database:` — documented limitation).

**Fail-loud contract:** `parse()` returns `(Pipeline, list[Diagnostic])`. Exit semantics: warnings ≠ failure; a file that cannot be tokenized/parsed at all is a hard error for `refactor` commands (exit 2), a warning for `inspect`/`drift` (drift skips that stage and says so). No path exists where a rewrite touches a stage the parser didn't understand.

### 4.2 Introspection model (IR)

Dataclasses in `core/ir/model.py`, all JSON-serializable via `serialize.py` (versioned `"v": 1` envelope so stored IR survives schema evolution):

```python
@dataclass(frozen=True)
class Span:          # byte offsets into the source file (sourcemap basis)
    start: int
    end: int

@dataclass(frozen=True)
class Column:
    name: str                # lowercase canonical
    source_expr: str         # original expression text (redacted at render time)
    alias: str | None
    span: Span

@dataclass
class Stage:
    name: str
    path: Path
    kind: str                # "model" | "source_table" | "view" ...
    raw: str
    fingerprint: str         # sha256(canonical IR json)
    columns: list[Column]
    refs: list[RefEdge]      # producer stage names
    sources: list[SourceRef] # (source, table)
    ctes: list[Cte]          # name, span, referenced_by: set[str]
    create_name: str | None  # from CREATE TABLE/VIEW
    dialect_hints: list[str] # e.g. "jinja_static_only", "unknown_template_region"
    diagnostics: list[Diagnostic]

@dataclass
class Pipeline:
    root: Path
    stages: list[Stage]
    fingerprint: str         # sha256 over sorted stage fingerprints
```

**Fingerprint contract (idempotency anchor):** the fingerprint is computed from the *canonical JSON* (sorted keys, stable column order from the tokenizer). Two files differing only in whitespace/comments produce the same fingerprint for *drift* purposes, but `apply` edits operate on raw spans — so a rewrite never depends on whitespace luck.

### 4.3 Lineage

Seeded logic in `driftguard/lineage.py` moves to `core/lineage/` (Phase 2):

- Graph = `edges: (producer, consumer, kind)`; `consumers()/producers()`; cycle detection (seeded DFS); missing-ref bookkeeping (`refs.py`).
- **Topological order** (Kahn's algorithm) added — required by the refactoring engine to analyze stages bottom-up (a rewrite to a producer must be validated against all consumers).
- `source()` refs resolve against `sources.yml` and are recorded as edges with kind `source` (not required for drift until Phase 3).
- Lineage output: `driftguard lineage <root> --json` exports `{stages, edges, cycles, missing, topo_order}`.

### 4.4 Refactoring rule engine

**Pattern (ADR-004): pure rule functions over the IR + sourcemap edits.** A rule *analyzes* (reads IR + raw text, produces candidates) and the *engine applies* (edits raw text at spans). Rules never mutate IR; the engine owns mutation. No rule ever regex-replaces text on the fly — candidates carry `before`/`after` snippets derived from spans, and `apply.py` performs the edit and asserts the result equals `after`.

**Rule protocol (the plugin seam):**

```python
class Rule(Protocol):
    id: str            # "REF-001"
    version: int       # bump = golden files must change
    tier: RiskTier     # SAFE | SUGGESTED | RISKY
    description: str
    def analyze(self, stage: Stage, ctx: AnalysisContext) -> list[RewriteCandidate]: ...
```

```python
@dataclass(frozen=True)
class RewriteCandidate:
    rule_id: str
    stage: str
    span: Span
    before: str
    after: str
    reason: str           # human-readable justification (goes in plan + report)
    security_note: str | None   # "touches SEC-002 span" etc.
```

**Risk tiers and the gate (ADR-006):** `SAFE` = provably semantics-preserving under the IR model (default on); `SUGGESTED` = very likely safe, requires `--max-risk suggested`; `RISKY` = behavior-changing in corner cases, requires `--max-risk risky` and prints a warning banner. Default is `--max-risk safe`. Nothing applies without `--max-risk` authorizing its tier.

**v1 rule catalog** (all idempotent by construction; each has golden tests, §8):

| ID | Rule | Tier | Precondition | Transform |
|----|------|------|--------------|-----------|
| REF-001 | `drop-dead-cte` | SAFE | CTE defined, `referenced_by` empty, no side-effecting content (only SELECTs) | delete CTE text + trailing comma fix |
| REF-002 | `dedupe-projection` | SAFE | two projection items with identical normalized expr+alias | remove the duplicate item |
| REF-003 | `inline-single-use-cte` | SUGGESTED | CTE referenced exactly once; no recursion; no name shadowing in the referencing scope; no `ORDER BY`/`LIMIT` in CTE (semantic risk) | substitute CTE body at the reference span |
| REF-004 | `quote-normalize` | SAFE | only *unquoted* identifiers (quoted ones can be case-sensitive — untouched) | canonical lowercase for unquoted identifiers |
| REF-005 | `star-expand` | RISKY | every source column known via lineage; no `*` inside subqueries with unknown columns | replace `*` with explicit column list (alphabetical) |
| REF-006 | `dead-alias` | SAFE | table alias defined but never referenced in that scope | drop the alias keyword |
| REF-007 | `drop-subquery-order-by` | SUGGESTED | `ORDER BY` directly inside a parenthesized query (`(SELECT …)` derived table, CTE body, IN/EXISTS subquery) with no `LIMIT`/`OFFSET`/`FETCH` after it at the same level; interior has no `UNION/INTERSECT/EXCEPT/MINUS`; no template regions inside; no side-effecting statements | delete the `ORDER BY …` clause (absorbs surrounding whitespace) |

Rule design law: *if a precondition cannot be proven, the rule does not fire.* This is what makes SAFE rules safe.

**Plan file (ADR-005) — the handoff artifact:**

```json
{
  "schema": "driftguard.plan.v1",
  "session_id": 42,
  "repo_fingerprint": "sha256:...",
  "base_commit": "abc1234",
  "created_at": "2026-08-18T10:00:00Z",
  "items": [
    {
      "item_hash": "sha256:...",
      "rule_id": "REF-001",
      "stage": "stg_orders",
      "path": "models/staging/stg_orders.sql",
      "span": [123, 456],
      "before": "WITH unused AS (...)",
      "after": "",
      "reason": "CTE `unused` is never referenced",
      "security_note": null,
      "tier": "safe"
    }
  ]
}
```

`dry-run` writes this file; `apply` consumes it. The file is the approval artifact — a human reviews *this JSON or its rendered diff*, then `apply --plan plan.json` runs. Plans are committed to git in the recommended workflow (auditable, reviewable in PRs).

**Apply semantics (`core/refactor/apply.py`):**

- Edits applied **bottom-up by span** (deeper spans first) so offsets stay valid; each edit asserts the bytes at `span` equal `before` (guard against stale plans).
- `--in-place` writes `.orig` backups (delete with `--no-backup`); `--out-dir DIR` writes the rewritten tree without touching inputs (default for CI).
- After each edit, the stage fingerprint is recomputed; a stage whose fingerprint did not change is reported as `NOOP` (idempotency proof, not an error).

### 4.5 Security scanning — integrated into the rewrite flow

**Decision (ADR-007):** security scanning is a **gate in the state machine**, not a side feature:

- **Baseline scan at `analyze`** — the *before* picture. Findings are recorded in `scans` and attached to candidates (`security_note`) when a rewrite touches a finding's span.
- **Regression scan at `verify`** — the *after* picture. `verify` fails (exit 1) if the rewrite *introduced* a finding at severity ≥ `--fail-on-severity` (default `high`), even if the refactor itself was clean. "Detecting vulnerabilities as it rewrites" = every rewrite's diff is security-scanned on both sides.
- **Block overlay:** candidates whose span intersects a `critical`/`high` finding are excluded from plans by default (`--allow-on-finding` re-includes with an audit note; the override is recorded, forever).

**v1 scanner catalog** (`core/security/rules/`), pattern-based (deterministic, zero-cost), each with positive/negative corpora (§8):

| ID | Rule | Severity | Detects |
|----|------|----------|---------|
| SEC-001 | `hardcoded-secret` | critical/high by context | known prefixes (`sk-`, `ghp_`, `AKIA`, `xoxb-`, `AIza…`), high-entropy tokens (≥ 20 chars, Shannon ≥ 3.5) in assignment contexts (`password=`, `api_key=`, `token=`) |
| SEC-002 | `sql-injection` | high | SQL assembled by string interpolation: f-strings/`%`/`.format`/`+` feeding `execute()`/`cursor()`/`spark.sql()` |
| SEC-003 | `unsafe-subprocess` | high | `os.system`, `subprocess.run/call/Popen` with `shell=True`, non-literal args to `shell=True` paths |
| SEC-004 | `conn-string-credential` | medium | `jdbc:…`, `postgres://user:pass@…`, DSN/`password=` in connection strings (redacted in output) |
| SEC-005 | `plaintext-auth-sql` | medium | `CREATE USER … IDENTIFIED BY 'plaintext'`, `GRANT … IDENTIFIED BY` in SQL files |

Scanner mechanics: hybrid of **token/IR matching** (for SQL files, spans come from the tokenizer) and **line-based regex** (for Python/Shell files in the repo tree, e.g. `macros/`, `scripts/` — these are scanned with line spans only). All findings: `{rule_id, severity, path, line, col, span, snippet_redacted, hint}`.

**Redaction (P6/PRD Security):** `security/redact.py` scrubs secret-shaped values (`sk-…`, high-entropy tokens, `password=…`) from every output surface: reports, JSON, plan files, audit rows, logs. The raw value never leaves the scanner.

**Suppression:** inline comments `-- driftguard:off SEC-002` (line-scoped) and `-- driftguard:off-all` (file-scoped) — audited exceptions, reviewed in the same diff as the code.

**Honest limits (documented, same spirit as README "Honest limits"):** heuristics, not a CSP. No guarantee of completeness; `critical`/`high` findings still require human review; LLM assist (Phase 4) can suggest candidates but never overrides the gate.

### 4.6 Optional LLM enrichment (Ollama) — Phase 4

- **Protocol (ADR-008):** `llm/suggestions.py` defines `Suggestion {rule_id: "LLM-<n>", stage, span, before, after, confidence, rationale}`. Suggestions are *candidates*; they flow through exactly the same `plan` → `dry-run` → approval → `apply` → `verify` path as rule output. The security gate runs on them like anything else.
- **Client:** `llm/ollama.py` uses stdlib `urllib` against `http://localhost:11434/api/generate` (configurable base URL). Offline-safe: if Ollama is absent/unreachable, commands fail with a clear message only when `--llm` was explicitly requested; otherwise LLM features are inert (P2).
- **Input hygiene:** prompts receive IR summaries + redacted snippets only — never raw secret values.
- **Cost:** $0 (local). `usage` bookkeeping (tokens per model) goes into `scans`-adjacent audit rows so `driftguard audit` shows LLM involvement per session.

---

## 5. Refactoring workflow state machine

### 5.1 States & transitions

```
        ┌────────┐   parse    ┌──────────┐   analyze   ┌─────────┐   plan   ┌──────────┐
        │  start │ ────────▶ │  parsed  │ ───────────▶ │analyzed │ ───────▶ │ planned  │
        └────────┘           └──────────┘              └─────────┘          └──────────┘
                                                                               │
                                          ┌────────────────────────────────────┤
                                          ▼                                    ▼
                                    ┌──────────┐  apply (from plan file)  ┌─────────┐
                                    │ applied  │ ◀─────────────────────── │approved │
                                    └────┬─────┘        dry-run preview   └─────────┘
                                         │ verify                         ▲
                                         ▼                                │
                                    ┌──────────┐  new findings?           │
                                    │ verified │ ──────── ✗ ──────────────┤ (re-plan / fix)
                                    └────┬─────┘   broken drift?          │
                                         │ ✓                              │
                                         ▼                                │
                                    ┌──────────┐   ✗ (no valid plan)      │
                                    │   done   │ ◀────────────────────────┘
                                    └──────────┘
```

| Transition | Guard (must hold) | Produces |
|---|---|---|
| `start → parsed` | ≥1 stage parsed; no hard parse errors (else exit 2) | `Pipeline` + diagnostics; audit row `PARSE` |
| `parsed → analyzed` | parse ok | baseline security scan, lineage, rule analysis; audit `ANALYZE` |
| `analyzed → planned` | ≥1 candidate within `--max-risk`; no blocked-on-finding candidates (unless `--allow-on-finding`) | `plan.json`; audit `PLAN` |
| `planned → approved` | plan file exists, fingerprints match, dry-run diff rendered & accepted (CLI: explicit `approve`; CI: the plan was committed in the PR) | approved plan pointer; audit `APPROVE` |
| `approved → applied` | plan hash matches; all span guards pass | rewritten files (+ `.orig` backups); `rewrites` rows; audit `APPLY` |
| `applied → verified` | re-parse ok; rule re-run yields 0 remaining candidates; security regression gate passes; drift re-check finds no *new* breaking drift | verify report; audit `VERIFY` |
| any → `aborted` | error / interrupted | audit `ABORT` with reason; session recoverable |

The machine is **pure** (`state/machine.py` has no I/O — transitions take `(state, event, context) → (state, effects)`); `session.py` executes effects and writes audit rows in the same transaction as the state change.

### 5.2 Idempotency contract (P5)

1. `parse` is deterministic: same bytes ⇒ same IR ⇒ same fingerprints.
2. `analyze` is deterministic: rules are pure; LLM output is *excluded* from the deterministic plan path (LLM suggestions enter only via explicit `--llm-suggestions` flag and are marked `LLM-*`, never merged into rule output).
3. `apply` is a pure function of `(file bytes, plan items)`. Applying a plan whose `item_hash` already appears in `rewrites` for that session is a `NOOP` with a warning, exit 0.
4. `verify` re-derives everything from disk — no cached analysis can mask a regression.
5. Confluence: a rule firing on output it just produced is a bug; golden tests assert `apply(apply(x)) == apply(x)` for every rule.

### 5.3 CLI surface & exit codes

Existing (unchanged, backward compatible):

```
python -m driftguard <root> [--db FILE] [--json|--markdown] [--no-persist]
```

Phase 1–4 additions:

```
driftguard inspect <root> [--json]                     # IR dump + diagnostics
driftguard lineage <root> [--json]                     # graph, cycles, missing, topo
driftguard drift <root> [--threshold 0.75] [--json]    # MVP gate (== default command behavior)
driftguard scan <root> [--severity medium] [--json]    # security baseline
driftguard refactor <root> plan    --rules REF-001,REF-002 --max-risk safe --out plan.json
driftguard refactor <root> dry-run --plan plan.json [--format diff|text|markdown|json]
driftguard refactor <root> apply   --plan plan.json [--in-place|--out-dir DIR] [--no-backup]
driftguard refactor <root> verify  [--plan plan.json] [--fail-on-severity high]
driftguard session show <id> | audit [--db FILE] [--since N]
driftguard --version
```

**Exit-code contract (unchanged from seed):** `0` clean / no findings · `1` findings (breaking drift, security regression, verify failure) · `2` usage/parse-hard-error. CI gates on `1`.

### 5.4 Audit trail requirements

- Every transition writes an `audit` row: `action` (PARSE/ANALYZE/PLAN/APPROVE/APPLY/VERIFY/ABORT), `from_state`, `to_state`, redacted `args_json` (plan hash, rule ids, thresholds — never secrets), `result_json` (counts, exit_code), timestamp.
- Audit rows are written **in the same transaction** as the state/rows they describe; an interrupted operation leaves the session in the prior state with an `ABORT` row (crash-resume safe: re-running the command resumes from the recorded state).
- `git_sha` captured when the repo root is a git worktree (via `git rev-parse` with a cleaned env, P7); not required — offline operation works without git.
- Reports and `--json` output include the session id so every artifact can be traced to its audit rows.

---

## 6. Implementation phases (PRD §Implementation Phases, expanded)

Effort estimates: solo engineer, part-time-ish calendar weeks; complexity S ≤ 1 wk, M ≤ 2 wk, L 3–4 wk, XL 5–6 wk.

### Phase 0 — Skeleton  *(largely landed in the seed)* — complexity S
**Deliverables:** flat `driftguard/` package, CLI entry with exit-code contract, SQLite store (runs/stages/edges/drifts), text/markdown/JSON reports, unittest suite, README, example dbt tree, CI workflow stub.
**Acceptance criteria:**
- `python -m unittest discover -s tests` green (currently 4 test modules).
- `python -m driftguard examples/models` exits 1 with breaking drift (`fct_orders_renamed` vs `stg_orders`); `--json` emits valid JSON; `--db` persists and `recent_runs()` returns history.
- ResourceWarning-clean under `python -W error::ResourceWarning`.

### Phase 1 — Core engine: pipeline parsing & introspection — complexity L
**Deliverables:** tokenizer + recursive-descent parser (§4.1), IR model + JSON serialization + fingerprints (§4.2), `jinja.py` static subset, `inspect` subcommand, parse diagnostics, package reorganization into `core/` (tests stay green), migration of seeded regex logic behind the new parser (old regex paths removed — one parser, not two).
**Acceptance criteria:**
- All `examples/` models parse with asserted IR JSON golden fixtures; projection/alias/CTE/create/ref/source/comment edge cases covered.
- Unknown constructs produce structured diagnostics; hard parse errors exit 2; no silent misparse (property: every parsed stage's `columns` are a subset of expressions the parser actually understood).
- `parse → serialize → parse` is stable (fingerprint equality).

### Phase 2 — Extend: data lineage tracking — complexity M
**Deliverables:** graph/cycles/refs modules (seeded logic migrated), topological order, `source()` resolution via `sources.yml`, `lineage` subcommand + JSON export, per-run edge persistence.
**Acceptance criteria:** known-answer lineage tests (diamond, cycle, missing ref, source ref); `lineage --json` matches golden; topo order validated on a synthetic DAG.

### Phase 3 — Polish: schema drift detection with diffs  *(MVP complete)* — complexity M
**Deliverables:** drift module migrated + threshold flag (`--threshold 0.75` default), unified diff preview (`drift diff`), history queries, golden drift fixtures, example GitHub Actions gate workflow in `examples/ci/drift.yml`.
**Acceptance criteria:** golden cases: removed=breaking, renamed=breaking (similarity ≥ threshold), added=non-breaking, identical=clean; exit codes 0/1; markdown report matches golden; SQLite history correct across runs; CI example gates a synthetic PR.

### Phase 4 — Refactoring engine + security scanning + optional LLM  *(post-MVP; PRD Features)* — complexity XL
**Deliverables:** rule registry + 6 v1 rules (§4.4), plan/dry-run/apply/verify commands, state machine + sessions + audit (§5), security scanners SEC-001..005 + regression gate + suppression (§4.5), Ollama suggestion channel (§4.6), optional DuckDB analytics backend, README limits update.
**Acceptance criteria:**
- Golden refactor tests for all 6 rules; idempotency property `apply(apply(x)) == apply(x)` holds for every rule on every golden input.
- Security corpus: 100% true positives on `positive/`, 0 false positives on `negative/` (regression-run in CI).
- E2E: `plan → dry-run → approve → apply → verify` on `examples/`; verify detects a deliberately introduced breaking change; `audit` shows one row per transition.
- Ollama-absent environment: all commands work; `--llm` without Ollama exits 2 with a clear message.

### Phase 5 — Docs, examples, launch — complexity M
**Deliverables:** `--rules-dir` plugin loading (documented trusted-code seam) — **LANDED**; PyInstaller one-dir binaries (3 OS) / stdlib `scripts/build_zipapp.py` fallback, Dockerfile + GHCR image, GitHub Pages docs, release workflow (tag → assets), `SECURITY.md`, `CONTRIBUTING.md`, changelog.
**Acceptance criteria:** binaries run on fresh ubuntu/windows/macos runners; `docker run … driftguard --version` works; docs deployed to Pages; tagged release produces assets + image; 100+ stars / 10+ contributors tracked via a `release.yml` step that prints GitHub API stats (PRD success criteria, measured, not gated). CI matrix (3 OS) and the release workflow run on GitHub only — cannot be executed on the local Windows/Python 3.14-only machine; workflows are tested by syntax/parse locally, execution is verified at the first push.

**Cross-phase rule:** no phase is "done" unless its acceptance criteria pass **and** the full suite is green on all three CI OSes.

---

## 7. Testing strategy

### 7.1 Layers

| Layer | Tool | What |
|---|---|---|
| Unit | stdlib `unittest` (flat `tests/test_*.py`) | parser grammar, IR serialization, fingerprints, lineage, drift math, redaction, state machine (pure transitions) |
| Golden-file | custom harness `scripts/run_golden.py` | per rule: `tests/golden/<rule-id>/input.sql` → run plan+apply → diff vs `expected.sql`; also assert `plan.json` matches golden candidate list |
| Security corpus | `scripts/run_corpus.py` | `tests/security_corpus/positive/<rule>/…` must each yield ≥1 finding of that rule; `negative/…` must yield 0 findings |
| Property/invariant | unittest | `apply(apply(x)) == apply(x)`; `parse→serialize→parse` fingerprint stability; plan span guard (stale plan rejected) |
| State machine | unittest | every transition incl. guards; crash-resume: interrupt after APPLY mid-batch → re-run resumes from audit state, no double-apply (idempotency) |
| E2E | unittest + temp workspaces | full `plan → … → verify` on `examples/` and on synthetic fixtures; exit codes |

### 7.2 Golden-file format (concrete)

```
tests/golden/REF-001-drop-dead-cte/
  input.sql       # dbt-style model with an unused CTE
  plan.json       # expected candidates (item_hash, span, before/after) — exact match
  expected.sql    # expected file bytes after apply
```

Harness contract: *if a rule's behavior changes, its golden files change in the same commit* (guards accidental behavior drift); `plan.json` exact-match keeps span math honest.

### 7.3 CI plan (GitHub Actions, zero-cost)

`.github/workflows/ci.yml` — every push/PR:

```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest, macos-latest]
        python: ["3.11", "3.12", "3.13"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python }}" }
      - run: python -W error::ResourceWarning -m unittest discover -s tests
      - run: python scripts/run_golden.py          # exits 1 on any mismatch
      - run: python scripts/run_corpus.py          # security regression gate
      - run: python -m driftguard examples/models --no-persist  # smoke
```

Zero-cost notes: stdlib-only ⇒ **no pip install**, no caching, fastest possible free runners; the `-W error::ResourceWarning` strictness catches unclosed connections (SQLite hygiene).

`release.yml` (tag `v*`): build PyInstaller binaries on the 3-OS matrix, attach to GitHub Release; build + push Docker image to GHCR (public, free). `docs.yml` (push to main, `docs/**`): deploy GitHub Pages.

---

## 8. Packaging & distribution

| Channel | Decision | Rationale |
|---|---|---|
| Source | `python -m driftguard` (no install) / `pip install .` with a `[project.scripts] driftguard = "driftguard.__main__:main"` entry point | zero deps ⇒ pip install is instant; setuptools is build-time only |
| Binary | **PyInstaller one-dir** (not one-file) per OS, zipped; entry `driftguard` | one-dir avoids one-file's startup extraction and Windows AV false positives; stdlib-only ⇒ ~15–25 MB, tiny |
| Docker | multi-stage: `python:3.11-slim` → copy source → `ENTRYPOINT ["python", "-m", "driftguard"]`; multi-arch (amd64/arm64) via buildx; push to GHCR | no `pip install` needed (stdlib) ⇒ small, fast, reproducible; source-image keeps the container debuggable |
| Docs | GitHub Pages, native Jekyll (no custom plugins): `docs/_config.yml` + markdown; deployed by `docs.yml` | zero-cost, zero tooling; markdown is the artifact format the whole repo already uses |
| Versioning | semver; `__version__` in `driftguard/__init__.py`; `--version`; changelog per release | boring and unambiguous |

---

## 9. Risks & mitigations (refactoring-tool-specific)

| # | Risk | Likelihood / Impact | Mitigation |
|---|------|---------------------|------------|
| R1 | **Rewrite changes behavior** (the cardinal sin of a refactoring tool) | Med / Critical | Semantics-preserving-by-construction rule design (P4); risk tiers + `--max-risk safe` default; dry-run + human approval mandatory; `verify` re-parses and re-derives everything; `.orig` backups; `--out-dir` for CI; golden tests per rule |
| R2 | **Parser misreads a construct → wrong IR → wrong rewrite** | Med / High | Narrow documented grammar; fail-loud diagnostics (P3); unknown template regions marked `dialect_hints` and excluded from rewrites; parse→serialize→parse stability property; corpus of real dbt projects in `tests/fixtures/` grown over time |
| R3 | **Security scanner false positives** (annoy users, erode trust) | Med / Med | Severity tiers; `negative/` corpus gated in CI; suppression comments reviewed in-diff; medium/low findings are advisory only |
| R4 | **Security scanner false negatives** (false safety) | Med / High | Documented "heuristics, not a CSP" limits; never auto-fix security findings; LLM assist marked as suggestion-only; `--fail-on-severity` default high keeps the gate meaningful without noise |
| R5 | **LLM suggestions are wrong or unsafe** | Med / High | ADR-008 suggestion-only channel; LLM output runs through plan approval + security gate + verify exactly like rule output; input hygiene (redacted snippets only) |
| R6 | **Idempotency violations** (double-apply, plan/disk divergence) | Low / High | Fingerprints on files, stages, pipelines, plan items; span guards assert `before` bytes; `rewrites` table dedupes by `item_hash`; property tests in CI |
| R7 | **Scope creep: Airflow/Spark/generic SQL demand** | High / Med | ADR-001 dbt-first; `Parser` protocol is the seam; roadmap explicitly says dialects land after MVP; `--rules-dir` trusted-code loading documented as such |
| R8 | **Jinja complexity explodes** | Med / Med | Static subset only; anything dynamic is marked and skipped; full rendering is a documented non-goal; if demand emerges, a render-on-demand experimental flag (Phase 5+, gated, sandboxed) |
| R9 | **Zero-cost drift** (CI minutes, image size) | Low / Med | Stdlib-only keeps CI fast; docs/release workflows are path-filtered; Docker image built on release tags only |
| R10 | **Windows-specific breakage** (PyInstaller, WAL, path separators) | Med / Med | 3-OS CI matrix from Phase 0; WAL tested on Windows; binary smoke test in release workflow |
| R11 | **Adoption friction** ("yet another CLI") | Med / Med | The CI-gate wedge: one YAML snippet to get value (README already shows it); zero-dependency install; instant local run |

---

## 10. ADR index (decisions locked in this document)

| ADR | Decision |
|-----|----------|
| 001 | Phase 1 parser target = **dbt-style SQL projects**; Airflow/generic SQL behind a `Parser` protocol later |
| 002 | **Hand-written tokenizer + recursive-descent parser** over a narrow grammar; not third-party, not regex-for-structure |
| 003 | **SQLite default persistence**; DuckDB optional analytics backend only, degrades to SQLite |
| 004 | Rule engine = **pure rule functions over IR + sourcemap edits**; no ad-hoc text rewriting |
| 005 | **Plan file is the handoff artifact**: `dry-run` output == `apply` input; plans are git-reviewable |
| 006 | **Risk tiers** (safe/suggested/risky) with a `--max-risk` gate; default `safe` |
| 007 | **Security scan is a gate in the state machine** (baseline at analyze, regression at verify, span-block overlay) |
| 008 | **Ollama = suggestion-only channel**; never auto-applied; same approval path as rules |
| 009 | PyInstaller **one-dir** binaries + **source-copy Docker** image (no pip install) + native Jekyll GitHub Pages |
| 010 | **Exit-code contract** 0/1/2 kept stable from the seed; CI gates on 1 |

---

## 11. Out of scope & future seams (PRD alignment)

- **REST API** (PRD §APIs, optional): not built in MVP; the `core` library boundary is the seam — a stdlib `http.server` wrapper is a Phase 5+ optional add-on, not a design change.
- **Failure isolation, retry policies, metrics/observability hooks per stage** (PRD §Features): these describe *pipeline runtime* behavior, not static refactoring. The audit/session system is the seam where a future watcher/server mode ("self-hosted server mode", PRD §Deployment) plugs in; nothing in the core blocks it, and nothing in the MVP pretends to provide it.
- **Web UI, collaboration, analytics dashboards, SSO, plugin marketplace** (PRD §Non-MVP): explicitly not designed for.
- **Full Jinja rendering, multi-dialect SQL, real SQL engine semantics** (types, CASE branches, macro-expanded schemas): documented limits (README "Honest limits" pattern continues).

---

## 12. First steps for the implementer (Phase 1 kickoff)

1. Reorganize `driftguard/` into the `core/` target layout (§2.1), moving seeded logic verbatim; run the flat test suite after each move.
2. Write `tokenizer.py` with span-preserving tokens; port `parser.py`'s behaviors as the acceptance baseline (every existing test must pass unchanged — they are the contract).
3. Add `ir/model.py` + `serialize.py` + `fingerprint.py`; make `inspect --json` produce versioned IR.
4. Add `jinja.py` static subset; mark unknown template regions as `dialect_hints`.
5. Land `inspect` subcommand + diagnostics; extend golden fixtures (`tests/golden/parse/*`).

Definition of done for every step: suite green on 3 OS × 3 Python versions, `-W error::ResourceWarning` clean, README honest-limits updated.