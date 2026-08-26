# Workflow Orchestration Design

**Project:** Security-Aware Refactoring Assistant for Data Pipelines
**Document:** `docs/ORCHESTRATION.md` — normative orchestration design
**Status:** Design v1 — implementable as specified
**PRD anchors:** §Architecture ("every operation is idempotent and recorded in an audit trail"), §Features (failure isolation & retry, metrics hooks, LLM via Ollama), §Security (cleaned subprocess env, sandboxed paths, redacted logs), §Technology Stack (Python 3.11+ stdlib, SQLite), §Integrations (GitHub Actions, GitLab CI).

---

## 1. Purpose & Scope

This document specifies the orchestration layer of the refactoring assistant: the state
machine that drives a refactoring **run** from invocation to terminal state, the
determinism contract, the crash-recovery protocol, the concurrency model, the failure
taxonomy, the audit/event stream, the plugin seams, and the configuration environment.

The engine's core guarantee, in one sentence:

> **Given identical input (workspace files + refactoring intents + engine/plugin versions),
> an offline (LLM-disabled) run produces byte-identical artifacts, diffs, and applied
> changes — regardless of machine, locale, timezone, clock, process scheduling, or how many
> times the run was interrupted and resumed.**

Everything in this document is normative. Tables are specifications, not suggestions.

### 1.1 Scope boundaries

- **In scope:** run/state orchestration, determinism, resume, parallelism, failure handling, audit, plugins, configuration.
- **Out of scope (other docs):** parser grammar details, security rule semantics, diff rendering, CLI help text, packaging.
- **Not in scope for Phase 1:** web UI, REST server, DuckDB analytics (only an optional export, §9.4).

---

## 2. Design Principles

| # | Principle | Enforced by |
|---|-----------|-------------|
| P1 | Every operation is idempotent | Stage contracts (§4.4), CAS apply (§6.4), content-addressed artifacts (§6.1) |
| P2 | Every operation is recorded | Append-only `events` table, exactly-once event ids (§9) |
| P3 | Deterministic by default | Canonicalization rules (§5.1), strict vs enriched mode (§5.4) |
| P4 | Crash-safe at any point | SQLite WAL journal + recovery protocol (§6) |
| P5 | Failure is data, not noise | Failure taxonomy, dead letters, partial results always preserved (§8) |
| P6 | Least privilege | Stages run as isolated subprocesses with cleaned env + sandboxed paths (§11.4) |
| P7 | Observable | Every state transition emits a structured event (§9) |
| P8 | Extensible without breaking determinism | Versioned plugin contracts, purity requirement (§10) |

---

## 3. Run Model & Input Snapshot

A **run** is one invocation of the tool against one pipeline workspace with one set of
refactoring intents. A run has a `run_id`, a single **input snapshot**, and drives one
execution of the stage DAG (§4.3).

### 3.1 Input snapshot

At run creation, the engine computes and freezes the input snapshot — everything that the
run's output is a function of:

```json
{
  "workspace_root": "/home/ci/app/pipelines",
  "fingerprint": "sha256:1f2e…",
  "files": {
    "models/marts/orders.sql": "sha256:9f8e…",
    "dbt_project.yml": "sha256:00ab…"
  },
  "intents": {"type": "sha256:71dd…", "spec": "…/intents.json"},
  "engine_version": "0.1.0",
  "plugin_versions": {"dbt-parser": "1.2.0", "oarl-security-rules": "0.4.1"},
  "env_facts": {
    "platform": "linux",
    "python": "3.11.9",
    "locale": "C",
    "tz": "UTC",
    "llm": null
  },
  "mode": {"determinism": "strict", "llm": false}
}
```

Rules:

- `fingerprint` = `sha256` over the canonical JSON (§5.1) of `files` + `intents` + `engine_version` + `plugin_versions`.
- `files` is the recursive listing of pipeline definition files, each keyed by **relative
  path** and hashed by content. Order is lexicographic by path.
- `intents` are the refactoring directives (targets, allowed transforms, exclusions) from
  CLI flags or an intents file; hashed the same way.
- `env_facts` are recorded for **audit only**. They never enter any output, hash, or artifact
  content (see §5.1 rule R3). The `locale`/`tz` facts exist so a determinism violation can
  be diagnosed, not so they can vary behavior.
- `mode` records the determinism mode and LLM state for this run (§5.4).

The snapshot is stored at `.refactor/jobs/<run_id>/input_snapshot.json` and mirrored in the
`runs` table. **The snapshot is immutable for the lifetime of the run.** Any change to the
workspace mid-run that would alter the snapshot (detected by re-hashing at stage boundaries
in strict mode) aborts the run with a `workspace_changed` event — never a silent divergence.

---

## 4. Orchestration Model — Formal State Machines

The engine is a two-level state machine:

1. **Run-level FSM** — lifecycle of the whole run.
2. **Stage-level FSM** — lifecycle of each stage instance (one per DAG node).

Plus a **dispatch loop** that computes eligible transitions deterministically.

### 4.1 Run-level FSM

`M_run = (S, E, T, s₀, F)` with:

- `S = {CREATED, READY, RUNNING, SUSPENDED, SUCCEEDED, FAILED, ABORTED}`
- `E = {snapshot_ok, snapshot_failed, dispatch, stage_fatal, all_succeeded, interrupt, resume, abort, budget_exhausted}`
- `s₀ = CREATED`
- `F = {SUCCEEDED, FAILED, ABORTED}` (terminal — no outgoing transitions)

