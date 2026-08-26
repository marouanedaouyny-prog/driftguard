# Zero-Cost & Performance Optimization Strategy

**Project:** Security-Aware Refactoring Assistant for data pipelines (MVP: DriftGuard)
**Status:** Ratified strategy — every budget below is implementable as a config constant (canonical block in §8)
**Constraint:** The tool must run entirely locally at **$0 infrastructure**. Freemium + Pro ($10–50/user/mo) monetization exists for conveniences only — the core (parse → lineage → drift detection → refactor suggestions) must never depend on a paid service.

---

## 0. Compliance statement (PRD cross-check)

This document is the operational implementation of PRD §"Zero-Cost Strategy" ("Default: SQLite + local models + GitHub Actions + free hosting") and PRD §"Zero-Cost Analysis" (`feasible: True; estimated infra: $0/month — green flags: runs locally`). It does not contradict the PRD:

| PRD statement | Implementation in this strategy |
|---|---|
| Zero-cost strategy: SQLite + local models + GitHub Actions + free hosting | §1 cost model; §2.3/§2.5 (SQLite); §3 (local Ollama); §5 (GitHub Actions); §1.1 C3/C6 (free hosting) |
| Infra stays at ~$0/month at MVP scale (Success Criteria) | §7: monthly infra bill is a KPI; hard target $0.00 |
| Optional local LLM (Ollama) — Phase 4 | §3.1: strictly optional, never default-on, deterministic fallback |
| SQLite + DuckDB for analytics | §2.5: DuckDB promotion trigger at SQLite size thresholds |
| No external secrets; optional read-only GitHub token; cleaned env; sandboxed paths; redacted logs | §3.6 (redaction), §4 (sandboxed resource guardrails) |
| Freemium + pro ($10–50/user/mo) | §6.3: paid tier = conveniences only (managed catalog, SSO), never core |
| Non-MVP: Web UI, team collab, enterprise SSO, plugin marketplace | §6.3: those land in Pro; the CLI stays complete and free |

---

## 1. Cost model — every dimension, and how each stays at $0

### 1.1 Cost dimensions ledger

| # | Dimension | Where it runs | Mechanism that keeps it $0 | Monthly cost |
|---|---|---|---|---|
| C1 | Compute — user runs | Developer machine / team runner | CLI executes locally (`python -m driftguard`); no daemon, no cloud call. CPU is the user's own. | $0 |
| C2 | Compute — CI | GitHub Actions | Free tier: **2,000 min/month** (private repos); **unlimited for public repos**. §5 fits the whole pipeline inside the budget with ≈ 14% headroom at the worst case (§5.3). | $0 |
| C3 | Compute — hosted server mode (optional, Phase 4) | User's own VM / Docker | Self-hosted by the customer; we ship a Docker image, they pay their own host. No managed control plane. | $0 |
| C4 | Storage — run history | Local SQLite (`driftguard.db`) | Embedded, file-based. Growth target < 2 MB per 1,000 stages (§2.5). User's disk, not ours. | $0 |
| C5 | Storage — analytics | Local SQLite → optional local DuckDB | DuckDB is also embedded and local (§2.5). Promotion trigger: SQLite > 500 MB or runs > 1M rows. | $0 |
| C6 | Storage — docs/website | GitHub Pages | Free static hosting (1 GB soft limit, 100 MB/file). Docs are pure markdown + one small theme; keep total < 50 MB. | $0 |
| C7 | LLM inference | User's machine via Ollama | Local inference only. `llm.provider` accepts only `ollama`; a cloud provider is a hard config error (§3.1). Tokens cost $0; *wall-clock and RAM are the real budget* → §3 caps. | $0 |
| C8 | CI minutes burn | GitHub Actions | §5: matrix policy, path filters, concurrency cancellation, caching. Worst case (400 PRs/mo, private repo) = **≈ 1,716 min ≤ 2,000**. | $0 |
| C9 | Distribution | PyPI + GitHub Releases | Both free. No CDN, no LFS (binary must stay < 10 MB via PyInstaller single-file; if it grows, split docs out). | $0 |
| C10 | Monitoring / observability | None external | Local logging into SQLite only. No SaaS APM, no error-tracking SaaS, no telemetry beacon. `--telemetry` flag exists but is `off` by default and only writes local JSON. | $0 |
| C11 | Secrets / credential infra | None | No external secrets by design (PRD §Security). Optional read-only GitHub token used only for private-source enrichment; never required. | $0 |
| C12 | Domain name (optional) | DNS | The **only** non-$0 line item in existence. Optional; GitHub Pages serves under `*.github.io` for free. If acquired: ~$10–15/yr ≈ $1/mo. It is not infrastructure and does not violate the $0-infra criterion. | $0–$1 |

