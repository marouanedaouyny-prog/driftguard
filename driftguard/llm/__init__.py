"""Optional LLM enrichment channel (Phase 4, ADR-008)."""
from driftguard.llm.ollama import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_SUGGESTIONS,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    LlmClient,
    LlmUnavailable,
    SUGGESTIONS_SCHEMA,
    build_prompt,
    request_suggestions,
    validate_suggestions,
)

__all__ = [
    "DEFAULT_BASE_URL", "DEFAULT_MAX_SUGGESTIONS", "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MODEL", "DEFAULT_TIMEOUT", "LlmClient", "LlmUnavailable",
    "SUGGESTIONS_SCHEMA", "build_prompt", "request_suggestions",
    "validate_suggestions",
]