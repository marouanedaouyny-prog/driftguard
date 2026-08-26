"""Refactor session state machine (ARCHITECTURE §5.1).

Pure: no I/O. The session module owns persistence + audit; this module only
answers "is the requested transition legal and what guards apply?".
"""
from __future__ import annotations

STATES = ("start", "parsed", "analyzed", "planned", "approved", "applied",
          "verified", "done", "aborted")

# state -> set of reachable states
_TRANSITIONS = {
    "start": {"parsed", "aborted"},
    "parsed": {"analyzed", "aborted"},
    "analyzed": {"planned", "aborted"},
    "planned": {"approved", "planned", "aborted"},
    "approved": {"applied", "approved", "aborted"},
    "applied": {"verified", "approved", "aborted"},
    "verified": {"done", "aborted"},
    "done": set(),
    "aborted": {"start"},
}

# human name for each state (reports + audit)
STATE_LABEL = {
    "start": "session created", "parsed": "pipeline parsed",
    "analyzed": "analysis complete", "planned": "plan written",
    "approved": "plan approved", "applied": "plan applied",
    "verified": "verification passed", "done": "session closed",
    "aborted": "aborted",
}


class TransitionError(Exception):
    def __init__(self, state: str, target: str, reason: str = ""):
        self.state, self.target = state, target
        super().__init__(f"state_error: cannot move session from "
                         f"{state!r} to {target!r}"
                         + (f": {reason}" if reason else ""))


def can_transition(state: str, target: str) -> bool:
    return target in _TRANSITIONS.get(state, set())


def transition(state: str, target: str) -> str:
    if target == state:
        return state
    if not can_transition(state, target):
        raise TransitionError(state, target)
    return target


def validate_state(state: str) -> str:
    if state not in STATES:
        raise ValueError(f"state_error: unknown session state {state!r}")
    return state