### 1.2 Budget caps and guardrails — free core

These are hard constants (canonical in §8). When a cap trips, the behavior is deterministic (see §4.3 exit codes) — never a silent partial run:

| Cap | Value | Behavior on breach |
|---|---|---|
| `resources.max_files` | 10,000 | Scan stops; run exits **5** (guardrail) with a report of what was skipped |
| `resources.max_file_bytes` | 10 MB | File skipped with warning (listed in report) |
| `resources.max_total_bytes` | 250 MB | Scan stops; exit **5** |
| `resources.max_depth` | 32 | Deeper paths ignored (also bounds symlink loops) |
| `resources.max_stages` | 50,000 | Lineage/cycle analysis aborts; exit **5** |
| `resources.watchdog_seconds` | 120 | Hard timeout for the whole run; exit **5** |
| `resources.max_memory_mb` | 256 | RSS ceiling; process aborts with exit **5** (see §4.1) |
| `run.batch_size` | 1,000 | Stages processed per batch when `--incremental`; bounds peak memory |

### 1.3 Budget caps and guardrails — Pro tier (managed catalog, Phase 4)

Pro is *hosting convenience*, so it is the only place with real money flows. The guardrails below are contractual, not aspirational:

| Cap | Value | Behavior on breach |
|---|---|---|
| Compute per analysis job | 30 min hard ceiling | Job killed; user charged nothing (fixed tier, no metering) |
| Concurrent jobs per org | 5 | Queue, never burst |
| Storage per project | 1 GB | Writes blocked + alert at 80% |
| Storage per org | 10 GB | Writes blocked + alert at 80% |
| API rate limit | 60 req/min/user, 10,000 req/day | HTTP 429; retry-after honored |
| Billing model | Fixed tiers only ($10 / $30 / $50 per user/mo) | **No usage-based billing in v1** — no surprise overages possible |
| Self-serve spend ceiling | N/A (fixed) | If a future usage-metered feature ships, it requires an explicit user-set monthly cap, defaults to $0 (off) |
| Data egress | N/A | Managed catalog stores only metadata (schemas, run summaries, PRD-grade docs); source code never leaves the customer |

**Guardrail principle:** the Pro control plane can *never* gate a core CLI feature. A Pro account expiring must degrade to "local CLI mode" — everything still works, minus hosted conveniences (§6.3).

---

## 2. Performance architecture

### 2.1 Performance budgets (SLOs, measured in CI + KPI reports)

Budgets are per-operation and must be enforced by tests (a `tests/bench` suite runs in CI nightly, §5.2):

| Operation | Workload | Budget (p50) | Budget (p95) | Hard ceiling | Config key |
|---|---|---|---|---|---|
| Parse pipeline | 1,000 SQL stages (large dbt project) | < 2.0 s | < 5.0 s | 30 s (watchdog segment) | `perf.parse_p50_s` |
| Parse single file | any one `*.sql` | ≤ 50 ms mean | — | 500 ms per file | `perf.parse_file_max_ms` |
| Build lineage + cycle detection | 1,000 stages / 5,000 edges | < 0.5 s | < 1.5 s | 5 s | `perf.lineage_max_s` |
| Drift detection | 1,000 producer/consumer pairs | < 0.5 s | < 1.0 s | 3 s | `perf.drift_max_s` |
| Full analyze | 10k-line codebase (≈100 files × 100 lines) | < 3.0 s | < 10.0 s | 120 s watchdog | `perf.analyze_p95_s` |
| Incremental re-analyze | same codebase, 5 files changed | < 1.0 s | < 2.0 s | 30 s | `perf.incremental_p95_s` |
| Report generation (text/json/markdown) | 1,000 stages | < 0.5 s | < 1.0 s | 3 s | `perf.report_max_s` |
| SQLite persist of a run | 1,000 stages + edges + drifts | < 0.5 s | < 1.0 s | 3 s | `perf.persist_max_s` |
| **Memory ceiling (whole process)** | any workload | p95 < 150 MB | — | **256 MB RSS** | `resources.max_memory_mb` |
| **End-to-end CI gate job** | default repo | < 1.5 min | < 3 min | — | §5 |

