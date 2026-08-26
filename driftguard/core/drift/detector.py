"""Schema drift detection between producers and consumers.

A consumer stage documents the columns it expects from each producer (its
SELECT projection on that stage). Drift = producer's current schema no longer
satisfies the consumer: removed columns are breaking, added columns are
non-breaking, renames are detected by best-match similarity (configurable
`threshold`, default 0.75).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from driftguard.core.drift.similarity import find_rename

DEFAULT_THRESHOLD = 0.75


@dataclass
class Drift:
    producer: str
    consumer: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def breaking(self) -> bool:
        return bool(self.removed) or bool(self.renamed)


def drift_to_dict(d: Drift) -> dict:
    return {"producer": d.producer, "consumer": d.consumer, "added": d.added,
            "removed": d.removed, "renamed": d.renamed, "breaking": d.breaking}


def detect_drifts(lineage, threshold: float = DEFAULT_THRESHOLD) -> list[Drift]:
    drifts: list[Drift] = []
    for producer, consumer in lineage.edges:
        prod_stage = lineage.stages.get(producer)
        cons_stage = lineage.stages.get(consumer)
        if prod_stage is None or cons_stage is None:
            continue
        if not prod_stage.columns:
            continue
        expected = {c: i for i, c in enumerate(cons_stage.columns)}
        for name in set(cons_stage.columns) & set(prod_stage.columns):
            del expected[name]
        taken = set(cons_stage.columns)
        removed, renamed = [], []
        for col in expected:
            candidate = find_rename(col, prod_stage.columns, taken, threshold)
            if candidate:
                taken.add(candidate)
                renamed.append((col, candidate))
            else:
                removed.append(col)
        added = [c for c in prod_stage.columns if c not in cons_stage.columns]
        if removed or renamed or added:
            drifts.append(Drift(producer, consumer, added=added,
                                removed=removed, renamed=renamed))
    return drifts