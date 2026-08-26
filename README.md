# DriftGuard

Schema-drift safety for SQL pipelines (dbt-style). The MVP wedge of the
*Security-Aware Refactoring Assistant for data pipelines*: refactor
pipeline stages freely, and know in CI the moment a change breaks the
schema a downstream stage relies on.

Zero dependencies — Python 3.11+ stdlib only. Runs locally, exits non-zero
on breaking drift so GitHub Actions / GitLab CI can gate merges.

## Usage

```sh
python -m driftguard <pipeline-dir>          # text report; exit 1 on breaking drift
python -m driftguard <pipeline-dir> --json   # machine-readable
python -m driftguard <pipeline-dir> --markdown
python -m driftguard <pipeline-dir> --db runs.db   # SQLite history (default driftguard.db)
python -m driftguard <pipeline-dir> --no-persist  # stateless
python -m driftguard --version
python -m driftguard parse <pipeline-dir> [--json] [--out FILE]   # IR snapshot + diagnostics (exit 2 on hard parse error)
python -m driftguard inspect <pipeline-dir> [--json]              # IR dump + diagnostics view
python -m driftguard lineage <pipeline-dir> [--json]              # graph, cycles, missing refs, topological order
python -m driftguard drift <pipeline-dir> [--threshold 0.75] [--json]  # MVP gate (driftguard.drift.v1)
python -m driftguard drift diff <pipeline-dir> [--threshold 0.75]      # unified diff preview per drift
python -m driftguard scan <dir> [--severity medium] [--fail-on-severity high] [--json]  # security baseline (SEC-001..005)
python -m driftguard security-scan <dir> [...]                         # alias of scan
python -m driftguard refactor analyze <dir> [--max-risk suggested] [--rules REF-001,REF-002] [--rules-dir DIR]  # parsed -> analyzed
python -m driftguard refactor plan <dir> [--max-risk suggested] [--allow-on-finding] [--out plan.json] [--rules-dir DIR]  # analysis -> plan
python -m driftguard refactor approve <session-id> [--all] [--ci]      # plan -> approved (approval gates)
python -m driftguard refactor apply <session-id> [--dry-run] [--no-persist] [--out-dir DIR]  # approved -> applied
python -m driftguard refactor verify <session-id>                      # applied -> verified; exit 1 on regression
python -m driftguard refactor plan <dir> --llm-suggestions             # + Ollama suggestion channel (LLM-N)
python -m driftguard refactor plan <dir> --rules-dir plugins/           # + custom Rule-protocol plugins (trusted-code)
python -m driftguard session show <id> | audit [--json]                # state machine / audit trail
```

## What it does

- **Parses stages** (Phase 1: core recursive-descent engine, one parser):
  every `*.sql` file in the tree is a stage. The output schema is the SELECT
  projection of the final (last top-level) query; inputs are `ref('x')`
  calls and — only when no `ref()` exists — bare `FROM x` references
  (subquery and CTE bodies included; `SELECT/values/dual` and dotted names
  skipped). Comments and whitespace are stripped; `CREATE TABLE/VIEW` names
  override file stems. `{{ source('raw','t') }}` is recognized as a source;
  `{{ config(...) }}`, `{# #}` comments and `{% raw %}` blocks are known and
  never flagged. Anything else in template regions is reported as a
  `unknown_template_region` dialect hint so refactors never rewrite it.
- **Builds lineage** (Phase 2: `core/lineage/`): stage dependency graph with
  consumers/producers, cycle detection (DFS), missing-ref bookkeeping and
  deterministic topological order (Kahn). `{{ source('s','t') }}` refs resolve
  against `sources.yml` files anywhere in the tree (`- name:` / `tables:` /
  `- name:` subset) and become edges with kind `source`; unresolved source
  refs are missing refs. `lineage --json` exports a `driftguard.lineage.v1`
  artifact; edges persist per run with their kind.
- **Detects drift** (Phase 3: `core/drift/`): a consumer documents the
  columns it expects from each producer. `removed` and `renamed`
  (best-match similarity ≥ `--threshold`, default 0.75) columns are
  **breaking**; `added` columns are non-breaking. `drift --json` exports a
  `driftguard.drift.v1` artifact (seed fields byte-compatible with the
  legacy payload, plus `schema`/`threshold`/`pipeline_fingerprint`/
  `breaking`); `drift diff` renders each drift as a unified diff
  (`--- a/<producer> (schema)` / `+++ b/<consumer> (expected)`) — the
  dry-run preview CI shows when the gate fails. `drift` builds the same
  lineage as `lineage` (sources.yml resolution included — source refs are
  edges, not missing refs). Run history is queryable
  (`Store.drift_history()` / `recent_runs()`).
- **Scans for security issues** (Phase 4: `core/security/`): five
  deterministic, pattern-based rules — SEC-001 hardcoded secrets (known
  provider prefixes `sk-`/`ghp_`/`AKIA`/`xoxb-`/`AIza` are critical;
  high-entropy values in `password=`/`api_key=`/`token=` contexts are high),
  SEC-002 SQL assembled by string interpolation, SEC-003 unsafe subprocess
  (`shell=True`, `os.system`, non-literal commands), SEC-004 credentials in
  connection strings/DSNs, SEC-005 plaintext credentials in SQL DDL/DCL.
  `scan --json` exports a `driftguard.scan.v1` artifact; exit 0 clean,
  1 findings ≥ `--fail-on-severity` (default high), 5 over
  `--max-findings`. Line-scoped (`-- driftguard:off SEC-002` / `# driftguard:off
  SEC-002`) and file-scoped (`-- driftguard:off-all`) suppressions keep
  findings `suppressed` and never gate. Every output surface is redacted —
  the raw secret never leaves the scanner. Findings persist per run
  (`Store.scan_findings()`).
