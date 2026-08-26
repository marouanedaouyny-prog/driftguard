"""Typed intermediate representation (IR) for parsed pipeline stages.

Design (ARCHITECTURE.md §4.2): dataclasses, all JSON-serializable via
`serialize.py` under a versioned `"v": 1` envelope so stored IR survives
schema evolution. Spans are byte offsets into the source file — the
sourcemap basis for precise, verifiable rewrites.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Span:
    """Byte offsets into the source file (sourcemap basis for rewrites)."""

    start: int
    end: int

    def to_list(self) -> list[int]:
        return [self.start, self.end]


@dataclass(frozen=True)
class Column:
    """A projection column of a stage's output schema."""

    name: str              # lowercase canonical name
    source_expr: str       # original expression text (redacted at render time)
    alias: str | None      # AS alias if present, else None
    span: Span             # byte span of the full expression


@dataclass(frozen=True)
class RefEdge:
    """A producer dependency of a stage."""

    name: str              # producer stage/table name (lowercase)
    kind: str              # "ref" | "bare" | "source"
    span: Span | None = None


@dataclass(frozen=True)
class SourceRef:
    """A dbt source() reference: (source, table)."""

    source: str
    table: str


@dataclass(frozen=True)
class Cte:
    """A common table expression declared by the stage."""

    name: str
    span: Span
    referenced_by: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Diagnostic:
    """Structured parse diagnostic.

    `kind` is "warning" (parsing continued, results partial but usable) or
    "error" (the affected file could not be parsed; refactor commands treat
    error-kind diagnostics as a hard failure — exit 2, ARCHITECTURE §4.1).
    """

    file: str
    line: int
    col: int
    reason: str
    kind: str = "warning"


@dataclass
class Stage:
    """A single pipeline stage (one dbt-style .sql model file)."""

    name: str
    path: Path
    kind: str = "model"    # "model" | "source_table" | "view"
    raw: str = ""
    fingerprint: str = ""
    columns: list[Column] = field(default_factory=list)
    refs: list[RefEdge] = field(default_factory=list)
    sources: list[SourceRef] = field(default_factory=list)
    ctes: list[Cte] = field(default_factory=list)
    create_name: str | None = None
    dialect_hints: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def ref_names(self) -> list[str]:
        return [r.name for r in self.refs]


@dataclass
class Pipeline:
    """The parsed set of stages for one project root."""

    root: Path
    stages: list[Stage]
    fingerprint: str = ""  # sha256 over sorted stage fingerprints