"""SEC-005 plaintext credentials in SQL DDL/DCL (SQL files only).

`CREATE USER/ROLE/LOGIN ... IDENTIFIED BY 'plaintext'` and
`GRANT ... IDENTIFIED BY 'plaintext'`.
"""
from __future__ import annotations

import re

from driftguard.core.security.rules import Match, SecRule

_CREATE_RE = re.compile(
    r"\bCREATE\s+(?:USER|ROLE|LOGIN)\b[^;]*\bIDENTIFIED\s+BY\s+['\"][^'\"]+['\"]",
    re.IGNORECASE)
_GRANT_RE = re.compile(
    r"\bGRANT\b[^;]*\bIDENTIFIED\s+BY\s+['\"][^'\"]+['\"]", re.IGNORECASE)


class PlaintextAuthSql(SecRule):
    id = "SEC-005"
    severity = "medium"
    sql_only = True

    def scan(self, text: str) -> list[Match]:
        matches: list[Match] = []
        for pattern in (_CREATE_RE, _GRANT_RE):
            for m in pattern.finditer(text):
                start = m.start()
                line_start = text.rfind("\n", 0, start) + 1
                line = text.count("\n", 0, start) + 1
                line_end = text.find("\n", start)
                if line_end == -1:
                    line_end = len(text)
                matches.append(Match(
                    line, start - line_start + 1, (start, m.end()),
                    text[line_start:line_end],
                    "plaintext credential in SQL DDL/DCL; use a secret manager "
                    "or hashed auth",
                    self.severity))
        return matches