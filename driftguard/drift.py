"""Schema drift detection between producers and consumers.

Compat shim (Phase 3): the implementation moved to `driftguard/core/drift/`;
this module re-exports the public API so `from driftguard.drift import ...`
imports keep working (legacy tests, store, reports).
"""
from driftguard.core.drift import (
    DEFAULT_THRESHOLD,
    Drift,
    detect_drifts,
    drift_to_dict,
    find_rename,
    rename_score,
    schema_diff,
)

__all__ = ["DEFAULT_THRESHOLD", "Drift", "detect_drifts", "drift_to_dict",
           "find_rename", "rename_score", "schema_diff"]