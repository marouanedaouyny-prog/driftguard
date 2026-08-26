"""Canonical fingerprints (idempotency anchor, ARCHITECTURE §4.2)."""
from driftguard.core.ir.serialize import (
    pipeline_fingerprint,
    stage_fingerprint,
)

__all__ = ["pipeline_fingerprint", "stage_fingerprint"]