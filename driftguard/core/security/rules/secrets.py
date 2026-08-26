"""SEC-001 hardcoded secrets.

Known provider prefixes (`sk-`, `ghp_`, `AKIA`, `xoxb-`, `AIza…`) are
critical; high-entropy values (>= 20 chars, Shannon >= 3.5) in assignment
contexts (`password=`, `api_key=`, `token=`) are high.
"""
from __future__ import annotations

import re

from driftguard.core.security.redact import shannon_entropy
from driftguard.core.security.rules import Match, SecRule

_PREFIX_RE = re.compile(
    r"\b(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{20,})\b")
_ASSIGN_RE = re.compile(
    r"\b(password|passwd|api[_-]?key|apikey|token|secret|client[_-]?secret)"
    r"\s*[:=]\s*['\"]?([^'\"\s,;)]{8,})", re.IGNORECASE)


def _locate(text: str, start: int) -> tuple[int, int, int, str]:
    line_start = text.rfind("\n", 0, start) + 1
    line = text.count("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if line_end == -1:
        line_end = len(text)
    return line, start - line_start + 1, line_end, text[line_start:line_end]


class HardcodedSecret(SecRule):
    id = "SEC-001"
    severity = "critical"
    sql_only = False

    def scan(self, text: str) -> list[Match]:
        matches: list[Match] = []
        prefix_spans = []
        for m in _PREFIX_RE.finditer(text):
            prefix_spans.append(m.span())
            line, col, line_end, snippet = _locate(text, m.start())
            matches.append(Match(
                line, col, (m.start(), m.end()), snippet,
                "hardcoded secret (known provider prefix); use a secret manager",
                "critical"))
        for m in _ASSIGN_RE.finditer(text):
            value = m.group(2)
            if len(value) < 20 or shannon_entropy(value) < 3.5:
                continue
            if any(m.start() >= a and m.end() <= b for a, b in prefix_spans):
                continue
            line, col, line_end, snippet = _locate(text, m.start())
            matches.append(Match(
                line, col, (m.start(), m.end()), snippet,
                f"high-entropy value assigned to `{m.group(1)}`; use a secret manager",
                "high"))
        return matches