**Rationale:** regex-based parsing of SQL (the current MVP approach) is single-digit MB/s; 1,000 files × ~50 KB = ~50 MB of text ⇒ well under 2 s. The p95 headroom absorbs cold disk cache and antivirus interference on Windows. The watchdog segment values exist so a pathological file cannot monopolize a run.

### 2.2 Lazy evaluation strategy

1. **Parse lazily, analyze eagerly.** `parse_pipeline` only reads metadata (stem, refs via regex on first pass). The raw SQL text (`Stage.raw`) is **not retained** after parsing unless a consumer needs it (LLM suggestions, refactor previews) — it is dropped from memory and re-read on demand from disk. For 1,000 stages this saves ~50–200 MB.
2. **Lineage on demand.** The edge matrix is materialized only for stages that have refs; isolated files never enter the graph.
3. **Column projection on demand.** `_select_projection` (the most expensive regex pass) runs only for stages that are *producers* in the drift graph or that changed in an incremental run — not for every file up front.
4. **Report views lazy.** Text/markdown/json render from the same in-memory model; no intermediate files; `--json` skips building the human reports entirely (already true in `__main__.py` — keep it).
5. **SQLite writes batched.** One transaction per run batch (1,000 stages), not per-file autocommits. WAL mode (`journal_mode=WAL`), `synchronous=NORMAL`, `busy_timeout=5000` — a CI crash mid-write cannot corrupt or wedge the DB (existing `Store` must adopt these pragmas in Phase 1).

### 2.3 Caching — SHA-based change detection & incremental analysis

The cache lives in SQLite (zero new infrastructure):

1. **Content-addressed stages.** Every stage gets `cache_key = sha256(parser_version ‖ file_sha256)`. The parser version string is bumped on any grammar/regex change — a cache-key change invalidates everything automatically.
2. **`stage_versions` table** (id, stage_name, path, sha256, parser_version, parsed_at, columns_json, refs_json). On a run:
   - File's `sha256` matches stored AND `parser_version` matches ⇒ **reuse cached columns/refs, skip reparse**.
   - Changed ⇒ reparse only that file, update row.
3. **Downstream closure.** Drift detection must always run on the full graph (drift is a consumer-side property), but it runs on *cached* column metadata — O(edges) dict lookups, not re-parsing. Only changed producers feed fresh metadata into the graph.
4. **Incremental mode:** `--incremental` (auto-enabled when `--db` is given and `--no-persist` is absent). Full scan of file mtimes/SHAs (fast `os.stat` walk, ~10k files/s) + reparse of changed files only.
5. **Cache hit KPI:** ≥ 70% hit rate on incremental runs (measured per run, recorded in SQLite `runs.cache_hit_rate`). If the repo churns > 30% per run, the mode self-disables and logs "incremental useless — full scan" (a mini circuit breaker).
6. **Failure policy:** cache rows are advisory. A corrupted or missing DB ⇒ full reparse, never a crash. Cache writes are fire-and-forget within the batch transaction; a failed persist does not fail the analysis.

### 2.4 Parallelism limits

| Setting | Value | Why |
|---|---|---|
| `perf.workers` | `min(4, os.cpu_count())` | Regex parsing is CPU-bound but each worker costs ~10–20 MB RSS + one open file handle. 4 workers bounds memory at ~80 MB while giving ~3× speedup on 4+ core machines. |
| Worker activation threshold | ≥ 200 files | Below 200 files, process-spawn overhead (~50–100 ms) exceeds the win; parse single-threaded. |
| Worker model | `ProcessPoolExecutor` (never threads — GIL makes threads pointless for regex) | stdlib only. |
| LLM inference concurrency | **1** (strictly serial, §3.2) | Ollama already saturates CPU/GPU; parallel requests only add queue latency. |
| I/O parallelism | 0 (single-threaded `rglob` + sequential reads) | Disk is not the bottleneck at these sizes; keeping I/O serial makes the watchdog deterministic. |
| SQLite concurrency | 1 writer (WAL allows readers) | `busy_timeout=5000`; the CLI is the only writer by design. |

### 2.5 Storage lifecycle

- **Growth target:** < 2 MB per 1,000 stages persisted (columns/refs are text; drifts are tiny). A 5,000-stage monorepo ⇒ ~10 MB/yr of history. No cleanup needed at MVP scale.
- **Retention:** `runs` table rows are summarized after 90 days (keep `run_id`, counts, exit code; drop per-drift rows) — `store.retention_days = 90`, a config constant, not a mystery cron.
- **DuckDB promotion trigger** (PRD lists DuckDB for analytics): when SQLite exceeds **500 MB** or `runs` exceeds **1,000,000 rows**, the CLI offers (never forces) `driftguard export --duckdb runs.duckdb` — a read-only analytics mirror. DuckDB is embedded and local, so the $0 invariant holds; it is an analytics escape hatch, not a dependency of the core.
- **`VACUUM` policy:** auto-`VACUUM` when DB > 50 MB and free pages > 30% (checked at run end, once per day max).

