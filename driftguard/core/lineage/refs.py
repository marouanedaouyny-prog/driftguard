"""Missing-ref bookkeeping (ARCH §4.3: `refs.py`).

A ref is *resolved* when it names an existing stage; a `source()` ref is
resolved when its qualified `source.table` name appears in the project's
`sources.yml` definitions. Everything else is a missing ref, recorded for
reporting — never a silent drop.
"""
from __future__ import annotations

from typing import Iterable


def resolve_refs(stages: Iterable, source_tables: set[str]) -> tuple[
        list[tuple[str, str]], dict[tuple[str, str], str],
        list[tuple[str, str]]]:
    """Resolve every stage's refs/sources into (edges, kinds, missing).

    Args:
        stages: objects with `.name`, `.refs` (names or RefEdge-like) and
            optional `.sources` (SourceRef-like or (source, table) pairs).
        source_tables: qualified `source.table` names from sources.yml.

    Returns:
        edges: deduped [(producer, consumer), ...] in encounter order.
        kinds: {(producer, consumer): kind} with kind in
            {"ref", "bare", "source"}.
        missing: [(ref_name, consumer), ...] — unresolved refs.
    """
    edges: list[tuple[str, str]] = []
    kinds: dict[tuple[str, str], str] = {}
    missing: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(a: str, b: str, kind: str) -> None:
        if (a, b) in seen:
            return
        seen.add((a, b))
        edges.append((a, b))
        kinds[(a, b)] = kind

    def _ref_name(ref) -> str:
        return ref.name if hasattr(ref, "name") else ref

    def _ref_kind(ref) -> str:
        return getattr(ref, "kind", None) or "ref"

    by_name = {s.name: s for s in stages}
    for stage in stages:
        for ref in stage.refs:
            name = _ref_name(ref)
            if name == stage.name:
                continue
            if name in by_name:
                _add(name, stage.name, _ref_kind(ref))
            else:
                missing.append((name, stage.name))
        for src in getattr(stage, "sources", []) or []:
            if hasattr(src, "name"):
                name = src.name
            elif isinstance(src, tuple) and len(src) == 2:
                name = f"{src[0]}.{src[1]}"
            else:
                name = f"{src.source}.{src.table}"
            if name in source_tables:
                _add(name, stage.name, "source")
            else:
                missing.append((name, stage.name))
    return edges, kinds, missing