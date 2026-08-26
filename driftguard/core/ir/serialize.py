"""IR JSON serialization (versioned envelopes) and canonical fingerprinting.

Serialize contract (ARCHITECTURE §4.2 / API_SPEC §1.5):

- Every document is wrapped in a versioned envelope: `"v": 1` plus a
  `"schema"` name like `driftguard.stage.v1` / `driftguard.pipeline.v1`.
- snake_case fields; paths are repo-relative with forward slashes; spans are
  byte offsets as `[start, end]`; fingerprints are `sha256:<hex>`.
- Writers must never remove/rename a field within a schema version.
- Consumers must ignore unknown fields.

Canonical form (used for fingerprints) omits spans so that files differing
only in whitespace/comments compare equal for drift purposes; `apply` edits
operate on raw spans and never depend on whitespace luck.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from driftguard.core.ir.model import Column, Cte, Pipeline, RefEdge, Span, Stage

IR_VERSION = 1


def _path_rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _span_to_list(span: Span | None) -> list[int] | None:
    return None if span is None else span.to_list()


def stage_dict(stage: Stage, root: Path, canonical: bool = False) -> dict:
    def _column(c: Column) -> dict:
        d = {
            "name": c.name,
            "source_expr": c.source_expr,
            "alias": c.alias,
        }
        if not canonical:
            d["span"] = _span_to_list(c.span)
        return d

    def _ref(r: RefEdge) -> dict:
        d = {"name": r.name, "kind": r.kind}
        if not canonical:
            d["span"] = _span_to_list(r.span)
        return d

    def _cte(c: Cte) -> dict:
        d = {"name": c.name}
        if not canonical:
            d["span"] = _span_to_list(c.span)
        return d

    return {
        "schema": "driftguard.stage.v1",
        "v": IR_VERSION,
        "name": stage.name,
        "path": _path_rel(stage.path, root),
        "kind": stage.kind,
        "fingerprint": stage.fingerprint,
        "columns": [_column(c) for c in stage.columns],
        "refs": [_ref(r) for r in stage.refs],
        "sources": [{"source": s.source, "table": s.table} for s in stage.sources],
        "ctes": [_cte(c) for c in stage.ctes],
        "create_name": stage.create_name,
        "dialect_hints": list(stage.dialect_hints),
        "diagnostics": [
            {
                "file": d.file,
                "line": d.line,
                "col": d.col,
                "reason": d.reason,
                "kind": d.kind,
            }
            for d in stage.diagnostics
        ],
    }


def pipeline_dict(pipeline: Pipeline, canonical: bool = False) -> dict:
    return {
        "schema": "driftguard.pipeline.v1",
        "v": IR_VERSION,
        "root": pipeline.root.as_posix() or ".",
        "fingerprint": pipeline.fingerprint,
        "stages": [stage_dict(s, pipeline.root, canonical=canonical)
                   for s in pipeline.stages],
    }


def _canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def stage_fingerprint(stage: Stage, root: Path) -> str:
    """sha256 over the canonical (span-free) stage IR JSON.

    Canonical form covers the parsed contract only (name, kind, columns,
    refs, sources, ctes, create_name); derived or location fields (path,
    dialect_hints, diagnostics) never affect it, so two files differing only
    in whitespace/comments compare equal for drift purposes.
    """
    d = stage_dict(stage, root, canonical=True)
    for key in ("path", "dialect_hints", "diagnostics"):
        d.pop(key, None)
    d["fingerprint"] = ""  # fingerprint is derived, never part of itself
    return _sha(_canonical_json(d))


def pipeline_fingerprint(pipeline: Pipeline) -> str:
    """sha256 over sorted stage fingerprints (ARCHITECTURE §4.2)."""
    fps = sorted(s.fingerprint for s in pipeline.stages)
    return _sha(_canonical_json(fps))


def to_json(obj: object) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"