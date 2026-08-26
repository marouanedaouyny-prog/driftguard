# Security

DriftGuard is a security-aware refactoring assistant for SQL data pipelines:
it scans for common security anti-patterns, then plans and applies
schema-drift-safe SQL rewrites with a human approval gate.

## Reporting a vulnerability

Open a **private** issue (or email the maintainer directly if one is listed
in `CONTRIBUTING.md`). Please include:

- affected version (`python -m driftguard --version`)
- the pipeline shape that triggers the problem (a minimal repro tree)
- what you expected vs. what happened

Do **not** open a public issue for credential exposure, RCE-class bugs in the
plugin seam, or anything that could hurt users of the tool.

## Security model

| Concern | Design |
|---|---|
| Secrets never leave the machine | Findings, reports and DB rows are **redacted** before any output surface (`core/security/redact.py`); raw secrets never reach JSON, text reports, or SQLite. LLM prompts carry IR summaries + redacted snippets only — raw code never reaches a model |
| Scanner is a heuristic, not a guarantee | SEC-001..005 are deterministic pattern rules. High-entropy detection requires ≥ 20 chars with Shannon ≥ 3.5; short secrets pass silently. Findings always require human review; `critical`/`high` findings gate `refactor plan` by default (the security block overlay) |
| Suppression is deliberate | `-- driftguard:off SEC-002` (line) / `-- driftguard:off-all` (file) comments. Suppressed findings are counted, never hidden |
| Trusted-code seam | `--rules-dir` **executes** the `.py` files it loads — this is the same trust level as running the tool itself. Never point it at unvetted code. Invalid plugins (bad fields, import failures) are warned and skipped; built-in id collisions are rejected, never silently shadowed |
| State integrity | Every `refactor` transition is a SQLite transaction with an audit row; a crash leaves the prior state, and re-running resumes it. `apply` is idempotent (`apply(apply(x)) == apply(x)`), writes `.orig` backups, and refuses stale plans (`ApplyError`) |
| No network by default | The core workflow is fully offline. The only network surface is the **optional** Ollama channel (`--llm-suggestions`), which never runs unless you pass the flag |

## Threat model (what this tool does NOT protect against)

- **Malicious rule plugins**: `--rules-dir` loads arbitrary code with your
  privileges. Vetting is on the user; the tool only documents it loudly.
- **A compromised Ollama endpoint**: suggestions are validated (schema,
  spans, no-op, confidence) but a hostile endpoint could in principle return
  harmful `after` text. Suggestions are never auto-applied — `approve` is a
  human decision, and `verify` re-analyzes after apply.
- **Jinja/templated SQL**: the parser handles a static subset; anything
  dynamic is marked and skipped. A refactor never rewrites template regions.
- **Full secret detection**: the entropy gate has documented blind spots
  (short secrets). Use a dedicated secret scanner (e.g. gitleaks) for
  credential sweep duties.

## Hardening checklist for contributors

- Never log or persist raw values from `password=`, `api_key=`, `token=`,
  `sk-`, `ghp_`, `AKIA`, `xoxb-` contexts — route through
  `core/security/redact.py`.
- New scanner rules must keep the regression corpus 100% TP / 0% FP
  (`tests/security_corpus/`) and honor suppression syntax.
- New refactor rules must be idempotent (`apply(apply(x)) == apply(x)`),
  byte-exact on spans, and deterministic (tests must not depend on file
  order or OS locale).
- Changes to exit codes or JSON envelopes are **breaking** (see
  `docs/API_SPEC.md` §9) — they need a MAJOR version bump and deprecation
  runway.