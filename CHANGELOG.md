# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) with
the API contract in `docs/API_SPEC.md` §9.

## [Unreleased]

## [0.5.2] — 2026-08-25

### Added

- **REF-007 `drop-subquery-order-by`** (SUGGESTED tier): removes an
  `ORDER BY` directly inside a parenthesized query (derived table, CTE
  body, IN/EXISTS subquery) — it has no effect on the outer query unless a
  `LIMIT`/`OFFSET`/`FETCH` follows at the same level. Conservative
  preconditions: compound interiors (`UNION`/`INTERSECT`/`EXCEPT`/`MINUS`),
  template regions, side-effecting statements, window (`OVER (ORDER
  BY …)`) and aggregate (`f(x ORDER BY y)`), and top-level clauses are all
  never touched. The span absorbs surrounding whitespace; 13 new tests
  (golden + kept-cases + nested disjoint spans + idempotency).

## [0.5.1] — 2026-08-20

### Fixed

- **`drift`/`lineage` alignment**: the `drift` gate now builds the same
  lineage as `lineage` — `sources.yml`-defined `source()` refs resolve to
  edges (kind `source`) instead of spurious missing refs. The flat shim
  `Stage` gains `sources`; `_drift_pipeline` passes `find_sources(root)`;
  `resolve_refs` accepts `(source, table)` pairs. No CLI/JSON change.

## [0.5.0] — 2026-08-20

### Added

- **Trusted-code plugin loader** (`--rules-dir DIR` on `refactor analyze` and
  `refactor plan`, `core/refactor/loader.py`, ARCHITECTURE §2.1): load
  `Rule`-protocol plugins from a directory of `*.py` modules.
  - Deterministic order (filenames sorted; module rules sorted by `id`).
  - Built-in id collisions rejected with a warning — no silent shadowing.
  - Invalid plugins (bad fields, import failures) warned and skipped; a bad
    plugin never breaks a run.
  - `--rules` filters across built-ins and plugins; unknown ids still hard
    error.
  - The directory is persisted on sessions (`sessions.rules_dir`, migration
    in `Store._migrate`) so `verify` re-analyzes with the same plugin set;
    `session show` prints it.
  - Example plugin: `examples/plugins/sample_rule.py` (PLUG-001).
- **Launch artifacts**: `SECURITY.md` (threat model + redaction policy),
  `CONTRIBUTING.md`, `CHANGELOG.md`, `Dockerfile` (python:3.11-slim,
  `python -m driftguard` entrypoint), `.github/workflows/release.yml`
  (tag → CI matrix + GHCR image + release assets + Pages docs),
  `docs/_config.yml` + `docs/index.md` (GitHub Pages, native Jekyll),
  `scripts/build_zipapp.py` (stdlib `python -m zipapp` packaging as a
  zero-dependency alternative to PyInstaller).

### Changed

- Version 0.4.0 → 0.5.0.
- `build_plan` records the effective rule set (enabled ids incl. plugins)
  in `plan.rule_ids` (previously only the explicit `--rules` list).

## [0.4.0] — 2026-08-20

### Added

- **Refactoring engine** (`driftguard/core/refactor/`, ARCHITECTURE §4.4):
  `Rule` protocol + ADR-006 risk tiers (`safe`/`suggested`/`risky`);
  six deterministic rules — REF-001 drop-dead-CTE, REF-002
  duplicate-projection, REF-003 inline-single-use-CTE, REF-004
  quote-normalize, REF-005 star-expand, REF-006 dead-alias.
  - `refactor analyze|plan|approve|apply|verify|dry-run` + `session show`
    + `audit --json`; FSM `start → parsed → analyzed → planned → approved →
    applied → verified` with an audit log.
  - Byte-exact, idempotent apply (`apply(apply(x)) == apply(x)`), `.orig`
    backups, `--dry-run`, `--out-dir`, `--ci`.
  - Security block overlay: `critical`/`high` findings block intersecting
    candidates unless `--allow-on-finding` (recorded in audit, forever).
  - Exit codes: 0 clean / 1 gate or verify-regression / 2 usage-parse-state /
    5 plan cap.
- **Ollama suggestion channel** (`driftguard/llm/`, API_SPEC §7, ADR-008):
  stdlib-`urllib` client (`qwen2.5-coder:7b` default, `temperature 0.2`,
  `num_ctx 8192`, `format json`); prompts carry IR summaries + redacted
  snippets only; `validate_suggestions` hard-rejects bad JSON / low
  confidence / no-ops / out-of-bounds spans / before-mismatch / duplicates;
  suggestions are `LLM-N` at forced tier `suggested`, never auto-applied.
  `--llm-suggestions` without a reachable Ollama exits 2
  (`llm_unavailable`) — and only when the flag was requested.

## [0.3.0] — 2026-08-19

### Added

- **Security scanner** (`driftguard/core/security/`): SEC-001 hardcoded
  secrets (provider prefixes → critical; entropy gate → high), SEC-002 SQL
  string interpolation, SEC-003 unsafe subprocess, SEC-004 credentials in
  connection strings, SEC-005 plaintext credentials in SQL DDL/DCL.
  - Redaction-first output on every surface; suppression comments
    (`-- driftguard:off SEC-002`, `-- driftguard:off-all`); `scan` /
    `security-scan` subcommand with `driftguard.scan.v1` envelope; findings
    persist per run; regression corpus pins 100% TP / 0% FP.

## [0.2.0] — 2026-08-18

### Added

- `drift` subcommand with unified diffs (`driftguard.drift.v1`), lineage
  edges with kinds, `sources.yml` resolution, cycle detection, topological
  order.

## [0.1.0] — 2026-08-17

### Added

- `parse` / `inspect` subcommands: versioned IR snapshots
  (`driftguard.parse.v1`), byte-offset spans, sha256 fingerprints,
  diagnostics; `lineage` subcommand (`driftguard.lineage.v1`).

## [0.0.1] — 2026-08-16

### Added

- Initial MVP: SQL stage parser, stage inventory, diagnostics.