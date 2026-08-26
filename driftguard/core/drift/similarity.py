"""Rename heuristic: SequenceMatcher similarity behind a configurable gate."""
from __future__ import annotations

from difflib import SequenceMatcher


def rename_score(a: str, b: str) -> float:
    """0.0-1.0 similarity between two column names."""
    return SequenceMatcher(None, a, b).ratio()


def find_rename(column: str, candidates: list[str], taken: set[str],
                threshold: float) -> str | None:
    """Best-match candidate for `column`, or None below `threshold`.

    Ties keep the first candidate (deterministic). `taken` columns (already
    present in the consumer, or already matched) are never returned.
    """
    best, best_score = None, 0.0
    for candidate in candidates:
        if candidate in taken:
            continue
        score = rename_score(column, candidate)
        if score > best_score:
            best, best_score = candidate, score
    if best is not None and best_score >= threshold:
        return best
    return None