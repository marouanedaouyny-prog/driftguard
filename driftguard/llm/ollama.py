"""Ollama suggestion channel (API_SPEC §7, ADR-008, P8).

Suggestion-only and advisory: suggestions are candidates marked ``LLM-N``,
flow through the exact same plan -> approval -> apply -> verify path as rule
output, are never auto-applied, and are never merged into deterministic rule
output. Stdlib ``urllib`` only (P1). Input hygiene: prompts carry IR
summaries + redacted snippets only; raw secret values never reach a prompt.

Availability never influences exit codes except the single documented
exception: ``--llm-suggestions`` requested and Ollama unreachable at call
time -> exit 2 ``llm_unavailable`` (raised here as LlmUnavailable; the CLI
maps it). Mid-run failures (network flap, timeout after a successful call,
malformed response, invalid suggestion JSON) degrade after one retry with a
1s backoff: zero suggestions + a stderr warning, deterministic path
proceeds unmodified.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from driftguard.core.security.redact import redact

SUGGESTIONS_SCHEMA = "driftguard.suggestions.v1"
PROMPT_VERSION = "prompt_v1"
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_TIMEOUT = 30
DEFAULT_MIN_CONFIDENCE = 0.7
DEFAULT_MAX_SUGGESTIONS = 50
RETRY_BACKOFF = 1.0


class LlmUnavailable(Exception):
    """Ollama unreachable at call time (exit 2 ``llm_unavailable``)."""


class LlmClient:
    """Minimal Ollama ``/api/generate`` client (stdlib urllib only)."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        """POST ``{base}/api/generate``; returns the ``response`` field.

        Raises LlmUnavailable on any network-level failure (connection
        refused, DNS, timeout, HTTP error).
        """
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_ctx": 8192},
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LlmUnavailable(f"cannot reach Ollama at {self.base_url}: "
                                 f"{exc}") from exc
        response = body.get("response")
        if not isinstance(response, str) or not response.strip():
            raise LlmUnavailable(
                f"Ollama at {self.base_url} returned an empty response")
        return response


def build_prompt(pipeline, candidates: list[dict],
                 findings: list) -> str:
    """Assemble the versioned ``prompt_v1`` (§7.3).

    Only IR summaries + redacted snippets leave this function.
    """
    lines = [
        "You are DriftGuard's suggestion channel for SQL data-pipeline "
        "refactoring.",
        f"Pipeline fingerprint: {pipeline.fingerprint}",
        "",
        "Stages (name: columns <- refs):",
    ]
    for s in pipeline.stages:
        cols = ", ".join(c.name for c in s.columns) or "-"
        refs = ", ".join(r.name for r in s.refs) or "-"
        lines.append(f"  {s.name}: {cols} <- {refs}")
    lines += [
        "",
        "Existing deterministic candidates (rule output; do not repeat "
        "identical before/after pairs):",
    ]
    if candidates:
        for it in candidates:
            lines.append(f"  {it['rule_id']} {it['stage']} {it['path']} "
                         f"[{it['span'][0]},{it['span'][1]}] "
                         f"{redact(it['before'])} -> {redact(it['after'])} "
                         f"({it['reason']})")
    else:
        lines.append("  (none)")
    lines += [
        "",
        "Baseline security findings (do not suggest edits that touch "
        "these spans):",
    ]
    if findings:
        for f in findings:
            lines.append(f"  {f.rule_id} {f.severity} {f.path}:{f.line}")
    else:
        lines.append("  (none)")
    lines += [
        "",
        "Rules available: REF-001 drop-dead-CTE, REF-002 duplicate "
        "projection, REF-003 inline-single-use-CTE, REF-004 "
        "quote-normalize, REF-005 star-expand, REF-006 dead-alias.",
        "",
        "Constraints: never invent file paths; never suggest spans "
        "outside the files above; snippet values are redacted (never "
        "repeat or emit secret-looking values); mark uncertainty with "
        "low confidence.",
        "",
        "Output JSON only, exactly: {\"suggestions\": [{\"stage\": "
        "\"<stage name>\", \"path\": \"<relative file path>\", \"span\": "
        "[<byte start>, <byte end>], \"before\": \"<exact current "
        "bytes>\", \"after\": \"<replacement bytes>\", \"confidence\": "
        "<0..1>, \"rationale\": \"<why it is safe>\"}]}",
    ]
    return "\n".join(lines)


def _warn(warn, message: str) -> None:
    if warn is not None:
        warn(message)


def validate_suggestions(raw: str, root: Path, pipeline,
                         existing: list[dict], min_confidence: float,
                         max_suggestions: int,
                         warn=None) -> list[dict]:
    """Parse + validate the model's JSON against §7.4.

    Hard rejects are dropped with a warning. Returns validated suggestions
    with sequential ``LLM-N`` ids and tier forced to ``suggested``.
    """
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        _warn(warn, f"llm: suggestion response is not JSON ({exc}); "
                    "dropped")
        return []
    items = doc.get("suggestions", doc) if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        _warn(warn, "llm: suggestion response has no suggestions array; "
                    "dropped")
        return []

    files: dict[str, Stage] = {}
    for s in pipeline.stages:
        try:
            rel = s.path.relative_to(pipeline.root).as_posix()
        except ValueError:
            rel = s.path.as_posix()
        files[rel] = s
        files[str(s.path)] = s
    existing_pairs = {(it["before"], it["after"]) for it in existing}
    out: list[dict] = []
    for i, sug in enumerate(items):
        if not isinstance(sug, dict):
            _warn(warn, f"llm: suggestion #{i} is not an object; dropped")
            continue
        stage = sug.get("stage")
        path = sug.get("path")
        span = sug.get("span")
        before = sug.get("before")
        after = sug.get("after")
        confidence = sug.get("confidence")
        if (not isinstance(stage, str) or not isinstance(path, str)
                or not isinstance(before, str) or not isinstance(after, str)
                or not isinstance(span, list) or len(span) != 2
                or not all(isinstance(v, int) and v >= 0 for v in span)
                or not isinstance(confidence, (int, float))):
            _warn(warn, f"llm: suggestion #{i} has invalid fields; dropped")
            continue
        if after == before:
            _warn(warn, f"llm: suggestion #{i} is a no-op; dropped")
            continue
        if not (0.0 <= float(confidence) <= 1.0):
            _warn(warn, f"llm: suggestion #{i} confidence {confidence} "
                        "outside [0,1]; dropped")
            continue
        if float(confidence) < min_confidence:
            _warn(warn, f"llm: suggestion #{i} confidence {confidence} "
                        f"below --llm-min-confidence {min_confidence}; "
                        "dropped")
            continue
        target = files.get(path)
        if target is None:
            _warn(warn, f"llm: suggestion #{i} path {path!r} is not a "
                        "pipeline stage; dropped")
            continue
        s, e = span
        raw_bytes = target.raw.encode("utf-8")
        if s > e or e > len(raw_bytes):
            _warn(warn, f"llm: suggestion #{i} span [{s},{e}] out of "
                        f"bounds for {path}; dropped")
            continue
        if before.encode("utf-8") != raw_bytes[s:e]:
            _warn(warn, f"llm: suggestion #{i} before does not match the "
                        f"file bytes at {path}:{s}; dropped")
            continue
        if (before, after) in existing_pairs:
            _warn(warn, f"llm: suggestion #{i} duplicates a deterministic "
                        "candidate; dropped")
            continue
        if len(out) >= max_suggestions:
            _warn(warn, f"llm: more than --max-llm-suggestions "
                        f"({max_suggestions}); excess dropped")
            break
        out.append({
            "rule_id": f"LLM-{len(out) + 1}",
            "tier": "suggested",
            "stage": stage,
            "path": path,
            "span": [s, e],
            "before": before,
            "after": after,
            "confidence": round(float(confidence), 3),
            "rationale": str(sug.get("rationale") or "")[:500],
            "reason": f"LLM suggestion (conf {float(confidence):.2f})",
        })
    return out


def request_suggestions(client: LlmClient, root: Path, pipeline,
                        candidates: list[dict], findings: list,
                        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                        max_suggestions: int = DEFAULT_MAX_SUGGESTIONS,
                        warn=None) -> tuple[list[dict], str]:
    """One Ollama call with one retry; returns (suggestions, raw_response).

    - Network failure on the first attempt raises LlmUnavailable (the CLI
      maps it to exit 2 ``llm_unavailable``).
    - A malformed response (or a network failure after a successful HTTP
      exchange) triggers one retry after a 1s backoff, then degrades to
      zero suggestions with a warning (never an error exit).
    """
    prompt = build_prompt(pipeline, candidates, findings)
    try:
        raw = client.generate(prompt)
    except LlmUnavailable:
        raise
    if not raw.strip():
        _warn(warn, "llm: empty response from Ollama; degrading")
        return [], ""
    if not raw.lstrip().startswith("{"):
        _warn(warn, "llm: malformed response; retrying once")
        time.sleep(RETRY_BACKOFF)
        try:
            raw = client.generate(prompt)
        except LlmUnavailable as exc:
            _warn(warn, f"llm: retry failed ({exc}); degrading to zero "
                        "suggestions")
            return [], ""
    suggestions = validate_suggestions(
        raw, root, pipeline, candidates, min_confidence, max_suggestions,
        warn=warn)
    return suggestions, raw