# DriftGuard — API & Interface Specification

**Status:** Living document · normative for all interfaces
**Source of truth:** `PRD.md` for scope; `docs/ARCHITECTURE.md` for how (this document extends both; it must never contradict them — where this spec adds detail, the addition is marked *extension*)
**Audience:** engineers implementing directly from this document

---

## 0. Purpose, scope, and reconciliations

This document is the normative contract for every interface a developer or CI system
can touch: the CLI surface, exit codes, error taxonomy, JSON artifact formats, the
optional REST API, integration contracts (GitHub Actions / GitLab CI / pre-commit),
the optional Ollama enrichment channel, and the backward-compatibility policy.

**MVP scope (from PRD §MVP Scope):** CLI-first, SQLite persistence, no REST server in
MVP. The REST API in §5 is **non-MVP (Phase 2+)** — specified now so the `core`
library boundary (ARCHITECTURE §11) is not polluted later, but it is explicitly
marked **not built in Phase 0–3**.

### 0.1 Reconciliations with ARCHITECTURE.md (read these first)

| # | Topic | ARCHITECTURE.md says | This spec resolves |
|---|-------|----------------------|--------------------|
| R-1 | Exit codes | §5.3 / ADR-010: `0` clean, `1` findings, `2` usage/parse-hard-error. **No other codes exist.** | §2 keeps 0/1/2 byte-identical and **extends** the table with `3` (internal error) and `5` (resource limit), `4` reserved. The extension is additive: meanings of 0/1/2 never change; CI gates on `1` exactly as ADR-010 says. |
| R-2 | LLM flag naming | §5.2 names the flag `--llm-suggestions`; §4.6/Phase-4 acceptance shorthand it as `--llm` | The **canonical flag is `--llm-suggestions`** (the idempotency contract §5.2 is authoritative). `--llm` is accepted as a deprecated alias, removed in 1.0. |
| R-3 | Security scan command name | §5.3 defines `driftguard scan <root> [--severity medium] [--json]` | `scan` is canonical; `security-scan` is a **hidden alias** for discoverability (task naming). Both resolve to the same command. |
| R-4 | `approve` transition | §5.1 state machine requires `planned → approved` with "CLI: explicit approve", but §5.3 lists no approve command | **Extension:** `driftguard refactor approve --plan FILE` (§3.9) fills the gap; CI uses `apply --ci` which records APPROVE with source `ci_committed_plan`. |
| R-5 | Session pointer | Plan file carries `session_id` (ADR-005 example) | The plan file **is** the session pointer: `dry-run`/`approve`/`apply`/`verify` resolve their session from `--plan FILE`. No separate `--session` flag needed on refactor commands. |
| R-6 | JSON envelope | IR serialization uses a `"v": 1` envelope (ARCHITECTURE §4.2); the plan file uses `"schema": "driftguard.plan.v1"` (ADR-005) | Both forms are kept exactly as documented: IR dumps use `"v"`; every other machine artifact uses `"schema": "driftguard.<kind>.vN"` (§8.3). |
| R-7 | `--llm` missing-Ollama exit code | Phase-4 acceptance: "`--llm` without Ollama exits 2 with a clear message" | Preserved and sharpened in §7.5: exit `2` fires **only** when the user explicitly requested `--llm-suggestions`. LLM availability never influences any other exit code, never changes findings, never changes the deterministic path. |

---

## 1. Global conventions

### 1.1 Invocation

- Module: `python -m driftguard` (no install required).
- Installed entry point: `driftguard` (pip `[project.scripts]`).
- Version: `driftguard --version` prints `driftguard <semver>` (e.g. `driftguard 0.4.0`)
  to stdout, exits `0`. `__version__` in `driftguard/__init__.py` is the single source.

### 1.2 Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DRIFTGUARD_DB` | `driftguard.db` | SQLite database path (overridden by `--db`) |
| `DRIFTGUARD_LLM_BASE_URL` | `http://localhost:11434` | Ollama base URL (overridden by `--llm-base-url`) |
| `DRIFTGUARD_LLM_MODEL` | `qwen2.5-coder:7b` | Ollama model for suggestions (overridden by `--llm-model`) |

Precedence: CLI flag > environment variable > default.

