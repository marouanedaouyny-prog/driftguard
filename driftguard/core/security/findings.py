"""Security findings model (ARCHITECTURE §4.5, ADR-007)."""
from __future__ import annotations

from dataclasses import dataclass, field

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@dataclass
class Finding:
    rule_id: str
    severity: str
    path: str
    line: int
    col: int
    span: tuple[int, int]
    snippet_redacted: str
    hint: str
    status: str = "open"

    def to_dict(self) -> dict:
        return {"rule_id": self.rule_id, "severity": self.severity,
                "path": self.path, "line": self.line, "col": self.col,
                "span": [self.span[0], self.span[1]],
                "snippet_redacted": self.snippet_redacted,
                "hint": self.hint, "status": self.status}


def at_least(severity: str, gate: str) -> bool:
    """True when `severity` is at or above the `gate` level ('none' = never)."""
    if gate == "none":
        return False
    return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(gate, 0)