"""SEC-003 unsafe subprocess.

`os.system(...)`; `subprocess.run/call/Popen/check_output/check_call` with
`shell=True`; or a non-literal first argument (f-string, variable, non-list).
"""
from __future__ import annotations

import re

from driftguard.core.security.rules import Match, SecRule

_SYSTEM_RE = re.compile(r"\bos\.system\s*\(")
_SHELL_RE = re.compile(
    r"\bsubprocess\.(?:run|call|Popen|check_output|check_call)\s*\([^)]*"
    r"\bshell\s*=\s*True")
_NONLITERAL_RE = re.compile(
    r"\bsubprocess\.(?:run|call|Popen)\s*\(\s*(?!['\"\[\{])")


class UnsafeSubprocess(SecRule):
    id = "SEC-003"
    severity = "high"
    sql_only = False

    def scan(self, text: str) -> list[Match]:
        matches: list[Match] = []
        for pattern in (_SYSTEM_RE, _SHELL_RE, _NONLITERAL_RE):
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
                    "unsafe subprocess: shell=True or non-literal command; "
                    "use subprocess.run([...], check=True)",
                    self.severity))
        return matches