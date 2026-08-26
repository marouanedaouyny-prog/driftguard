"""Cycle detection over the stage dependency graph (seeded DFS, ARCH §4.3)."""
from __future__ import annotations


def find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Return elementary cycles found by depth-first search.

    Deterministic: iteration follows insertion order of `graph` keys and edge
    lists. A cycle is recorded once (either orientation), matching the seed
    contract: `cycle[::-1]` duplicates are not appended.
    """
    cycles: list[list[str]] = []
    visiting: set[str] = set()
    done: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            start = stack.index(node) if node in stack else 0
            cycle = stack[start:] + [node]
            if cycle not in cycles and cycle[::-1] not in cycles:
                cycles.append(cycle)
            return
        visiting.add(node)
        stack.append(node)
        for nxt in graph.get(node, []):
            visit(nxt)
        stack.pop()
        visiting.discard(node)
        done.add(node)

    for node in graph:
        visit(node)
    return cycles