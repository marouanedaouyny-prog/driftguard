"""Parser protocol (ARCHITECTURE §4.1).

Phase 1 ships exactly one implementation: the dbt-style project parser.
Airflow/generic-SQL dialects slot in behind this protocol later — the
`Rule`/`Parser` seams are the plugin interface.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from driftguard.core.ir.model import Pipeline, Stage


class Parser(Protocol):
    """Parse a pipeline root into typed IR stages."""

    def parse_file(self, path: Path) -> Stage | None:
        """Parse one model file into a Stage (None if not a model file)."""

    def parse_project(self, root: Path) -> Pipeline:
        """Parse all model files under root into a Pipeline."""