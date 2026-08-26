"""Lineage graph: stages, edges (producer, consumer, kind), cycles, topo.

Seeded logic from `driftguard/lineage.py` moved here in Phase 2
(ARCHITECTURE §4.3). The flat `driftguard/lineage.py` module is now a compat
shim re-exporting this package, so the seed's drift/report/CLI consumers and
the flat test suite keep working unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from driftguard.core.lineage.cycles import find_cycles
from driftguard.core.lineage.refs import resolve_refs
from driftguard.core.lineage.topo import topological_order


@dataclass
class Lineage:
    stages: dict[str, object]
    edges: list[tuple[str, str]] = field(default_factory=list)
    edge_kinds: dict[tuple[str, str], str] = field(default_factory=dict)
    cycles: list[list[str]] = field(default_factory=list)
    missing: list[tuple[str, str]] = field(default_factory=list)
    topo_order: list[str] = field(default_factory=list)

    def consumers(self, name: str) -> list[str]:
        return sorted(b for a, b in self.edges if a == name)

    def producers(self, name: str) -> list[str]:
        return sorted(a for a, b in self.edges if b == name)

    def kind(self, producer: str, consumer: str) -> str:
        return self.edge_kinds.get((producer, consumer), "ref")


def build_lineage(stages: list, source_tables: set[str] | None = None) -> Lineage:
    """Build the stage dependency graph.

    Args:
        stages: pipeline stages (flat shim `Stage` or core `ir.model.Stage`).
        source_tables: qualified `source.table` names defined in the project's
            `sources.yml` files; unresolved source refs become missing refs.
    """
    if source_tables is None:
        source_tables = set()
    by_name = {s.name: s for s in stages}
    edges, kinds, missing = resolve_refs(stages, source_tables)

    graph: dict[str, list[str]] = {s.name: [] for s in stages}
    for a, b in edges:
        graph.setdefault(a, []).append(b)

    lineage = Lineage(
        stages=by_name,
        edges=edges,
        edge_kinds=kinds,
        missing=missing,
        cycles=find_cycles(graph),
    )
    lineage.topo_order = topological_order(list(graph.keys()), edges)
    return lineage


__all__ = ["Lineage", "build_lineage", "find_cycles", "topological_order"]