### 2.6 Performance test enforcement

- `tests/bench/test_perf.py` runs nightly in CI (§5.2): generates synthetic corpora (1k / 5k / 10k-line) and asserts every p50/p95 budget above with a 2× CI-slack multiplier (CI runners are slower than dev machines).
- Budget regressions fail the nightly job → alert via GitHub issue comment (no paid alerting).
- The bench suite itself is included in the CI-minute budget (§5.3 table).

---

## 3. LLM cost control (optional Ollama enrichment — Phase 4)

### 3.1 Gating rules — never default-on

1. **Feature flag:** `llm.enabled = false` in config; CLI opt-in per run via `--llm` flag. Absent flag + absent config ⇒ no LLM code path is even imported (import-time cost = 0).
2. **Provider lock:** `llm.provider` accepts only `ollama`. Any other value (e.g., `openai`, `anthropic`, `gemini`) is a **hard configuration error at startup** (exit 3 — config error, §4.3). The core must not contain credentials plumbing for cloud LLMs — matching PRD §Security ("no external secrets required").
3. **Local-only discovery:** the Ollama endpoint defaults to `http://127.0.0.1:11434`; configurable, but the CLI refuses non-loopback hosts unless `--llm-allow-remote` is passed with an explicit warning (users can point at a LAN Ollama on a team machine — still zero marginal cost).
4. **Model default:** `llm.model = "qwen2.5:7b"` (or a similarly small local model); docs recommend 7B-class models. Larger models are the user's choice but do not change the budget math (they cost wall-clock, not money).

### 3.2 Prompt & token budgets (constants)

| Budget | Value | Enforced by |
|---|---|---|
| `llm.max_prompt_chars` | 8,000 chars | Truncation before send (never send a whole file: only the SELECT projection + refs + drift diff, i.e., the *delta*) |
| `llm.max_tokens` | 512 output tokens | Passed as Ollama `num_predict` |
| `llm.max_context_chars` | 24,000 chars | Per-request context (file excerpt + 2–5 surrounding stages) |
| `llm.max_requests_per_run` | 20 | Global per-run cap, hard |
| `llm.max_files_per_batch` | 5 | One prompt can cover at most 5 related stages (a refactor suggestion is local by design) |
| `llm.timeout_s` | 30 s per request | Abort + count as failure (feeds circuit breaker) |
| `llm.health_check_s` | 2 s | `GET /api/tags` at startup; miss ⇒ graceful degradation (§3.3), zero LLM attempts |
| `llm.concurrency` | 1 | Strictly serial (see §2.4) |

Token accounting: every response records `prompt_eval_count` / `eval_count` from Ollama's response into SQLite `llm_usage` (run_id, model, prompt_tokens, completion_tokens, duration_ms, ok). This is the local equivalent of cloud spend tracking — **cost-per-1M-tokens is $0, so the tracked currency is wall-clock and request count**, and both are capped.

### 3.3 Graceful degradation ladder (deterministic, 100% free fallback)

The tool's *semantics never depend on the LLM*. Exit codes, drift verdicts, lineage, and security findings are computed by deterministic heuristics; the LLM only *adds suggestion text* to reports.

| Condition | Behavior |
|---|---|
| No Ollama installed / not running | Health check fails (2 s) → run proceeds in **deterministic heuristic mode**; report notes "LLM suggestions disabled (no local model detected)"; exit code unaffected |
| Ollama reachable, model missing | `pull` is never attempted automatically (it downloads GBs); report says which model to install; heuristic mode |
| Request timeout / HTTP error | Count failure; retry up to `llm.max_retries = 2` with 1 s backoff; then circuit breaker (§3.4) |
| Token/prompt cap exceeded | Prompt truncated before send — cap is pre-enforced, never post-hoc |
| Any LLM failure at any point | Analysis output already computed; LLM section of the report is omitted; **exit code identical** to the no-LLM run |

**Invariant (must be tested):** for any corpus, `driftguard run --llm` and `driftguard run` (no LLM) return the **same exit code** and the same drift verdicts. LLM output can only ever add text, never change decisions.

