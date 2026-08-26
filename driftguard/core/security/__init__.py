"""Security scanning (ARCHITECTURE §4.5)."""
from driftguard.core.security.findings import Finding, SEVERITY_RANK, at_least
from driftguard.core.security.redact import redact, shannon_entropy
from driftguard.core.security.rules import Match, RULES, RULES_BY_ID
from driftguard.core.security.scanner import (DEFAULT_MAX_BYTES,
                                              DEFAULT_MAX_FINDINGS,
                                              parse_suppressions, scan_file,
                                              scan_root)

__all__ = ["Finding", "SEVERITY_RANK", "at_least", "redact",
           "shannon_entropy", "Match", "RULES", "RULES_BY_ID",
           "DEFAULT_MAX_BYTES", "DEFAULT_MAX_FINDINGS", "parse_suppressions",
           "scan_file", "scan_root"]