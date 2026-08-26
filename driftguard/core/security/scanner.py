"""Security scan orchestrator (ARCHITECTURE §4.5).

Owns file discovery, per-file decoding, suppression, redaction and the
`--max-findings` cap. Rules are pure; all output surfaces get `redact()`.
"""
from __future__ import annotations

import re
from pathlib import Path

from driftguard.core.security.findings import Finding, SEVERITY_RANK
from driftguard.core.security.redact import redact
from driftguard.core.security.rules import RULES

_FILE_EXTS = {".sql", ".py", ".sh", ".bash"}
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".dbt"}
_SUPP_RE = re.compile(r"(?:--|#)\s*driftguard:off(-all)?(\s+[\w,\- ]+)?")

DEFAULT_MAX_FINDINGS = 500
DEFAULT_MAX_BYTES = 1_000_000


def parse_suppressions(text: str) -> tuple[bool, dict[int, set[str]]]:
    file_all = False
    by_line: dict[int, set[str]] = {}
    for i, line in enumerate(text.splitlines(), 1):
        if "driftguard:off" not in line:
            continue
        m = _SUPP_RE.search(line)
        if not m:
            continue
        if m.group(1):
            file_all = True
        if m.group(2):
            for rid in re.split(r"[\s,]+", m.group(2).strip()):
                by_line.setdefault(i, set()).add(rid)
    return file_all, by_line


def scan_file(path: Path, root: Path,
              max_findings: int = DEFAULT_MAX_FINDINGS,
              max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[list[Finding], int]:
    """Returns (findings, max_findings_reached). Findings are redacted."""
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        return [], False
    text = raw.decode("utf-8", errors="replace")
    file_all, by_line = parse_suppressions(text)
    findings: list[Finding] = []
    rel = path.relative_to(root).as_posix()
    for rule in RULES:
        if rule.sql_only and path.suffix.lower() != ".sql":
            continue
        matches = _dedupe(rule.scan(text))
        for m in matches:
            suppressed = file_all or (m.line in by_line and rule.id in by_line[m.line])
            findings.append(Finding(
                rule_id=rule.id, severity=m.severity, path=rel,
                line=m.line, col=m.col, span=m.span,
                snippet_redacted=redact(m.snippet), hint=m.hint,
                status="suppressed" if suppressed else "open"))
    if max_findings and len(findings) >= max_findings:
        return findings[:max_findings], True
    return findings, False


def _dedupe(matches: list) -> list:
    """Drop overlapping matches unless the later one is more severe
    (e.g. SEC-001 critical prefix inside a high-severity assignment match)."""
    matches = sorted(matches, key=lambda m: (m.span[0], m.span[1]))
    kept: list = []
    for m in matches:
        overlapping = [k for k in kept
                       if m.span[0] < k.span[1] and k.span[0] < m.span[1]]
        if not overlapping:
            kept.append(m)
            continue
        if any(SEVERITY_RANK.get(k.severity, 0) >= SEVERITY_RANK.get(m.severity, 0)
               for k in overlapping):
            continue
        kept = [k for k in kept
                if not (m.span[0] < k.span[1] and k.span[0] < m.span[1])]
        kept.append(m)
    return kept


def scan_root(root: Path, max_findings: int = DEFAULT_MAX_FINDINGS,
              max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[list[Finding], int, int]:
    """Returns (findings, files_scanned, max_findings_reached)."""
    findings: list[Finding] = []
    files = 0
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in p.relative_to(root).parts[:-1]):
            continue
        if p.suffix.lower() not in _FILE_EXTS:
            continue
        files += 1
        f, reached = scan_file(p, root, max_findings, max_bytes)
        findings.extend(f)
        if reached or (max_findings and len(findings) >= max_findings):
            return findings[:max_findings], files, True
    return findings, files, False