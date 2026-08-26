"""Refactoring rule engine (Phase 4 milestone 2).

Rule catalog REF-001..REF-006, deterministic plan construction with a
security block overlay, span-guarded application, a pure state machine, and
session/audit persistence (ARCHITECTURE §4.4, §5).
"""
from driftguard.core.refactor.model import AnalysisContext, RewriteCandidate
from driftguard.core.refactor.state import (STATE_LABEL, STATES, TransitionError,
                                            can_transition, transition,
                                            validate_state)

__all__ = ["AnalysisContext", "RewriteCandidate", "STATE_LABEL", "STATES",
           "TransitionError", "can_transition", "transition",
           "validate_state"]