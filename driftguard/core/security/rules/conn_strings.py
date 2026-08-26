"""SEC-004 credentials in connection strings / DSNs.

`jdbc:...`, `postgres://user:pass@...` style URLs, and `password=` inside
DSN/URL assignment strings.
"""
from __future__ import annotations

import re

from driftguard.core.security.rules import Match, SecRule

_JDBC_RE = re.compile(r"\bjdbc:[^'\"\s]{8,}")
_USERPASS_RE = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mssql|mongodb(?:\+srv)?)://"
    r"[^'\"\s@:/?#]+:[^'\"\s@:/?#]+@")
_DSN_KEY_RE = re.compile(
    r"\b(?:dsn|connect_string|conn_string|url|uri)\s*[:=]\s*['\"]"
    r"[^'\"]*(?:password|pwd)[^'\"]*['\"]", re.IGNORECASE)


class ConnStringCredential(SecRule):
    id = "SEC-004"
    severity = "medium"
    sql_only = False

    def scan(self, text: str) -> list[Match]:
        matches: list[Match] = []
        for pattern in (_JDBC_RE, _USERPASS_RE, _DSN_KEY_RE):
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
                    "credentials in connection string; use a secret manager",
                    self.severity))
        return matches