### 1.3 Global flags (valid on **every** command, including the legacy bare command)

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--db FILE` | path | `driftguard.db` | SQLite persistence file |
| `--json` | store_true | off | Emit exactly one machine-readable JSON document on stdout (§1.4) |
| `--markdown` | store_true | off | Emit a human-readable Markdown report on stdout |
| `--no-persist` | store_true | off | Skip all SQLite writes (read-only analysis) |
| `--artifacts-dir DIR` | path | *none* | Also write machine artifacts to `DIR/` (§8.1). Never required for stdout output. |
| `-v` / `--verbose` | store_true | off | Progress detail on stderr (`info: …` lines) |
| `-q` / `--quiet` | store_true | off | Suppress non-error stderr output |
| `-h` / `--help` | — | — | argparse help, exits `0` |
| `--version` | — | — | root command only, exits `0` |

`--json` and `--markdown` are mutually exclusive (argparse error, exit `2`).
`--no-persist` implies session-less behavior: refactor commands requiring a session
state (`apply`, `verify`) reject `--no-persist` with exit `2` (`state_error`).

### 1.4 stdout / stderr routing contract

- **stdout carries exactly one artifact document**: either the human report (text or
  Markdown) or, with `--json`, exactly one JSON document. Nothing else. Never progress,
  never notes, never errors. `stdout | jq` must always be safe.
- **stderr carries everything that is not the artifact**: `error:`/`warning:`/`info:`
  lines, progress, the legacy "persisted to …" note (see below).
- **Redaction (PRD §Security, P6):** secret-shaped values (`sk-…`, `ghp_…`, `AKIA…`,
  `xoxb-…`, `AIza…`, high-entropy tokens, `password=…`, DSN credentials) are replaced
  with `<redacted>` on **every** output surface: stdout, stderr, JSON, reports, plan
  files, diff files, audit rows, logs. The raw value never leaves the scanner.
- **Legacy exception:** the bare command (`driftguard <root>`, seed behavior) prints
  `(run #N persisted to <db>)` to **stdout** exactly as the seed does. This note moves
  to stderr in `1.0` (announced deprecation, stderr warning from `0.6`). All *new*
  commands follow the strict contract.
- **`--json` on hard error:** stdout emits nothing; stderr emits
  `error: <error_code>: <message>`. If `--artifacts-dir` is set, a JSON error document
  (`error.json`, schema `driftguard.error.v1`, §2.4) is written there.

### 1.5 JSON formatting contract

- UTF-8, `json.dumps(..., indent=2)`, single trailing newline, stable documented key
  order (field order in the schemas below is normative).
- Field naming: **snake_case everywhere** (`run_id`, `fail_on_severity`,
  `snippet_redacted`). Flags are **kebab-case** (`--fail-on-severity`, `--max-risk`).
- Timestamps: ISO 8601 UTC with `Z` suffix (`2026-08-18T10:00:00Z`).
- IDs: `run_id`/`session_id` are positive integers (SQLite row ids). Hashes are
  `sha256:<64 lowercase hex chars>`.
- Enums (lowercase, exact): severity `critical|high|medium|low`; risk tier
  `safe|suggested|risky`; finding status `open|suppressed|resolved`; rule IDs
  `REF-NNN` / `SEC-NNN` / `LLM-N`.
- Consumers **must** ignore unknown JSON fields; writers **must** never remove or
  rename a field within a schema version (see §9).

### 1.6 Path conventions

- `root` is always a directory, searched recursively for `*.sql` (Phase 1 = dbt-style
  projects per ADR-001).
- Paths in JSON artifacts are **repo-relative**, forward slashes (`models/staging/x.sql`).
- Subprocesses (only the optional `git rev-parse` / `git diff --stat`) run with a
  cleaned environment and sandboxed paths (P7, PRD §Security).

---

## 2. Exit-code and error taxonomy

### 2.1 Exit codes (the contract)

| Code | Meaning | Examples | CI semantics |
|---|---|---|---|
| **0** | Success / clean | No breaking drift; no findings at/above gate; plan written; apply completed; verify passed | Gate passed |
| **1** | **Findings** | Breaking drift; security finding ≥ `--fail-on-severity`; verify regression (new finding or new breaking drift); plan fully blocked by findings; remaining rule candidates after apply | **CI gate fails** (ADR-010: "CI gates on 1") |
| **2** | Usage / input / hard-parse error | Unknown flag; missing or invalid `root`; no `*.sql` found; hard parse error in a refactor command; missing/invalid/stale plan; state-machine violation; `--llm-suggestions` without reachable Ollama | Configuration/sequence bug — fix the invocation, not the pipeline |
| **3** | Internal error (*extension, R-1*) | Unhandled exception; invariant violated (e.g. span edit assert failure of an *internally consistent* plan) | Tool bug — report upstream; never masks 0 or 1 |
| **4** | Reserved | — | Do not assign without a major version |
| **5** | Resource limit (*extension, R-1*) | Input or output exceeds a configured cap (§2.3) | Capacity issue — raise the cap or shrink the input |

> **Reconciliation (R-1):** ARCHITECTURE §5.3/ADR-010 defines only `0/1/2`. Codes
> `3` and `5` are **additive extensions** with documented rationale: `3` protects CI
> from a Python crash defaulting to exit `1` (which would falsely report "findings"),
> and `5` lets CI distinguish capacity failures from findings. The meaning of `0/1/2`
> is frozen forever within a major version (§9.2).

**Exit-code precedence when multiple conditions apply:** `3` > `5` > `2` > `1` > `0`
(internal errors and capacity failures outrank findings).

### 2.2 Error codes (machine-readable, stable)

Emitted as `error: <code>: <message>` on stderr; `code` values are frozen strings:

| code | exit | Meaning |
|---|---|---|
| `usage_error` | 2 | Bad flags, bad arguments, missing required values |
| `no_input` | 2 | `root` missing/not a dir, or no `*.sql` stages found |
| `parse_error` | 2 | Hard parse failure (file that cannot be tokenized/parsed; refactor commands) |
| `plan_error` | 2 | Plan file missing, malformed, schema mismatch, stale (span guard failed: bytes at `span` ≠ `before`) |
| `state_error` | 2 | Command invoked in an invalid session state (e.g. `apply` before `approved`) |
| `llm_unavailable` | 2 | `--llm-suggestions` requested but Ollama unreachable (§7.5) |
| `resource_limit` | 5 | A §2.3 cap was exceeded |
| `internal_error` | 3 | Unexpected exception (tool bug) |

Parse **warnings** are not errors (ARCHITECTURE §4.1: "warnings ≠ failure"):
`parse`/`inspect`/`drift`/`scan` exit `0` with diagnostics attached; only a file that
cannot be parsed *at all* is a hard error, and only for refactor commands.

### 2.3 Resource limits (exit `5`)

| Cap | Flag | Default | When exceeded |
|---|---|---|---|
| Findings per scan | `--max-findings` | `10000` | exit `5`, artifact marked `"truncated": true` |
| Stages per pipeline | `--max-stages` | `10000` | exit `5` (refuse to analyze) |
| Rewrite candidates per plan | `--max-candidates` | `10000` | exit `5` |
| LLM suggestions per plan | `--max-llm-suggestions` | `50` | suggestions truncated with warning on stderr (never exit 5 — see §7.5) |

Per-file scan size cap: files > 2 MiB are **skipped with a warning** (not an error).
A `"truncated": true` artifact must not be consumed as complete; CI should treat it
as exit `5` already does.

### 2.4 Error artifact (written to `--artifacts-dir` on hard error)

```json
{
  "schema": "driftguard.error.v1",
  "code": "parse_error",
  "message": "models/staging/bad.sql:12: unexpected token ')'",
  "details": {"path": "models/staging/bad.sql", "line": 12, "col": 1},
  "exit_code": 2,
  "request_id": "run_7"
}
```

`details` is free-form context for self-diagnosis; `request_id` matches the audit/log
row so support can trace (§3.13).

---

## 3. CLI command reference

Command tree (all paths verified against ARCHITECTURE §5.3; additions marked
*extension*):

```
driftguard <root> [--db FILE] [--json|--markdown] [--no-persist]   # legacy gate (unchanged seed)
driftguard parse <root> [--out FILE] [--json]
driftguard inspect <root> [--json]
driftguard lineage <root> [--json]
driftguard drift <root> [--threshold 0.75] [--json]
driftguard scan <root> [--severity medium] [--fail-on-severity high] [--json]
driftguard security-scan <root> [...]                     # alias of scan (R-3)
driftguard analyze <root> [--rules ...] [--max-risk safe] [--json]
driftguard refactor <root> plan    --rules REF-001,REF-002 --max-risk safe --out plan.json [--llm-suggestions] [--allow-on-finding]
driftguard refactor <root> approve --plan plan.json                 # extension (R-4)
driftguard refactor <root> dry-run --plan plan.json [--format diff|text|markdown|json]
driftguard refactor <root> apply   --plan plan.json [--in-place|--out-dir DIR] [--no-backup] [--ci]
driftguard refactor <root> verify  [--plan plan.json] [--fail-on-severity high]
driftguard session show <id> [--json]
driftguard audit [--since N|ISO] [--session ID] [--limit N] [--json]
driftguard --version
```

### 3.1 Legacy bare command — `driftguard <root>` (unchanged, backward compatible)

Exactly the seed surface (§5.3): equivalent to `driftguard drift <root>` (the MVP
gate). Flags: `--db`, `--json`, `--markdown`, `--no-persist`. Output shape unchanged:
see `drift` (§3.5). Exit: `0` clean, `1` breaking drift, `2` usage/no-input.

### 3.2 `driftguard parse <root>`

State machine: `start → parsed`. Runs tokenizer → recursive-descent parser → IR
(Phase 1), persists `stages` rows (unless `--no-persist`), returns diagnostics.

Flags: `--out FILE` (*extension*: write the IR snapshot artifact, default
`parse.json` when `--out` given), plus global flags.

Exit: `0` parsed (warnings allowed — they ride in `diagnostics`); `2` `no_input` /
`parse_error`; `5` `--max-stages` exceeded.

`--json` output — schema `driftguard.parse.v1`:

```json
{
  "schema": "driftguard.parse.v1",
  "version": 1,
  "run_id": 14,
  "root": "examples/models",
  "checked_at": "2026-08-18T10:00:00Z",
  "git_sha": "abc1234",
  "pipeline_fingerprint": "sha256:1a2b...",
  "stage_count": 3,
  "diagnostics": [
    {"file": "models/staging/stg_orders.sql", "line": 5, "col": 1,
     "reason": "unknown_template_region", "severity": "warning"}
  ],
  "stages": [
    {"name": "stg_orders", "path": "models/staging/stg_orders.sql",
     "kind": "model", "fingerprint": "sha256:...",
     "columns": [{"name": "order_id", "source_expr": "id", "alias": null, "span": [18, 20]}],
     "refs": [{"producer": "raw_orders", "consumer": "stg_orders", "kind": "ref", "expected_columns": []}],
     "sources": [{"source": "raw", "table": "orders"}],
     "ctes": [{"name": "unused", "span": [10, 120], "referenced_by": []}],
     "create_name": null,
     "dialect_hints": [],
     "diagnostics": []}
  ]
}
```

### 3.3 `driftguard inspect <root>`

IR dump + diagnostics view (ARCHITECTURE §5.3). Same parser as `parse`; persists a
`runs` row. `--json` output uses the **IR envelope** `{"v": 1, "pipeline": {…},
"diagnostics": […]}` (ARCHITECTURE §4.2 — the one artifact that uses `"v"`, R-6).
Exit codes identical to `parse`.

### 3.4 `driftguard lineage <root>`

Graph, cycles, missing refs, topological order (ARCHITECTURE §4.3). Flags: `--json`,
globals. Exit: `0` always (a cycle or missing ref is a *finding*, reported in the
artifact — see note), `2` no-input/parse-hard-error. `--json` — schema
`driftguard.lineage.v1`:

```json
{
  "schema": "driftguard.lineage.v1",
  "version": 1,
  "run_id": 15,
  "root": "examples/models",
  "checked_at": "2026-08-18T10:00:00Z",
  "git_sha": "abc1234",
  "pipeline_fingerprint": "sha256:...",
  "edges": [{"producer": "stg_orders", "consumer": "fct_orders", "kind": "ref"}],
  "cycles": [],
  "missing": [{"consumer": "fct_orders", "ref": "stg_customers"}],
  "topo_order": ["stg_orders", "fct_orders"]
}
```

### 3.5 `driftguard drift <root>` — the MVP gate (== legacy default)

Schema drift detection (Phase 3 semantics): `removed` columns = breaking; `renamed`
(similarity ≥ `--threshold`) = breaking; `added` = non-breaking; identical = clean.
Lineage matches `lineage` exactly — `sources.yml`-defined `source()` refs resolve to
edges (kind `source`) instead of missing refs; drift is only computed for
stage→stage edges (a source producer has no schema, so it never drifts).

Flags: `--threshold FLOAT` (0.0–1.0, default `0.75`), globals. Exit: `0` no breaking
drift; `1` breaking drift; `2` usage/no-input. `--json` — schema
`driftguard.drift.v1` — **adds** `schema`/`version`/`root`/`checked_at`/`git_sha`/
`threshold`/`pipeline_fingerprint`/`breaking` to the seed payload; the seed fields
`stages`, `edges`, `cycles`, `drifts`, `run_id` are **byte-compatible with the seed**
(`drifts[].producer|consumer|added|removed|renamed|breaking` unchanged, R-6):

```json
{
  "schema": "driftguard.drift.v1",
  "version": 1,
  "run_id": 16,
  "root": "examples/models",
  "checked_at": "2026-08-18T10:00:00Z",
  "git_sha": "abc1234",
  "pipeline_fingerprint": "sha256:...",
  "threshold": 0.75,
  "stages": 2,
  "edges": 1,
  "cycles": 0,
  "breaking": true,
  "drifts": [
    {"producer": "stg_orders", "consumer": "fct_orders",
     "added": [], "removed": ["name"], "renamed": [], "breaking": true}
  ]
}
```

**Extension — `driftguard drift diff <root>` (dry-run preview):** renders each
drift as a unified diff (`--- a/<producer> (schema)` / `+++ b/<consumer>
(expected)`; removed columns and rename sources are `-` lines, added columns
and rename targets are `+` lines) — the schema delta the CI gate shows when it
fails. Same flags (`--threshold`), same exit codes (0/1/2/5); text-only, no
JSON artifact. `diff` is only treated as a subcommand when no directory named
`diff` exists (mirrors the `_is_unknown_subcommand` rule).

### 3.6 `driftguard scan <root>` (alias: `security-scan`)

Security baseline scan (ADR-007). Applies suppression comments, records findings in
`scans`, honors redaction.

Flags: `--severity LEVEL` (report filter, default `medium`); `--fail-on-severity LEVEL`
(gate threshold, default `high`; `none` disables the gate); globals. Exit: `0` no
finding ≥ `--fail-on-severity`; `1` findings ≥ gate; `2` usage/no-input; `5`
`--max-findings`. `--json` — schema `driftguard.scan.v1`:

```json
{
  "schema": "driftguard.scan.v1",
  "version": 1,
  "run_id": 17,
  "root": ".",
  "checked_at": "2026-08-18T10:00:00Z",
  "git_sha": "abc1234",
  "severity": "medium",
  "fail_on_severity": "high",
  "gate": "passed",
  "counts": {"critical": 0, "high": 0, "medium": 1, "low": 2, "suppressed": 1},
  "findings": [
    {"rule_id": "SEC-004", "severity": "medium", "path": "macros/jdbc_conn.py",
     "line": 12, "col": 5, "span": [120, 180],
     "snippet_redacted": "conn = jdbc(url='jdbc:postgresql://db:5432/app?user=app&password=<redacted>')",
     "hint": "credentials in connection string; use a secret manager",
     "status": "open"}
  ]
}
```

Suppression syntax: `-- driftguard:off SEC-002` (SQL line-scoped), `-- driftguard:off-all`
(SQL file-scoped), `# driftguard:off SEC-002` (Python/Shell line-scoped). Suppressed
findings keep `status: "suppressed"`, never count toward the gate, always appear in
`counts.suppressed` (auditable exceptions, reviewed in the same diff as the code —
ARCHITECTURE §4.5).

### 3.7 `driftguard analyze <root>` (*extension*: exposes the `parsed → analyzed` transition)

Runs baseline security scan + lineage + rule candidate analysis in one pass and
persists the session-ready snapshot. This is what `refactor plan` consumes; it is
also callable standalone for CI baselining.

Flags: `--rules LIST` (comma-separated rule IDs or `all`, default `all`);
`--max-risk safe|suggested|risky` (default `safe`, ADR-006); `--fail-on-severity`
(default `high`, informational here — baseline findings never fail this command);
`--rules-dir DIR` (trusted-code plugin loader, same semantics as `refactor plan`);
globals.

Exit: `0` (baseline findings are **recorded, not gating** — the "before" picture);
`2` usage/parse-error; `5` caps. `--json` — schema `driftguard.analysis.v1`:

```json
{
  "schema": "driftguard.analysis.v1",
  "version": 1,
  "run_id": 18,
  "session_id": null,
  "root": ".",
  "checked_at": "2026-08-18T10:00:00Z",
  "git_sha": "abc1234",
  "pipeline_fingerprint": "sha256:...",
  "baseline_scan": {"findings": [], "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0, "suppressed": 0}},
  "lineage": {"edges": [], "cycles": [], "missing": [], "topo_order": []},
  "candidates": [
    {"rule_id": "REF-001", "tier": "safe", "stage": "stg_orders",
     "path": "models/staging/stg_orders.sql", "span": [10, 120],
     "before": "WITH unused AS (...)", "after": "",
     "reason": "CTE `unused` is never referenced", "security_note": null}
  ],
  "blocked": [],
  "llm": {"used": false, "suggestions": 0}
}
```

### 3.8 `driftguard refactor <root> plan`

State machine: `analyzed → planned`. Creates a new session (or continues `--session ID`
— *extension* — which must be in `analyzed` state), evaluates rule candidates,
applies the security block overlay, optionally requests LLM suggestions, writes the
plan file (ADR-005).

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--rules LIST` | `all` | Comma-separated rule IDs (`REF-001,REF-003`) — filters across built-ins and plugins |
| `--rules-dir DIR` | none | Load Rule-protocol plugins from DIR (**trusted-code seam**: the files are executed; load only code you wrote or audited). Deterministic order; built-in id collisions rejected with a warning; invalid plugins warned and skipped. Persisted on the session (`sessions.rules_dir`) so `verify` re-analyzes with the same set |
| `--max-risk safe\|suggested\|risky` | `safe` | Highest tier admitted to the plan (ADR-006) |
| `--out FILE` | `plan.json` | Plan artifact path (the approval artifact) |
| `--session ID` | new session | Continue an existing session in `analyzed` state |
| `--allow-on-finding` | off | Re-include candidates whose span intersects a critical/high finding (recorded in audit, forever) |
| `--llm-suggestions` | off | Request Ollama suggestions (R-2; §7) |
| `--llm-min-confidence FLOAT` | `0.7` | Drop LLM suggestions below this confidence |
| `--llm-base-url URL`, `--llm-model NAME`, `--llm-timeout SECS` | env defaults, 30 | Ollama client tuning |

Exit: `0` plan written (may be empty when no candidates exist); `1` **all** candidates
were blocked by security findings (blocked-only plan — the rewrite you asked for is
blocked; use `--allow-on-finding` deliberately); `2` usage/parse/state errors;
`5` `--max-candidates`; `3` internal.

Output file — schema `driftguard.plan.v1` (ADR-005 shape **verbatim**, plus additive
`blocked` and `llm_used`):

```json
{
  "schema": "driftguard.plan.v1",
  "session_id": 42,
  "repo_fingerprint": "sha256:...",
  "base_commit": "abc1234",
  "created_at": "2026-08-18T10:00:00Z",
  "llm_used": false,
  "items": [
    {"item_hash": "sha256:...", "rule_id": "REF-001", "stage": "stg_orders",
     "path": "models/staging/stg_orders.sql", "span": [123, 456],
     "before": "WITH unused AS (...)", "after": "",
     "reason": "CTE `unused` is never referenced", "security_note": null,
     "tier": "safe"}
  ],
  "blocked": [
    {"rule_id": "REF-005", "stage": "fct_orders", "path": "models/marts/fct_orders.sql",
     "span": [10, 40], "reason": "span intersects SEC-001 (critical)"}
  ]
}
```

The plan file **is** the session pointer (R-5): every later refactor command reads
`session_id` from it. Plans are committed to git in the recommended workflow
(ARCHITECTURE ADR-005) — they are the reviewable, approvable artifact.

### 3.9 `driftguard refactor <root> approve` (*extension*, R-4)

State machine: `planned → approved`. Flags: `--plan FILE` (required), globals.

Guard: plan exists, `repo_fingerprint` matches the current root, all `item_hash`
values match the session's stored plan. Records audit `APPROVE`. Exit: `0` approved;
`2` `plan_error` (missing/stale/fingerprint mismatch), `state_error` (session not in
`planned`); `3` internal.

### 3.10 `driftguard refactor <root> dry-run`

State machine: `planned → approved` is *rendered* here for review (approval itself is
`approve` or CI's committed plan). Renders the diff preview without touching files.

Flags: `--plan FILE` (required); `--format diff|text|markdown|json` (default `diff`);
globals. Exit: `0` (dry-run is a preview — findings do not fail it); `2` `plan_error`.

`--format json` — schema `driftguard.dryrun.v1`:

```json
{
  "schema": "driftguard.dryrun.v1",
  "version": 1,
  "session_id": 42,
  "plan_hash": "sha256:...",
  "items": [
    {"item_hash": "sha256:...", "rule_id": "REF-001", "stage": "stg_orders",
     "path": "models/staging/stg_orders.sql",
     "diff": "--- a/models/staging/stg_orders.sql\n+++ b/models/staging/stg_orders.sql\n@@ -1,7 +1,4 @@\n ...",
     "status": "will_apply"}
  ],
  "summary": {"will_apply": 1, "noop": 0, "errors": 0}
}
```

`noop` items are those whose `before` already matches the file (idempotency proof,
not an error — ARCHITECTURE §4.4).

### 3.11 `driftguard refactor <root> apply`

State machine: `approved → applied`. Consumes the plan, edits via sourcemap spans
(bottom-up by span; each edit asserts bytes at `span` == `before`), recomputes
fingerprints after each edit.

Flags: `--plan FILE` (required); `--in-place` XOR `--out-dir DIR` (exactly one; if
neither, exit `2` `usage_error` — seed/ARCHITECTURE: `--out-dir` is the CI default,
`--in-place` the local workflow); `--no-backup` (with `--in-place`, suppress `.orig`
backups); `--ci` (*extension*: records APPROVE with source `ci_committed_plan` — the
plan was reviewed and committed in the PR, ARCHITECTURE §5.1); globals.

Exit: `0` applied (any NOOP items reported, not errored); `1` — *not used here*;
`2` `plan_error` (stale: span guard failed), `state_error` (not approved; without
`--ci`), `usage_error` (`--in-place` + `--out-dir` both given); `3` internal
(invariant violation: applied bytes ≠ `after`); `5` caps.

`--json` — schema `driftguard.apply.v1`:

```json
{
  "schema": "driftguard.apply.v1",
  "version": 1,
  "session_id": 42,
  "plan_hash": "sha256:...",
  "mode": "in-place",
  "backups": true,
  "items": [
    {"item_hash": "sha256:...", "rule_id": "REF-001", "stage": "stg_orders",
     "path": "models/staging/stg_orders.sql",
     "fingerprint_before": "sha256:...", "fingerprint_after": "sha256:...",
     "status": "applied"}
  ],
  "summary": {"applied": 1, "noop": 0, "skipped": 0}
}
```

Idempotency: applying an `item_hash` already present in `rewrites` for the session is
a `NOOP` with a warning on stderr, exit `0` (ARCHITECTURE §5.2.3). `--no-persist` is
rejected here (`state_error`, exit `2`).

### 3.12 `driftguard refactor <root> verify`

State machine: `applied → verified` (or `done`). Re-derives **everything** from disk:
re-parse, re-run rules (0 remaining candidates expected), security regression gate
(no finding ≥ `--fail-on-severity` that is **new** vs the session's baseline), drift
re-check (no **new** breaking drift).

Flags: `--plan FILE` (optional — without it, uses the latest session whose
`repo_fingerprint` matches the current root; missing → `state_error` exit `2`);
`--fail-on-severity LEVEL` (default `high`); globals.

Exit: `0` verified; `1` verify failure — new security finding ≥ gate, remaining
candidates, or new breaking drift (ARCHITECTURE §4.5: "verify fails (exit 1) if the
rewrite introduced a finding"); `2` usage/parse/state; `3` internal; `5` caps.

`--json` — schema `driftguard.verify.v1`:

```json
{
  "schema": "driftguard.verify.v1",
  "version": 1,
  "session_id": 42,
  "plan_hash": "sha256:...",
  "reparse": {"ok": true, "diagnostics": []},
  "remaining_candidates": 0,
  "security": {
    "fail_on_severity": "high",
    "baseline_findings": 0, "findings_after": 0,
    "introduced": [], "resolved": [],
    "gate": "passed"
  },
  "drift": {"new_breaking": [], "gate": "passed"},
  "result": "verified",
  "failures": []
}
```

On failure, `result: "failed"` and `failures` carries the failing gate codes
(`security_regression`, `remaining_candidates`, `breaking_drift`).

### 3.13 `driftguard session show <id>` / `driftguard audit`

Observability (ARCHITECTURE §5.4). Flags: `--json`, `--since N|ISO` (hours ago, or an
ISO 8601 timestamp), `--session ID`, `--limit N` (default 100), globals.

Exit: `0`; `2` unknown session (`no_input`-style error, message on stderr). `--json` —
schema `driftguard.audit.v1`:

```json
{
  "schema": "driftguard.audit.v1",
  "version": 1,
  "rows": [
    {"id": 301, "session_id": 42, "ts": "2026-08-18T10:00:00Z",
     "action": "PLAN", "from_state": "analyzed", "to_state": "planned",
     "args_json": {"plan_hash": "sha256:...", "rule_ids": ["REF-001"], "thresholds": {"max_risk": "safe"}},
     "result_json": {"items": 1, "exit_code": 0}, "exit_code": 0}
  ]
}
```

`args_json` is always redacted before persist (P6). Every state transition writes ≥ 1
row in the same transaction as the state change; an interrupted operation leaves the
prior state plus an `ABORT` row (crash-resume safe — ARCHITECTURE §5.4).

---

## 4. State-machine alignment (commands ↔ transitions)

| CLI command | Transition | Exit on failure |
|---|---|---|
| `parse` | `start → parsed` | `2` hard parse; `5` caps |
| `inspect` / `lineage` / `drift` | read-only views of `parsed` | `2` |
| `scan` / `security-scan` | read-only security view | `1` findings ≥ gate |
| `analyze` | `parsed → analyzed` | `2` / `5` (baseline findings never fail) |
| `refactor plan` | `analyzed → planned` | `1` fully blocked by findings; `2` state/usage |
| `refactor approve` | `planned → approved` | `2` plan/state errors |
| `refactor dry-run` | renders `planned → approved` preview | `2` |
| `refactor apply` | `approved → applied` | `2` stale plan / state; `3` invariant |
| `refactor verify` | `applied → verified` / `done` | `1` regression (security/drift/candidates) |
| any (error/interrupt) | `→ aborted` | `2`/`3`; session recoverable by re-running |

Idempotency contract (ARCHITECTURE §5.2, normative): `parse` deterministic; `analyze`
deterministic (LLM excluded from the deterministic path); `apply` is a pure function
of (file bytes, plan items) with `rewrites` dedupe by `item_hash`; `verify` re-derives
everything from disk; `apply(apply(x)) == apply(x)` is asserted by golden tests.

---

## 5. REST API (non-MVP — Phase 2+, not built in MVP)

> **Status:** specified for the seam, **not implemented** in MVP (PRD §APIs,
> ARCHITECTURE §11). Built on the stdlib `http.server` wrapper around `driftguard.core`
> — the library boundary, not a new design. When built, it ships behind `driftguard server`
> (Phase 2+ CLI command).

### 5.1 Server model

- Single process, single worker, SQLite single-writer (WAL, `busy_timeout=5000`).
- Base path: `/v1`. Content type: `application/json` only.
- Default bind `127.0.0.1:8717`. Binding a non-loopback address **requires**
  `--api-key-file` (startup error otherwise). TLS is terminated by a reverse proxy
  (documented deployment), never by the stdlib server.
- Auth: `X-API-Key: <key>` header. Keys are generated by `driftguard server keys add`
  (Phase 2+), stored hashed (sha256) in an additive `api_keys` migration. Loopback
  connections may run without a key (documented local default).
- Idempotency: `POST` endpoints accept `Idempotency-Key: <uuid>`; a replayed key
  returns the stored response with header `Idempotent-Replay: true` and the same
  status (additive `idempotency_keys` migration).

### 5.2 Rate limits (communicated, never ambush)

Per API key, token bucket: **60 req/min, burst 120** (configurable at startup).
Every response carries:

```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 47
X-RateLimit-Reset: 1720483200
```

On breach: `429 Too Many Requests` + `Retry-After: 30` + error body
`rate_limit_exceeded`. Findings/analysis responses are never rate-limited differently
by payload size — only by request count.

### 5.3 HTTP ↔ exit-code mapping

| HTTP | Meaning | Error `code` | CLI exit |
|---|---|---|---|
| `200` / `201` | success | — | 0/1 (per artifact `exit_code` field) |
| `400` | bad request | `usage_error` | 2 |
| `404` | unknown run/session | `not_found` | 2 |
| `409` | state-machine violation | `state_conflict` | 2 |
| `422` | unprocessable (hard parse, stale plan) | `parse_error` / `plan_error` | 2 |
| `429` | rate limited | `rate_limit_exceeded` | — |
| `500` | internal | `internal_error` | 3 |
| `503` | capacity (caps exceeded) | `resource_limit` | 5 |

Error body (one shape, everywhere):

```json
{
  "code": "state_conflict",
  "message": "session 42 is in state 'planned'; apply requires 'approved'",
  "details": {"session_id": 42, "state": "planned", "required_state": "approved"},
  "request_id": "req_9f3a"
}
```

### 5.4 Endpoints

#### `GET /v1/health`
```json
{"status": "ok", "version": "0.4.0",
 "llm": {"available": true, "model": "qwen2.5-coder:7b", "base_url": "http://localhost:11434"},
 "db": "ok"}
```

#### `POST /v1/runs` — run one analysis command
Request: `{"root": ".", "command": "drift|scan|parse|analyze|lineage|inspect",
"threshold": 0.75, "severity": "medium", "fail_on_severity": "high",
"max_risk": "safe", "rules": ["REF-001"]}`
Response `200`: the command's artifact (§3) wrapped as
`{"run_id": 16, "command": "drift", "exit_code": 1, "result": {…artifact…}}`.
Honors `Idempotency-Key`.

#### `GET /v1/runs/{run_id}`, `GET /v1/runs?root=&command=&limit=100&offset=0`
List envelope: `{"items": […], "limit": 100, "offset": 0, "total": 12}`.
`limit` max `1000`.

#### `POST /v1/sessions` — `{"root": "."}` → `{"session_id": 42, "state": "start"}`

#### `POST /v1/sessions/{id}/plan`
Request: `{"rules": ["REF-001"], "max_risk": "safe", "llm_suggestions": false,
"allow_on_finding": false, "llm_min_confidence": 0.7}` → plan artifact (§3.8).

#### `POST /v1/sessions/{id}/approve` → `{"state": "approved", "plan_hash": "sha256:..."}`

#### `POST /v1/sessions/{id}/dry-run` — `{"format": "diff"}` → dry-run artifact (§3.10)

#### `POST /v1/sessions/{id}/apply`
Request: `{"mode": "in-place|out-dir", "out_dir": null, "no_backup": false, "ci": false}`
→ apply artifact (§3.11). Honors `Idempotency-Key` (the dangerous one — double-apply
must be impossible).

#### `POST /v1/sessions/{id}/verify` — `{"fail_on_severity": "high"}` → verify artifact (§3.12)

#### `GET /v1/sessions/{id}` → `{"session_id": 42, "state": "applied", "repo_fingerprint": "sha256:...", "plan_path": "plan.json", "created_at": "…"}`
#### `GET /v1/sessions/{id}/audit` → audit artifact (§3.13)
#### `GET /v1/scans?run_id=&session_id=` and `POST /v1/scans` — scan artifact (§3.6)

---

## 6. Integration contracts

### 6.1 GitHub Actions action (`action.yml`, composite — primary)

The action is a **composite action** (runs on any runner; pip install is instant —
stdlib-only package, ARCHITECTURE §8). A Docker-image variant is documented in the
repo `README` (image tag updated per release; docker actions cannot interpolate
`inputs.version` into `runs.image`).

```yaml
name: 'DriftGuard'
description: 'Schema-drift & security gate for dbt-style SQL pipelines (exit 1 = gate failed)'
inputs:
  path:
    description: 'Pipeline root (searched recursively for *.sql)'
    required: false
    default: '.'
  command:
    description: 'drift | scan | analyze | verify'
    required: false
    default: 'drift'
  threshold:
    description: 'Rename similarity threshold (0.0-1.0)'
    required: false
    default: '0.75'
  severity:
    description: 'Scan report filter (critical|high|medium|low)'
    required: false
    default: 'medium'
  fail-on-severity:
    description: 'Gate threshold; none disables'
    required: false
    default: 'high'
  max-risk:
    description: 'safe | suggested | risky'
    required: false
    default: 'safe'
  rules:
    description: 'Comma-separated rule IDs or all'
    required: false
    default: 'all'
  version:
    description: 'PyPI version; "latest" = latest release'
    required: false
    default: 'latest'
  artifacts-dir:
    description: 'Where machine artifacts are written (relative to workspace)'
    required: false
    default: 'driftguard-out'
outputs:
  exit-code:
    description: 'driftguard exit code (0 clean, 1 findings/gate failed, 2 usage, 3 internal, 5 resource limit)'
    value: ${{ steps.run.outputs.exit-code }}
  breaking:
    description: 'true when breaking drift or gate-level findings were reported'
    value: ${{ steps.run.outputs.breaking }}
  findings:
    description: 'Number of findings at/above the gate severity'
    value: ${{ steps.run.outputs.findings }}
  report-path:
    description: 'Path of the JSON artifact (under artifacts-dir)'
    value: ${{ steps.run.outputs.report-path }}
runs:
  using: composite
  steps:
    - name: Install DriftGuard
      shell: bash
      run: |
        if [ "${{ inputs.version }}" = "latest" ]; then
          python -m pip install --quiet driftguard
        else
          python -m pip install --quiet "driftguard==${{ inputs.version }}"
        fi
    - name: Run DriftGuard gate
      id: run
      shell: bash
      run: |
        set +e
        driftguard "${{ inputs.command }}" "${{ inputs.path }}" \
          --json \
          --threshold "${{ inputs.threshold }}" \
          --severity "${{ inputs.severity }}" \
          --fail-on-severity "${{ inputs.fail-on-severity }}" \
          --max-risk "${{ inputs.max-risk }}" \
          --rules "${{ inputs.rules }}" \
          --no-persist \
          --artifacts-dir "${{ inputs.artifacts-dir }}"
        code=$?
        echo "exit-code=$code" >> "$GITHUB_OUTPUT"
        echo "report-path=${{ inputs.artifacts-dir }}/report.json" >> "$GITHUB_OUTPUT"
        exit $code
```

**Gate semantics:** the step `exit $code` fails the job on `1` (findings), `2`, `3`,
`5` — exactly the contract. **Advisory mode:** users set
`continue-on-error: true` on the step to report without failing (documented in
README). The drift-only gate equivalent (Phase 3 example, ARCHITECTURE §7.3):

```yaml
- uses: driftguard/driftguard@v1
  with:
    command: drift
    threshold: 0.75
```

### 6.2 GitLab CI job template

```yaml
# .gitlab-ci.yml — include or paste; the gate = job failure on exit 1
driftguard:
  stage: test
  image: ghcr.io/driftguard/driftguard:v1   # or: python:3.11-slim + pip install driftguard
  variables:
    DRIFTGUARD_PATH: "."
    DRIFTGUARD_COMMAND: "drift"            # drift | scan | analyze | verify
    DRIFTGUARD_FAIL_ON_SEVERITY: "high"
  script:
    - driftguard "$DRIFTGUARD_COMMAND" "$DRIFTGUARD_PATH" --json --no-persist
      --fail-on-severity "$DRIFTGUARD_FAIL_ON_SEVERITY"
      --artifacts-dir "$CI_PROJECT_DIR/driftguard-out"
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  artifacts:
    paths:
      - driftguard-out/
    expire_in: 7 days
    when: always
  allow_failure: false          # gate: exit 1 fails the MR pipeline
```

Advisory mode: `allow_failure: true`. The pipeline gate, the `expire_in` artifacts
(JSON + diffs), and the MR-scoped rule are the whole contract.

### 6.3 pre-commit hook (`.pre-commit-hooks.yaml`)

```yaml
- id: driftguard
  name: driftguard (SQL pipeline drift & security gate)
  description: Fail pre-commit when dbt-style pipeline refactors break schema or introduce security findings
  entry: driftguard drift --json --no-persist
  language: python
  types_or: [sql, python, shell]
  pass_filenames: false          # drift needs the whole tree, not single files
  minimum_pre_commit_version: '2.9.0'
```

`pass_filenames: false` is mandatory (drift is a whole-pipeline property). The
Docker variant uses `language: docker_image` + `entry: driftguard`. Users pin with
`rev: v1.2.3`; pre-commit caches the env — driftguard is stdlib-only, so installs
are near-instant.

---

## 7. LLM enrichment interface (Ollama) — suggestion-only channel

### 7.1 Role (ADR-008, P8)

LLM output is a **suggestion channel, not an author**: suggestions are candidates
marked `LLM-N`, enter only via `--llm-suggestions`, flow through the exact same
plan → approval → apply → verify path as rule output, and the security gate runs on
them like anything else. They are **never auto-applied** and **never** merged into
deterministic rule output.

### 7.2 Client contract

`driftguard/llm/ollama.py` — stdlib `urllib` only (P1), against
`POST {base}/api/generate` (default `http://localhost:11434`, overridable:
`--llm-base-url` / `DRIFTGUARD_LLM_BASE_URL`).

