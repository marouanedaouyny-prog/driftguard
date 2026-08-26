"""Text/markdown drift reports."""
from __future__ import annotations

from driftguard.drift import Drift
from driftguard.lineage import Lineage
from driftguard.parser import Stage


def report_markdown(stages: list[Stage], lineage: Lineage,
                    drifts: list[Drift]) -> str:
    lines = ["# DriftGuard report", ""]
    lines.append(f"- stages: {len(stages)}")
    lines.append(f"- edges: {len(lineage.edges)}")
    lines.append(f"- missing refs: {len(lineage.missing)}")
    lines.append(f"- drifts: {len(drifts)} "
                 f"({sum(1 for d in drifts if d.breaking)} breaking)")
    if lineage.cycles:
        lines.append(f"- cycles: {len(lineage.cycles)}")
        for cycle in lineage.cycles:
            lines.append(f"  - {' -> '.join(cycle)}")
    if lineage.missing:
        lines.append("")
        lines.append("## Unresolved refs")
        for ref, consumer in lineage.missing:
            lines.append(f"- `{consumer}` references missing stage `{ref}`")
    if not drifts:
        lines.append("")
        lines.append("## Drifts")
        lines.append("None detected.")
        return "\n".join(lines) + "\n"
    lines.append("")
    lines.append("## Drifts")
    for d in drifts:
        flag = "BREAKING" if d.breaking else "warning"
        lines.append(f"### {d.producer} -> {d.consumer} [{flag}]")
        for col in d.removed:
            lines.append(f"- removed: `{col}`")
        for old, new in d.renamed:
            lines.append(f"- renamed: `{old}` -> `{new}`")
        for col in d.added:
            lines.append(f"- added (non-breaking): `{col}`")
    return "\n".join(lines) + "\n"


def report_text(stages: list[Stage], lineage: Lineage,
                drifts: list[Drift]) -> str:
    lines = []
    lines.append(f"stages={len(stages)} edges={len(lineage.edges)} "
                 f"missing_refs={len(lineage.missing)} "
                 f"drifts={len(drifts)} "
                 f"breaking={sum(1 for d in drifts if d.breaking)}")
    for d in drifts:
        parts = []
        if d.removed:
            parts.append("removed: " + ", ".join(d.removed))
        if d.renamed:
            parts.append("renamed: " + ", ".join(f"{a}->{b}" for a, b in d.renamed))
        if d.added:
            parts.append("added: " + ", ".join(d.added))
        lines.append(f"  {d.producer} -> {d.consumer}: " + "; ".join(parts))
    return "\n".join(lines) + "\n"