| From | Event | To | Guard / Action |
|---|---|---|---|
| CREATED | `snapshot_ok` | READY | Snapshot computed, validated, persisted; `run_ready` event |
| CREATED | `snapshot_failed` | FAILED | Input unreadable / unsupported format; error recorded |
| READY | `dispatch` | RUNNING | Stage graph instantiated; `parse` → PENDING; `run_started` event |
| RUNNING | `stage_fatal` | FAILED | A stage reached terminal FAILED or BLOCKED; run failure recorded |
| RUNNING | `all_succeeded` | SUCCEEDED | All stage instances terminal-success (SUCCEEDED/SKIPPED); `run_succeeded` |
| RUNNING | `interrupt` | SUSPENDED | SIGINT / crash / timeout; journal flushed atomically (§6) |
| RUNNING | `budget_exhausted` | ABORTED | Wall-clock/artifact budget exceeded; `run_aborted` |
| SUSPENDED | `resume` | RUNNING | Recovery protocol (§6.3) re-enables eligible stages; `run_resumed` |
| RUNNING | `abort` | ABORTED | Explicit `oarl abort` / fatal config error mid-run |
| SUSPENDED | `abort` | ABORTED | Explicit abort of a suspended run |

Guards:

- No transition out of any state in `F`.
- `interrupt` is the only event that may fire mid-stage; it leaves the stage instance in
  `INTERRUPTED` (crash) or `RUNNING` (clean interrupt that still needs the stage re-checked
  on resume — see §6.3).
- `resume` is only legal from `SUSPENDED` and only for the same `run_id`.

### 4.2 Stage-level FSM

`M_stage = (S, E, T, s₀, F)` with:

- `S = {PENDING, RUNNING, SUCCEEDED, FAILED, INTERRUPTED, SKIPPED, BLOCKED}`
- `E = {start, complete, fail_retryable, fail_fatal, retry_ok, interrupt, skip, block}`
- `s₀ = PENDING`
- Terminal: `{SUCCEEDED, SKIPPED, BLOCKED, FAILED (when attempts == max_attempts or non-retryable)}`
- Non-terminal: `{PENDING, RUNNING, INTERRUPTED, FAILED (retryable)}`

Each stage instance carries `attempt` (1-based), `max_attempts`, `retryable` (from the
failure taxonomy §8.1), `input_hash`, `output_hash`, and artifact refs.

| From | Event | To | Guard / Action |
|---|---|---|---|
| PENDING | `start` | RUNNING | All parents SUCCEEDED; attempt = 1; `stage_started` |
| RUNNING | `complete` | SUCCEEDED | Output persisted to content-addressed artifact; `input_hash`/`output_hash` recorded; determinism self-check passed (§5.3); `stage_completed` |
| RUNNING | `fail_retryable` | FAILED | Error class ∈ retryable (§8.1) AND attempt < max; backoff computed (§8.2); `stage_failed` + `stage_retry_scheduled` |
| FAILED (retryable) | `retry_ok` | RUNNING | Backoff elapsed; attempt += 1; `stage_retrying` |
| RUNNING | `fail_fatal` | FAILED | attempt == max OR non-retryable class; terminal; run receives `stage_fatal` |
| RUNNING | `interrupt` | INTERRUPTED | Process crash / SIGINT; no further writes (atomic journal covers this) |
| INTERRUPTED | `retry_ok` | RUNNING | Resume path: output artifact verified missing/stale (§6.3); attempt += 1 |
| PENDING | `skip` | SKIPPED | Parent non-fatally failed / mode excludes stage (e.g., dry-run mode skips `apply`) |
| PENDING | `block` | BLOCKED | Parent terminally failed; run will receive `stage_fatal` from the BLOCKED transition |

Dispatch rules (normative):

1. A stage in `PENDING` becomes eligible when **all** parent stages are terminal-success.
2. `FAILED` (retryable) instances are re-dispatched after their backoff deadline.
3. `INTERRUPTED` instances are re-dispatched immediately on resume, subject to the
   recovery protocol (§6.3).
4. `SKIPPED` counts as success for downstream eligibility; `BLOCKED` never happens to a
   stage with successful parents (it is a propagation state, §8.5).

### 4.3 Stage DAG & contracts

```
                        ┌─ lineage ─────────┐
                        ├─ schema_drift ────┤
 parse ──► analyze ──► plan ──► dry_run ──► apply ──► verify
                        ├─ security ────────┤
                        ├─ complexity ──────┤
                        └─ quality ─────────┘
```

- `analyze` is a **fan-out/fan-in** stage: it executes independent analyzers as parallel
  tasks and merges their outputs (§7). The fan-in merge is deterministic (keyed merge, sorted).
- `plan` and `apply` are strictly sequential — `plan` is the single decision point; `apply`
  mutates the shared workspace and must run in deterministic order (§7.1).
- Edges are `(parent → child)`: `parse → analyze → plan → dry_run → apply → verify`.

Stage contracts (what each stage receives, produces, and must not do):

| Stage | Inputs | Outputs (artifact) | Mutates workspace? | Parallel unit |
|---|---|---|---|---|
| `parse` | snapshot `files` | `parsed_model.json` (per-file nodes) | No | file |
| `analyze` | `parsed_model.json` | `analysis.json` (merged, per-analyzer artifacts) | No | analyzer |
| `plan` | `analysis.json` + `intents` | `plan.json` (ordered `changes[]`) | No | — (single) |
| `dry_run` | `plan.json` | `diffs/<change_id>.patch` + `dry_run_report.json` | No (read-only, renders previews in scratch) | change |
| `apply` | `plan.json` + restore point | `apply_manifest.json` | **Yes** | — (single, ordered) |
| `verify` | `plan.json` + post-apply workspace | `verify_manifest.json` | No | file |

Change identity: `change_id = c<index>` where `<index>` is the position in the plan's
deterministically ordered `changes[]`. Change ids are stable across resume (plan is
re-created byte-identically from the same inputs) — this is what makes per-change
dead-lettering and per-change apply bookkeeping possible.

### 4.4 Idempotency contract per stage (P1)

A stage is idempotent if **re-executing it with the same `input_hash` is a no-op** — either
it reproduces byte-identical outputs, or it provably skips already-done work:

