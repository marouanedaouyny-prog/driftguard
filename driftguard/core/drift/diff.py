"""Unified diff preview of schema drift (dry-run renderer)."""
from __future__ import annotations

from driftguard.core.drift.detector import Drift


def schema_diff(drift: Drift) -> str:
    """Render one drift as a unified schema diff.

    `--- a/<producer> (schema)` is the producer's actual column set; `+++ b/
    <consumer> (expected)` is what the consumer's projection demands. Removed
    columns and rename sources are `-` lines; added columns and rename targets
    are `+` lines.
    """
    lines = [f"--- a/{drift.producer} (schema)",
             f"+++ b/{drift.consumer} (expected)"]
    minus = len(drift.removed) + len(drift.renamed)
    plus = len(drift.renamed) + len(drift.added)
    lines.append(f"@@ -1,{minus} +1,{plus} @@")
    for col in drift.removed:
        lines.append(f"-{col}")
    for old, new in drift.renamed:
        lines.append(f"-{old}")
        lines.append(f"+{new}")
    for col in drift.added:
        lines.append(f"+{col}")
    return "\n".join(lines) + "\n"