- **Refactors safely** (Phase 4: `core/refactor/`): seven deterministic rules
  (REF-001 drop-dead-CTE, REF-002 duplicate-projection, REF-003
  inline-single-use-CTE, REF-004 quote-normalize, REF-005 star-expand,
  REF-006 dead-alias, REF-007 drop-subquery-order-by) behind a `Rule`
  protocol and ADR-006 risk tiers
  (`safe`/`suggested`/`risky`, default gate `safe`). `refactor plan`
  produces a versioned `refactor_plan.json` with byte-exact spans and
  per-item hashes; `apply` is idempotent (`apply(apply(x)) == apply(x)`,
  `.orig` backups, `--dry-run` preview), and `verify` re-analyzes and
  flags regressions (session back to `approved`, exit 1). A state machine
  (`start → parsed → analyzed → planned → approved → applied → verified`)
  with an audit log keeps every transition inspectable. `--llm-suggestions`
  opens the optional Ollama channel (stdlib `urllib`, `qwen2.5-coder:7b`
  default): suggestions are `LLM-N` candidates at forced tier `suggested`,
  never auto-applied, never merged into deterministic output; prompts carry
  IR summaries + redacted snippets only. Exit 2 `llm_unavailable` when the
  flag is used and Ollama is unreachable; mid-run failures degrade to zero
  suggestions with a warning. `--rules-dir DIR` (Phase 5) loads custom
  `Rule`-protocol plugins from a directory — a **trusted-code seam** (the
  files are executed): deterministic order, built-in id collisions rejected,
  invalid plugins warned and skipped; the directory is persisted on the
  session so `verify` re-analyzes with the same plugin set.
- **Persists**: stages, edges, drifts, scan findings and run history in SQLite.
- **Inspects** (`parse` / `inspect` subcommands): parse a pipeline and emit
  versioned IR JSON (`driftguard.parse.v1` snapshot, or the `{"v": 1,
  "pipeline": …}` IR envelope for `inspect`) with byte-offset spans, sha256
  fingerprints and structured diagnostics. Exit 0 parsed (warnings ride in
  `diagnostics`), 2 for no input or hard parse errors, 5 over
  `--max-stages`. Fingerprints are canonical (span-free, location-free):
  files differing only in whitespace/comments compare equal.

## CI gate

```yaml
# examples/ci/drift.yml — the full gated workflow; minimal form below
on: [pull_request]
jobs:
  driftguard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python -m driftguard drift models --threshold 0.75 --json
```

## Honest limits (MVP)

- No real SQL engine: types, CASE branches, macro-expanded schemas and
  `COUNT(*) AS n` aliases are not modeled — aggregate aliases count as
  expected columns (false positives on purpose).
- Star projections (`SELECT *`, `t.*`) make a stage's schema unassertable:
  the parser emits a warning and drift assertions are skipped for that
  stage — real dbt repos (e.g. jaffle_shop) end their models in
  `select * from final`, so a rename in a producer is invisible to the
  gate unless the consumer pins explicit columns.
- Rename detection is heuristic (token similarity), not column lineage.
- Jinja macros are only resolved for `ref('name')` / `source('s','t')` /
  `config(...)`; dynamic refs (`ref(var('x'))`) and everything else in
  template regions is left untouched and flagged as a
  `unknown_template_region` hint. `{% raw %}` blocks are literal.
- `sources.yml` is parsed by a tiny key:value reader (2-space `- name:`
  sources, 6-space `- name:` tables); anything YAML beyond that subset is
  ignored — documented limitation, not a YAML parser.
- Source tables are lineage nodes only; their schemas are not modeled
  (drift skips `source` edges — there is no producer schema to compare).
- `drift --threshold` is the rename similarity gate: a pair at or above the
  threshold is a rename (breaking), below it is removed + added (also
  breaking). One producer column can be the rename target of at most one
  consumer column.
- Parse failures never abort the run: they surface as structured
  diagnostics on the affected stage (exit code stays 0 for `inspect`;
  hard input errors exit 2).
- DBT v1 supports the same SQL dialect as the seed (dbt-style projects);
  other dialects are a Phase-2+ extension (see `docs/ARCHITECTURE.md`).
- Security scanning is pattern-based (regex + entropy heuristics), not a
  full secret-detection engine: it finds *shaped* secrets, not novel
  formats. High-entropy assignment detection requires ≥ 20 chars with
  Shannon entropy ≥ 3.5 — short-but-real secrets pass silently. Redaction
  covers the known shapes; an unknown secret format could surface
  unredacted in a snippet. The `tests/security_corpus` regression suite
  pins 100% true positives (positive/) and 0 false positives (negative/);
  parameterized SQL placeholders (`%s` + params tuple) are not flagged.
- Refactor rules are heuristic (token-based) and whitespace-sensitive by
  design: spans are byte-exact and `apply` only touches matched text, but
  a rule can miss a construct it does not recognize — `verify` exists
  because rules are not a proof. LLM suggestions are advisory: confidence
  is the model's own, the tier gate (`--max-risk`) is the only filter, and
  a `safe` run never includes them.

## Development

```sh
python -m unittest discover -s tests
```