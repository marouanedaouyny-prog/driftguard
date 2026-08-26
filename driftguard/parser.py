"""Compat shim (Phase 1): one parser — the core engine.

`driftguard/parser.py` now delegates to the core recursive-descent parser
(`driftguard.core.parser.dialects.dbt`) and exposes the seed's flat `Stage`
contract (name/path/refs: list[str]/columns: list[str]/raw) so the flat
lineage/drift/store/report modules and the existing test suite keep working
unchanged during the `core/` reorganization. The seed's regex parsing is
gone — there is exactly one parser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from driftguard.core.parser.dialects.dbt import (
    parse_pipeline as _core_parse_pipeline,
)
from driftguard.core.parser.dialects.dbt import (
    parse_sql_file as _core_parse_sql_file,
)

__all__ = ["Stage", "parse_pipeline", "parse_sql_file"]


@dataclass
class Stage:
    name: str
    path: Path
    refs: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    opaque: bool = False
    raw: str = ""
    sources: list[tuple[str, str]] = field(default_factory=list)


def _convert(core) -> Stage:
    return Stage(name=core.name, path=core.path, refs=core.ref_names,
                 columns=core.column_names, raw=core.raw,
                 sources=[(s.source, s.table) for s in core.sources])


def parse_sql_file(path: Path) -> Stage | None:
    core = _core_parse_sql_file(path)
    if core is None:
        return None
    return _convert(core)


def parse_pipeline(root: Path) -> list[Stage]:
    core_pipeline = _core_parse_pipeline(root)
    return [_convert(s) for s in core_pipeline.stages]