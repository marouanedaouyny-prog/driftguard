"""SEC-002 SQL assembled by string interpolation.

F-strings, `%`-formatting, `.format()` or `+` concatenation feeding a
DB-execution call (`execute`, `executemany`, `spark.sql`, `read_sql`,
`run_query`).
"""
from __future__ import annotations

import re

from driftguard.core.security.rules import Match, SecRule

_CALL_RE = re.compile(
    r"\b(execute|executemany|spark\.sql|read_sql|run_query)\s*\(",
    re.IGNORECASE)
_FSTRING_RE = re.compile(r'\bf["\']')
_FORMAT_RE = re.compile(r"\.format\s*\(")
_CONCAT_RE = re.compile(r"\+")
_PCT_RE = re.compile(r"%\s*[srdx]")
_STRIP_STRINGS_RE = re.compile(r'f?["\'](?:\\.|[^"\'\\])*["\']')


def _strip_literals(args: str) -> str:
    """Remove quoted literal contents (keep quotes) so that `%` inside a
    placeholder string (e.g. `WHERE id = %s` + params tuple) is not read as
    the interpolation operator."""
    return _STRIP_STRINGS_RE.sub('""', args)


class SqlInjection(SecRule):
    id = "SEC-002"
    severity = "high"
    sql_only = False

    def scan(self, text: str) -> list[Match]:
        matches: list[Match] = []
        for call in _CALL_RE.finditer(text):
            end = text.find(")", call.end())
            if end == -1:
                end = min(len(text), call.end() + 300)
            args = text[call.end():end]
            stripped = _strip_literals(args)
            injected = (_FSTRING_RE.search(args) or _FORMAT_RE.search(stripped)
                        or _CONCAT_RE.search(stripped) or _PCT_RE.search(stripped))
            if not injected:
                continue
            start = call.start()
            line_start = text.rfind("\n", 0, start) + 1
            line = text.count("\n", 0, start) + 1
            line_end = text.find("\n", start)
            if line_end == -1:
                line_end = len(text)
            matches.append(Match(
                line, start - line_start + 1, (start, end), text[line_start:line_end],
                "SQL assembled by string interpolation; use parameterized queries",
                self.severity))
        return matches