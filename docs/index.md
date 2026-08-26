# DriftGuard

**Security-Aware Refactoring Assistant for Data Pipelines** — parse SQL
pipeline stages, build lineage, detect schema drift, scan for common
security anti-patterns, and plan **safe, byte-exact, idempotent SQL
refactors** behind a human approval gate.

- Zero runtime dependencies (Python 3.11+ stdlib, SQLite)
- Deterministic and auditable: every refactor transition is a state-machine
  step with an audit trail; `verify` re-analyzes and flags regressions
- Security-first: redacted output everywhere, findings gate refactors by
  default, optional Ollama suggestions never auto-apply

## Quick start

```console
$ python -m driftguard --version
driftguard 0.5.0
$ python -m driftguard drift pipeline/                 # schema-drift gate
$ python -m driftguard scan pipeline/                  # security baseline
$ python -m driftguard refactor plan pipeline/ --max-risk suggested
$ python -m driftguard refactor apply 1 --dry-run      # preview
```

## Documentation

- [README](../README.md) — overview, usage, honest limits
- [API specification](API_SPEC.md) — CLI contract, exit codes, JSON envelopes
- [Architecture](ARCHITECTURE.md) — design decisions, phases, rules catalog
- [Data model](DATA_MODEL.md) — SQLite schema and migrations
- [Security](SECURITY.md) — threat model, redaction policy, reporting
- [Contributing](CONTRIBUTING.md) — development setup and conventions
- [Changelog](../CHANGELOG.md)

## Examples

- `examples/models/` — a small dbt-style pipeline to experiment on
- `examples/plugins/sample_rule.py` — a custom `Rule`-protocol plugin

## Packaging

- `Dockerfile` — `docker run --rm -v "$PWD:/pipeline" driftguard:0.5.0
  refactor plan /pipeline`
- `scripts/build_zipapp.py` — stdlib single-file `dist/driftguard.pyz`
- Release workflow (`.github/workflows/release.yml`): tag `v*` → CI matrix
  (3 OS × Python 3.11–3.14) → GHCR image + release asset + Pages docs.