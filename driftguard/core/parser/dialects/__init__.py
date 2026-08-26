"""Dialect parsers. Phase 1: dbt-style SQL projects only (ADR-001)."""
from driftguard.core.parser.dialects.base import Parser
from driftguard.core.parser.dialects.dbt import (
    parse_pipeline,
    parse_sources_yml,
    parse_sql_file,
)

__all__ = [
    "Parser",
    "parse_pipeline",
    "parse_sources_yml",
    "parse_sql_file",
]