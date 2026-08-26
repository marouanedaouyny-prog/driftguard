# DriftGuard — Data Model & Storage Design

**Project:** Security-Aware Refactoring Assistant for Data Pipelines (working name: driftguard)
**Document:** `docs/DATA_MODEL.md` — normative storage design
**Status:** Design v1 — implementable as specified
**Source of truth hierarchy:** `PRD.md` wins for scope · `ORCHESTRATION.md` wins for the run/stage/event/dead-letter state machine (its §4, §5.1, §6, §7.3, §9.1, §11.1 are normative) · `ARCHITECTURE.md` wins for how · **this document is the single authority for the SQLite schema and must not contradict ORCHESTRATION.md** (it extends it: every orchestrator table from §6.1/§9.1 is hosted here verbatim, plus the domain tables the PRD's MVP features require).
**Anchors:** PRD §Features (parsing/introspection, lineage, schema drift, dry-run preview, failure isolation & retry, metrics hooks, security findings, refactor plans, audit trail) · PRD §Database (SQLite embedded / DuckDB analytics) · ARCHITECTURE §3 (storage design, ADR-003, §3.1 table list) · ORCHESTRATION §6.1 (SQLite tables), §9 (event/audit stream), §11.3 (SQLite discipline).

---

## 1. Scope & guarantees

One SQLite database **per workspace** at `.refactor/refactor.db` (ORCHESTRATION §11.2; `--db FILE` overrides the path, ARCHITECTURE §5.3). The database is the **source of truth**; the per-run `journal.json` and `events.jsonl` are filesystem **mirrors** (ORCHESTRATION §6.1B) and are *not* tables — do not re-model them in SQL.

The schema hosts **two cooperating models in one file**:

1. **Orchestration model** (normative, ORCHESTRATION §6.1): `runs`, `stage_instances`, `events`, `artifacts`, `dead_letters`, `meta` — the two-level FSM (run-level §4.1, stage-level §4.2), retry/backoff, circuit breakers, exactly-once audit.
2. **Domain model** (this document): `pipelines`, `stages`, `lineage_edges`, `cross_pipeline_edges`, `schema_snapshots`, `schema_drifts`, `security_findings`, `sessions`, `refactor_plans`, `refactor_plan_items`, `dry_run_previews`, `metrics`, `audit`, `config`, `schema_migrations`.

Guarantees this schema is built for (each mapped to a mechanism):

| Guarantee | Mechanism |
|---|---|
| Idempotent everything (P1) | content-addressed `artifacts` (hash PK), `input_hash`/`output_hash` on every `stage_instances` row, `item_hash` on plan items, `preview_hash` on dry-runs |
| Exactly-once audit (P2) | deterministic `events.event_id` + `INSERT OR IGNORE`; deterministic `audit.idempotency_key` |
| Crash-safe at any point (P4) | WAL + `synchronous=NORMAL` + `busy_timeout=5000`; state machine transitions and their event rows commit **in the same transaction** (§9.3) |
| Failure is data (P5) | `dead_letters` table + `BLOCKED` propagation in `stage_instances`; partial results preserved |
| Observable (P7) | every transition emits an `events` row; `metrics` rows per stage hook |
| Deterministic (P3) | all timestamps are UTC ISO-8601 with `Z` and are **audit-only** (never part of hashes/artifacts); JSON stored canonically (`sort_keys`, no trailing newline) |

---

## 2. Connection & pragma discipline (normative)

Set by `store/db.py` on **every** connection, in this order (foreign_keys is a no-op inside a transaction, so it is set before any `BEGIN`):

```sql
PRAGMA journal_mode = WAL;          -- ORCHESTRATION §6.1 — may return a row; set once
PRAGMA synchronous = NORMAL;        -- durable-enough for WAL + single writer
PRAGMA busy_timeout = 5000;         -- ARCHITECTURE §3 / ORCHESTRATION §6.1
PRAGMA foreign_keys = ON;           -- every FK below is enforced at write time
PRAGMA wal_autocheckpoint = 1000;   -- default; keep (1 MB WAL checkpoint granularity)
```

Concurrency model (ORCHESTRATION §7.3): **only the orchestrator process writes**; workers never open the DB. Readers (CLI `driftguard`, `oarl report/trace/metrics`) open read-only connections and benefit from WAL (readers never block the writer). The engine additionally enforces `python -W error::ResourceWarning` cleanliness (ARCHITECTURE §7.3) — one owner per connection, closed deterministically.

WAL lifecycle: while open, `.refactor/refactor.db-wal` and `-shm` exist; the orchestrator checkpoints on clean close; `oarl checkpoint` uses the stdlib `sqlite3` backup API (ORCHESTRATION §11.3) to snapshot a consistent copy.

---

## 3. Schema at a glance

| # | Table | Model | Role |
|---|-------|-------|------|
| 1 | `meta` | Orchestration | key/value: schema version, engine version (ORCH §6.1) |
| 2 | `schema_migrations` | System | ordered migration history + checksums |
| 3 | `config` | System | resolved config snapshot (precedence §11.1) |
| 4 | `pipelines` | Domain | stable catalog identity per analyzed workspace |
| 5 | `runs` | Orchestration | one row per `driftguard <root>` / `oarl run` invocation (ORCH §6.1) |
| 6 | `stage_instances` | Orchestration | one row per (run, stage, attempt) — stage FSM + retry state (ORCH §6.1) |
| 7 | `events` | Orchestration | append-only audit/event stream, exactly-once (ORCH §9.1) |
| 8 | `artifacts` | Orchestration | content-addressed artifact index (ORCH §6.1, R7) |
| 9 | `dead_letters` | Orchestration | terminally failed units (ORCH §6.1, §8.4) |
| 10 | `stages` | Domain | per-run parsed stage snapshot (supersedes seed `stages`) |
| 11 | `lineage_edges` | Domain | per-run producer→consumer edges (supersedes seed `lineage_edges`) |
| 12 | `cross_pipeline_edges` | Domain | cross-workspace edges (future catalog; empty in MVP) |
| 13 | `schema_snapshots` | Domain | versioned output schema per (pipeline, stage) |
| 14 | `schema_drifts` | Domain | drift findings per run (supersedes seed `drifts`) |
| 15 | `security_findings` | Domain | scanner findings per run + status lifecycle (supersedes seed `scans`) |
| 16 | `sessions` | Domain | refactor workflow state machine (ARCH §3.1, §5.1) |
| 17 | `refactor_plans` | Domain | plan artifacts (ARCH ADR-005) |
| 18 | `refactor_plan_items` | Domain | plan items + applied state (supersedes seed `rewrites`) |
| 19 | `dry_run_previews` | Domain | per-change dry-run previews (sample rows, hashes, full vs sampled) |
| 20 | `metrics` | Domain | per-stage observability hooks |
| 21 | `audit` | Domain | session/CLI-level append-only transition log (ARCH §3.1, §5.4) |

Naming rules: `*_json` columns hold canonical JSON text (never a blob); `*_hash` columns hold `sha256:`-prefixed hex strings; booleans are `INTEGER 0/1`; timestamps are `TEXT` UTC ISO-8601 with trailing `Z` and are **audit-only metadata** (R3 — they never feed a hash).

> **Terminology guard-rail:** `stages` (domain: parsed pipeline nodes) and `stage_instances` (orchestration: run/stage/attempt state machine rows) are different concepts that share the word "stage". Queries must never join them directly — they relate only through `run_id`.

---

## 4. Complete DDL

The schema is applied via `store/db.py` `executescript` inside one transaction at first open, then evolved exclusively through the migration runner (§11). Tables are ordered by dependency for readability; SQLite resolves FK targets lazily, so order is not load-bearing when `foreign_keys=ON` is set outside the transaction.

### 4.1 System tables

```sql
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,          -- 'schema_version', 'engine_version', 'created_at'
    value TEXT NOT NULL
);

CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY, -- 1-based migration number; head == meta['schema_version']
    name        TEXT NOT NULL,       -- short human name, e.g. 'orchestration_v2'
    applied_at  TEXT NOT NULL,       -- UTC; audit-only (R3)
    checksum    TEXT NOT NULL,       -- sha256 of the migration script text (tamper-evidence)
    duration_ms INTEGER NOT NULL
);

CREATE TABLE config (
    key        TEXT PRIMARY KEY,          -- dot path, e.g. 'retry.max_attempts'
    value      TEXT NOT NULL,             -- JSON-encoded scalar/array/object
    source     TEXT NOT NULL DEFAULT 'file', -- defaults|file|cli|env  (precedence, ORCH §11.1)
    updated_at TEXT NOT NULL
);
```

Rationale: `meta` hosts the orchestrator's schema-version bookkeeping (ORCH §6.1) and `schema_version` (ARCH §3); `schema_migrations` extends it with ordered history + checksums; `config` persists the **resolved** configuration so a run's effective settings are inspectable after the fact (a silently accepted config typo is a determinism hazard, ORCH §11.1 — the persisted snapshot makes it visible).

### 4.2 Catalog

```sql
CREATE TABLE pipelines (
    pipeline_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_root      TEXT NOT NULL,
    repo_fingerprint    TEXT NOT NULL,     -- sha256 over sorted relative paths + content hashes
    current_fingerprint TEXT NOT NULL,     -- latest known value of repo_fingerprint
    engine_version      TEXT NOT NULL,
    parser_plugin       TEXT NOT NULL,     -- 'dbt@1.2.0'
    first_seen_at       TEXT NOT NULL,
    last_run_id         TEXT,              -- denormalized; refreshed at run creation
    UNIQUE (workspace_root)
);
```

Rationale: `pipelines` is the **stable catalog identity** a workspace's lineage/drift/security history hangs off. One row per workspace; `repo_fingerprint` changes only when the pipeline definition changes. Every `runs` row references it, so "history correct across runs" (ARCHITECTURE §Phase 3 acceptance) is a simple query over `runs` + domain tables filtered by `pipeline_id`.

### 4.3 Orchestration tables (ORCHESTRATION §6.1, normative)

```sql
CREATE TABLE runs (
    run_id               TEXT PRIMARY KEY,       -- 'ref-20260818T061200Z-3f9a2c'
    pipeline_id          INTEGER REFERENCES pipelines(pipeline_id) ON DELETE SET NULL,
    status               TEXT NOT NULL,          -- CREATED|READY|RUNNING|SUSPENDED|SUCCEEDED|FAILED|ABORTED
    workspace_root       TEXT NOT NULL,
    snapshot_json        TEXT NOT NULL,          -- frozen input snapshot (§3.1) — immutable for run lifetime
    snapshot_hash        TEXT NOT NULL,          -- sha256(canonical snapshot_json); denormalized for indexing
    mode_json            TEXT NOT NULL,          -- {"determinism":"strict"|"enriched","llm":true|false}
    intents_json         TEXT,                   -- refactoring intents (canonical JSON); NULL when none
    engine_version       TEXT NOT NULL,
    plugin_versions_json TEXT NOT NULL,          -- {"dbt-parser":"1.2.0","oarl-security-rules":"0.4.1"}
    git_sha              TEXT,                   -- optional; audit-only (P7, ORCH §3.1)
    exit_code            INTEGER,                -- 0..5 per ORCH §11.5; NULL while running
    error_json           TEXT,                   -- fatal run-level {type,message}; NULL otherwise
    created_at           TEXT NOT NULL,          -- audit-only (R3)
    started_at           TEXT,
    ended_at             TEXT
);

CREATE TABLE stage_instances (
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage         TEXT NOT NULL,                 -- parse|analyze|plan|dry_run|apply|verify
    attempt       INTEGER NOT NULL,              -- 1-based; part of identity (ORCH §8.2)
    status        TEXT NOT NULL,                 -- PENDING|RUNNING|SUCCEEDED|FAILED|INTERRUPTED|SKIPPED|BLOCKED
    parents_json  TEXT,                          -- DAG parent stage names (eligibility + BLOCKED propagation)
    input_hash    TEXT,                          -- sha256(canonical_json(stage_input_payload)) — R9
    output_hash   TEXT,                          -- sha256(artifact bytes); NULL until SUCCEEDED
    artifact      TEXT,                          -- 'artifacts/<stage>/<hash>.json' relative to job dir
    retry_after   TEXT,                          -- UTC backoff deadline; dispatch only after it (§8.2)
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    retryable     INTEGER NOT NULL DEFAULT 0,
    error_json    TEXT,                          -- last-attempt {type,message}
    duration_ms   INTEGER,
    started_at    TEXT,
    ended_at      TEXT,
    PRIMARY KEY (run_id, stage, attempt)
);

CREATE TABLE events (
    event_id         TEXT PRIMARY KEY,  -- deterministic: f"{run_id}:{stage}:{attempt}:{seq}" (ORCH §9.1)
    run_id           TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    ts               TEXT NOT NULL,     -- UTC; audit-only
    seq              INTEGER NOT NULL,  -- per-run monotonic
    stage            TEXT NOT NULL,     -- parse|analyze|plan|dry_run|apply|verify|run|llm|plugin
    stage_instance_id TEXT,             -- 'apply@1' == f"{stage}@{attempt}"
    kind             TEXT NOT NULL,     -- run_ready|stage_started|security_finding|change_applied|... (ORCH §9.2)
    status           TEXT,              -- succeeded|failed|skipped|blocked|...
    attempt          INTEGER,
    input_hash       TEXT,
    output_hash      TEXT,
    artifact         TEXT,
    duration_ms      INTEGER,
    error_json       TEXT,              -- {type,message}
    extra_json       TEXT,              -- kind-scoped free-form; redacted before persist (P6)
    UNIQUE (run_id, seq)
);

CREATE TABLE artifacts (
    hash                 TEXT PRIMARY KEY,  -- sha256 of canonical artifact bytes (R7)
    path                 TEXT NOT NULL,     -- absolute or workspace-relative artifact path
    stage                TEXT NOT NULL,
    engine_version       TEXT NOT NULL,
    plugin_versions_json TEXT NOT NULL,
    run_id               TEXT REFERENCES runs(run_id) ON DELETE SET NULL, -- NULL => cross-run cache entry
    size_bytes           INTEGER NOT NULL,
    created_at           TEXT NOT NULL,
    UNIQUE (path)
);

CREATE TABLE dead_letters (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage         TEXT NOT NULL,          -- parse|analyze|plan|dry_run|apply|verify
    attempt       INTEGER NOT NULL,       -- terminal attempt number
    entity        TEXT NOT NULL,          -- file path | analyzer id | change_id | plugin id (unit identity)
    error_type    TEXT NOT NULL,          -- ParseError|AnalyzerCrash|CasConflict|TimeoutError|...
    error_message TEXT NOT NULL,
    payload_hash  TEXT,                   -- sha256 of the unit input payload (reproducibility)
    created_at    TEXT NOT NULL
);
```

Notes on the orchestration group:

- `run_id` is **TEXT** (not the seed's AUTOINCREMENT INTEGER) — ORCHESTRATION §11.3 explicitly supersedes the seed and maps `runs.id → run_id`; see §11 for the migration.
- `stage_instances` is the **retry store**: `attempt`, `max_attempts`, `retryable`, `retry_after` together encode the backoff policy (ORCH §8.2) and the retryable/fatal distinction from the failure taxonomy (§8.1). There is **no separate "attempts" table** — attempt is part of the row identity `(run_id, stage, attempt)`, exactly as ORCH §6.1 specifies.
- `events` is append-only (triggers in §9). `event_id` is deterministic (ORCH §9.1) so a crashed engine that replays a transition re-emits the same id and `INSERT OR IGNORE` dedupes it — exactly-once even under at-least-once execution.
- `artifacts` is the content-addressed index (R7): hash → path; `run_id` NULL marks a cross-run cache entry; version pinning (R8) lives in `engine_version` + `plugin_versions_json`.
- `dead_letters` mirrors ORCH §8.4's `dead_letters.json` artifact per row, with `entity` as the unit identity and `payload_hash` for reproducibility.

### 4.4 Domain: parsed pipeline & lineage

```sql
CREATE TABLE stages (
    stage_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    pipeline_id        INTEGER NOT NULL REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    path               TEXT NOT NULL,          -- relative to workspace root
    kind               TEXT NOT NULL,          -- model|source_table|view|external
    fingerprint        TEXT NOT NULL,          -- sha256(canonical IR json) — drift/rewrite identity
    ir_json            TEXT NOT NULL,          -- versioned IR envelope {"v":1,...} (serialize.py)
    columns_json       TEXT NOT NULL,          -- normalized [{name,data_type,nullable,ordinal}]
    refs_json          TEXT NOT NULL,          -- producer ref names (denormalized from IR)
    sources_json       TEXT NOT NULL,          -- [["source","table"],...]
    dialect_hints_json TEXT,                   -- e.g. ["jinja_static_only","unknown_template_region"]
    diagnostics_json   TEXT,                   -- structured ParseWarning/ParseError list (P3)
    raw_sha256         TEXT NOT NULL,          -- sha256 of raw file bytes (≠ IR fingerprint)
    created_at         TEXT NOT NULL,
    UNIQUE (run_id, name)
);

CREATE TABLE lineage_edges (
    run_id                 TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    pipeline_id            INTEGER NOT NULL REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    producer               TEXT NOT NULL,
    consumer               TEXT NOT NULL,
    kind                   TEXT NOT NULL,      -- ref|bare|source
    expected_columns_json  TEXT NOT NULL,      -- columns the consumer expects from the producer
    created_at             TEXT NOT NULL,
    PRIMARY KEY (run_id, producer, consumer),
    FOREIGN KEY (run_id, producer) REFERENCES stages(run_id, name),
    FOREIGN KEY (run_id, consumer) REFERENCES stages(run_id, name)
);

CREATE TABLE cross_pipeline_edges (
    edge_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    producer_pipeline_id  INTEGER NOT NULL REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    producer_stage        TEXT NOT NULL,
    consumer_pipeline_id  INTEGER NOT NULL REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    consumer_stage        TEXT NOT NULL,
    kind                  TEXT NOT NULL DEFAULT 'external_ref',
    discovered_run_id     TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
    created_at            TEXT NOT NULL,
    UNIQUE (producer_pipeline_id, producer_stage, consumer_pipeline_id, consumer_stage)
);
```

Notes:

- `stages` supersedes the seed's single-global-`name`-PK table with a **per-run snapshot** model: every run that parses a workspace records the stages it saw. `UNIQUE (run_id, name)` makes the composite FKs from `lineage_edges` enforceable. `fingerprint` is the IR fingerprint (whitespace/comment-insensitive, ARCH §4.2); `raw_sha256` is the byte hash — the two are kept apart so drift can ignore formatting while `apply`/`verify` can assert byte identity.
- `lineage_edges` supersedes the seed's `(producer, consumer)` PK with `(run_id, producer, consumer)` — same shape as ARCH §3.1, now per-run. `expected_columns_json` records what the consumer's projection expects, which is the input to producer→consumer drift (§6).
- `cross_pipeline_edges` is the **cross-pipeline seam** (§5): a managed catalog (Phase 4+) links stages that live in different workspaces. MVP never populates it — intra-workspace `source()` refs are modeled as ordinary `stages` rows of `kind='source_table'` resolved from `sources.yml`, so they already participate in the graph.

### 4.5 Domain: schema snapshots & drift

```sql
CREATE TABLE schema_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_id     INTEGER NOT NULL REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage_name      TEXT NOT NULL,
    version         INTEGER NOT NULL,           -- per-stage monotonic sequence (1,2,3,...)
    parent_version  INTEGER,                    -- previous version for the same stage; NULL on first
    columns_json    TEXT NOT NULL,              -- [{name,data_type,nullable,ordinal}]
    column_hash     TEXT NOT NULL,              -- sha256(canonical columns_json) — content identity
    fingerprint     TEXT NOT NULL,              -- sha256 over full canonical schema incl. provenance
    source_artifact TEXT NOT NULL,              -- 'artifacts/analyze/<hash>.json'
    created_at      TEXT NOT NULL,
    UNIQUE (pipeline_id, stage_name, version)
);

CREATE TABLE schema_drifts (
    drift_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    pipeline_id           INTEGER NOT NULL REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    producer              TEXT NOT NULL,
    consumer              TEXT NOT NULL,
    edge_kind             TEXT NOT NULL,        -- ref|bare|source
    diff_kind             TEXT NOT NULL,        -- between_snapshots | producer_consumer
    base_snapshot_id      INTEGER REFERENCES schema_snapshots(snapshot_id),
    compared_snapshot_id  INTEGER REFERENCES schema_snapshots(snapshot_id),
    added_json            TEXT NOT NULL,        -- [{name,data_type,nullable}]
    removed_json          TEXT NOT NULL,        -- [{name,data_type,nullable}]
    changed_json          TEXT NOT NULL,        -- [{name,old_type,new_type,old_nullable,new_nullable,kind}]
    renamed_json          TEXT NOT NULL,        -- [{old_name,new_name,similarity}]
    breaking              INTEGER NOT NULL,     -- 0/1 per the breaking policy (§6)
    severity              TEXT NOT NULL DEFAULT 'warning',  -- warning|error (breaking => error)
    created_at            TEXT NOT NULL,
    UNIQUE (run_id, producer, consumer, diff_kind)
);
```

Notes:

- `schema_snapshots` is the **versioned snapshot history** per (pipeline, stage). `column_hash` is the fast content identity; `version` is the monotonic sequence number. A "schema change" is a `column_hash` difference between consecutive versions. `parent_version` links the chain for cheap forward diffs.
- `schema_drifts` supersedes the seed's `drifts` with a richer diff representation (added/removed/**changed**/renamed). `diff_kind='producer_consumer'` is the seed's semantics (producer's snapshot vs what the consumer's projection expects); `diff_kind='between_snapshots'` is time-based (same stage, consecutive versions). `base_snapshot_id`/`compared_snapshot_id` point at the two snapshots being compared when applicable (NULL for producer_consumer diffs where the "expected" side is the consumer's projection, not a stored snapshot).
- The orchestrator writes these rows as **queryable projections of the content-addressed `analyze` artifacts** in the same transaction that persists the artifact (ORCH §9.3 discipline). The artifact remains canonical (R7); the tables exist so lineage/drift history queries never load JSON blobs.

### 4.6 Domain: security findings

```sql
CREATE TABLE security_findings (
    finding_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    pipeline_id       INTEGER NOT NULL REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    session_id        INTEGER REFERENCES sessions(session_id) ON DELETE SET NULL,
    rule_id           TEXT NOT NULL,            -- SEC-001..005 or plugin rule id
    rule_version      INTEGER NOT NULL DEFAULT 1,
    severity          TEXT NOT NULL,            -- critical|high|medium|low
    path              TEXT NOT NULL,            -- relative file path
    line              INTEGER,
    col               INTEGER,
    span_start        INTEGER,                  -- byte offsets when tokenizer-derived
    span_end          INTEGER,
    snippet_redacted  TEXT NOT NULL,            -- redacted evidence (P6; ARCH §4.5)
    hint              TEXT,
    status            TEXT NOT NULL DEFAULT 'open',  -- open|suppressed|resolved
    suppression_reason TEXT,                    -- populated when status='suppressed'
    scan_stage        TEXT NOT NULL DEFAULT 'analyze',  -- analyze (baseline) | verify (regression)
    created_at        TEXT NOT NULL,
    UNIQUE (run_id, rule_id, path, line, col, scan_stage)
);
```

Notes: supersedes the seed's `scans`. `scan_stage` distinguishes the **baseline** scan (at `analyze`) from the **regression** scan (at `verify`) — the ADR-007 gate (§4.5 of ARCHITECTURE) needs both pictures. `span_start/span_end` let the refactor engine compute span intersections for the block overlay ("candidates whose span intersects a critical/high finding are excluded"). `session_id` is SET NULL so a scan that predates a session still keeps its finding. Suppression (inline `-- driftguard:off` comments) flips `status` with `suppression_reason` — an audited exception, never a deletion.

### 4.7 Domain: refactor workflow

```sql
CREATE TABLE sessions (
    session_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
    repo_fingerprint TEXT NOT NULL,
    state            TEXT NOT NULL,   -- start|parsed|analyzed|planned|approved|applied|verified|done|aborted
    plan_path        TEXT,            -- plan.json path (the approval artifact, ADR-005)
    plan_hash        TEXT,
    rule_ids_json    TEXT NOT NULL,   -- ["REF-001","REF-002"]
    max_risk         TEXT NOT NULL DEFAULT 'safe',   -- safe|suggested|risky (ADR-006)
    llm_used         INTEGER NOT NULL DEFAULT 0,
    base_commit      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE refactor_plans (
    plan_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    run_id     TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
    plan_hash  TEXT NOT NULL,          -- sha256 of canonical plan.json bytes
    schema_ver TEXT NOT NULL,          -- 'driftguard.plan.v1'
    item_count INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'proposed',  -- proposed|approved|applied|partially_applied|rejected
    created_at TEXT NOT NULL,
    UNIQUE (session_id, plan_hash)
);

CREATE TABLE refactor_plan_items (
    item_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id            INTEGER NOT NULL REFERENCES refactor_plans(plan_id) ON DELETE CASCADE,
    item_hash          TEXT NOT NULL,   -- sha256 of canonical item JSON (idempotency anchor, ARCH §5.2)
    change_id          TEXT NOT NULL,   -- 'c0','c1',... deterministic plan order (ORCH §4.3)
    rule_id            TEXT NOT NULL,   -- REF-001..006 | LLM-<n>
    rule_version       INTEGER NOT NULL DEFAULT 1,
    tier               TEXT NOT NULL,   -- safe|suggested|risky
    stage              TEXT NOT NULL,
    path               TEXT NOT NULL,
    span_start         INTEGER NOT NULL,
    span_end           INTEGER NOT NULL,
    before             TEXT NOT NULL,
    after              TEXT NOT NULL,
    reason             TEXT NOT NULL,
    security_note      TEXT,            -- 'touches SEC-002 span' (ARCH §4.4)
    blocked_by_finding INTEGER REFERENCES security_findings(finding_id) ON DELETE SET NULL,
    state              TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|noop|conflict|deadlettered|skipped
    fingerprint_before TEXT,            -- stage fingerprint before the edit
    fingerprint_after  TEXT,            -- after the edit; NULL until applied
    applied_at         TEXT,
    UNIQUE (plan_id, item_hash),
    UNIQUE (plan_id, change_id)
);

CREATE TABLE dry_run_previews (
    preview_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id         INTEGER NOT NULL REFERENCES refactor_plans(plan_id) ON DELETE CASCADE,
    change_id       TEXT NOT NULL,
    path            TEXT NOT NULL,
    old_hash        TEXT NOT NULL,
    new_hash        TEXT NOT NULL,
    diff_patch      TEXT,               -- unified diff text (full preview); NULL when sampled
    patch_artifact  TEXT,               -- 'diffs/<change_id>.patch' (ORCH §4.3)
    lines_added     INTEGER NOT NULL DEFAULT 0,
    lines_removed   INTEGER NOT NULL DEFAULT 0,
    sample_strategy TEXT NOT NULL DEFAULT 'full',  -- full|sample
    sample_rows_json TEXT,              -- redacted sampled rows (deterministic sampling, R6)
    sample_count    INTEGER,
    preview_hash    TEXT NOT NULL,      -- sha256(canonical preview content) — idempotency
    created_at      TEXT NOT NULL,
    UNIQUE (plan_id, change_id)
);
```

Notes:

- `sessions` is the **refactor workflow FSM** (ARCH §5.1: start→parsed→analyzed→planned→approved→applied→verified→done, plus aborted). It is *longer-lived* than any single run: a workflow can span several orchestrator runs (baseline run, then an apply/verify run). `run_id` links the driving run; SET NULL keeps the workflow when a run is pruned.
- `refactor_plan_items.state` **is the applied-state bookkeeping** — it subsumes the seed's separate `rewrites` table (a row with `state='applied'` ≡ a `rewrites` row). The idempotent re-apply guard (ARCH §5.2: "item_hash already appears in rewrites ⇒ NOOP") is the query `SELECT ... WHERE session_id=? AND item_hash=? AND state='applied'` (§12 Q13).
- `dry_run_previews` stores **what the human approved**: old/new hashes, the unified diff (or an explicit sampled preview), and the `preview_hash` so `apply` can verify the preview that was approved still matches the plan (stale-preview rejection).

### 4.8 Domain: metrics & audit

```sql
CREATE TABLE metrics (
    metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage     TEXT NOT NULL,          -- parse|analyze|plan|dry_run|apply|verify
    unit      TEXT,                   -- file | analyzer | change_id | NULL for stage-level
    name      TEXT NOT NULL,          -- duration_ms|files_parsed|rows_parsed|retry_count|cache_hit|cache_miss|llm_calls|determinism_violations|...
    value     REAL NOT NULL,
    ts        TEXT NOT NULL
);

CREATE TABLE audit (
    audit_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER REFERENCES sessions(session_id) ON DELETE SET NULL,
    run_id         TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
    event_id       TEXT REFERENCES events(event_id) ON DELETE SET NULL,
    ts             TEXT NOT NULL,
    actor          TEXT NOT NULL DEFAULT 'cli',  -- cli|human|plugin|llm
    action         TEXT NOT NULL,     -- PARSE|ANALYZE|PLAN|APPROVE|APPLY|VERIFY|ABORT|APPROVAL_REQUESTED|...
    from_state     TEXT,
    to_state       TEXT,
    args_json      TEXT,              -- redacted (plan hash, rule ids, thresholds) — never secrets (P6)
    result_json    TEXT,              -- counts, exit_code, fingerprints
    exit_code      INTEGER,
    idempotency_key TEXT UNIQUE,      -- deterministic exactly-once key (§10)
    created_at     TEXT NOT NULL
);
```

Notes on the two audit surfaces (kept deliberately separate, both append-only):

- `events` (orchestration) is written by the **orchestrator** for run/stage FSM transitions (ORCH §9.3).
- `audit` (domain) is written by the **session/state layer** for the refactor workflow FSM and CLI operations (ARCH §5.4). `event_id` cross-links a domain audit row to the orchestration event that triggered it, so `driftguard audit` can trace one transition end-to-end.

### 4.9 Append-only enforcement triggers

```sql
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only; UPDATE denied'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only; prune via checkpoint rebuild'); END;
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only; UPDATE denied'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only; prune via checkpoint rebuild'); END;
```

Retention (ORCH §9.4, `audit.retention_days` default 90) is implemented as **checkpoint rebuild**, not DELETE: `oarl prune` copies the DB to a new file (stdlib `sqlite3` backup API or `VACUUM INTO`) omitting rows older than the retention window, then atomically replaces the DB. This keeps the immutability triggers honest and makes pruning a *new snapshot*, not a mutation.

### 4.10 Indexes (DDL)

Every index below is justified by a concrete query in §12 or an orchestrator hot path (see the full rationale table in §7).

```sql
-- runs
CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_runs_pipeline_created ON runs(pipeline_id, created_at);
CREATE INDEX idx_runs_snapshot_hash ON runs(snapshot_hash);

-- stage_instances
CREATE INDEX idx_stage_instances_run_status ON stage_instances(run_id, status);
CREATE INDEX idx_stage_instances_retry_after ON stage_instances(retry_after) WHERE status = 'FAILED';
CREATE INDEX idx_stage_instances_latest ON stage_instances(run_id, stage);

-- events (event_id PK + UNIQUE(run_id, seq) already serve exactly-once and ordering)
CREATE INDEX idx_events_kind ON events(kind);
CREATE INDEX idx_events_run_stage ON events(run_id, stage);

-- artifacts (hash PK + UNIQUE(path) already serve content addressing)
CREATE INDEX idx_artifacts_stage_version ON artifacts(stage, engine_version);

-- dead_letters
CREATE INDEX idx_dead_letters_run_stage ON dead_letters(run_id, stage);
CREATE INDEX idx_dead_letters_created ON dead_letters(created_at);

-- stages (UNIQUE(run_id, name) already serves snapshot identity + FK targets)
CREATE INDEX idx_stages_pipeline_name ON stages(pipeline_id, name);
CREATE INDEX idx_stages_run ON stages(run_id);
CREATE INDEX idx_stages_fingerprint ON stages(fingerprint);

-- lineage_edges (PK (run_id, producer, consumer) prefix serves per-run walks)
CREATE INDEX idx_lineage_pipeline_producer ON lineage_edges(pipeline_id, producer);
CREATE INDEX idx_lineage_pipeline_consumer ON lineage_edges(pipeline_id, consumer);

-- cross_pipeline_edges
CREATE INDEX idx_cpe_producer ON cross_pipeline_edges(producer_pipeline_id, producer_stage);
CREATE INDEX idx_cpe_consumer ON cross_pipeline_edges(consumer_pipeline_id, consumer_stage);

-- schema_snapshots (UNIQUE(pipeline_id, stage_name, version) is the history chain)
CREATE INDEX idx_snapshots_pipeline_stage ON schema_snapshots(pipeline_id, stage_name);
CREATE INDEX idx_snapshots_run ON schema_snapshots(run_id);

-- schema_drifts (UNIQUE(run_id, producer, consumer, diff_kind) dedupes per run)
CREATE INDEX idx_drifts_pipeline_edge ON schema_drifts(pipeline_id, producer, consumer);
CREATE INDEX idx_drifts_pipeline_breaking ON schema_drifts(pipeline_id, breaking);
CREATE INDEX idx_drifts_run ON schema_drifts(run_id);

-- security_findings (UNIQUE(run_id, rule_id, path, line, col, scan_stage) dedupes per scan)
CREATE INDEX idx_findings_pipeline_sev ON security_findings(pipeline_id, severity, status);
CREATE INDEX idx_findings_run_scan ON security_findings(run_id, scan_stage);
CREATE INDEX idx_findings_rule ON security_findings(rule_id);

-- sessions
CREATE INDEX idx_sessions_state ON sessions(state);

-- refactor_plans (UNIQUE(session_id, plan_hash) identifies the plan file)
CREATE INDEX idx_plans_status ON refactor_plans(status);

-- refactor_plan_items (UNIQUE(plan_id, item_hash) + UNIQUE(plan_id, change_id))
CREATE INDEX idx_items_state ON refactor_plan_items(state);
CREATE INDEX idx_items_hash ON refactor_plan_items(item_hash);
CREATE INDEX idx_items_plan_stage ON refactor_plan_items(plan_id, stage);

-- dry_run_previews (UNIQUE(plan_id, change_id) is the one-preview-per-change rule)
CREATE INDEX idx_previews_plan ON dry_run_previews(plan_id);

-- metrics
CREATE INDEX idx_metrics_run_stage_name ON metrics(run_id, stage, name);
CREATE INDEX idx_metrics_run_name ON metrics(run_id, name);

-- audit (UNIQUE(idempotency_key) is the exactly-once key)
CREATE INDEX idx_audit_session ON audit(session_id, ts);
CREATE INDEX idx_audit_run ON audit(run_id);
CREATE INDEX idx_audit_action ON audit(action);
```

---

## 5. Lineage model

**Nodes** = `stages` rows (per-run snapshots, identified by `(run_id, name)`). **Edges** = `lineage_edges` rows (per run, `(run_id, producer, consumer)`). An edge `(producer, consumer)` means **data flows from producer to consumer** — i.e. the consumer *depends on* the producer. Directionality is therefore:

- **Downstream** (consumers of X): walk edges where `producer = X` — direction of data flow.
- **Upstream** (producers of X): walk edges where `consumer = X` — dependency direction.

`kind` records how the edge was discovered: `ref` (dbt `ref('x')`), `bare` (bare `FROM table`), `source` (`source('s','t')` resolved against `sources.yml`). `expected_columns_json` is the consumer's projection of the producer — the "contract" drift checks.

**Fan-in** (a stage that joins/refs many producers) and **fan-out** (a stage consumed by many stages) are both ordinary indexed lookups — Q5/Q6 in §12. The transitive closure of either direction uses a recursive CTE over `(run_id, producer)` (the PK prefix) — Q1 and Q4.

**Cross-pipeline edges**: intra-workspace `source()` references are modeled as ordinary `stages` rows with `kind='source_table'` (parsed from `sources.yml`), so they already participate in fan-in/fan-out and drift. Edges between *different workspaces* (a future managed catalog, Phase 4+) live in `cross_pipeline_edges` (producer pipeline/stage → consumer pipeline/stage, unique per quadruple). MVP never writes it; the FK to `discovered_run_id` keeps provenance when it is populated.

Normative invariants:

1. Every `lineage_edges` row has matching `stages` rows for both endpoints in the same run (composite FKs enforce this).
2. The graph is **acyclic by construction for refactorable stages**; a cycle is *reported* (as an analyze artifact + `cycle_detected`-style event), never stored as an edge that the plan engine would walk.
3. A stage whose parse produced hard errors has no `columns_json`/`expected_columns_json` usable for drift — the schema-drift analyzer skips it and says so (P3 fail-loud, ARCH §4.1).

The "what breaks if this stage changes" question decomposes into three queries, all in §12: **(a)** direct + transitive consumers (Q1), **(b)** the breaking drift along each edge into those consumers (Q1's join), **(c)** the security block overlay for any rewrite touching the changed spans (Q3's sibling query on `security_findings`).

---

## 6. Schema drift detection

### 6.1 Snapshot versioning

`schema_snapshots` records one row per (pipeline, stage, **version**) whenever the stage is analyzed in a run. `version` is the per-stage monotonic sequence (1, 2, 3, …); `column_hash` is `sha256(canonical columns_json)` and is the **content identity** — two consecutive versions with equal `column_hash` mean "no schema change"; a difference means "schema changed". `parent_version` links the chain so the detector can diff adjacent versions without a self-join on `MAX(version)`.

Canonical column record (one element of `columns_json` / `added_json` / `removed_json`):

```json
{"name": "order_id", "data_type": "INTEGER", "nullable": false, "ordinal": 0}
```

`data_type` is `"unknown"` when the parser cannot infer it (no CREATE TABLE AS type info). **Unknown types never count as changes** (P3: fail loud rather than guess; a rewrite must not be blocked by a phantom type change).

### 6.2 Diff representation

Two kinds of diff row in `schema_drifts`:

- `diff_kind='producer_consumer'` — the seed's semantics: the producer's actual schema vs. what a consumer's projection expects (`expected_columns_json`). This is the CI gate.
- `diff_kind='between_snapshots'` — time-based: the same stage's schema in two consecutive runs/snapshots. This powers "what changed in this PR" and feeds `verify`'s regression check.

Diff algorithm (deterministic, in the `schema_drift` analyzer): for each pair, classify every column as **added**, **removed**, **renamed**, or **changed**:

1. Match columns by name. Names present in the new side only → **added**; present in the old side only → candidate **removed** or **renamed**.
2. For each candidate removed name, run the rename heuristic (`difflib.SequenceMatcher` ratio ≥ `--threshold`, default 0.75, ARCH Phase 3) against candidate new names not already matched → **renamed** with `similarity`; else **removed**.
3. For matched names, compare `data_type` (both known) → **changed** with `kind='type'`; compare `nullable` → **changed** with `kind='nullability'` (or `'both'` when both differ).

Resulting JSON: `added_json`, `removed_json`, `changed_json` (each record carries `name`, old/new `data_type`, old/new `nullable`, `kind`), `renamed_json` (`old_name`, `new_name`, `similarity`).

### 6.3 Breaking policy (normative)

| Change | `breaking` | `severity` |
|---|---|---|
| added column | 0 | warning |
| removed column | 1 | error |
| renamed column (similarity ≥ threshold) | 1 | error |
| type change (both types known, differ) | 1 | error |
| nullability tightening (`nullable → not null`) | 1 | error |
| nullability widening (`not null → nullable`) | 0 | warning |

`breaking` drives the CI exit code (0/1, ARCH §5.3): any `schema_drifts.breaking = 1` in the run's latest analysis makes `drift`/`--ci` exit 1. The `verify` stage's drift re-check looks for drifts whose `created_at` is newer than the apply-run's start — "no **new** breaking drift" (ARCH §5.1 table, `applied → verified` guard).

---

## 7. Indexes & rationale (complete list)

Every index is justified by a concrete query in §12 or an orchestrator hot path:

| Table | Index | Purpose |
|---|---|---|
| `runs` | `idx_runs_status` | resume scan: find SUSPENDED/RUNNING runs (Q7) |
| `runs` | `idx_runs_pipeline_created (pipeline_id, created_at)` | "latest run per pipeline" history (Q2) and per-pipeline recency |
| `runs` | `idx_runs_snapshot_hash` | identical-input detection (same snapshot → cache reuse, determinism checks) |
| `stage_instances` | PK `(run_id, stage, attempt)` | row identity + latest attempt = `MAX(attempt)` per (run, stage) (ORCH §6.1) |
| `stage_instances` | `idx_stage_instances_run_status (run_id, status)` | recovery classification of every stage of a run (§6.3) |
| `stage_instances` | `idx_stage_instances_retry_after (retry_after) WHERE status='FAILED'` | dispatch of due retries after backoff (Q8) — partial index keeps it tiny |
| `stage_instances` | `idx_stage_instances_latest (run_id, stage)` | current attempt lookup for trace/report without a MAX scan |
| `events` | PK `event_id` | exactly-once dedupe via `INSERT OR IGNORE` (ORCH §9.1) |
| `events` | `UNIQUE (run_id, seq)` | per-run sequence integrity (defends fabricated ids) |
| `events` | `idx_events_kind` | metrics/alert consumers filtering by kind (`security_finding`, `verify_failed`, …) |
| `events` | `idx_events_run_stage` | stage timelines for `oarl trace` |
| `artifacts` | PK `hash` | content addressing (R7): cache hit = hash lookup |
| `artifacts` | `UNIQUE (path)` | no two rows can claim the same artifact path |
| `artifacts` | `idx_artifacts_stage_version (stage, engine_version)` | cache invalidation when engine/plugin versions bump (R8) |
| `dead_letters` | `idx_dead_letters_run_stage` | per-run dead-letter report (Q12) |
| `dead_letters` | `idx_dead_letters_created` | retention pruning |
| `stages` | `UNIQUE (run_id, name)` | snapshot identity + FK target for lineage edges |
| `stages` | `idx_stages_pipeline_name (pipeline_id, name)` | cross-run history of one stage ("what did this stage look like last week") |
| `stages` | `idx_stages_run` | all stages of one run (perf on the run FK) |
| `stages` | `idx_stages_fingerprint` | find identical stage fingerprints across runs (cache/NOOP detection) |
| `lineage_edges` | PK `(run_id, producer, consumer)` | edge identity; `(run_id, producer)` prefix serves fan-out + transitive walks (Q1, Q4) |
| `lineage_edges` | `idx_lineage_pipeline_producer (pipeline_id, producer)` | cross-run fan-out history ("everything that ever consumed this stage") |
| `lineage_edges` | `idx_lineage_pipeline_consumer (pipeline_id, consumer)` | cross-run fan-in history |
| `cross_pipeline_edges` | `idx_cpe_producer (producer_pipeline_id, producer_stage)` | catalog queries: what does this external stage feed |
| `cross_pipeline_edges` | `idx_cpe_consumer (consumer_pipeline_id, consumer_stage)` | catalog queries: what external stages does this one consume |
| `schema_snapshots` | `UNIQUE (pipeline_id, stage_name, version)` | versioned history chain |
| `schema_snapshots` | `idx_snapshots_pipeline_stage (pipeline_id, stage_name)` | latest-version-per-stage lookups (drift base) |
| `schema_snapshots` | `idx_snapshots_run` | all snapshots produced by a run |
| `schema_drifts` | `UNIQUE (run_id, producer, consumer, diff_kind)` | dedupe: one diff per edge per kind per run |
| `schema_drifts` | `idx_drifts_pipeline_edge (pipeline_id, producer, consumer)` | edge drift history (Q9) + affected-stage joins (Q1) |
| `schema_drifts` | `idx_drifts_pipeline_breaking (pipeline_id, breaking)` | CI gate: "any breaking drift in latest run" |
| `schema_drifts` | `idx_drifts_run` | per-run drift list for reports |
| `security_findings` | `UNIQUE (run_id, rule_id, path, line, col, scan_stage)` | dedupe per scan run |
| `security_findings` | `idx_findings_pipeline_sev (pipeline_id, severity, status)` | "top security findings per pipeline" (Q3) |
| `security_findings` | `idx_findings_run_scan (run_id, scan_stage)` | baseline vs regression gate comparison |
| `security_findings` | `idx_findings_rule` | per-rule stats / false-positive review |
| `sessions` | `idx_sessions_state` | list open/aborted workflows |
| `refactor_plans` | `UNIQUE (session_id, plan_hash)` | plan-file identity per session |
| `refactor_plans` | `idx_plans_status` | approved plans waiting for apply |
| `refactor_plan_items` | `UNIQUE (plan_id, item_hash)`, `UNIQUE (plan_id, change_id)` | item identity + deterministic change-id order (ORCH §4.3) |
| `refactor_plan_items` | `idx_items_state` | applied/pending/deadlettered filters |
| `refactor_plan_items` | `idx_items_hash` | idempotent re-apply check: has this item_hash been applied anywhere in the session (Q13) |
| `refactor_plan_items` | `idx_items_plan_stage (plan_id, stage)` | per-stage plan summary |
| `dry_run_previews` | `UNIQUE (plan_id, change_id)` | one preview per plan change |
| `dry_run_previews` | `idx_previews_plan` | all previews of a plan (approval rendering) |
| `metrics` | `idx_metrics_run_stage_name (run_id, stage, name)` | per-run per-stage summaries (Q11) |
| `metrics` | `idx_metrics_run_name (run_id, name)` | cross-stage metric queries (e.g. all `llm_calls`) |
| `audit` | `UNIQUE (idempotency_key)` | exactly-once audit insertion |
| `audit` | `idx_audit_session (session_id, ts)` | session timeline (ARCH §5.4) |
| `audit` | `idx_audit_run` | audit rows by run |
| `audit` | `idx_audit_action` | action-type filtering |

---

## 8. DuckDB role & interop boundary

**SQLite is the source of truth and the only schema writer** (ORCH §6.1, ADR-003). **DuckDB is an optional, read-only analytics backend** behind `store/analytics.py`; if `import duckdb` fails, the same queries run against SQLite (ARCH §3, P1/P2).

- **What moves to DuckDB** (via `oarl export-events --duckdb <file>`, ORCH §9.4, plus a `--metrics` export): the analytical projection of `events`, `metrics`, `schema_drifts`, `security_findings`, `stage_instances`, `runs` — i.e. everything a cross-run analytics dashboard needs. Exported columns are the same names/types as the SQLite tables (a documented export schema), so one query body runs on both backends.
- **What stays in SQLite**: all transactional state, all point lookups, everything a CLI command needs synchronously (session state, plan items, applied state, audit insertion). The core engine's deterministic path **never** touches DuckDB.
- **Interop boundary**: a one-way ETL at export time (SQLite → DuckDB file). DuckDB never writes to the `.refactor/refactor.db`; it cannot open the WAL database concurrently (DuckDB would need the sqlite extension, which is not stdlib — out of scope). Consumers of the export get a self-contained `.duckdb` file for ad-hoc analytics.
- **Why**: (a) SQLite's single-writer + WAL design is optimized for small transactional workloads; long analytical scans over the event stream would contend with the orchestrator's writes. (b) DuckDB's columnar engine makes scan-heavy aggregates (p95 latency over 10k events, failure rates across 500 runs, top findings) orders of magnitude cheaper. (c) Keeping DuckDB optional preserves the zero-cost, stdlib-only core (P2) — the export is a convenience, not a dependency.

The analytics interface contract: `analytics.run(sql, params)` returns rows on whichever backend is available; queries are written once against the export schema and executed on SQLite when DuckDB is absent.

---

## 9. Dry-run & preview storage

`dry_run` (ORCH §4.3) renders per-change previews read-only; the **approval artifact** is the plan + its previews (ADR-005: "dry-run output == apply input"). Storage is split:

- **On disk** (content-addressed): `diffs/<change_id>.patch` under `.refactor/jobs/<run_id>/` (ORCH §4.3), referenced by `patch_artifact`.
- **In DB** (`dry_run_previews`): queryable metadata + the preview itself (diff text or sampled rows), so `driftguard refactor dry-run` history and approval rendering don't need to re-read artifacts.

**Full vs sampled:**

| `sample_strategy` | `diff_patch` | `sample_rows_json` | When |
|---|---|---|---|
| `full` | unified diff text | NULL | small files (default under a size cap, e.g. 64 KiB rendered) |
| `sample` | NULL | redacted sampled rows | large previews; sampling is **deterministic** — `random.Random(seed=preview_hash)` picks rows (R6), capped by `dry_run.sample_rows` (default 100) |

Both variants store `old_hash`/`new_hash` (the CAS inputs, ORCH §6.4) and `preview_hash` (`sha256` of the canonical preview content). `preview_hash` gives **idempotent re-dry-run** (re-rendering the same plan produces the same hash, so the approval can be re-verified) and **stale-preview rejection** (`apply` re-checks that the preview rows still match the plan + workspace hashes).

Redaction (P6/PRD §Security): every preview byte — `diff_patch`, `sample_rows_json` — passes through `security.redact` before persist. A preview never contains a raw secret; the scanner's `snippet_redacted` discipline extends to all rendered output.

Preview lifecycle: `refactor_plans.status` tracks `proposed → approved → applied | partially_applied | rejected`; a rejected/aborted workflow keeps its previews (audit trail) — rows are never deleted by the writer API.

---

## 10. Audit trail & idempotency

### 10.1 Who/what/when/input-hash/result-hash

| Requirement | Where it lives |
|---|---|
| who | `audit.actor` (`cli` \| `human` \| `plugin` \| `llm`) + `audit.session_id`/`run_id` |
| what | `audit.action` + `from_state`/`to_state`; `events.kind` for orchestrator transitions |
| when | `audit.ts` / `events.ts` — UTC ISO-8601, audit-only (R3) |
| input hash | `events.input_hash`, `audit.args_json` (redacted inputs: plan hash, rule ids, thresholds) |
| result hash | `events.output_hash` + `artifact`, `audit.result_json` (counts, exit_code, fingerprints) |

### 10.2 Immutability

`events` and `audit` are append-only at three layers:

1. **Triggers** (§4.9) reject any `UPDATE`/`DELETE` — including from interactive SQL.
2. **Writer API** exposes insert-only methods; no code path issues DML on these tables.
3. **Retention** (`oarl prune`, ORCH §9.4) is a checkpoint rebuild (copy to a new DB omitting old rows, then atomic replace) — a new snapshot, never a mutation. Default retention 90 days, never implicit.

### 10.3 Exactly-once idempotency keys

- **`events.event_id`** is deterministic: `f"{run_id}:{stage}:{attempt}:{seq}"` (ORCH §9.1; run-level events use `stage='run'`, `attempt=1`). Combined with `INSERT OR IGNORE`, a crashed engine that replays a transition re-emits the **same** id and is deduplicated — exactly-once audit insertion under at-least-once execution.
- **`audit.idempotency_key`** mirrors the triggering `event_id` when one exists; otherwise it is computed deterministically as `f"{session_id}:{action}:{n}"` where `n` is the next integer in the session's action sequence. `UNIQUE` + `INSERT OR IGNORE` gives the same guarantee for the domain-level log.

### 10.4 Transaction discipline

State-machine rows and their event/audit rows commit **in the same transaction** (ORCH §9.3): `stage_instances` update + `events` insert + (`audit` insert where a session transition fires) are one commit. If the process dies between the state write and the event write, recovery reconstructs the missing events on resume — deduplicated by `event_id`. Every CLI operation that touches a session writes ≥ 1 `audit` row in the same transaction as the state change (ARCH §5.4); an interrupted operation leaves the session in its prior state with an `ABORT` row.

---

## 11. Migration strategy

### 11.1 Versioning

- `meta['schema_version']` is **authoritative** (ORCH §6.1). `PRAGMA user_version` is mirrored as belt-and-braces (survives even if `meta` is tampered with).
- `schema_migrations` records each applied migration: `version` (1-based), `name`, `applied_at` (UTC), `checksum` (sha256 of the migration script text — tamper evidence), `duration_ms`.

### 11.2 Migration runner (`store/db.py`)

On open: read `meta['schema_version']` (absent ⇒ 0) and `PRAGMA user_version`; assert they agree (mismatch = corrupted DB, fail loudly). Apply pending migrations in order, **each in its own transaction**; after each, write its `schema_migrations` row and update `meta['schema_version']` + `PRAGMA user_version` in the same transaction. No auto-downgrade.

### 11.3 Forward-compatible evolution rules (normative)

1. **Append-only DDL**: new migrations may only `CREATE TABLE`/`CREATE INDEX`/`ADD COLUMN`; never `DROP`, `RENAME`, or change column types.
2. **`ADD COLUMN` guards**: SQLite lacks `ADD COLUMN IF NOT EXISTS`; the runner checks `PRAGMA table_info(<table>)` and skips existing columns. New columns are nullable or have a `DEFAULT`.
3. **Semantic evolution goes into JSON, not DDL**: IR (`{"v": 1}` envelope), plan (`"driftguard.plan.v1"`), and snapshot column records are versioned documents — parser/rule/schema-semantic changes bump the *envelope* version, never the table shape.
4. **Deterministic migrations**: scripts contain no wall-clock reads, no randomness, no env dependence (they may embed fixed UTC constants where a seed value is needed).
5. **Never migrate data destructively in-place**: value transformations (e.g. seed `runs.id` → `run_id`) are computed into new rows in the same transaction, with the old rows left for one release then dropped by a later additive migration + documented cleanup.

### 11.4 Seed → target mapping (migration 2)

ORCHESTRATION §11.3 supersedes the Phase-0 skeleton (`driftguard/store.py`): the drift-check run becomes the `analyze` stage's `schema_drift` analyzer. The mapping:

| Seed table (ARCH §3.1 / `store.py`) | Target |
|---|---|
| `runs.id INTEGER` | `runs.run_id TEXT` — migrated as `'legacy-' || id` |
| `stages (name PK, global)` | `stages` per `(run_id, name)`; `pipeline_id` upserted from `workspace_root` |
| `lineage_edges (producer, consumer)` | `lineage_edges (run_id, producer, consumer)` re-pointed at migrated run ids |
| `drifts` | `schema_drifts` (`added_json`/`removed_json`/`renamed_json` preserved; `changed_json` empty; `diff_kind='producer_consumer'`) |
| `sessions` | `sessions` (unchanged shape, gains `run_id` link) |
| `audit` | `audit` (+ `idempotency_key`, backfilled `'legacy:' || audit_id`) |
| `rewrites` | `refactor_plan_items` with `state='applied'` (plan context synthesized into a `refactor_plans` row per session) |
| `scans` | `security_findings` (`scan_stage='analyze'`, `status='open'`) |
| `schema_version` | `meta['schema_version']` + `schema_migrations` |

`oarl migrate` runs pending migrations; `oarl checkpoint` snapshots the DB + journal (ORCH §11.3) before a migration in interactive use.

---

## 12. Example queries

These are the queries the schema is designed around. Parameters use `:name` bindings.

**Q1 — What breaks if stage `:changed_stage` changes in run `:run_id`?** (transitive consumers + breaking drift along each edge into them)

```sql
WITH RECURSIVE downstream(producer, consumer, depth) AS (
    SELECT producer, consumer, 1
      FROM lineage_edges
     WHERE run_id = :run_id AND producer = :changed_stage
    UNION ALL
    SELECT d.consumer, le.consumer, d.depth + 1
      FROM lineage_edges le
      JOIN downstream d ON le.producer = d.consumer AND le.run_id = :run_id
     WHERE d.depth < 32
)
SELECT d.depth,
       d.consumer AS affected_stage,
       s.path,
       COALESCE(sd.breaking, 0)        AS has_breaking_drift,
       COALESCE(sd.diff_kind, 'none')  AS diff_kind
  FROM downstream d
  JOIN stages s ON s.run_id = :run_id AND s.name = d.consumer
  LEFT JOIN schema_drifts sd
         ON sd.run_id = :run_id
        AND sd.producer = d.producer
        AND sd.consumer = d.consumer
 ORDER BY d.depth, d.consumer;
```

Uses: `lineage_edges` PK prefix `(run_id, producer)` and `idx_drifts_pipeline_edge`.

**Q2 — Latest successful run per pipeline** (history anchor)

```sql
SELECT r.pipeline_id, r.run_id, r.created_at, r.exit_code, r.snapshot_hash
  FROM runs r
  JOIN (SELECT pipeline_id, MAX(created_at) AS latest
          FROM runs
         WHERE status = 'SUCCEEDED'
         GROUP BY pipeline_id) m
    ON m.pipeline_id = r.pipeline_id AND m.latest = r.created_at;
```

Uses: `idx_runs_pipeline_created`.

**Q3 — Top security findings per pipeline (latest run, open only)**

```sql
SELECT f.severity, f.rule_id, COUNT(*) AS open_findings
  FROM security_findings f
  JOIN runs r ON r.run_id = f.run_id
 WHERE f.pipeline_id = :pipeline_id
   AND f.status = 'open'
   AND r.created_at = (SELECT MAX(created_at) FROM runs
                        WHERE pipeline_id = :pipeline_id)
 GROUP BY f.severity, f.rule_id
 ORDER BY CASE f.severity
          WHEN 'critical' THEN 0 WHEN 'high' THEN 1
          WHEN 'medium' THEN 2 ELSE 3 END,
          open_findings DESC;
```

Uses: `idx_findings_pipeline_sev`, `idx_runs_pipeline_created`.

**Q4 — Lineage path between two stages in a run** (shortest downstream route)

```sql
WITH RECURSIVE path(producer, consumer, depth, route) AS (
    SELECT producer, consumer, 1, producer || ' -> ' || consumer
      FROM lineage_edges
     WHERE run_id = :run_id AND producer = :from_stage
    UNION ALL
    SELECT p.consumer, le.consumer, p.depth + 1, p.route || ' -> ' || le.consumer
      FROM lineage_edges le
      JOIN path p ON le.producer = p.consumer AND le.run_id = :run_id
     WHERE p.depth < 32 AND le.consumer <> :from_stage
)
SELECT depth, route
  FROM path
 WHERE consumer = :to_stage
 ORDER BY depth
 LIMIT 1;
```

**Q5 — Fan-in of a stage** (all producers)

```sql
SELECT le.producer, le.kind, le.expected_columns_json
  FROM lineage_edges le
 WHERE le.run_id = :run_id AND le.consumer = :stage
 ORDER BY le.producer;
```

**Q6 — Fan-out of a stage** (all direct consumers)

```sql
SELECT le.consumer, le.kind
  FROM lineage_edges le
 WHERE le.run_id = :run_id AND le.producer = :stage
 ORDER BY le.consumer;
```

**Q7 — Resumable interrupted runs** (SUSPENDED/RUNNING runs with unfinished stage instances)

```sql
SELECT r.run_id, r.status AS run_status, r.snapshot_hash, r.created_at,
       SUM(CASE WHEN si.status IN ('RUNNING','INTERRUPTED') THEN 1 ELSE 0 END) AS interrupted_stages,
       SUM(CASE WHEN si.status = 'PENDING' THEN 1 ELSE 0 END)                  AS pending_stages
  FROM runs r
  JOIN stage_instances si ON si.run_id = r.run_id
 WHERE r.status IN ('RUNNING','SUSPENDED')
 GROUP BY r.run_id
HAVING interrupted_stages + pending_stages > 0
 ORDER BY r.created_at DESC;
```

Uses: `idx_runs_status`, `idx_stage_instances_run_status`. (Recovery then classifies each unfinished instance per ORCH §6.3.)

**Q8 — Retryable stages past their backoff deadline** (dispatch loop)

```sql
SELECT si.run_id, si.stage, si.attempt, si.retry_after, si.error_json
  FROM stage_instances si
 WHERE si.status = 'FAILED'
   AND si.retryable = 1
   AND si.attempt < si.max_attempts
   AND si.retry_after IS NOT NULL
   AND si.retry_after <= :now_utc
 ORDER BY si.retry_after;
```

Uses: `idx_stage_instances_retry_after` (partial index).

**Q9 — Schema drift history for one edge across runs**

```sql
SELECT r.created_at AS run_at, sd.diff_kind, sd.breaking,
       sd.added_json, sd.removed_json, sd.changed_json, sd.renamed_json
  FROM schema_drifts sd
  JOIN runs r ON r.run_id = sd.run_id
 WHERE sd.pipeline_id = :pipeline_id
   AND sd.producer = :producer
   AND sd.consumer = :consumer
 ORDER BY r.created_at DESC
 LIMIT 20;
```

**Q10 — Determinism violation check** (same input_hash, different output_hash — ORCH §5.2)

```sql
SELECT si.stage, si.input_hash,
       COUNT(DISTINCT si.output_hash) AS distinct_outputs
  FROM stage_instances si
 WHERE si.output_hash IS NOT NULL
 GROUP BY si.stage, si.input_hash
HAVING COUNT(DISTINCT si.output_hash) > 1;
```

**Q11 — p95 stage latency for a run** (metrics hook aggregate)

```sql
WITH ranked AS (
    SELECT stage, value,
           ROW_NUMBER() OVER (PARTITION BY stage ORDER BY value) AS rn,
           COUNT(*) OVER (PARTITION BY stage) AS cnt
      FROM metrics
     WHERE run_id = :run_id AND name = 'duration_ms'
)
SELECT stage, MAX(value) AS p95_ms, MAX(cnt) AS samples
  FROM ranked
 WHERE rn <= MAX(1, CAST(0.95 * cnt AS INTEGER))
 GROUP BY stage
 ORDER BY stage;
```

Uses: `idx_metrics_run_stage_name`.

**Q12 — Dead letters per run** (failure-isolation report, ORCH §8.4)

```sql
SELECT dl.stage, dl.entity, dl.error_type, dl.attempt, dl.payload_hash, dl.created_at
  FROM dead_letters dl
 WHERE dl.run_id = :run_id
 ORDER BY dl.id;
```

**Q13 — Idempotent re-apply guard** (has `:item_hash` already been applied in `:session_id`? — ARCH §5.2)

```sql
SELECT i.plan_id, i.item_hash, i.state, i.fingerprint_before, i.fingerprint_after, i.applied_at
  FROM refactor_plan_items i
  JOIN refactor_plans p ON p.plan_id = i.plan_id
 WHERE p.session_id = :session_id
   AND i.item_hash = :item_hash
   AND i.state = 'applied';
```

Uses: `idx_items_hash`. Non-empty result ⇒ re-apply is a NOOP (exit 0 with warning).

**Q14 — Security regression gate at verify** (new findings introduced by the rewrite)

```sql
SELECT f.rule_id, f.severity, f.path, f.line, f.col, f.snippet_redacted
  FROM security_findings f
 WHERE f.run_id = :verify_run_id
   AND f.scan_stage = 'verify'
   AND f.status = 'open'
   AND f.severity IN ('critical','high')
   AND NOT EXISTS (
       SELECT 1 FROM security_findings b
        WHERE b.run_id = :baseline_run_id
          AND b.scan_stage = 'analyze'
          AND b.rule_id = f.rule_id
          AND b.path = f.path
          AND b.line = f.line
          AND b.col = f.col);
```

The ADR-007 gate: any row here at severity ≥ `--fail-on-severity` fails `verify` (exit 1).

---

## 13. PRD feature traceability

| PRD feature / requirement | Tables that host it |
|---|---|
| Pipeline parsing & introspection | `stages` (`ir_json`, `columns_json`, `diagnostics_json`, `dialect_hints_json`), `pipelines`, `artifacts` |
| Data lineage tracking across stages | `lineage_edges`, `cross_pipeline_edges`, `stages` |
| Schema drift detection with diffs | `schema_snapshots`, `schema_drifts` (added/removed/changed/renamed, `breaking`) |
| Dry-run transformations with preview output | `dry_run_previews`, `refactor_plans`, `refactor_plan_items` |
| Failure isolation & retry policies | `stage_instances` (attempt/retryable/max_attempts/retry_after), `dead_letters`, `runs.status` |
| Metrics/observability hooks per stage | `metrics`, `events` (duration_ms per event) |
| Local LLM suggestions via Ollama | `refactor_plan_items` (`rule_id LIKE 'LLM-%'`), `sessions.llm_used`, `audit.actor='llm'`, `events` (`llm_call`/`llm_suggestion_rejected`) |
| Security findings (scanners + gate) | `security_findings` (`scan_stage` baseline/regression, severity, span, status), `refactor_plan_items.blocked_by_finding` |
| Refactor plans + applied state | `refactor_plans`, `refactor_plan_items` (item_hash, state, fingerprints) |
| Audit trail / idempotency | `events` (exactly-once), `audit` (append-only), triggers, `artifacts`, `meta` |
| Optional DuckDB analytics | export view over `events`/`metrics`/`schema_drifts`/`security_findings`/`stage_instances`/`runs` (§8) |
| Crash-resume (PRD §Architecture "every operation is idempotent") | `runs.status`, `stage_instances`, `events`, `artifacts`, `refactor_plan_items.state` (Q7/Q8/Q13) |

---

## 14. Open seams & non-goals

- **Journal/events.jsonl** are filesystem mirrors by design (ORCH §6.1B) — this document deliberately does **not** model them as tables; they are written by the orchestrator, never read as the source of truth.
- **DuckDB writes** are out of scope; the export is one-way (§8).
- **Multi-workspace catalog edges** (`cross_pipeline_edges`) are schema-ready but unpopulated until the Phase-4 managed catalog.
- **STRICT tables** (SQLite 3.37+): the canonical DDL above deliberately omits `STRICT` for maximum compatibility with every SQLite bundled with Python 3.11+; the writer enforces types in Python, and `STRICT` can be enabled in a future migration if the minimum SQLite version is raised.

*End of data model & storage design. Normative: §2 (pragma discipline), §4 (DDL), §5 (lineage invariants), §6.3 (breaking policy), §10 (audit/idempotency), §11.3 (migration rules).*