### 3.4 Circuit breaker

```
state: closed → (3 consecutive failures) → open (remaining run) → closed (next run)
```

- **Trip:** 3 consecutive request failures (timeout, 5xx, malformed response).
- **Open behavior:** all further `--llm` requests for this run are skipped immediately (zero wall-clock burn); report notes "LLM disabled after 3 failures (circuit open)".
- **Reset:** next CLI invocation starts closed (per-run state, persisted counters in SQLite `llm_usage` for observability).
- **Why per-run:** a dead model today may be pulled tomorrow; a per-run breaker prevents runaway retry loops inside one CI job while keeping the tool permanently recoverable.

### 3.5 What the LLM is allowed to touch

- Allowed: refactor suggestions, naming improvements, "why is this drift risky" prose, docstring/comment drafts.
- Forbidden (heuristics only, always): drift verdicts, breaking/renamed classification, lineage edges, cycle detection, security severity scores, exit codes.
- The security scanner (Phase 2) is 100% rule-based; the LLM never performs the security check itself — it may only explain a finding already produced by rules.

### 3.6 Privacy guardrails (matching PRD §Security)

- Prompts are built from the local repo excerpt only — **nothing leaves the machine** (loopback Ollama).
- Subprocess/LLM calls run with the same cleaned environment as every other subprocess (`clean_env`), and any token present in a prompt is redacted before logging (redact rules from PRD §Security: `sk-`, `ghp_`, `AKIA`, `xoxb`, private-key, `key=value`).
- If a user ever points at a remote Ollama (`--llm-allow-remote`), the CLI prints an explicit warning that repo content will traverse the network; it is opt-in per run, never persisted as default.

---

## 4. Automation guardrails — runaway operations must be impossible

### 4.1 Resource limits (hard constants — canonical in §8)

