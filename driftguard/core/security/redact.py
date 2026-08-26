"""Secret scrubbing for every output surface (P6 / PRD Security).

The raw secret value never leaves the scanner: reports, JSON, plan files,
audit rows and logs all receive `redact()`ed text.
"""
from __future__ import annotations

import math
import re
from collections import Counter

_PREFIX_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{20,})")
_KEY_EQ_RE = re.compile(
    r"((?:password|passwd|api[_-]?key|token|secret|client[_-]?secret)"
    r"\s*[:=]\s*['\"]?[^'\"\s,;)]{6,}['\"]?)", re.IGNORECASE)


def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    n = len(text)
    counts = Counter(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def redact(text: str) -> str:
    """Replace secret-shaped values with `<redacted>`."""
    out = _PREFIX_RE.sub("<redacted>", text)
    return _KEY_EQ_RE.sub(lambda m: re.sub(r"[=:]\s*(?:.*)$", "=<redacted>",
                                           m.group(1)), out)