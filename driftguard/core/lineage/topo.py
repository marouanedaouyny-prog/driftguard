"""Topological order (Kahn's algorithm) — ARCH §4.3.

Required by the refactoring engine so rewrites are validated bottom-up
(producer before its consumers). Deterministic: nodes are visited in sorted
order at every step, so the output depends only on the graph, never on dict
iteration or insertion order.
"""
from __future__ import annotations

from collections import deque


def topological_order(nodes: list[str], edges: list[tuple[str, str]]) -> list[str]:
    """Topologically sort `nodes` under `edges` (a -> b means a before b).

    Cyclic nodes cannot be ordered; they are appended in sorted order after
    the ordered portion (documented behavior — cycles are reported separately
    in the lineage artifact).
    """
    node_set = set(nodes)
    indegree = {n: 0 for n in node_set}
    adjacency: dict[str, list[str]] = {n: [] for n in node_set}
    for a, b in edges:
        if a in node_set and b in node_set:
            adjacency[a].append(b)
            indegree[b] += 1

    ready = deque(sorted(n for n, d in indegree.items() if d == 0))
    order: list[str] = []
    while ready:
        node = ready.popleft()
        order.append(node)
        for nxt in sorted(adjacency[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)

    remaining = sorted(n for n, d in indegree.items() if d > 0)
    return order + remaining