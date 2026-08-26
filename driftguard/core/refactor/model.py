"""Refactoring rule engine model (ARCHITECTURE §4.4, ADR-006).

Pattern: pure rule functions over the IR + raw text. A rule *analyzes*
(reads IR + raw text, produces candidates); the engine *applies* (edits raw
text at byte spans). Rules never mutate IR; the engine owns mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from driftguard.core.ir.model import Pipeline, Span, Stage

TIER_RANK = {"safe": 0, "suggested": 1, "risky": 2}


@dataclass(frozen=True)
class RewriteCandidate:
    rule_id: str
    tier: str                 # safe | suggested | risky (ADR-006)
    stage: str
    span: Span                # byte offsets into the stage source file
    before: str
    after: str
    reason: str               # human-readable justification (plan + report)
    security_note: str | None = None  # "touches SEC-002 span" etc.

    def to_dict(self) -> dict:
        return {"rule_id": self.rule_id, "tier": self.tier,
                "stage": self.stage, "span": self.span.to_list(),
                "before": self.before, "after": self.after,
                "reason": self.reason, "security_note": self.security_note}


@dataclass
class AnalysisContext:
    """Everything a rule may read. Deterministic by construction."""
    pipeline: Pipeline
    stages_by_name: dict[str, Stage] = field(default_factory=dict)
    rules_enabled: frozenset[str] = frozenset()

    @classmethod
    def build(cls, pipeline: Pipeline) -> "AnalysisContext":
        return cls(pipeline=pipeline,
                    stages_by_name={s.name: s for s in pipeline.stages})


class Rule(Protocol):
    id: str
    version: int
    tier: str
    description: str

    def analyze(self, stage: Stage, ctx: AnalysisContext) -> list[RewriteCandidate]: ...