Request:

```json
{
  "model": "qwen2.5-coder:7b",
  "prompt": "<assembled prompt, §7.3>",
  "stream": false,
  "format": "json",
  "options": {"temperature": 0.2, "num_ctx": 8192}
}
```

Response (Ollama's shape): `{"model": "...", "created_at": "...", "response": "<json string>",
"done": true, "total_duration": 123, "prompt_eval_count": 10, "eval_count": 20}`.
DriftGuard parses `response` as JSON and validates it against §7.4. Failures to parse
or validate count as *degradation* (§7.5), never as findings.

### 7.3 Prompt contract

- **Input hygiene:** prompts receive IR summaries + **redacted** snippets only. Raw
  secret values never reach the prompt (§4.6). The prompt must instruct the model:
  output JSON only; never invent file paths; never touch spans outside the provided
  file; mark uncertainty with low `confidence`.
- Prompt structure (stable, versioned `prompt_v1`): system block (role, redaction,
  output schema reference), context block (pipeline fingerprint, stage summaries —
  names/columns/refs/dialect hints), candidate-relevant snippets (redacted), rule
  catalog summary, output instruction (exact JSON shape).

### 7.4 Suggestion schema — `driftguard.suggestions.v1`

```json
{
  "schema": "driftguard.suggestions.v1",
  "model": "qwen2.5-coder:7b",
  "suggestions": [
    {"rule_id": "LLM-1", "stage": "stg_orders", "path": "models/staging/stg_orders.sql",
     "span": [200, 260], "before": "SELECT id, name, name AS name2 FROM ...",
     "after": "SELECT id, name FROM ...",
     "confidence": 0.8, "rationale": "duplicate projection column; identical expression+alias"}
  ]
}
```

Validation rules (hard rejects → suggestion dropped with stderr warning):
1. `span` in-bounds for `path`; `before` must equal the file bytes at `span`
   (same guard as plans — stale/imaginary snippets never enter a plan).
2. `confidence` ∈ [0,1] and ≥ `--llm-min-confidence` (default `0.7`).
3. No overlap with an existing deterministic candidate (exact `before`/`after`
   equality → dedupe in favor of the rule).
4. **Tier is forced to `suggested`** — LLM output cannot be *proven* safe under the
   IR model (P4), so it can never be `SAFE`; including it requires
   `--max-risk suggested` or higher. (Consequence: default `--max-risk safe` runs
   never include LLM suggestions — by design.)
5. Security block overlay applies (§3.8 `--allow-on-finding`).
6. Cap: `--max-llm-suggestions` (default `50`); excess suggestions are dropped with a
   stderr warning — this is a suggestion-channel cap, **never** exit `5` (§7.5).
7. `rule_id` numbering (`LLM-1`, `LLM-2`, …) is assigned sequentially by DriftGuard
   at plan time, never by the model; the model's own IDs are ignored.

### 7.5 Offline degradation — the hard rule

> **LLM availability must NEVER influence exit codes.** Exit codes are a pure
> function of deterministic analysis. Ollama's presence or absence never changes
> findings, never changes the deterministic plan path, never changes `verify`'s
> result, and never degrades a run's artifact. A run without `--llm-suggestions`
> makes zero network calls and is bit-for-bit identical to a world without Ollama.

The **single, deliberate exception** (R-2/R-7, ARCHITECTURE Phase-4 acceptance):
`--llm-suggestions` was explicitly requested and Ollama is unreachable at call time
→ exit `2`, code `llm_unavailable`, clear stderr message. This is a
**usage/configuration error** (the user requested a capability whose dependency is
absent) — not an availability-driven outcome of any analysis. Every other command,
and every other run, is immune to Ollama's state.

| Scenario | Behavior | Exit code |
|---|---|---|
| No `--llm-suggestions` | zero network calls; inert channel (P2) | deterministic path only |
| `--llm-suggestions`, Ollama absent/unreachable at start | `error: llm_unavailable: …` on stderr | `2` (only exception) |
| `--llm-suggestions`, Ollama fails **mid-run** (network flap, timeout, malformed response, invalid suggestion JSON) | 1 retry with 1s backoff, then degrade: zero suggestions, `warning: llm: …` on stderr; deterministic plan proceeds unmodified | deterministic path only (never `1`, never `5`) |
| Suggestion rejected by validation (§7.4) | dropped with warning; plan proceeds | deterministic path only |

Degradation is observable, never silent: `"llm": {"used": true, "suggestions": 0}`
in the analysis artifact, `llm_used` in audit rows, stderr warnings. `--llm-timeout`
(default `30`s) bounds each call; exceeding it is a mid-run failure (degrade), not a
resource limit.

---

## 8. Output artifact formats

### 8.1 Artifact layout (`--artifacts-dir DIR`)

Machine artifacts are written to stdout under `--json`; `--artifacts-dir` additionally
persists them. Layout:

```
DIR/
  report.json                  # PRIMARY artifact of the command — STABLE name, always
                               # the machine artifact the command produced (§1.4)
  drift-<run_id>.json          # versioned copies (run/session id when persisted)
  scan-<run_id>.json  analysis-<run_id>.json  verify-<session_id>.json
  parse-<run_id>.json  lineage-<run_id>.json  dryrun-<session_id>.json
  apply-<session_id>.json  audit.json  error.json
  plan.json                    # plan artifact (mirrors --out)
  diffs/<stage>.diff           # per-stage unified diffs (dry-run / apply)