| Constant | Default | Enforcement point |
|---|---|---|
| `resources.max_files` | 10,000 | During `rglob` scan; counting stops the walk |
| `resources.max_file_bytes` | 10 MB | Before `read_text` — a bigger file is skipped with a warning (regex on 100 MB of SQL is the #1 hang vector) |
| `resources.max_total_bytes` | 250 MB | Accumulated while scanning; stop + exit 5 |
| `resources.max_depth` | 32 | Depth-limited walk (also breaks symlink cycles) |
| `resources.max_stages` | 50,000 | Lineage construction; guards O(V+E) from becoming O(V²) |
| `resources.max_memory_mb` | 256 | RSS monitor thread samples every 500 ms; abort + exit 5 |
| `resources.watchdog_seconds` | 120 | Whole-run deadline; abort + exit 5 |

Watchdog implementation: `signal.SIGALRM` on POSIX; a `threading.Timer` fallback on Windows (both stdlib). The watchdog is started before parsing and canceled on clean exit; it must be unit-tested with an injected fake-slow parser.

### 4.2 Circuit breakers (beyond the LLM one in §3.4)

| Breaker | Trip condition | Action |
|---|---|---|
| Parse-per-file | single file parse > 500 ms | File marked `parse_timeout`, skipped, listed in report; continue (never abort the whole run for one file) |
| Incremental-useless | cache hit rate < 30% | Log + fall back to full scan for the remainder |
| SQLite busy | `busy_timeout` exceeded 3 times consecutively | Abort persist (analysis already done); exit 5 with "storage unavailable" |
| Regex blowup guard | file over `max_file_bytes` OR total over `max_total_bytes` | Files are gated *before* regex; this is the pre-emptive breaker for catastrophic backtracking (Python `re` has no timeout) |
| CI minutes | run > 90 s inside a CI job (2× p95) | The watchdog (`120 s`) trips; job still exits with distinct code 5 so the workflow can log "resource limit" instead of false drift |

### 4.3 Exit-code contract (machine-readable, CI-friendly)

**This table is the project-wide canonical contract.** It extends the seed contract (ARCHITECTURE.md §5.3: `0`/`1`/`2` unchanged, backward compatible) and aligns exactly with the orchestrator taxonomy (ORCHESTRATION.md §11.5: `0`–`5`). Any change here must be mirrored in both sibling docs.

| Code | Meaning |
|---|---|
| 0 | SUCCEEDED — clean, no findings |
| 1 | FAILED — findings: breaking drift, security regression, verify failure (**gate the merge**) |
| 2 | ABORTED / usage error — run never started or was cancelled (bad args/flags, Ctrl-C, `oarl abort`) |
| 3 | CONFIG / snapshot error — bad config file, unknown `llm.provider`, snapshot mismatch |
| 4 | DETERMINISM VIOLATION (strict mode only) |
| 5 | INTERNAL / GUARDRAIL error — internal error, storage failure, **and every resource-limit trip** (watchdog, memory, file/byte/stage caps) |

Exit codes 0/1 are the only ones GitHub Actions gates on; 2–5 must be distinguishable in logs so a runaway operation can't masquerade as a drift failure (or vice versa). Guardrail trips (5) are disambiguated from genuine internal errors by the machine-readable `reason` field in `--json` output and the report text — CI treats both as "infra failure, not drift".

---

## 5. CI cost optimization (GitHub Actions)

### 5.1 The free-tier math

- Private repos: **2,000 min/month** on `ubuntu-latest` (Linux multiplier = 1.0; a Windows runner would cost 2× — **do not use Windows runners**; the tool is stdlib-only and platform-agnostic, so Linux suffices).
- Public repos: **unlimited minutes** for GitHub-hosted runners. The repo is planned public (PRD: open-source). Even so, the budget below is designed for the *private* worst case so the CI never depends on the repo being public.

### 5.2 Per-job cost table (measured targets)

| Job | Runs on | Minutes/run | Trigger |
|---|---|---|---|
| `driftgate` (the product itself: parse + drift check on `examples/` + a 1k-stage synthetic corpus) | every PR push + push to `main` | ~1.5 | `paths: ['driftguard/**', 'tests/**', '.github/**', 'examples/**']` |
| `tests` smoke (unit suite, Python 3.11 only) | every PR + `main` | ~2.5 (incl. pip cache restore) | same path filter |
| `bench` (performance SLO suite, §2.6) | nightly only | ~4 | `schedule: cron '0 3 * * *'` |
| `matrix` (full unit suite on 3.11 / 3.12 / 3.13 / 3.14) | nightly only | ~8 (4 × 2 min) | same schedule |
| `pages` (docs build + deploy) | on tag only | ~3 | `push: tags: ['v*']` |
| `release` (PyInstaller binary + PyPI) | on tag only | ~6 | same |

### 5.3 Monthly budget model (private-repo worst case)

| Scenario (PRs/mo) | driftgate | tests smoke | nightly (bench+matrix) | tags (pages+release) | **Total** |
|---|---|---|---|---|---|
| 100 PRs × 1.3 runs avg (concurrency-cancelled) | 195 | 325 | 30×12 = 360 | 12×9 = 108 | **988 min** |
| 200 PRs × 1.3 | 390 | 650 | 360 | 108 | **1,508 min** |
| 400 PRs × 1.3 | 780 | 1,300 | 360 | 108 | **2,548 min** ⚠ |

⚠ At 400 PRs/mo the naive math exceeds 2,000. Mitigations (each already in §5.4) cut the real number: path filters mean only PRs touching `driftguard/**|tests/**|.github/**|examples/**` run CI at all. In a healthy repo, ≤ 60% of PRs touch those paths ⇒ effective total ≈ **1,716 min** at 400 PRs (≈ 14% headroom). If a team genuinely exceeds this, they are on a Pro tier and the answer is a **self-hosted runner** (their hardware, still $0 for us, §6.2); a public repo also removes the ceiling entirely (§5.1).

### 5.4 Optimization mechanics (all config-as-code in `.github/workflows`)

1. **Path filters** on every workflow (`paths:`) — docs/README-only PRs consume 0 minutes.
2. **Concurrency cancellation:** `concurrency: { group: ${{ github.workflow }}-${{ github.ref }}, cancel-in-progress: true }` — a push during a running PR job cancels the stale job; each PR costs ~1.3 runs instead of N.
3. **Matrix policy:** full Python matrix nightly only; PRs run 3.11 only. Stdlib-only code makes cross-version risk low; nightly catches drift within 24 h. Nightly job also does `paths` check via a tiny "changed" probe step to skip when nothing changed since last run (store a marker file in the cache).
4. **Caching:** `actions/setup-python@v5` with `cache: 'pip'` (key: `${{ runner.os }}-py-${{ matrix.python }}-${{ hashFiles('**/requirements*.txt') }}` — we have zero deps, so the pip step is near-free; the cache mainly saves the interpreter download, ~30–60 s/run). `actions/cache` for the synthetic corpus (the 1k-stage fixture is generated once, cached by content-hash).
5. **Scheduled job reduction:** exactly **one** nightly schedule (`0 3 * * *`); no hourly, no per-commit matrix. Manual `workflow_dispatch` exists for ad-hoc full runs.
6. **No Windows/macOS runners** (2×/10× multiplier respectively — macOS is 10×!). Linux only; Windows coverage is a manual `workflow_dispatch` option for release week only.
7. **Pages deploy on tags only** — docs are built from `main` branch pages source; the expensive deploy job runs 12×/year, not 300×.

### 5.5 CI-minute KPI

`ci_minutes_per_release` (§7) is computed from the Actions API (free) once per release via a small script; target ≤ 30 min per release (nightly excluded, those are continuous overhead) and ≤ 5 min per PR.

---

## 6. Scaling path — growth without breaking $0

### 6.1 Project growth (bigger pipelines)

| Trigger | Response | Still $0 because… |
|---|---|---|
| > 1,000 stages per repo | `--incremental` becomes the default (SHA cache pays off; §2.3) | local SQLite |
| > 50,000 stages (monorepo) | `resources.max_stages` raises **only with explicit `--large-repo` flag** (user acknowledges slower runs); guidance: split per-domain runs, feed results into one DB | local SQLite + user's CPU |
| SQLite > 500 MB / 1M rows | optional DuckDB analytics export (§2.5) | DuckDB is embedded |
| Parse p95 regresses | bench suite fails nightly; fix grammar/regex before shipping | CI minutes are the only cost, and they're budgeted |
| Users want history sharing | `driftguard export --json` + any free paste service; no hosted storage | user's choice of transport |

### 6.2 Team growth (multiple engineers on one repo)

- CLI is stateless and shareable: the SQLite DB can live in the repo (gitignored by default, but `--db` can point anywhere) or on a shared drive — zero infra.
- Self-hosted server mode (PRD §Deployment): a Docker image on the team's own VM. We ship the image; they pay their host. Multi-user auth in server mode is basic token auth at first (still no SSO — SSO is Pro).
- The managed catalog (PRD Phase 4: "Managed pipeline catalog + team collaboration") is **Pro-only by definition** — hosting is the paid convenience, the analysis never is.

### 6.3 The freemium boundary — non-negotiable

**Free forever (core):** parsing, lineage, drift detection, schema diffs, dry-run previews, refactor suggestions (heuristic + local LLM), security rule scanning, SQLite history, CI gate, all report formats, incremental analysis, DuckDB export.

**Paid (Pro $10–50/user/mo, conveniences only):** managed hosted catalog, SSO (SAML/OIDC — PRD non-MVP), team dashboards, advanced analytics dashboards (the *data* is free, the hosted *dashboard* is paid), priority support.

**Explicitly forbidden:** paywalling any core analysis feature, requiring a Pro license to run in CI, telemetry that phones home, cloud LLM calls in the core path, usage-metered billing (§1.3).

### 6.4 Capacity plan for the managed catalog (the only real server)

- Single small VM + SQLite (later Postgres when > 100 orgs); $0 to build; the *customer* pays for their own instance in self-hosted mode, or we absorb a single shared instance until it reaches ~500 orgs — at which point the infra cost is a rounding error against Pro revenue, and it is *still* not part of the core product's $0 claim.
- Pro SLA guardrails: the §1.3 caps apply; no auto-scaling bill surprises; fixed tiers.

---

## 7. KPIs — concrete metrics, concrete targets

| KPI | Target | Where measured |
|---|---|---|
| Parse p50 / p95 (1,000 stages) | < 2 s / < 5 s | bench suite (nightly CI), recorded to SQLite `bench_runs` |
| Analyze p50 / p95 (10k-line codebase) | < 3 s / < 10 s | same |
| Incremental re-analyze p95 (5 files changed) | < 2 s | same |
| Peak RSS p95 / ceiling | < 150 MB / 256 MB | bench suite samples `resource.getrusage` / psutil-free stdlib `tracemalloc` + `psutil`-free RSS via `/proc` or `ctypes` (Linux CI) |
| Cache hit rate (incremental runs) | ≥ 70% | per-run, `runs.cache_hit_rate` |
| LLM runs vs offline runs | ≥ 90% of all runs offline (opt-in only); LLM success rate ≥ 95% when enabled | `runs.llm_used` + `llm_usage` rows |
| LLM cost per run | $0.00 always; wall-clock ≤ 10 min/run worst case (20 × 30 s) | `llm_usage.duration_ms` sum |
| CI minutes per PR | ≤ 5 min | Actions API scrape (release script) |
| CI minutes per release | ≤ 30 min | same |
| Nightly CI overhead | ≤ 12 min/day | same |
| SQLite growth | < 2 MB per 1,000 stages | `VACUUM`/size check on `driftguard db-stats` |
| **Monthly infra bill** | **$0.00** (hard) | finance review: no external service can be added without this doc's amendment |
| 429s / circuit-breaker trips | 0 (nothing external to 429) | `llm_usage` + `runs` logs |
| Drift false-positive rate (renamed ≥ 0.75 similarity) | < 5% on labeled corpus | nightly eval on `examples/` + synthetic renamed pairs |

KPI telemetry is **local only**: a `driftguard kpi` command renders these from SQLite into a markdown report; nothing is transmitted. The CI scrapes the Actions API for minutes (read-only, no cost).

---

## 8. Canonical config block (implement these exact constants)

`driftguard.config` (JSON, loaded from `driftguard.json` or `--config`; env overrides `DRIFTGUARD_*`):

```json
{
  "resources": {
    "max_files": 10000,
    "max_file_bytes": 10485760,
    "max_total_bytes": 262144000,
    "max_depth": 32,
    "max_stages": 50000,
    "max_memory_mb": 256,
    "watchdog_seconds": 120
  },
  "perf": {
    "parse_p50_s": 2.0,
    "parse_p95_s": 5.0,
    "parse_file_max_ms": 500,
    "lineage_max_s": 5.0,
    "drift_max_s": 3.0,
    "analyze_p95_s": 10.0,
    "incremental_p95_s": 2.0,
    "report_max_s": 3.0,
    "persist_max_s": 3.0,
    "workers": 4,
    "worker_min_files": 200,
    "batch_size": 1000
  },
  "cache": {
    "enabled": true,
    "parser_version": "1",
    "min_hit_rate": 0.30,
    "target_hit_rate": 0.70
  },
  "store": {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "busy_timeout_ms": 5000,
    "page_size": 4096,
    "cache_size_kb": 65536,
    "retention_days": 90,
    "vacuum_min_mb": 50,
    "vacuum_free_page_pct": 0.30,
    "duckdb_promote_mb": 512,
    "duckdb_promote_rows": 1000000
  },
  "llm": {
    "enabled": false,
    "provider": "ollama",
    "endpoint": "http://127.0.0.1:11434",
    "allow_remote": false,
    "model": "qwen2.5:7b",
    "timeout_s": 30,
    "health_check_s": 2,
    "max_prompt_chars": 8000,
    "max_context_chars": 24000,
    "max_tokens": 512,
    "max_requests_per_run": 20,
    "max_files_per_batch": 5,
    "max_retries": 2,
    "retry_backoff_s": 1,
    "circuit_breaker_failures": 3,
    "concurrency": 1
  },
  "ci": {
    "linux_only": true,
    "pr_matrix_python": ["3.11"],
    "nightly_matrix_python": ["3.11", "3.12", "3.13", "3.14"],
    "nightly_cron": "0 3 * * *",
    "pages_on_tags_only": true,
    "cancel_in_progress": true,
    "path_filters": ["driftguard/**", "tests/**", ".github/**", "examples/**"]
  },
  "pro": {
    "tiers": [10, 30, 50],
    "job_max_minutes": 30,
    "concurrent_jobs": 5,
    "storage_per_project_mb": 1024,
    "storage_per_org_mb": 10240,
    "rate_limit_per_min": 60,
    "rate_limit_per_day": 10000,
    "alert_at_pct": 0.80
  },
  "telemetry": { "enabled": false, "local_only": true }
}
```

---

## 9. Sign-off checklist

- [ ] §1: every cost dimension enumerated; each $0 mechanism is a design invariant, not an accident.
- [ ] §1.2/1.3: caps are config constants, trip deterministically, and never silently degrade output.
- [ ] §2: SLOs are test-enforced nightly with CI-slack multiplier.
- [ ] §3: LLM is off by default, local-only, capped, and its output can never change a verdict or exit code.
- [ ] §4: no operation can run longer than 120 s, touch more than 10 MB/file / 250 MB total, or exceed 256 MB RSS.
- [ ] §5: worst-case private-repo CI ≤ 2,000 min/month with mitigations; public repo = unlimited.
- [ ] §6: freemium boundary documented; Pro = convenience only; no usage metering.
- [ ] §7: KPI table has concrete numbers and a local-only measurement path.
- [ ] §8: config block is the single source of truth for all constants.