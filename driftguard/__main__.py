"""CLI entry: `python -m driftguard <root> [--db FILE] [--json]`.

Legacy bare command stays byte-compatible with the seed; Phase 1 adds the
`parse` / `inspect` subcommands (versioned IR JSON + diagnostics) and
`--version`.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

from driftguard import __version__
from driftguard.core.drift import drift_to_dict, schema_diff
from driftguard.core.ir.serialize import pipeline_dict, to_json
from driftguard.core.lineage import build_lineage
from driftguard.core.parser.dialects.dbt import DbtParser
from driftguard.core.refactor.apply import ApplyError, apply_plan, plan_summary
from driftguard.core.refactor.planner import (MAX_CANDIDATES, analyze_pipeline,
                                              build_plan, read_plan, write_plan)
from driftguard.core.refactor.session import (create_session, mark_aborted,
                                              mark_analyzed, mark_applied,
                                              mark_approved, mark_parsed,
                                              mark_planned, mark_verified,
                                              persist_plan)
from driftguard.core.security import at_least, scan_root
from driftguard.drift import detect_drifts
from driftguard.llm import LlmUnavailable
from driftguard.lineage import build_lineage as _legacy_build_lineage
from driftguard.parser import Stage, parse_pipeline
from driftguard.report import report_markdown, report_text
from driftguard.store import Store

SUBCOMMANDS = {"inspect", "parse", "lineage", "drift", "scan", "security-scan",
               "refactor", "session", "audit"}
_MAX_STAGES = 10000
_ENV_DB = "DRIFTGUARD_DB"
_GIT_ENV = {k: v for k, v in os.environ.items()
            if k not in ("GIT_ASKPASS", "GIT_TERMINAL_PROMPT", "SSH_ASKPASS")}
_GIT_ENV["GIT_TERMINAL_PROMPT"] = "0"


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    if argv and argv[0] in ("-V", "--version"):
        print(f"driftguard {__version__}")
        return 0
    if argv and argv[0] in SUBCOMMANDS:
        return _dispatch(argv[0], argv[1:])
    if argv and _is_unknown_subcommand(argv[0]):
        print(f"error: unknown subcommand: {argv[0]}", file=sys.stderr)
        return 2
    return _legacy_main(argv)


def _dispatch(cmd: str, argv: list[str]) -> int:
    if cmd == "inspect":
        return _cmd_parse_or_inspect(argv, is_inspect=True)
    if cmd == "parse":
        return _cmd_parse_or_inspect(argv, is_inspect=False)
    if cmd == "lineage":
        return _cmd_lineage(argv)
    if cmd == "drift":
        return _cmd_drift(argv)
    if cmd in ("scan", "security-scan"):
        return _cmd_scan(argv)
    if cmd == "refactor":
        return _cmd_refactor(argv)
    if cmd == "session":
        return _cmd_session(argv)
    if cmd == "audit":
        return _cmd_audit(argv)
    print(f"error: unknown subcommand: {cmd}", file=sys.stderr)
    return 2


def _is_unknown_subcommand(word: str) -> bool:
    """A bare first argument that is not a flag and not an existing path is
    treated as a subcommand typo (exit 2) instead of a directory error."""
    if word.startswith("-") or word in (".", ".."):
        return False
    if "/" in word or "\\" in word:
        return False
    return not Path(word).is_dir()


# ---- parse / inspect -----------------------------------------------------------


def _cmd_parse_or_inspect(argv: list[str], is_inspect: bool) -> int:
    prog = "driftguard inspect" if is_inspect else "driftguard parse"
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Parse a pipeline and emit versioned IR with diagnostics.")
    parser.add_argument("root", nargs="?", default=".",
                        help="pipeline directory (searched recursively for *.sql)")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON on stdout")
    parser.add_argument("--out", metavar="FILE",
                        help="write the IR snapshot artifact to FILE (parse only)")
    parser.add_argument("--no-persist", action="store_true",
                        help="skip all SQLite writes")
    parser.add_argument("--db", default=os.environ.get(_ENV_DB, "driftguard.db"),
                        help="SQLite persistence file (default: driftguard.db)")
    parser.add_argument("--max-stages", type=int, default=_MAX_STAGES,
                        help="refuse to analyze pipelines with more stages")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"error: no_input: {root} is not a directory", file=sys.stderr)
        return 2

    pipeline = DbtParser().parse_project(root)
    if not pipeline.stages:
        print("error: no_input: no *.sql pipeline stages found", file=sys.stderr)
        return 2
    if len(pipeline.stages) > args.max_stages:
        print(f"error: resource_limit: {len(pipeline.stages)} stages exceeds "
              f"--max-stages {args.max_stages}", file=sys.stderr)
        return 5

    hard = [d for s in pipeline.stages for d in s.diagnostics
            if d.kind == "error"]
    if hard:
        for d in hard:
            print(f"error: parse_error: {d.file}:{d.line}:{d.col}: {d.reason}",
                  file=sys.stderr)
        return 2
    _emit_diagnostics(pipeline)

    run_id = None
    if not args.no_persist:
        store = Store(args.db)
        stages = [Stage(name=s.name, path=s.path, refs=s.ref_names,
                        columns=s.column_names, raw=s.raw)
                  for s in pipeline.stages]
        run_id = store.save_stages(str(root), stages)
        store.close()

    if is_inspect:
        if args.json:
            print(to_json(_inspect_envelope(pipeline)), end="")
        else:
            print(_inspect_text(pipeline), end="")
    else:
        if args.json:
            print(to_json(_parse_envelope(pipeline, run_id, root)), end="")
        else:
            print(_parse_text(pipeline, run_id), end="")
        if args.out:
            artifact = _parse_envelope(pipeline, run_id, root)
            Path(args.out).write_text(to_json(artifact), encoding="utf-8")
    return 0


def _emit_diagnostics(pipeline) -> None:
    for stage in pipeline.stages:
        for d in stage.diagnostics:
            level = "error" if d.kind == "error" else "warning"
            print(f"{level}: {d.file}:{d.line}:{d.col}: {d.reason}",
                  file=sys.stderr)


def _inspect_text(pipeline) -> str:
    errors = sum(1 for s in pipeline.stages
                 for d in s.diagnostics if d.kind == "error")
    warnings = sum(1 for s in pipeline.stages
                   for d in s.diagnostics if d.kind == "warning")
    lines = [
        f"pipeline: {pipeline.root}",
        f"stages: {len(pipeline.stages)}",
        f"fingerprint: {pipeline.fingerprint}",
        f"diagnostics: {errors} error(s), {warnings} warning(s)",
        "",
    ]
    for stage in pipeline.stages:
        cols = ", ".join(stage.column_names) or "(none)"
        refs = ", ".join(stage.ref_names) or "(none)"
        lines.append(f"{stage.name} [{stage.kind}]")
        lines.append(f"  path:    {stage.path.as_posix()}")
        lines.append(f"  columns: {cols}")
        lines.append(f"  refs:    {refs}")
        for src in stage.sources:
            lines.append(f"  source:  {src.source}.{src.table}")
        if stage.create_name:
            lines.append(f"  create:  {stage.create_name}")
        if stage.dialect_hints:
            lines.append(f"  hints:   {', '.join(stage.dialect_hints)}")
        for d in stage.diagnostics:
            lines.append(f"  [{d.kind}] {d.line}:{d.col} {d.reason}")
    return "\n".join(lines) + "\n"


def _parse_text(pipeline, run_id: int | None) -> str:
    errors = sum(1 for s in pipeline.stages
                 for d in s.diagnostics if d.kind == "error")
    warnings = sum(1 for s in pipeline.stages
                   for d in s.diagnostics if d.kind == "warning")
    persisted = f" (run #{run_id} persisted)" if run_id is not None else ""
    return (f"stages={len(pipeline.stages)} errors={errors} "
            f"warnings={warnings}{persisted}\n")


def _diagnostics_list(pipeline) -> list[dict]:
    return [{
        "file": d.file,
        "line": d.line,
        "col": d.col,
        "reason": d.reason,
        "severity": d.kind,
    } for s in pipeline.stages for d in s.diagnostics]


def _git_sha(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=5, env=_GIT_ENV)
        sha = out.stdout.strip()
        return sha or None
    except (OSError, subprocess.SubprocessError):
        return None


def _checked_at() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _inspect_envelope(pipeline) -> dict:
    return {
        "v": 1,
        "pipeline": pipeline_dict(pipeline),
        "diagnostics": _diagnostics_list(pipeline),
    }


def _parse_envelope(pipeline, run_id: int | None, root: Path) -> dict:
    return {
        "schema": "driftguard.parse.v1",
        "version": 1,
        "run_id": run_id,
        "root": root.as_posix() or ".",
        "checked_at": _checked_at(),
        "git_sha": _git_sha(root),
        "pipeline_fingerprint": pipeline.fingerprint,
        "stage_count": len(pipeline.stages),
        "diagnostics": _diagnostics_list(pipeline),
        "stages": [
            {"name": s.name,
             "path": s.path.relative_to(root).as_posix(),
             "kind": s.kind,
             "fingerprint": s.fingerprint,
             "columns": [{"name": c.name, "source_expr": c.source_expr,
                          "alias": c.alias, "span": c.span.to_list()}
                         for c in s.columns],
             "refs": [{"producer": r.name, "consumer": s.name,
                       "kind": r.kind, "expected_columns": []}
                      for r in s.refs],
             "sources": [{"source": src.source, "table": src.table}
                         for src in s.sources],
             "ctes": [{"name": c.name,
                       "span": c.span.to_list() if c.span else None,
                       "referenced_by": []}
                      for c in s.ctes],
             "create_name": s.create_name,
             "dialect_hints": list(s.dialect_hints),
             "diagnostics": [{"file": d.file, "line": d.line, "col": d.col,
                              "reason": d.reason, "severity": d.kind}
                             for d in s.diagnostics]}
            for s in pipeline.stages
        ],
    }


# ---- lineage -------------------------------------------------------------------


def _cmd_lineage(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="driftguard lineage",
        description="Stage dependency graph: edges, cycles, missing refs, "
                    "topological order.")
    parser.add_argument("root", nargs="?", default=".",
                        help="pipeline directory (searched recursively for *.sql)")
    parser.add_argument("--json", action="store_true",
                        help="emit a driftguard.lineage.v1 artifact")
    parser.add_argument("--no-persist", action="store_true",
                        help="skip all SQLite writes")
    parser.add_argument("--db", default=os.environ.get(_ENV_DB, "driftguard.db"),
                        help="SQLite persistence file (default: driftguard.db)")
    parser.add_argument("--max-stages", type=int, default=_MAX_STAGES,
                        help="refuse to analyze pipelines with more stages")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"error: no_input: {root} is not a directory", file=sys.stderr)
        return 2

    pipeline = DbtParser().parse_project(root)
    if not pipeline.stages:
        print("error: no_input: no *.sql pipeline stages found", file=sys.stderr)
        return 2
    if len(pipeline.stages) > args.max_stages:
        print(f"error: resource_limit: {len(pipeline.stages)} stages exceeds "
              f"--max-stages {args.max_stages}", file=sys.stderr)
        return 5

    hard = [d for s in pipeline.stages for d in s.diagnostics
            if d.kind == "error"]
    if hard:
        for d in hard:
            print(f"error: parse_error: {d.file}:{d.line}:{d.col}: {d.reason}",
                  file=sys.stderr)
        return 2
    _emit_diagnostics(pipeline)

    from driftguard.core.lineage import build_lineage
    from driftguard.core.parser.dialects.dbt import find_sources

    source_tables = find_sources(root)
    lineage = build_lineage(pipeline.stages, source_tables)

    run_id = None
    if not args.no_persist:
        store = Store(args.db)
        stages = [Stage(name=s.name, path=s.path, refs=s.ref_names,
                        columns=s.column_names, raw=s.raw)
                  for s in pipeline.stages]
        run_id = store.save_lineage(str(root), stages, lineage)
        store.close()

    if args.json:
        envelope = {
            "schema": "driftguard.lineage.v1",
            "version": 1,
            "run_id": run_id,
            "root": root.as_posix() or ".",
            "checked_at": _checked_at(),
            "git_sha": _git_sha(root),
            "pipeline_fingerprint": pipeline.fingerprint,
            "edges": [{"producer": a, "consumer": b,
                       "kind": lineage.kind(a, b)} for a, b in lineage.edges],
            "cycles": lineage.cycles,
            "missing": [{"consumer": consumer, "ref": ref}
                        for ref, consumer in lineage.missing],
            "topo_order": lineage.topo_order,
            "source_tables": sorted(source_tables),
        }
        print(to_json(envelope), end="")
    else:
        print(_lineage_text(pipeline, lineage, run_id), end="")
    return 0


def _lineage_text(pipeline, lineage, run_id: int | None) -> str:
    persisted = f" (run #{run_id} persisted)" if run_id is not None else ""
    lines = [
        f"stages={len(pipeline.stages)} edges={len(lineage.edges)} "
        f"cycles={len(lineage.cycles)} missing={len(lineage.missing)}"
        f"{persisted}",
        "topo: " + ", ".join(lineage.topo_order),
    ]
    for cycle in lineage.cycles:
        lines.append(f"cycle: {' -> '.join(cycle)}")
    for ref, consumer in lineage.missing:
        lines.append(f"missing: {consumer} -> {ref}")
    return "\n".join(lines) + "\n"


# ---- drift (Phase 3: the MVP gate + unified diff preview) -------------------


def _load_pipeline(args) -> tuple[Path, object] | int:
    """Parse + validate a root; returns (root, pipeline) or an exit code."""
    root = Path(args.root)
    if not root.is_dir():
        print(f"error: no_input: {root} is not a directory", file=sys.stderr)
        return 2
    pipeline = DbtParser().parse_project(root)
    if not pipeline.stages:
        print("error: no_input: no *.sql pipeline stages found", file=sys.stderr)
        return 2
    if len(pipeline.stages) > args.max_stages:
        print(f"error: resource_limit: {len(pipeline.stages)} stages exceeds "
              f"--max-stages {args.max_stages}", file=sys.stderr)
        return 5
    hard = [d for s in pipeline.stages for d in s.diagnostics
            if d.kind == "error"]
    if hard:
        for d in hard:
            print(f"error: parse_error: {d.file}:{d.line}:{d.col}: {d.reason}",
                  file=sys.stderr)
        return 2
    _emit_diagnostics(pipeline)
    return root, pipeline


def _drift_args(prog: str, description: str, argv: list[str], add_json: bool):
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("root", nargs="?", default=".",
                        help="pipeline directory (searched recursively for *.sql)")
    parser.add_argument("--threshold", type=float, default=0.75, metavar="F",
                        help="rename similarity gate 0.0-1.0 (default: 0.75)")
    if add_json:
        parser.add_argument("--json", action="store_true",
                            help="emit a driftguard.drift.v1 artifact")
        parser.add_argument("--markdown", action="store_true",
                            help="emit a markdown report")
    parser.add_argument("--no-persist", action="store_true",
                        help="skip all SQLite writes")
    parser.add_argument("--db", default=os.environ.get(_ENV_DB, "driftguard.db"),
                        help="SQLite persistence file (default: driftguard.db)")
    parser.add_argument("--max-stages", type=int, default=_MAX_STAGES,
                        help="refuse to analyze pipelines with more stages")
    return parser.parse_args(argv)


def _drift_pipeline(args) -> tuple[Path, object, list, object, list, bool] | int:
    """Shared drift computation; returns (root, pipeline, stages, lineage,
    drifts, breaking) or an exit code."""
    if not 0.0 <= args.threshold <= 1.0:
        print(f"error: usage: --threshold must be in [0.0, 1.0], got "
              f"{args.threshold!r}", file=sys.stderr)
        return 2
    res = _load_pipeline(args)
    if isinstance(res, int):
        return res
    root, pipeline = res
    from driftguard.core.parser.dialects.dbt import find_sources
    source_tables = find_sources(root)
    stages = [Stage(name=s.name, path=s.path, refs=s.ref_names,
                    columns=s.column_names, raw=s.raw,
                    sources=[(src.source, src.table) for src in s.sources])
              for s in pipeline.stages]
    lineage = build_lineage(stages, source_tables)
    drifts = detect_drifts(lineage, args.threshold)
    return root, pipeline, stages, lineage, drifts, any(d.breaking for d in drifts)


def _cmd_drift(argv: list[str]) -> int:
    if argv and argv[0] == "diff" and not Path(argv[0]).is_dir():
        return _cmd_drift_diff(argv[1:])
    args = _drift_args(
        "driftguard drift",
        "Schema-drift gate (MVP): removed/renamed columns are breaking, "
        "added are non-breaking, identical is clean.",
        argv, add_json=True)
    res = _drift_pipeline(args)
    if isinstance(res, int):
        return res
    root, pipeline, stages, lineage, drifts, breaking = res

    run_id = None
    if not args.no_persist:
        store = Store(args.db)
        run_id = store.save_run(str(root), stages, lineage, drifts)
        store.close()

    if args.json:
        envelope = {
            "schema": "driftguard.drift.v1",
            "version": 1,
            "run_id": run_id,
            "root": root.as_posix() or ".",
            "checked_at": _checked_at(),
            "git_sha": _git_sha(root),
            "pipeline_fingerprint": pipeline.fingerprint,
            "threshold": args.threshold,
            "stages": len(stages),
            "edges": len(lineage.edges),
            "cycles": len(lineage.cycles),
            "breaking": breaking,
            "drifts": [drift_to_dict(d) for d in drifts],
        }
        print(json.dumps(envelope, indent=2))
    else:
        out = report_markdown(stages, lineage, drifts) if args.markdown \
            else report_text(stages, lineage, drifts)
        print(out, end="")
        if run_id is not None:
            print(f"(run #{run_id} persisted to {args.db})")
    return 1 if breaking else 0


def _cmd_drift_diff(argv: list[str]) -> int:
    args = _drift_args(
        "driftguard drift diff",
        "Dry-run preview: render each schema drift as a unified diff.",
        argv, add_json=False)
    res = _drift_pipeline(args)
    if isinstance(res, int):
        return res
    root, pipeline, stages, lineage, drifts, breaking = res

    run_id = None
    if not args.no_persist:
        store = Store(args.db)
        run_id = store.save_run(str(root), stages, lineage, drifts)
        store.close()

    for i, drift in enumerate(drifts):
        if i:
            print()
        print(schema_diff(drift), end="")
    if run_id is not None:
        print(f"(run #{run_id} persisted to {args.db})")
    return 1 if breaking else 0


# ---- scan (Phase 4: security baseline) ---------------------------------------


def _scan_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="driftguard scan",
        description="Security baseline (SEC-001..005): secrets, SQL "
                    "interpolation, unsafe subprocess, credentials in "
                    "connection strings, plaintext SQL auth.")
    parser.add_argument("root", nargs="?", default=".",
                        help="directory to scan (recursive)")
    parser.add_argument("--severity", default="medium",
                        choices=["critical", "high", "medium", "low"],
                        help="report filter: only findings at or above this "
                             "severity (default: medium)")
    parser.add_argument("--fail-on-severity", default="high",
                        help="gate threshold: exit 1 when a reported finding "
                             "is at or above this level; 'none' disables the "
                             "gate (default: high)")
    parser.add_argument("--max-findings", type=int, default=500,
                        help="abort (exit 5) when findings reach this cap "
                             "(default: 500)")
    parser.add_argument("--json", action="store_true",
                        help="emit a driftguard.scan.v1 artifact")
    parser.add_argument("--no-persist", action="store_true",
                        help="skip all SQLite writes")
    parser.add_argument("--db", default=os.environ.get(_ENV_DB, "driftguard.db"),
                        help="SQLite persistence file (default: driftguard.db)")
    return parser.parse_args(argv)


def _cmd_scan(argv: list[str]) -> int:
    args = _scan_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"error: no_input: {root} is not a directory", file=sys.stderr)
        return 2
    findings, files, capped = scan_root(
        root, max_findings=args.max_findings)
    if capped:
        print(f"error: resource_limit: reached --max-findings "
              f"{args.max_findings}", file=sys.stderr)
        return 5

    if args.severity != "low":
        findings = [f for f in findings
                    if at_least(f.severity, args.severity)]

    gate = "failed" if any(at_least(f.severity, args.fail_on_severity)
                           for f in findings if f.status == "open") else "passed"
    run_id = None
    if not args.no_persist:
        store = Store(args.db)
        run_id = store.save_scan(str(root), findings)
        store.close()

    if args.json:
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0,
                  "suppressed": 0}
        for f in findings:
            if f.status == "suppressed":
                counts["suppressed"] += 1
            else:
                counts[f.severity] += 1
        envelope = {
            "schema": "driftguard.scan.v1",
            "version": 1,
            "run_id": run_id,
            "root": root.as_posix() or ".",
            "checked_at": _checked_at(),
            "git_sha": _git_sha(root),
            "severity": args.severity,
            "fail_on_severity": args.fail_on_severity,
            "gate": gate,
            "counts": counts,
            "findings": [f.to_dict() for f in findings],
        }
        print(json.dumps(envelope, indent=2))
    else:
        for f in findings:
            print(f"  {f.rule_id} {f.severity} {f.path}:{f.line}:{f.col} "
                  f"[{f.span[0]}, {f.span[1]}] {f.hint}"
                  + ("" if f.status == "open" else " (suppressed)"))
            print(f"      {f.snippet_redacted}")
        total = sum(1 for f in findings if f.status == "open")
        suppressed = sum(1 for f in findings if f.status == "suppressed")
        print(f"files={files} findings={total} "
              f"suppressed={suppressed} gate={gate}")
        if run_id is not None:
            print(f"(run #{run_id} persisted to {args.db})")
    return 1 if gate == "failed" else 0


# ---- refactor (Phase 4: rule engine + state machine) -------------------------

_REFACTOR_SUB = {"plan", "analyze", "approve", "dry-run", "apply", "verify"}
_MAX_RISKS = ("safe", "suggested", "risky")


def _cmd_refactor(argv: list[str]) -> int:
    if not argv or argv[0] not in _REFACTOR_SUB:
        print("error: usage: refactor <plan|analyze|approve|dry-run|apply|"
              "verify> ...", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "analyze":
        return _refactor_analyze(rest)
    if cmd == "plan":
        return _refactor_plan(rest)
    if cmd == "approve":
        return _refactor_approve(rest)
    if cmd == "dry-run":
        return _refactor_dry_run(rest)
    if cmd == "apply":
        return _refactor_apply(rest)
    return _refactor_verify(rest)


def _refactor_args(prog: str, description: str, argv: list[str],
                   add_root: bool = True):
    parser = argparse.ArgumentParser(prog=prog, description=description)
    if add_root:
        parser.add_argument("root", nargs="?", default=".",
                            help="pipeline directory (searched recursively "
                                 "for *.sql)")
    parser.add_argument("--db", default=os.environ.get(_ENV_DB,
                                                       "driftguard.db"),
                        help="SQLite persistence file (default: driftguard.db)")
    parser.add_argument("--json", action="store_true",
                        help="emit the JSON envelope on stdout")
    return parser


def _refactor_session_args(parser, argv) -> argparse.Namespace:
    parser.add_argument("--session", type=int, metavar="ID",
                        help="continue an existing session")
    return parser.parse_args(argv)


def _llm_args(parser) -> None:
    parser.add_argument("--llm-suggestions", action="store_true",
                        help="request Ollama suggestions (API_SPEC §7)")
    parser.add_argument("--llm", action="store_true", dest="llm_suggestions",
                        help=argparse.SUPPRESS)  # deprecated alias (R-2)
    parser.add_argument("--llm-min-confidence", type=float,
                        default=0.7, metavar="FLOAT",
                        help="drop LLM suggestions below this confidence "
                             "(default: 0.7)")
    parser.add_argument("--llm-base-url", default=os.environ.get(
        "DRIFTGUARD_LLM_BASE_URL", "http://localhost:11434"),
        metavar="URL", help="Ollama base URL (default: "
                            "http://localhost:11434)")
    parser.add_argument("--llm-model", default=os.environ.get(
        "DRIFTGUARD_LLM_MODEL", "qwen2.5-coder:7b"), metavar="NAME",
        help="Ollama model for suggestions (default: qwen2.5-coder:7b)")
    parser.add_argument("--llm-timeout", type=int, default=30, metavar="SECS",
                        help="per-call timeout (default: 30)")
    parser.add_argument("--max-llm-suggestions", type=int, default=50,
                        metavar="N",
                        help="cap on accepted suggestions per run "
                             "(default: 50)")


def _rules_dir_args(parser) -> None:
    parser.add_argument("--rules-dir", metavar="DIR",
                        help="load Rule-protocol plugins from DIR "
                             "(trusted-code seam; *.py modules with rule "
                             "objects)")


def _refactor_analyze(argv: list[str]) -> int:
    parser = _refactor_args("driftguard refactor analyze",
                            "Parse + baseline security scan + rule analysis "
                            "(parsed -> analyzed).", argv)
    parser.add_argument("--rules", metavar="LIST",
                        help="comma-separated rule ids (default: all)")
    parser.add_argument("--max-risk", choices=_MAX_RISKS, default="safe",
                        help="exclude rules above this ADR-006 risk tier "
                             "(default: safe)")
    parser.add_argument("--max-stages", type=int, default=_MAX_STAGES,
                        help="refuse pipelines with more stages")
    parser.add_argument("--session", type=int, metavar="ID",
                        help="continue an existing session")
    _llm_args(parser)
    _rules_dir_args(parser)
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"error: no_input: {root} is not a directory", file=sys.stderr)
        return 2

    store = Store(args.db)
    try:
        session = _resume_or_create(store, args, root)
        try:
            session, analysis = _parse_analyze(store, session, root, args)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            session = _abort(store, session, "ANALYZE", 2, str(exc))
            return 2
        except LlmUnavailable as exc:
            print(f"error: llm_unavailable: {exc}", file=sys.stderr)
            session = _abort(store, session, "ANALYZE", 2, str(exc))
            return 2
        _print_analysis(analysis, root)
        if args.json:
            print(json.dumps(_analysis_envelope(analysis, session, root,
                                                args), indent=2))
        return 0
    finally:
        store.close()
    return 0


def _refactor_plan(argv: list[str]) -> int:
    parser = _refactor_args("driftguard refactor plan",
                            "Parse, analyze, and write a refactor plan "
                            "(parsed -> analyzed -> planned).", argv)
    parser.add_argument("--rules", metavar="LIST",
                        help="comma-separated rule ids (default: all)")
    parser.add_argument("--max-risk", choices=_MAX_RISKS, default="safe",
                        help="exclude rules above this ADR-006 risk tier "
                             "(default: safe)")
    parser.add_argument("--max-stages", type=int, default=_MAX_STAGES,
                        help="refuse pipelines with more stages")
    parser.add_argument("--allow-on-finding", action="store_true",
                        help="plan candidates that overlap critical/high "
                             "security findings (blocked by default)")
    parser.add_argument("--session", type=int, metavar="ID",
                        help="continue an existing session")
    parser.add_argument("--out", metavar="FILE", default="refactor_plan.json",
                        help="plan file (default: refactor_plan.json)")
    _llm_args(parser)
    _rules_dir_args(parser)
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"error: no_input: {root} is not a directory", file=sys.stderr)
        return 2

    store = Store(args.db)
    session = None
    try:
        session = _resume_or_create(store, args, root)
        try:
            session, analysis = _parse_analyze(store, session, root, args,
                                               args.allow_on_finding)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            session = _abort(store, session, "PLAN", 2, str(exc))
            return 2
        except LlmUnavailable as exc:
            print(f"error: llm_unavailable: {exc}", file=sys.stderr)
            session = _abort(store, session, "PLAN", 2, str(exc))
            return 2

        if len(analysis["candidates"]) + len(analysis["blocked"]) \
                > MAX_CANDIDATES:
            print(f"error: resource_limit: more than {MAX_CANDIDATES} "
                  f"candidates", file=sys.stderr)
            session = _abort(store, session, "PLAN", 5,
                             "candidate cap exceeded")
            return 5

        plan = build_plan(analysis, session["session_id"], root,
                          args.max_risk, args.rules.split(",")
                          if args.rules else [])
        plan_path = Path(args.out)
        write_plan(plan, plan_path)
        run_id = None
        session = mark_planned(store, session, str(plan_path),
                               plan["plan_hash"], run_id,
                               len(plan["items"]), len(analysis["blocked"]))
        persist_plan(store, session["session_id"], run_id, plan)
        print(f"plan: {len(plan['items'])} candidate(s) written to "
              f"{plan_path} (session #{session['session_id']})")
        if analysis["blocked"]:
            for b in analysis["blocked"]:
                print(f"  blocked {b['rule_id']} {b['stage']} "
                      f"{b['path']}:{b['span'][0]}: {b['block_reason']}")
        if args.json:
            print(json.dumps(plan, indent=2))
        return 1 if not plan["items"] and analysis["blocked"] else 0
    finally:
        store.close()


def _refactor_approve(argv: list[str]) -> int:
    parser = _refactor_args("driftguard refactor approve",
                            "Approve the plan of a session (planned -> "
                            "approved).", argv, add_root=False)
    parser.add_argument("--session", type=int, required=True, metavar="ID")
    parser.add_argument("--ci", action="store_true",
                        help="record the approval as a committed-plan CI "
                             "approval")
    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        session = store.get_session(args.session)
        if session is None:
            print(f"error: no_input: no session #{args.session}",
                  file=sys.stderr)
            return 2
        if session["state"] != "planned":
            print(f"error: state_error: session #{args.session} is "
                  f"{session['state']!r}, requires 'planned'",
                  file=sys.stderr)
            return 2
        mark_approved(store, session, "ci_committed_plan" if args.ci
                      else "cli")
        print(f"session #{args.session} approved")
        return 0
    finally:
        store.close()


def _refactor_dry_run(argv: list[str]) -> int:
    parser = _refactor_args("driftguard refactor dry-run",
                            "Render the plan as a diff preview (no state "
                            "change).", argv, add_root=False)
    parser.add_argument("--plan", metavar="FILE", required=True,
                        help="plan file to preview")
    args = parser.parse_args(argv)
    try:
        plan = read_plan(Path(args.plan))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: plan_error: {exc}", file=sys.stderr)
        return 2
    for it in plan["items"]:
        print(f"  {it['change_id']} {it['rule_id']} ({it['tier']}) "
              f"{it['stage']} {it['path']}:{it['span'][0]}"
              + (f"  [{it['security_note']}]" if it["security_note"] else ""))
        print(f"      reason: {it['reason']}")
        print(f"      - {it['before']!r}")
        print(f"      + {it['after']!r}")
    print(f"({len(plan['items'])} candidate(s) in {args.plan})")
    return 0


def _refactor_apply(argv: list[str]) -> int:
    parser = _refactor_args("driftguard refactor apply",
                            "Apply an approved plan (approved -> applied).",
                            argv, add_root=False)
    parser.add_argument("--plan", metavar="FILE", required=True,
                        help="plan file to apply")
    parser.add_argument("--in-place", action="store_true",
                        help="rewrite files in the pipeline directory")
    parser.add_argument("--out-dir", metavar="DIR",
                        help="write rewritten files under DIR (keeps the "
                             "pipeline untouched)")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip .orig backups")
    parser.add_argument("--no-persist", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--ci", action="store_true",
                        help="approve (ci_committed_plan) and apply in one "
                             "command")
    args = parser.parse_args(argv)
    if args.no_persist:
        print("error: state_error: apply requires persistence "
              "(--no-persist unsupported)", file=sys.stderr)
        return 2
    if args.in_place == (args.out_dir is not None):
        print("error: usage: exactly one of --in-place or --out-dir",
              file=sys.stderr)
        return 2

    try:
        plan = read_plan(Path(args.plan))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: plan_error: {exc}", file=sys.stderr)
        return 2

    store = Store(args.db)
    try:
        session = store.get_session(plan["session_id"])
        if session is None:
            print(f"error: state_error: plan references unknown session "
                  f"#{plan['session_id']}", file=sys.stderr)
            return 2
        if args.ci:
            if session["state"] != "planned":
                print(f"error: state_error: session #{session['session_id']} "
                      f"is {session['state']!r}, requires 'planned' for --ci",
                      file=sys.stderr)
                return 2
            session = mark_approved(store, session, "ci_committed_plan")
        if session["state"] == "done":
            # re-apply of a closed session: idempotent no-op (all items
            # already applied), no state transition.
            db_plan = store.get_plan(session_id=session["session_id"])
            done_hashes = {it["item_hash"] for it in (db_plan or
                                                      {"items": []})["items"]
                           if it["state"] in ("applied", "noop", "skipped")}
            try:
                result = apply_plan(Path(args.plan), "in_place"
                                    if args.in_place else "out_dir",
                                    Path(args.out_dir) if args.out_dir
                                    else None, args.no_backup, done_hashes)
            except ApplyError as exc:
                print(f"error: {exc}", file=sys.stderr)
                session = _abort(store, session, "APPLY", 2, str(exc))
                return 2
            _print_apply(plan, result, "in_place" if args.in_place
                         else "out_dir")
            for row in result["skipped"]:
                print(f"  warning: {row['rule_id']} {row['stage']} already "
                      f"applied, skipped", file=sys.stderr)
            return 0
        if session["state"] != "approved":
            print(f"error: state_error: session #{session['session_id']} is "
                  f"{session['state']!r}, requires 'approved'",
                  file=sys.stderr)
            return 2
        if session.get("plan_hash") and session["plan_hash"] != \
                plan.get("plan_hash"):
            print("error: plan_error: plan hash does not match the "
                  "session's recorded plan", file=sys.stderr)
            return 2

        db_plan = store.get_plan(session_id=session["session_id"])
        done_hashes = {it["item_hash"] for it in (db_plan or
                                                  {"items": []})["items"]
                       if it["state"] in ("applied", "noop", "skipped")}
        try:
            result = apply_plan(Path(args.plan), "in_place"
                                if args.in_place else "out_dir",
                                Path(args.out_dir) if args.out_dir else None,
                                args.no_backup, done_hashes)
        except ApplyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            session = _abort(store, session, "APPLY", 2, str(exc))
            return 2

        mode = "in_place" if args.in_place else "out_dir"
        _persist_apply(store, plan, result, mode)
        summary = plan_summary(result)
        session = mark_applied(store, session, plan["plan_hash"], summary,
                               None)
        _print_apply(plan, result, mode)
        for row in result["skipped"]:
            print(f"  warning: {row['rule_id']} {row['stage']} already "
                  f"applied, skipped", file=sys.stderr)
        if args.json:
            envelope = {
                "schema": "driftguard.apply.v1", "version": 1,
                "session_id": session["session_id"],
                "plan_hash": plan["plan_hash"], "mode": mode,
                "backups": result["backups"],
                "items": result["applied"] + result["noop"]
                         + result["skipped"],
                "summary": summary,
            }
            print(json.dumps(envelope, indent=2))
        return 0
    finally:
        store.close()


def _refactor_verify(argv: list[str]) -> int:
    parser = _refactor_args("driftguard refactor verify",
                            "Verify an applied session (applied -> verified/"
                            "done, or back to approved on regression).",
                            argv, add_root=False)
    parser.add_argument("--session", type=int, required=True, metavar="ID")
    parser.add_argument("--severity", default="medium", choices=[
        "critical", "high", "medium", "low"],
        help="security regression gate: findings at/above this severity "
             "fail verification (default: medium)")
    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        session = store.get_session(args.session)
        if session is None:
            print(f"error: no_input: no session #{args.session}",
                  file=sys.stderr)
            return 2
        if session["state"] != "applied":
            print(f"error: state_error: session #{args.session} is "
                  f"{session['state']!r}, requires 'applied'",
                  file=sys.stderr)
            return 2
        try:
            plan = read_plan(Path(session["plan_path"]))
            root = Path(plan["root"])
        except (TypeError, OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: plan_error: cannot reload the session plan: "
                  f"{exc}", file=sys.stderr)
            return 2
        problems = _verify_session(store, session, root, args.severity)
        if problems:
            session = mark_verified(store, session, ok=False)
            for p in problems:
                print(f"  regression: {p}", file=sys.stderr)
            print(f"session #{args.session} verification FAILED "
                  f"(back to approved)", file=sys.stderr)
            return 1
        session = mark_verified(store, session, ok=True)
        store.audit_log(session["session_id"], "CLOSE", "verified", "done",
                        None, None, 0)
        store.set_session_state(session["session_id"], "done")
        print(f"session #{args.session} verified and closed")
        return 0
    finally:
        store.close()


def _verify_session(store: Store, session: dict, root: Path,
                    severity: str) -> list[str]:
    from driftguard.core.refactor.planner import MAX_CANDIDATES
    problems = []
    try:
        analysis = _run_analysis_planning(root, session["rule_ids"],
                                          session["max_risk"], False,
                                          _session_rules_dir(session))
    except ValueError as exc:
        return [str(exc)]
    remaining = analysis["candidates"]
    for it in remaining:
        problems.append(f"{it['rule_id']} {it['stage']} {it['path']}:"
                        f"{it['span'][0]} still matches")
    findings, _, _ = scan_root(root, max_findings=sys.maxsize)
    for f in findings:
        if f.status == "open" and at_least(f.severity, severity):
            problems.append(f"{f.rule_id} {f.severity} {f.path}:{f.line}")
    return problems


def _run_analysis_planning(root: Path, rules: list[str] | None,
                           max_risk: str, allow_on_finding: bool,
                           rules_dir: Path | None = None) -> dict:
    return analyze_pipeline(root, rules, max_risk, allow_on_finding,
                            rules_dir)


def _session_rules_dir(session: dict) -> Path | None:
    rd = session.get("rules_dir")
    return Path(rd) if rd else None


def _resume_or_create(store: Store, args, root: Path) -> dict:
    sid = getattr(args, "session", None)
    if sid is not None:
        session = store.get_session(sid)
        if session is None:
            raise ValueError(f"no_input: no session #{sid}")
        return session
    rules = args.rules.split(",") if args.rules else []
    return create_session(store, _repo_fingerprint(root), rules,
                          args.max_risk,
                          llm_used=bool(getattr(args, "llm_suggestions",
                                                False)),
                          rules_dir=getattr(args, "rules_dir", None))


def _repo_fingerprint(root: Path) -> str:
    return _git_sha(root) or str(root.resolve())


def _parse_analyze(store: Store, session: dict, root: Path, args,
                   allow_on_finding: bool = False) -> tuple[dict, dict]:
    if session["state"] in ("start", "parsed"):
        pipeline = DbtParser().parse_project(root)
        if not pipeline.stages:
            raise ValueError("no_input: no *.sql pipeline stages found")
        if len(pipeline.stages) > args.max_stages:
            raise ValueError(
                f"resource_limit: {len(pipeline.stages)} stages exceeds "
                f"--max-stages {args.max_stages}")
        run_id = _persist_pipeline(store, pipeline, root)
        session = mark_parsed(store, session, run_id)
    analysis = _run_analysis_planning(root, session["rule_ids"],
                                      session["max_risk"], allow_on_finding,
                                      _session_rules_dir(session))
    if session["state"] != "analyzed":
        run_id = _persist_analysis(store, analysis, root)
        session = mark_analyzed(store, session, run_id,
                                _analysis_summary(analysis))
    if getattr(args, "llm_suggestions", False):
        analysis = _apply_llm(analysis, args, root, session["max_risk"])
    return session, analysis


def _apply_llm(analysis: dict, args, root: Path, max_risk: str) -> dict:
    """Run the Ollama suggestion channel (API_SPEC §7); merge accepted
    suggestions into the analysis. LlmUnavailable propagates to the CLI."""
    from driftguard.core.refactor.planner import merge_suggestions
    from driftguard.llm import LlmClient, request_suggestions

    def warn(message: str) -> None:
        print(f"warning: {message}", file=sys.stderr)

    client = LlmClient(args.llm_base_url, args.llm_model, args.llm_timeout)
    suggestions, _ = request_suggestions(
        client, root, analysis["pipeline"], analysis["candidates"],
        analysis["findings"], args.llm_min_confidence,
        args.max_llm_suggestions, warn=warn)
    analysis, added, blocked = merge_suggestions(
        analysis, suggestions, getattr(args, "allow_on_finding", False),
        max_risk)
    analysis["llm"] = {"used": True, "suggestions": len(added),
                       "blocked": len(blocked), "model": args.llm_model,
                       "base_url": args.llm_base_url}
    return analysis


def _analysis_summary(analysis: dict) -> dict:
    return {"candidates": len(analysis["candidates"]),
            "blocked": len(analysis["blocked"]),
            "findings": len(analysis["findings"])}


def _persist_pipeline(store: Store, pipeline, root: Path) -> int | None:
    stages = [Stage(name=s.name, path=s.path, refs=s.ref_names,
                    columns=s.column_names, raw=s.raw)
              for s in pipeline.stages]
    return store.save_stages(str(root), stages)


def _persist_analysis(store: Store, analysis: dict, root: Path) -> int | None:
    stages = [Stage(name=s.name, path=s.path, refs=s.ref_names,
                    columns=s.column_names, raw=s.raw)
              for s in analysis["pipeline"].stages]
    run_id = store.save_stages(str(root), stages)
    return run_id


def _persist_apply(store: Store, plan: dict, result: dict,
                   mode: str) -> None:
    db_plan = store.get_plan(session_id=plan["session_id"])
    if db_plan is None:
        return
    for row in result["applied"] + result["noop"] + result["skipped"]:
        store.update_plan_item(db_plan["plan_id"], row["item_hash"],
                               row["status"], row["fingerprint_after"])
    store.set_plan_status(db_plan["plan_id"], "applied")


def _print_apply(plan: dict, result: dict, mode: str) -> None:
    print(f"applied {len(result['applied'])} item(s), "
          f"{len(result['noop'])} noop, {len(result['skipped'])} skipped "
          f"({mode})")
    for row in result["applied"] + result["noop"]:
        print(f"  {row['change_id']} {row['rule_id']} {row['stage']} "
              f"{row['path']} {row['status']}")
    for b in result["backups"]:
        print(f"  backup: {b['path']} -> {b['backup']}")


def _print_analysis(analysis: dict, root: Path) -> None:
    print(f"analyzed {root.as_posix() or '.'}: "
          f"{len(analysis['candidates'])} candidate(s), "
          f"{len(analysis['blocked'])} blocked, "
          f"{len(analysis['findings'])} security finding(s)")
    for it in analysis["candidates"]:
        print(f"  {it['rule_id']} ({it['tier']}) {it['stage']} "
              f"{it['path']}:{it['span'][0]}  {it['reason']}"
              + (f"  [{it['security_note']}]" if it["security_note"]
                 else ""))
    for b in analysis["blocked"]:
        print(f"  blocked {b['rule_id']} {b['stage']} {b['path']}:"
              f"{b['span'][0]}: {b['block_reason']}")


def _analysis_envelope(analysis: dict, session: dict, root: Path,
                       args) -> dict:
    lineage = build_lineage(analysis["pipeline"].stages)
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0,
              "suppressed": 0}
    for f in analysis["findings"]:
        if f.status == "suppressed":
            counts["suppressed"] += 1
        else:
            counts[f.severity] += 1
    return {
        "schema": "driftguard.analysis.v1", "version": 1,
        "session_id": session["session_id"],
        "root": root.as_posix() or ".", "checked_at": _checked_at(),
        "git_sha": _git_sha(root),
        "pipeline_fingerprint": analysis["pipeline"].fingerprint,
        "baseline_scan": {"findings": [f.to_dict()
                                       for f in analysis["findings"]],
                          "counts": counts},
        "lineage": {"edges": len(lineage.edges),
                    "cycles": len(lineage.cycles),
                    "missing": len(lineage.missing),
                    "topo_order": lineage.topo_order},
        "candidates": analysis["candidates"],
        "blocked": analysis["blocked"],
        "llm": analysis.get("llm",
                            {"used": False, "suggestions": []}),
        "state": session["state"],
    }


def _abort(store: Store, session: dict, action: str, code: int,
           reason: str) -> dict:
    try:
        mark_aborted(store, session, action, code, reason)
    except Exception:
        pass
    return store.get_session(session["session_id"]) if session else session


# ---- session / audit ----------------------------------------------------------


def _cmd_session(argv: list[str]) -> int:
    if not argv or argv[0] != "show":
        print("error: usage: session show <ID>", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="driftguard session show")
    parser.add_argument("--db", default=os.environ.get(_ENV_DB,
                                                       "driftguard.db"))
    parser.add_argument("id", type=int)
    args = parser.parse_args(argv[1:])
    store = Store(args.db)
    try:
        session = store.get_session(args.id)
        if session is None:
            print(f"error: no_input: no session #{args.id}",
                  file=sys.stderr)
            return 2
        print(f"session #{session['session_id']} state={session['state']}")
        print(f"  repo_fingerprint: {session['repo_fingerprint']}")
        print(f"  rules: {', '.join(session['rule_ids']) or 'all'}")
        print(f"  rules_dir: {session.get('rules_dir') or '-'}")
        print(f"  max_risk: {session['max_risk']}")
        print(f"  plan: {session['plan_path'] or '-'} "
              f"{session['plan_hash'] or ''}")
        print(f"  llm_used: {session['llm_used']}")
        plan = store.get_plan(session_id=args.id)
        if plan:
            counts = {}
            for it in plan["items"]:
                counts[it["state"]] = counts.get(it["state"], 0) + 1
            print(f"  plan #{plan['plan_id']}: {plan['item_count']} "
                  f"item(s) {counts}")
        rows = store.audit_rows(args.id, limit=10)
        for r in reversed(rows):
            print(f"  {r['ts']} {r['action']} "
                  f"{r['from_state'] or '-'} -> {r['to_state'] or '-'} "
                  f"(exit {r['exit_code']})")
        return 0
    finally:
        store.close()


def _cmd_audit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="driftguard audit",
        description="Audit trail of refactor sessions (args always "
                    "redacted).")
    parser.add_argument("--db", default=os.environ.get(_ENV_DB,
                                                       "driftguard.db"))
    parser.add_argument("--session", type=int, metavar="ID")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--json", action="store_true",
                        help="emit the JSON envelope on stdout")
    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        rows = store.audit_rows(args.session, args.limit)
        if args.json:
            envelope = {"schema": "driftguard.audit.v1", "version": 1,
                        "rows": [{"id": r["id"], "session_id": r[
                            "session_id"], "ts": r["ts"],
                            "action": r["action"], "from_state": r[
                            "from_state"], "to_state": r["to_state"],
                            "args_json": r["args_json"], "result_json": r[
                            "result_json"], "exit_code": r["exit_code"]}
                            for r in rows]}
            print(json.dumps(envelope, indent=2))
        else:
            for r in reversed(rows):
                print(f"{r['ts']} #{r['session_id'] or '-'} {r['action']} "
                      f"{r['from_state'] or '-'} -> {r['to_state'] or '-'} "
                      f"exit={r['exit_code']}")
        return 0
    finally:
        store.close()


# ---- legacy bare command (byte-compatible with the seed) ----------------------


def _legacy_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="driftguard",
        description="Schema-drift safety for SQL pipelines (dbt-style). "
                    "Fails (exit 1) when a refactor breaks downstream schema.")
    parser.add_argument("root", nargs="?", default=".",
                        help="pipeline directory (searched recursively for *.sql)")
    parser.add_argument("--db", default=os.environ.get(_ENV_DB, "driftguard.db"),
                        help="SQLite persistence file (default: driftguard.db)")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON instead of text")
    parser.add_argument("--markdown", action="store_true",
                        help="emit a markdown report")
    parser.add_argument("--no-persist", action="store_true",
                        help="skip writing to the SQLite store")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    stages = parse_pipeline(root)
    if not stages:
        print("error: no *.sql pipeline stages found", file=sys.stderr)
        return 2
    lineage = build_lineage(stages)
    drifts = detect_drifts(lineage)
    breaking = any(d.breaking for d in drifts)

    payload = {
        "stages": len(stages),
        "edges": len(lineage.edges),
        "cycles": len(lineage.cycles),
        "drifts": [{
            "producer": d.producer,
            "consumer": d.consumer,
            "added": d.added,
            "removed": d.removed,
            "renamed": d.renamed,
            "breaking": d.breaking,
        } for d in drifts],
    }

    if not args.no_persist:
        store = Store(args.db)
        payload["run_id"] = store.save_run(str(root), stages, lineage, drifts)
        if not args.json:
            out = report_markdown(stages, lineage, drifts) if args.markdown \
                else report_text(stages, lineage, drifts)
            print(out, end="")
            print(f"(run #{payload['run_id']} persisted to {args.db})")
        store.close()
    if args.json:
        print(json.dumps(payload, indent=2))
    elif args.no_persist:
        print(report_text(stages, lineage, drifts), end="")
    return 1 if breaking else 0


if __name__ == "__main__":
    sys.exit(main())