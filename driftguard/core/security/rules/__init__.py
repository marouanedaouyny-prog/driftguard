"""Security rule catalog (ARCHITECTURE §4.5, ADR-007).

Every rule is a pure `scan(text) -> list[Match]` function; the scanner owns
redaction, suppression and span bookkeeping. Pattern-based and deterministic
(zero-cost).
"""
from __future__ import annotations

from driftguard.core.security.rules.base import Match, SecRule
from driftguard.core.security.rules.conn_strings import ConnStringCredential
from driftguard.core.security.rules.secrets import HardcodedSecret
from driftguard.core.security.rules.sql_injection import SqlInjection
from driftguard.core.security.rules.subprocess import UnsafeSubprocess
from driftguard.core.security.rules.weak_auth_sql import PlaintextAuthSql

__all__ = ["Match", "SecRule", "RULES", "RULES_BY_ID"]

RULES: list[SecRule] = [
    HardcodedSecret(),
    SqlInjection(),
    UnsafeSubprocess(),
    ConnStringCredential(),
    PlaintextAuthSql(),
]

RULES_BY_ID = {r.id: r for r in RULES}