```

Rules:

- `report.json` is a byte-identical copy of what `--json` printed to stdout — CI
  consumers read one stable path (§6.1 `report-path`).
- Versioned copies (`<kind>-<id>.json`) preserve history when the same directory is
  reused across runs; ids come from `run_id` (analysis commands) or `session_id`
  (refactor commands). With `--no-persist`, only `report.json` is written.
- `error.json` (schema `driftguard.error.v1`, §2.4) is written only on hard error
  (exit ≥ 2).
- Diff files: unified diff (`--- a/<path>` / `+++ b/<path>`), UTF-8, redacted; stage
  name slugified to a path-safe filename. `diffs/` is written by `dry-run --format diff`
  and `apply`.
- `report.json` and every artifact end with a single trailing newline; JSON follows
  §1.5.

### 8.2 Machine artifact index (schema ↔ command)

| Artifact | Schema | Command | Exit carries |
|---|---|---|---|
| IR dump | `"v": 1` envelope (ARCHITECTURE §4.2, R-6) | `inspect --json` | `0`/`2` |
| Parse snapshot | `driftguard.parse.v1` | `parse --json` | `0`/`2`/`5` |
| Lineage | `driftguard.lineage.v1` | `lineage --json` | `0`/`2` |
| Drift | `driftguard.drift.v1` | `drift --json` / legacy bare | `0`/`1` |
| Scan | `driftguard.scan.v1` | `scan --json` | `0`/`1`/`5` |
| Analysis | `driftguard.analysis.v1` | `analyze --json` | `0`/`2`/`5` |
| Plan | `driftguard.plan.v1` | `refactor plan` | `0`/`1`/`2`/`5` |
| Dry-run | `driftguard.dryrun.v1` | `refactor dry-run --format json` | `0`/`2` |
| Apply | `driftguard.apply.v1` | `refactor apply --json` | `0`/`2`/`3` |
| Verify | `driftguard.verify.v1` | `refactor verify --json` | `0`/`1` |
| Audit | `driftguard.audit.v1` | `audit --json` / `session show --json` | `0`/`2` |
| Suggestions | `driftguard.suggestions.v1` | internal (LLM channel, §7.4) | — |
| Error | `driftguard.error.v1` | hard errors with `--artifacts-dir` | ≥ 2 |

Every JSON artifact that results from a persisted run/session carries `run_id` or
`session_id` so it can be traced to audit rows (§5.4 ARCHITECTURE: "Reports and --json
output include the session id").

### 8.3 Schema versioning rules

1. **Envelope first:** every machine artifact is self-describing
   (`"schema": "driftguard.<kind>.v<N>"`, or the IR `"v": 1` envelope). A consumer
   must never guess the shape from the command name alone.
2. **Additive evolution within a version:** writers may add fields; consumers **must**
   ignore unknown fields (§1.5). This is the default path — most improvements never
   bump a schema.
3. **New version** (`v2`, …) when a change is breaking *to consumers of the old
   version*: field removed or renamed, type changed, semantics changed, a previously
   optional field becomes required. Writers of `vN+1` keep a `vN` compatibility
   shim until the deprecation runway (§9.3) closes.
4. **Artifact schema ≠ DB schema.** `schema_version` (ARCHITECTURE §3.1) tracks
   SQLite migrations; artifact schemas track JSON documents. Both follow §9's
   discipline; neither implies the other.
5. **Plan files are the sharpest edge:** `apply` accepts only the plan schema of its
   own major version. A foreign or future `schema` value → `plan_error` (exit `2`),
   never a silent misread (§9.4).

---

## 9. Backward-compatibility policy (semver)

### 9.1 Versioning

Semver, applied with contract discipline: `MAJOR.MINOR.PATCH`
(`driftguard/__init__.py` is the single source; `--version` prints it; changelog per
release). Pre-`1.0` (`0.x`) follows the **same** rules — a dev tool's contracts are
contracts the moment CI systems depend on them; `0.x` only signals that the *feature
set* is still growing.

| Release | Safe (additive — ships in MINOR/PATCH) | Breaking (needs MAJOR + deprecation runway §9.3) |
|---|---|---|
| CLI | New subcommand, new flag, new `--rules`/rule IDs (REF-007, SEC-006, PLUG-*), new artifact kinds | Removing/renaming a flag or subcommand; changing a default (e.g. `--threshold` default, `--fail-on-severity` default, `--max-risk` default); changing `driftguard <root>` legacy output |
| Exit codes | Adding a new code with a documented meaning (`3`, `5` are the precedents) | Changing an existing code's meaning; reusing a reserved code |
| JSON | Adding a field; adding an enum value the schema documents as open | Removing/renaming a field; changing a field's type; changing an enum's closed set |
| Rules | New rule; tier *lowering* (SAFE → stays); severity recalibration (changelog-flagged, MINOR) | Rule ID removal; tier *raising* (a rule becoming riskier than documented) |
| Plan format | Additive fields to `driftguard.plan.v1` | Changing `item` shape, span semantics, or `schema` name without a shim |
| LLM | New model defaults, new suggestion fields | Changing the suggestion validation rules such that accepted suggestions would be rejected (or vice versa) |
| DB | `schema_version` migration (additive tables/columns) | Dropping tables/columns (never; append-only audit is frozen, ARCHITECTURE §3.1) |

### 9.2 Frozen surface (never changes within a major)

- Exit codes `0/1/2` semantics (ADR-010) and "CI gates on 1".
- Field names in shipped artifact schemas and the IR model.
- The plan file as the handoff artifact (`dry-run` output == `apply` input, ADR-005).
- The rule protocol (id/version/tier) — the plugin seam (ARCHITECTURE §2.1).
- Flag spelling (`--max-risk`, `--fail-on-severity`, `--no-persist`, …). The single
  exception is `--llm`, which is a deprecated alias of `--llm-suggestions` and is
  removed in `1.0` (R-2).

### 9.3 Deprecation lifecycle (announce, signal, runway, sunset)

1. **Announce:** changelog entry + migration note in the docs (and, for removals,
   a section in the migration guide) at the start of the runway.
2. **Signal:** the deprecated surface emits `warning: deprecation: …` on stderr and
   is marked `(deprecated)` in `--help`, for **≥ 2 minor releases** before removal.
3. **Runway:** no removal before the announced MAJOR; no silent break — a removed
   flag errors with `usage_error` (exit `2`) *naming the replacement*, not a bare
   argparse error.
4. **Sunset:** removal happens only in a MAJOR release, listed in the changelog's
   "Breaking changes" section with the exact migration path.

### 9.4 Compatibility guarantees for CI consumers

- GitHub Action/GitLab job behavior is pinned by **major version tag** (`@v1`).
  A MAJOR publishes a new tag (`@v2`); `latest`/`@v1` never silently change exit
  semantics.
- Rule/severity changes that could flip a gate are announced in the changelog
  **before** the release that ships them (changelog discipline = the compatibility
  signal).
- Foreign plan files, foreign artifact schemas, and unknown rule IDs are **hard
  errors** (`plan_error` / `usage_error`), never warnings that get consumed.
- JSON consumers ignore unknown fields; JSON writers never remove fields within a
  schema version.

---

## 10. Quick reference

**Exit codes:** `0` clean · `1` findings (gate fails) · `2` usage/input/hard-parse/state/plan errors · `3` internal error · `4` reserved · `5` resource limit.

**Error codes:** `usage_error` · `no_input` · `parse_error` · `plan_error` · `state_error` · `llm_unavailable` (all exit `2`) · `resource_limit` (exit `5`) · `internal_error` (exit `3`).

**Command surface:**

```
driftguard <root> [--db FILE] [--json|--markdown] [--no-persist]        # legacy gate == drift
driftguard parse <root> [--out FILE] [--json]
driftguard inspect <root> [--json]
driftguard lineage <root> [--json]
driftguard drift <root> [--threshold 0.75] [--json]
driftguard scan <root> [--severity medium] [--fail-on-severity high] [--json]   # alias: security-scan
driftguard analyze <root> [--rules all] [--max-risk safe] [--rules-dir DIR] [--json]
driftguard refactor <root> plan    --out plan.json [--rules all] [--max-risk safe] [--rules-dir DIR] [--allow-on-finding] [--llm-suggestions]
driftguard refactor <root> approve --plan plan.json
driftguard refactor <root> dry-run --plan plan.json [--format diff|text|markdown|json]
driftguard refactor <root> apply   --plan plan.json [--in-place|--out-dir DIR] [--no-backup] [--ci]
driftguard refactor <root> verify  [--plan plan.json] [--fail-on-severity high]
driftguard session show <id> [--json]
driftguard audit [--since N|ISO] [--session ID] [--limit N] [--json]
driftguard --version
```

**Global flags:** `--db FILE` · `--json` · `--markdown` · `--no-persist` · `--artifacts-dir DIR` · `-v/--verbose` · `-q/--quiet` · `-h/--help`.
