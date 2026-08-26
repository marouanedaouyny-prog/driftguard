"""Compat shim (Phase 2): seeded lineage logic now lives in
`driftguard.core.lineage` (ARCHITECTURE §4.3). This module re-exports it so
the flat drift/report/CLI consumers and the seed test suite keep working
unchanged. `Lineage.edges` stays `[(producer, consumer)]` (seed contract);
edge kinds live in `Lineage.edge_kinds`.
"""
from driftguard.core.lineage import Lineage, build_lineage

__all__ = ["Lineage", "build_lineage"]