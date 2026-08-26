"""Security rule protocol (ARCHITECTURE §4.5)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Match:
    line: int
    col: int
    span: tuple[int, int]
    snippet: str
    hint: str
    severity: str


class SecRule:
    id = ""
    severity = "medium"
    sql_only = False

    def scan(self, text: str) -> list[Match]:
        raise NotImplementedError