| Stage | Re-run behavior | Mechanism |
|---|---|---|
| `parse` | overwrite identical artifact bytes | Pure function of snapshot files; content-addressed artifact |
| `analyze` | same | Pure function of parsed model; per-analyzer content-addressed artifacts |
| `plan` | same | Pure function of analysis + intents; change ids deterministic |
| `dry_run` | same | Pure function of plan |
| `apply` | skip already-applied, conflict on drift | CAS manifest (§6.4) — never blindly re-applies |
| `verify` | same | Pure hash comparison of workspace vs plan expectations |

The engine **never re-runs a `SUCCEEDED` stage**, on resume or otherwise; it reuses the
recorded artifact and `output_hash` (skip-via-hashing, §6.3).

---

## 5. Determinism Strategy

### 5.1 Canonicalization rules (normative)

The engine is built under these rules; every module that produces output obeys them.

| # | Rule | Concrete practice |
|---|---|---|
| R1 | **Stable ordering everywhere** | All iteration over sets/dicts uses sorted order. All DB queries that feed output have `ORDER BY` on every column that can affect content. Filesystem iteration is sorted by relative path. Fan-in merges sort by key. Never rely on `dict` insertion order from unordered sources. |
| R2 | **Canonical serialization** | JSON: `sort_keys=True`, `ensure_ascii=False`, UTF-8, separators `(",", ":")`, no trailing newline variance — one canonical `to_json()` helper used everywhere. Text files written with `newline="\n"` and `encoding="utf-8"` (never `os.linesep`). |
| R3 | **No wall-clock dependence in content** | Timestamps may appear in **metadata** (events, journal `updated_at`) but never in artifacts, diffs, plans, or written code. A run's `started_at` is audit-only. The only clock the engine exposes to pipeline templates is a frozen sentinel: `REFACTOR_FROZEN_CLOCK = "1970-01-01T00:00:00Z"` used in dry-run previews; nodes using other clock functions are flagged `non_deterministic_template` and dead-lettered in strict mode (§8.4). |
| R4 | **Timezone fixed** | All metadata timestamps are UTC ISO-8601 with explicit `Z`. No local-time conversion anywhere. `TZ=UTC` is set in worker environments. |
| R5 | **Locale independence** | `LC_ALL=C` in worker environments. Engine bans `locale` module, locale-aware formatting, and `str(float)` — floating point and numbers are formatted with explicit, locale-independent specifiers (e.g., `f"{x:.2f}"`); sorting uses code-point order (Python's default `str.sort`), never `locale.strxfrm`. |
| R6 | **Seeded randomness** | Any randomized behavior (retry jitter, sampling) uses `random.Random()` seeded from the run's `fingerprint` **for content-affecting randomness** — which should be none; jitter is scheduling metadata and may use the system RNG (it never enters content). If a future feature must sample (e.g., rule-pack demo selection), the seed derives from the input hash. |
| R7 | **Content-addressed outputs** | Every stage artifact is written to `artifacts/<stage>/<output_hash>.json`; the output hash is `sha256` of the canonical bytes. Identical input ⇒ identical path ⇒ overwrite is byte-identical (no-op). |
| R8 | **Version-pinned inputs** | `engine_version` + `plugin_versions` are part of every stage `input_hash` computation. Bumping the engine or a plugin invalidates caches and changes hashes **explicitly and visibly** (cache invalidation is never implicit). |
| R9 | **Hash-input discipline** | Stage `input_hash` = `sha256(canonical_json(stage_input_payload))` where the payload references **content hashes, not paths or mtimes**. For `apply`, the input hash is computed over `(plan.json, expected old file hashes)` — not over the live workspace (§6.4). |
| R10 | **Worker env pinning** | `PYTHONHASHSEED=0`, `TZ=UTC`, `LC_ALL=C` in every worker subprocess (defensive; the engine never relies on hash order). |

### 5.2 Determinism self-check (runtime guard)

The engine keeps a per-run map `input_hash → output_hash` for every completed stage
(also persisted in `stage_instances`). On stage completion:

- If this `input_hash` was already recorded for this stage **with a different**
  `output_hash` → emit `determinism_violation` event with both hashes.
- In `determinism_mode = strict`, a violation **fails the run** (exit code 4, §11.5).
- In `enriched` mode, violations are recorded and reported but only advisory.

Cross-run: the content-addressed artifact cache (§6.1) is keyed by `input_hash`; a cache hit
returns the artifact without recomputation, so cross-run byte-identity is structurally
enforced for pure stages.

### 5.3 What identical input means

Two runs have "identical input" iff: identical snapshot `fingerprint` **and** identical
`engine_version` **and** identical `plugin_versions` **and** identical `mode.determinism`.
The mode is part of the run, so an enriched run is never byte-comparable to a strict run of
the same workspace.

### 5.4 LLM enrichment boundary (documented non-determinism)

Ollama is **optional** and strictly **advisory**. The deterministic pipeline never depends
on LLM output; enabling the LLM changes `mode` and therefore the run's identity.

Rules (normative):

1. **Advisory only.** LLM output is stored under `llm/` artifacts, validated against a JSON
   schema, and surfaced in reports/previews flagged `[LLM advisory]`. It can never:
   - alter `parsed_model.json`, `analysis.json` (deterministic sections), `plan.json`
     (the plan's security findings and diffs come from deterministic rules only),
   - gate or veto `apply` (a change may be *flagged* by an LLM suggestion but only
     deterministic gates decide application),
   - participate in any stage `input_hash`.
2. **Tagged artifacts.** Every artifact touched by enrichment carries
   `"llm_enriched": true` and an `llm_hash` (hash of the exact LLM responses used), so any
   non-determinism is attributable to a recorded input.
3. **Schema-validated.** LLM suggestions that fail validation are dropped with a
   `llm_suggestion_rejected` event (invalid JSON, wrong types, unexpected keys) — injected
   or malformed content cannot enter the pipeline.
4. **Hardened calls.** Ollama calls run with `temperature=0` where supported, a hard
   timeout, a circuit breaker (§8.3), and prompt + output hashes in the audit trail.
   Network is allowlisted to the Ollama endpoint only; all other workers are networkless.
5. **Mode semantics.** `determinism_mode = strict` (default, and the only mode allowed in
   `--ci`): LLM disabled. `enriched`: LLM enabled; guarantee is "deterministic pipeline +
   recorded advisory deltas" — **not** byte-identical output across runs.
6. **Opt-out at any layer.** `--no-llm` CLI flag overrides config; a failing LLM degrades
   to strict behavior (suggestions simply absent), never to pipeline failure.

---

## 6. Crash-Resume & Idempotency

### 6.1 Persistence of in-progress state

Two cooperating stores, both inside the workspace's self-hosted state directory (§11.3):

**A. SQLite database `.refactor/refactor.db` — source of truth.**
WAL mode (`PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`). Tables:

| Table | Purpose | Key |
|---|---|---|
| `runs` | run metadata, snapshot hash, mode, status | `run_id TEXT PK` |
| `stage_instances` | one row per (run, stage, attempt): status, timestamps, `input_hash`, `output_hash`, artifact ref, retry deadline, error | `(run_id, stage, attempt) PK` |
| `events` | append-only audit stream (§9) | `event_id TEXT PK` (dedup) |
| `artifacts` | content-addressed artifact index: hash → path, stage, engine version | `hash TEXT PK` |
| `dead_letters` | per-unit failures (§8.4) | `id INTEGER PK AUTOINCREMENT` |
| `meta` | schema version, migration history | `key TEXT PK` |

Only the **orchestrator process** writes to the DB (single-writer). Workers receive inputs
and return results; they never open the DB (§7.3).

**B. Per-run journal `.refactor/jobs/<run_id>/journal.json` — human-readable mirror.**
Written atomically (write to `journal.json.tmp`, `os.replace`) after every state transition,
in the same transaction discipline as the DB write. It is a **mirror**, never authoritative;
if DB and journal disagree, the DB wins and the journal is rewritten on next transition.

### 6.2 Journal format

```json
{
  "schema_version": 1,
  "run_id": "ref-20260818T061200Z-3f9a2c",
  "engine_version": "0.1.0",
  "mode": {"determinism": "strict", "llm": false},
  "status": "SUSPENDED",
  "input_snapshot": {"fingerprint": "sha256:1f2e…", "file_count": 41},
  "stages": {
    "parse":   {"status": "SUCCEEDED",   "attempts": 1, "input_hash": "sha256:aa…", "output_hash": "sha256:bb…", "artifact": "artifacts/parse/bb….json"},
    "analyze": {"status": "SUCCEEDED",   "attempts": 1, "input_hash": "sha256:cc…", "output_hash": "sha256:dd…", "artifact": "artifacts/analyze/dd….json"},
    "plan":    {"status": "SUCCEEDED",   "attempts": 1, "input_hash": "sha256:ee…", "output_hash": "sha256:ff…", "artifact": "artifacts/plan/ff….json"},
    "dry_run": {"status": "SUCCEEDED",   "attempts": 1, "input_hash": "sha256:11…", "output_hash": "sha256:22…", "artifact": "artifacts/dry_run/22….json"},
    "apply":   {"status": "INTERRUPTED", "attempts": 1, "input_hash": "sha256:33…", "output_hash": null,        "artifact": null},
    "verify":  {"status": "PENDING",     "attempts": 0, "input_hash": null,        "output_hash": null,        "artifact": null}
  },
  "restore_point": "restore_points/apply-1",
  "next_eligible": [],
  "updated_at": "2026-08-18T06:12:41Z"
}
```

`updated_at` is metadata (R3). `attempts` counts started attempts; `output_hash` is null
until `SUCCEEDED`.

### 6.3 Recovery protocol

Triggered by `oarl resume <run_id>` (or `oarl run` detecting a `SUSPENDED`/orphaned run —
an engine process died while `stage_instances` had `RUNNING` rows with no live PID).

Normative steps:

1. **Load** run from DB. If status ∈ `{SUCCEEDED, FAILED, ABORTED}` → refuse unless
   `--force` (documented as "replay" mode: re-runs everything from the existing snapshot,
   which is safe because all stages are idempotent).
2. **Reconcile the snapshot**: re-hash the workspace and compare to the frozen snapshot.
   Mismatch → `workspace_changed` event; abort (strict) or re-snapshot with user consent
   (`--accept-changes`).
3. **Classify every stage instance** (in DAG topo order):
   - `SUCCEEDED` → **SKIP** (never re-run; artifact + `output_hash` are authoritative —
     this is skip-via-hashing).
   - `RUNNING`/`INTERRUPTED` → check: if artifact exists at `artifact` path **and**
     `sha256(artifact bytes) == output_hash` → **SKIP** (crashed after persist); else
     **RE-RUN** from the stage's recorded `input_hash` inputs (its input payload is
     reconstructable from the snapshot + parent artifacts).
   - `FAILED` retryable, attempts < max → **RETRY** (backoff from §8.2).
   - `FAILED` terminal → run goes `FAILED` (recovery cannot resurrect a fatal stage;
     `--force` replay is the escape hatch).
   - `PENDING` → becomes eligible iff all parents terminal-success, else `BLOCKED`.
4. **Apply special handling for `apply`**: RE-RUN executes the CAS protocol (§6.4) against
   the live workspace — files already written (match new hash) are skipped, unapplied
   (match old hash) are written, drift is a conflict. This is what makes a crash
   mid-`apply` recoverable without rollback.
5. **Dispatch loop** until no actionable stages; then `all_succeeded` → SUCCEEDED.

Recovery is deterministic: the resume outcome (final artifact set) is identical to what an
uninterrupted run would have produced, because every stage is a pure function of its inputs
and `apply` is CAS-guarded.

### 6.4 Apply idempotency: CAS manifest + restore points

`apply` is the only stage that mutates the workspace; it gets its own protocol.

1. **Precondition check (CAS).** For each change in deterministic order:
   `old_hash` (expected) vs live file hash:
   - live == `old_hash` → write new bytes (temp file + `os.replace`);
   - live == `new_hash` → skip (already applied — resume case);
   - otherwise → **CONFLICT**: apply `on_conflict` policy (`abort` (default) | `skip_change` | `manual`). `abort` fails the stage (safe); `skip_change` dead-letters the change and continues; `manual` halts for operator decision.
2. **Restore point.** Before the first write of an `apply` attempt, the engine snapshots the
   original bytes of every file to be touched into
   `.refactor/restore_points/<apply-attempt>/files/` + a `manifest.json` (path → old hash).
   Rollback = restore from manifest (atomic per file, in reverse order).
3. **Atomic per file.** Each write is temp-file + `os.replace` (no torn writes on crash).
4. **Verify after apply.** `verify` re-hashes the workspace against the plan's
   `new_hash` map. On failure: `verify_failed` event; `rollback.on_failure` policy
   (`auto` (default) | `manual`); on `auto`, engine restores the restore point, emits
   `rollback_performed`, and the run ends `FAILED` with a precise report (workspace is
   back to the pre-apply state — safe to re-run).

---

## 7. Concurrency & Parallelism

### 7.1 Parallelism map

| Stage | Parallel unit | Safe? | Why / mechanism |
|---|---|---|---|
| `parse` | pipeline file | ✅ | Read-only; per-file outputs merged deterministically |
| `analyze` | analyzer (lineage, schema_drift, security, complexity, quality) | ✅ | Fan-out/fan-in; analyzers are pure, independent, no shared state |
| `plan` | — | ❌ | Single deterministic decision point |
| `dry_run` | change | ✅ | Per-change diff computation; read-only |
| `apply` | — | ❌ | Shared mutable workspace; ordering must be deterministic (plan order) |
| `verify` | file | ✅ | Read-only hashing |

Fan-out width cap: `parallelism.max_workers` bounds the number of concurrent tasks; a stage
with more units than workers processes them in deterministic batches (sorted unit order).

### 7.2 Process model (stdlib only)

- `concurrent.futures.ProcessPoolExecutor` for `parse`, `analyze` (analyzers), `dry_run`,
  `verify` units. Processes give **crash isolation** (a segfaulting parser kills its worker,
  not the engine), no GIL contention, and enforce the subprocess/sandbox story (§11.4).
- `ThreadPoolExecutor` only for **LLM enrichment I/O** (Ollama HTTP) — bounded, advisory,
  never in the deterministic path.
- No threads for pipeline work. The orchestrator process is the only DB writer (§7.3).
- Default workers: `min(os.cpu_count(), config.parallelism.max_workers)` with
  `max_workers = 4` default. **`--jobs 1` is a supported determinism test mode** (still
  byte-identical to `--jobs 4` — parallelism must never change output, only wall time).

### 7.3 Shared-state isolation (normative)

1. Workers receive **immutable input payloads** (canonical JSON or artifact paths) and
   return results via futures. **No shared mutable state, ever.**
2. Workers **never open the DB**. All persistence happens in the orchestrator's event loop.
3. Worker outputs are written to content-addressed paths; two workers can never collide
   (different content ⇒ different hash ⇒ different path; same content ⇒ same bytes).
4. Fan-in merge is a **keyed merge with sorted keys** — result is order-independent.
5. Deterministic task submission: units are always submitted in sorted order, so even
   observable side effects (logs, artifact writes) appear in a stable sequence.

### 7.4 Resource limits

| Limit | Default | Enforcement |
|---|---|---|
| `parallelism.max_workers` | 4 | Worker pool size |
| `stage.timeout_s` | 300 | Per stage instance; exceeded → `fail_retryable` (`TimeoutError`) |
| `parse.file_timeout_s` | 60 | Per-file parse deadline |
| `limits.max_artifact_bytes` | 50 MB | Artifact write rejected → stage failed |
| `run.timeout_s` | 3600 | Run wall budget → `budget_exhausted` → ABORTED (safe: abort ≠ partial content) |
| `limits.scan_max_files` | 10 000 | Snapshot size guard |

---

## 8. Failure Taxonomy & Handling

### 8.1 Per-stage failure modes

| Stage | Failure mode | Class | Recovery |
|---|---|---|---|
| `parse` | file unparsable | retryable (2×) → per-file dead letter | File excluded, `excluded_inputs` recorded in model; downstream continues with a `partial` flag. All files failed → stage fatal |
| `parse` | file IO error | retryable (3×) | Backoff retry → dead letter |
| `analyze` | analyzer crash | retryable (3×) | Backoff retry; persistent failure → analyzer dead letter; merged analysis marked `incomplete: true`; plan degrades to conservative/no-op for affected areas with a `degraded` warning |
| `analyze` | analyzer exception | retryable (3×) | Same as crash (exception payload in dead letter) |
| `plan` | internal error | retryable (3×) → **fatal** | No safe fallback (plan is the decision); run FAILED |
| `plan` | no feasible changes | success (empty plan) | Run SUCCEEDED with empty plan; `plan_empty` event (not a failure) |
| `dry_run` | per-change error | retryable (2×) → dead letter | Change excluded from application with reason; others proceed |
| `apply` | CAS conflict | **non-retryable** | `apply.on_conflict` policy: `abort` \| `skip_change` \| `manual` (default `abort`) |
| `apply` | transient IO error | retryable (2×) | Backoff; CAS still enforced on retry (no blind re-write) |
| `verify` | file hash mismatch | retryable (1×) → fatal | Re-verify; persistent mismatch → `verify_failed` → rollback policy (§6.4) |
| `verify` | missing file | fatal | Rollback; run FAILED |
| any | timeout | retryable | Backoff; circuit breaker counts it |
| any | workspace changed mid-run | **fatal** (strict) | `workspace_changed`; abort (never silently diverge, §3.1) |
| `llm` | Ollama timeout/refusal | circuit-broken | Suggestion absent; pipeline unaffected (§5.4.6) |

### 8.2 Retry policy

- **Exponential backoff with full jitter** (scheduling metadata — never part of content, R6):
  `delay = uniform(0, min(retry.max_delay_s, retry.base_delay_s * 2^(attempt-1)))`
- Defaults: `retry.base_delay_s = 1`, `retry.max_delay_s = 30`, `retry.max_attempts = 3`
  (overridden per stage class by §8.1).
- Retry deadlines are persisted in `stage_instances.retry_after`; the dispatch loop
  re-dispatches only after the deadline — a crash between backoff and retry is recovered by
  the same mechanism (§6.3).
- Retries emit `stage_retry_scheduled` / `stage_retrying` events; the attempt counter is
  part of the stage instance identity `(run_id, stage, attempt)`.

### 8.3 Circuit breaker

Applied per stage-class (analyze analyzers, apply writes, llm calls):

- `CLOSED` (normal) → count failures in rolling window (`circuit.window = 5`); ≥
  `circuit.max_failures = 3` → **OPEN**.
- `OPEN` → calls are not attempted; retryable failures are immediately scheduled with
  backoff; after `circuit.cooldown_s = 60` → **HALF-OPEN**.
- `HALF-OPEN` → one test call; success → `CLOSED`; failure → `OPEN` again.
- Transitions emit `circuit_opened` / `circuit_closed` / `circuit_half_open` events with the
  stage class. In strict mode a breaker that opens **fails the stage** rather than silently
  degrading (degradation is an enriched-mode behavior, recorded in the report).

### 8.4 Dead-letter handling

Every unit that terminally fails its retries goes to the `dead_letters` table and the
`dead_letters.json` artifact:

```json
{
  "run_id": "ref-…",
  "stage": "parse",
  "attempt": 3,
  "entity": "models/marts/broken.sql",
  "error": {"type": "ParseError", "message": "unexpected token 'SELCT' at line 12"},
  "payload_hash": "sha256:…",
  "created": "2026-08-18T06:12:41Z"
}
```

- `entity` is the unit identity: file path, analyzer id, `change_id`, or plugin id.
- Downstream stages receive explicit exclusion lists (`excluded_inputs` for parse,
  `incomplete` + affected areas for analyze, excluded `change_id`s for dry_run) — **never
  silently absent data** (a leading cause of silent failures).
- `--strict` promotes any dead letter to run failure. Default mode: continue with
  degraded-but-explicit behavior; the final report lists every dead letter with its reason.

### 8.5 Partial results preservation

- Every completed unit's artifact is persisted **immediately** (content-addressed) — a
  crash never discards finished work.
- A `FAILED` run still produces the full report: completed diffs, applied changes (if any),
  dead letters, and the rollback status. `oarl report` always works on whatever exists.
- `BLOCKED` is the propagation state: when a parent is terminally failed, downstream
  `PENDING` stages transition to `BLOCKED` (recorded), the run goes `FAILED`, and nothing is
  silently skipped.

---

## 9. Event / Audit Stream

### 9.1 Event schema (normative)

Every event is one row in `events` and one line in `.refactor/jobs/<run_id>/events.jsonl`:

```json
{
  "event_id": "ref-20260818T061200Z-3f9a2c:apply:1:7",
  "run_id": "ref-20260818T061200Z-3f9a2c",
  "ts": "2026-08-18T06:12:41Z",
  "seq": 27,
  "stage": "apply",
  "stage_instance_id": "apply@1",
  "kind": "change_applied",
  "status": "succeeded",
  "attempt": 1,
  "input_hash": "sha256:33…",
  "output_hash": "sha256:44…",
  "artifact": "artifacts/apply/44….json",
  "duration_ms": 412,
  "error": null,
  "extra": {"change_id": "c12", "path": "models/marts/orders.sql", "old_hash": "sha256:9f…", "new_hash": "sha256:8e…"}
}
```

- `event_id` is **deterministic**: `f"{run_id}:{stage}:{attempt}:{seq}"` (run-level events
  use `stage = "run"`, `attempt = 1`). Combined with `INSERT OR IGNORE`, this gives
  **exactly-once audit insertion** even under at-least-once execution — a crashed engine
  that replays a transition re-emits the same `event_id` and is deduplicated.
- `ts` is UTC (R4), metadata only.
- `error` carries `{type, message}` for failure kinds; `extra` is a free-form JSON object
  scoped by `kind` (schema-validated per kind).

### 9.2 Event kinds

| Kind | Emitted on | Consumers |
|---|---|---|
| `run_created`, `run_ready`, `run_started`, `run_suspended`, `run_resumed`, `run_succeeded`, `run_failed`, `run_aborted` | Run FSM transitions | audit, metrics |
| `stage_started`, `stage_completed`, `stage_failed`, `stage_retry_scheduled`, `stage_retrying`, `stage_skipped`, `stage_blocked`, `stage_restored` | Stage FSM transitions | audit, metrics, trace |
| `file_parsed`, `parse_failed` | Per-file parse units | audit, report |
| `analyzer_started`, `analyzer_completed`, `analyzer_failed` | analyze fan-out units | metrics, report |
| `security_finding` | Security analyzer output | audit, report, CI gate |
| `change_proposed`, `change_rejected_deadletter`, `change_applied`, `change_conflict`, `change_skipped` | plan / dry_run / apply units | audit, report |
| `verify_ok`, `verify_failed` | verify units | audit, CI gate |
| `rollback_performed`, `rollback_skipped` | rollback policy execution | audit, report |
| `approval_requested`, `approval_granted`, `approval_denied` | HITL gate between dry_run and apply | audit, metrics |
| `llm_call`, `llm_suggestion_rejected` | Enrichment only | audit, metrics |
| `determinism_violation` | Self-check (§5.2) | alert, CI gate |
| `circuit_opened`, `circuit_closed`, `circuit_half_open` | Circuit breakers (§8.3) | metrics, alert |
| `cache_hit`, `cache_miss` | Artifact cache (§6.1) | metrics |
| `plugin_loaded`, `plugin_rejected` | Plugin loading (§10) | audit |
| `workspace_changed` | Snapshot reconciliation (§6.3) | alert, CI gate |
| `plan_empty` | plan with zero changes | report |

### 9.3 Emission points (normative)

Events are emitted **synchronously with the transition they describe**, in the same
transaction: the `stage_instances` update and the `events` insert commit atomically. If the
process dies between the state write and the event write, the recovery protocol
reconstructs and emits the missing events on resume (deduplicated by `event_id`).

### 9.4 Consumers

| Consumer | Backing | Behavior |
|---|---|---|
| Audit table | `events` in refactor.db | Append-only; never mutated (rollbacks are *events*, not deletions); `audit.retention_days` (default 90) purges with `oarl prune`, never implicitly |
| JSONL stream | `events.jsonl` per run | Same rows, line-delimited, grep-able; written after each commit |
| Metrics | Derived, read-only | Per-stage p50/p95 latency, failure rate, retry rate, cache hit rate, determinism violations, LLM call count/cost; `oarl metrics --run <id>`; exported to `metrics.json`; optional `oarl export-events --duckdb <file>` for analytics (DuckDB is optional, per PRD) |
| Human logs | `logs/refactor.log` | Redacted (PRD §Security: no secrets, keys scrubbed by the same redactor used in §11.4) |
| CI gate | Exit codes + `security_finding`/`verify_failed`/`determinism_violation` | `--ci` mode: strict, non-interactive (§11.5) |

---

## 10. Extensibility

Two plugin seams, both stdlib `importlib.metadata.entry_points`, both **determinism-pinned**.

### 10.1 Pipeline parser plugins

- Entry point group: `refactor.pipeline_parsers`. Example: a `dbt` parser, an `airflow`
  parser, a `spark` parser — each registers `{name, version, contract_version,
  file_extensions, parse(path, ctx) -> ParsedPipeline}`.
- `contract_version` declares which `ParsedPipeline` model the plugin emits (e.g.
  `"parsed-model@1"`). The engine supports a set of contracts; a plugin declaring an
  unsupported contract is **rejected** with a `plugin_rejected` event (never silently
  mis-parsed).
- Parse dispatch: a file's extension → plugin; unknown extension → dead letter
  (`unsupported_extension`) with the file excluded from the model.

### 10.2 Security rule packs

- Entry point group: `refactor.security_rules`. Each pack: `{id, version, contract_version,
  rules: list[Rule]}`; `Rule = {id, severity, description, match(parsed_node, ctx) -> list[Finding]}`.
- **Purity requirement (normative):** rules are pure functions of their inputs — no IO, no
  wall clock, no randomness, no network. Violating this breaks determinism, so rule packs
  run in a worker subprocess with network blocked and a cleared environment (§11.4); the
  engine additionally runs the self-check (§5.2) over analyzer outputs, so a non-pure rule
  is caught as a `determinism_violation` rather than trusted.
- Findings are validated against the `Finding` schema (node ref, rule id, severity,
  message, evidence snippet + line/col) before entering `analysis.json` — malformed or
  injected rule output cannot propagate (prompt-injection discipline applied to plugins).

### 10.3 Versioned contracts & determinism

- `plugin_versions` are recorded in the run snapshot and folded into every stage
  `input_hash` (R8). Upgrading a plugin changes hashes → caches invalidate → outputs change
  **visibly and traceably**.
- Plugin loading is deterministic: plugins load in sorted order by `(group, name)`; a
  `plugin_loaded`/`plugin_rejected` event is emitted per plugin; load failures reject the
  plugin, never the run (unless the plugin was the only parser for the input's extension →
  that's a parse-stage failure).
- The engine ships **no built-in rules in the core loop** beyond the mandatory
  `builtin` pack — rule behavior is always attributable to a named, versioned plugin.

---

## 11. Configuration & Environment

### 11.1 Precedence (normative)

```
defaults < config file < CLI flags < environment variables
```

- **Defaults**: `oarl/config/defaults.toml` (compiled in; the single source of defaults).
- **Config file**: `.refactor.toml` in the workspace, or `--config <path>` (TOML via stdlib
  `tomllib`, Python 3.11+).
- **CLI flags**: explicit per-invocation overrides.
- **Env vars**: `REFACTOR_`-prefixed, dot-path mapping (`REFACTOR_PARALLELISM_MAX_WORKERS=2`
  ⇒ `parallelism.max_workers`). Env vars intentionally win over CLI flags because the CI
  boundary (GitHub Actions / GitLab CI) is where operators must have final control — do not
  "fix" this order.

Validation happens at load: unknown keys, wrong types, or out-of-range values fail
`run_created` (exit 3, §11.5) — a silently accepted config typo is a determinism hazard.

### 11.2 Workspace layout (self-hosted state)

The tool self-hosts its entire state **inside the workspace** it operates on — no global
daemon, no cross-workspace mixing:

```
<workspace>/
├── dbt_project.yml | airflow/ | spark/ …     # target pipeline (read-mostly input)
├── .refactor/
│   ├── refactor.db                           # SQLite source of truth (WAL)
│   ├── config.toml                           # workspace-scoped config (lowest file precedence)
│   ├── jobs/<run_id>/
│   │   ├── journal.json                      # human mirror (§6.2)
│   │   ├── events.jsonl                      # event stream (§9)
│   │   ├── input_snapshot.json               # frozen input (§3.1)
│   │   ├── artifacts/<stage>/<hash>.json     # content-addressed outputs (R7)
│   │   ├── diffs/<change_id>.patch           # dry-run previews
│   │   ├── restore_points/<apply-attempt>/   # pre-apply bytes + manifest (§6.4)
│   │   ├── dead_letters.json                 # (§8.4)
│   │   └── llm/                              # advisory enrichment (never in deterministic path)
│   ├── cache/                                # content-addressed cross-run artifact cache
│   ├── scratch/<run_id>/                     # worker cwd (sandbox root, §11.4)
│   └── logs/refactor.log                     # redacted human log
```

- `.refactor/` is added to the snapshot's exclusion list (never self-referential).
- Global (machine-level) state — plugin wheel cache, `~/.cache/refactor/` — is limited to
  things that are not workspace content (plugin metadata cache, engine version marker).
- The artifact cache is keyed by `(engine_version, plugin_versions, stage, input_hash)` →
  artifact path. Cache misses are re-computed and re-persisted; `cache_hit`/`cache_miss`
  events (R8: version pinning makes invalidation explicit).

### 11.3 SQLite discipline

- One DB per workspace (`.refactor/refactor.db`); WAL mode; single-writer (orchestrator
  only); `meta` table tracks schema version with explicit migrations (`oarl migrate`).
- The existing Phase-0 skeleton (`driftguard/store.py`: `stages`, `lineage_edges`, `drifts`,
  `runs`) is **superseded** by the orchestration schema (§6.1) — the drift-check run becomes
  the `analyze` stage's `schema_drift` analyzer; migration maps `runs.id` → `run_id` refs.
- Backups: `oarl checkpoint` snapshots the DB + journal while WAL is clean (safe-copy via
  `sqlite3` backup API — stdlib).

### 11.4 Subprocess & sandbox hygiene (PRD §Security)

Every worker/plugin subprocess runs with:

- **Cleaned environment**: credential env vars stripped (same redaction/clean-env discipline
  as the sandbox module in the parent OARL project), `TZ=UTC`, `LC_ALL=C`,
  `PYTHONHASHSEED=0` (R10), no `PATH` additions beyond the venv.
- **Sandboxed paths**: cwd = `.refactor/scratch/<run_id>/`; allowed roots = workspace
  (read) + `.refactor/` (read/write). Path escape attempts fail the worker.
- **Network**: blocked by default; only the LLM enrichment worker may reach the Ollama
  endpoint (allowlist `127.0.0.1` by default). Parser/rule plugins are offline by contract.
- **Resource guards**: per-worker timeout, artifact size cap (§7.4).

### 11.5 Operator surface & exit codes

| Command | Purpose |
|---|---|
| `oarl run [--apply] [--ci]` | Create + drive a run; without `--apply`, stops after `dry_run` (HITL gate) |
| `oarl resume <run_id>` | Recovery protocol (§6.3) |
| `oarl abort <run_id>` | Request ABORTED (cooperative; safe point = stage boundary) |
| `oarl report <run_id>` | Full report incl. partial results, dead letters, rollback status |
| `oarl trace <run_id>` | Stage/event timeline for the run |
| `oarl metrics [--run <id>]` | Aggregates (§9.4) |
| `oarl approve <run_id>` / `oarl deny <run_id>` | HITL gate decision |
| `oarl checkpoint` / `oarl prune` | Backup / retention |

Exit codes: `0` SUCCEEDED · `1` FAILED (or verify failed) · `2` ABORTED · `3` config/snapshot
error · `4` determinism violation (strict) · `5` internal error.

`--ci` mode: forces `determinism_mode=strict`, non-interactive (no confirmations; `--apply`
must still be explicit), no color (`NO_COLOR` respected), exit codes are the CI gate, and
the final event line is a single JSON summary (parseable by GitHub Actions/GitLab CI).

---

## 12. PRD Requirement Traceability

| PRD requirement | Where honored |
|---|---|
| "Every operation is idempotent and recorded in an audit trail" (§Architecture) | §4.4 idempotency contracts; §6.4 CAS apply; §9 exactly-once append-only audit; every transition + artifact write is an event |
| "Failure isolation & retry policies", "Metrics/observability hooks per stage" (§Features) | §7 process isolation; §8 retry/backoff/circuit breakers; §9 metrics consumer |
| "Optional local LLM (Ollama)" (§Features, §Stack) | §5.4 advisory-only enrichment boundary |
| "Subprocesses run with a cleaned environment and sandboxed paths; all logs are redacted" (§Security) | §11.4 sandbox hygiene; §9.4 redacted logs |
| "Plugin interface for extensibility" (§Architecture) | §10 versioned parser + rule-pack plugins |
| "SQLite + DuckDB for analytics" (§Stack) | §6.1 SQLite source of truth; §9.4 optional DuckDB export |
| "Dry-run transformations with preview output" (§Features) | Stage DAG `dry_run` + HITL approval gate (§4.3, §9.2) |
| "CLI as primary interface" (§APIs); CI integrations (§Integrations) | §11.5 operator surface, `--ci`, exit codes |
| "Free tier for CI / small teams"; zero-cost (§Zero-Cost Strategy) | stdlib-only orchestration; SQLite; no paid services in the deterministic path |

---

## 13. Implementation Checklist

Engineers implement directly from this document. Order:

1. `meta`/`runs`/`stage_instances`/`events`/`artifacts`/`dead_letters` schema + WAL setup (§6.1).
2. Canonical JSON helper + hash discipline (R1–R10) as the shared foundation.
3. Run FSM + stage FSM as explicit transition tables (§4.1–4.2) with event emission (§9) — a pure state machine, no IO in the table.
4. Dispatch loop (eligible computation, backoff deadlines, deterministic submission §4.2, §7.3).
5. Process pool + worker sandbox (§7, §11.4).
6. Snapshot creation/reconciliation (§3, §6.3).
7. Content-addressed artifacts + cache (§6.1, R7, R8).
8. Apply protocol: CAS, restore points, rollback (§6.4).
9. Retry/backoff + circuit breakers (§8.2–8.3).
10. Dead letters + partial-results reporting (§8.4–8.5).
11. Plugins: entry-point loading, contract versioning, purity sandbox (§10).
12. Config precedence + `--ci` + exit codes (§11).
13. Determinism self-check wiring (§5.2) and the strict/enriched mode switch (§5.4).
14. Eval suite: golden runs — same input run twice ⇒ byte-identical artifacts; crash-injected at every stage boundary ⇒ resume reproduces the uninterrupted result; LLM on ⇒ strict sections byte-identical.

---

*End of orchestration design. Normative sections: §4 (state machines), §5.1 (canonicalization),
§6 (persistence/recovery), §7.3 (isolation), §9.1 (event schema), §11.1 (precedence).*
