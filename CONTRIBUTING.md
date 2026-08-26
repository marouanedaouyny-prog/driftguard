# Contributing

Thanks for considering a contribution. This project runs on
**zero-cost, deterministic, auditable** principles — keep that in mind.

## Ground rules

- **Python 3.11+ stdlib only.** No runtime dependencies. Ever. New code that
  needs a third-party import is a PR that gets sent back.
- **SQLite is the only persistence** (`driftguard/store.py`, WAL,
  thread-local connections). No other stores without a phase-level decision.
- **Agents/JSON for machines, markdown for humans.** Machine output is
  versioned JSON envelopes (`driftguard.*.v1`); human output is plain text.
- **Deterministic by default.** No unordered iteration, no locale-dependent
  output, no file-order-dependent results. Tests must pass on any OS.
- **Redaction first.** Any code path that touches secrets must scrub before
  persisting or printing (see `SECURITY.md`).
- **No secrets in commits.** Ever.

## Development setup

```powershell
git clone <repo-url>
cd driftguard
python -W error::ResourceWarning -m unittest discover -s tests   # full suite
```

Windows note: PowerShell 5.1 `Set-Content -Encoding UTF8` writes a BOM
(`\ufeff`) which the SQL tokenizer rejects. Write test fixtures with
`[System.IO.File]::WriteAllText(path, text, UTF8Encoding($false))`.

## What to work on

Check `docs/STEPS.md`-style tracking (top of repo state notes) and the
open issues. Suggested starter areas:

- New refactor rules (REF-*) behind the `Rule` protocol — each needs a
  golden test, idempotency proof (`apply(apply(x)) == apply(x)`), and a
  documented risk tier.
- New security rules (SEC-*) — each needs positive + negative corpus files
  in `tests/security_corpus/` pinning 100% TP / 0% FP.
- More built-in rules for `--rules-dir` examples (`examples/plugins/`).

## Before opening a PR

1. Run the full suite: `python -W error::ResourceWarning -m unittest
   discover -s tests` — it must be green and ResourceWarning-clean.
2. If you changed CLI flags, exit codes, JSON envelopes, or defaults,
   update `docs/API_SPEC.md` (§9 change policy — additive is MINOR/PATCH,
   breaking is MAJOR with deprecation runway).
3. Update `docs/ARCHITECTURE.md` status line and `CHANGELOG.md`.
4. If your change touches the threat model, update `SECURITY.md`.

## Testing conventions

- Tests are flat modules in `tests/test_*.py` (unittest, no pytest).
- Golden corpora live in `tests/golden/` and `tests/security_corpus/` —
  they are data, not test modules.
- New refactor rules: byte-exact span assertions + stale-span rejection +
  idempotency.
- New security rules: positive/negative corpus + redaction + suppression
  semantics.

## Code of conduct

Be professional. Disagreement about design is welcome; ad hominem is not.
The maintainers' decision is final on scope — this project is intentionally
small and prefers fewer, deeper features over breadth (quality over
quantity).

## License

See `LICENSE` (not yet assigned — ask the maintainer before reusing the
code elsewhere).