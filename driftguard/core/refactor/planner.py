"""Refactor plan construction (ARCHITECTURE §5): analysis, security overlay,
risk gating, span-conflict dedupe, and plan-file I/O.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Callable

from driftguard.core.ir.model import Pipeline
from driftguard.core.parser.dialects.dbt import DbtParser
from driftguard.core.refactor.loader import load_rules
from driftguard.core.refactor.model import (AnalysisContext, RewriteCandidate,
                                            TIER_RANK)
from driftguard.core.security.findings import at_least
from driftguard.core.security.scanner import scan_root

PLAN_SCHEMA = "driftguard.plan.v1"
PLAN_VERSION = 1
MAX_CANDIDATES = 500

BLOCKING_SEVERITIES = ("critical", "high")


def analyze_pipeline(root: Path, rules: list[str] | None = None,
                     max_risk: str = "safe",
                     allow_on_finding: bool = False,
                     rules_dir: Path | None = None) -> dict:
    """Parse + security baseline + rule analysis. Returns an analysis dict.

    Deterministic: rules run in catalog order (then plugin rules, sorted by
    id) over stages in parse order.
    """
    rule_objects = load_rules(rules_dir)
    enabled = _resolve_rules(rules, {r.id for r in rule_objects})
    pipeline = DbtParser().parse_project(root)
    ctx = AnalysisContext.build(pipeline)
    findings, _, capped = scan_root(root, max_findings=sys.maxsize)
    findings_by_path: dict[str, list] = {}
    for f in findings:
        findings_by_path.setdefault(f.path, []).append(f)

    candidates: list[dict] = []
    for stage in pipeline.stages:
        rel = _rel_path(root, stage.path)
        for rule in rule_objects:
            if rule.id not in enabled:
                continue
            if TIER_RANK[rule.tier] > TIER_RANK[max_risk]:
                continue
            for cand in rule.analyze(stage, ctx):
                candidates.append(_candidate_to_item(
                    cand, rel, stage.fingerprint))

    candidates = _dedupe_spans(candidates)

    items: list[dict] = []
    blocked: list[dict] = []
    for it in candidates:
        overlap = _finding_overlap(it, findings_by_path.get(it["path"], []))
        blocking = [f for f in overlap
                    if f.status == "open"
                    and at_least(f.severity, "high")]
        if blocking and not allow_on_finding:
            it["block_reason"] = ("overlaps %s" % ", ".join(
                f"{f.rule_id}:{f.severity}" for f in blocking))
            blocked.append(it)
            continue
        if overlap:
            it["security_note"] = "touches %s" % ", ".join(
                f"{f.rule_id}:{f.severity}" for f in overlap)
        items.append(it)

    items.sort(key=lambda i: (i["path"], i["span"][0], i["rule_id"]))
    for idx, it in enumerate(items):
        it["change_id"] = f"c{idx}"

    return {"pipeline": pipeline, "root": root, "findings": findings,
            "candidates": items, "blocked": blocked, "rules": sorted(enabled),
            "rules_dir": str(rules_dir) if rules_dir else None}


def build_plan(analysis: dict, session_id: int | None, root: Path,
               max_risk: str, rules: list[str]) -> dict:
    for it in analysis["candidates"]:
        it["item_hash"] = item_hash(it)
    rule_ids = analysis.get("rules") or sorted(rules)
    plan = {"schema": PLAN_SCHEMA, "version": PLAN_VERSION,
            "session_id": session_id, "root": root.as_posix() or ".",
            "pipeline_fingerprint": analysis["pipeline"].fingerprint,
            "max_risk": max_risk, "rule_ids": sorted(rule_ids),
            "rules_dir": analysis.get("rules_dir"),
            "created_at": None, "items": analysis["candidates"]}
    plan["plan_hash"] = _plan_hash(plan)
    return plan


def write_plan(plan: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")


def read_plan(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != PLAN_SCHEMA or data.get("version") != PLAN_VERSION:
        raise ValueError(f"plan_error: unsupported plan schema "
                         f"{data.get('schema')!r} v{data.get('version')}")
    return data


def _resolve_rules(rules: list[str] | None,
                   known_ids: set[str] | None = None) -> set[str]:
    if known_ids is None:
        known_ids = set()
    if not rules:
        return set(known_ids)
    out = set()
    for r in rules:
        if r not in known_ids:
            raise ValueError(f"usage: unknown rule {r!r}")
        out.add(r)
    return out


def _candidate_to_item(cand: RewriteCandidate, rel_path: str,
                       fingerprint_before: str) -> dict:
    return {"rule_id": cand.rule_id, "rule_version": 1, "tier": cand.tier,
            "stage": cand.stage, "path": rel_path,
            "span": [cand.span.start, cand.span.end],
            "before": cand.before, "after": cand.after,
            "reason": cand.reason, "security_note": cand.security_note,
            "fingerprint_before": fingerprint_before}


def _dedupe_spans(candidates: list[dict]) -> list[dict]:
    """Drop candidates whose span overlaps a kept candidate's span in the
    same file. Most-surgical (smallest span) wins; ties by rule id."""
    kept: list[dict] = []
    for it in sorted(candidates,
                     key=lambda i: (i["path"], i["span"][0],
                                    i["span"][1] - i["span"][0],
                                    i["rule_id"])):
        if any(it["path"] == k["path"] and _overlap(it["span"], k["span"])
               for k in kept):
            continue
        kept.append(it)
    return kept


def _overlap(a: list[int], b: list[int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _finding_overlap(item: dict, findings: list) -> list:
    return [f for f in findings
            if _overlap(item["span"], list(f.span))]


def merge_suggestions(analysis: dict, suggestions: list[dict],
                      allow_on_finding: bool, max_risk: str
                      ) -> tuple[dict, list, list]:
    """Merge validated LLM suggestions into an analysis (API_SPEC §7.4/§7.5).

    Suggestions are forced to tier ``suggested``, so a ``safe`` max_risk
    never includes them (by design). Accepted suggestions run through the
    security block overlay exactly like rule candidates and rejoin the
    candidates list; blocked ones go to the ``blocked`` list. Returns
    ``(analysis, added, blocked)``.
    """
    if TIER_RANK[max_risk] < TIER_RANK["suggested"]:
        return analysis, [], []
    findings_by_path: dict[str, list] = {}
    for f in analysis["findings"]:
        findings_by_path.setdefault(f.path, []).append(f)
    added: list[dict] = []
    blocked: list[dict] = []
    for sug in suggestions:
        overlap = _finding_overlap(sug,
                                   findings_by_path.get(sug["path"], []))
        blocking = [f for f in overlap
                    if f.status == "open" and at_least(f.severity, "high")]
        if blocking and not allow_on_finding:
            sug["block_reason"] = "overlaps %s" % ", ".join(
                f"{f.rule_id}:{f.severity}" for f in blocking)
            blocked.append(sug)
            continue
        if overlap:
            sug["security_note"] = "touches %s" % ", ".join(
                f"{f.rule_id}:{f.severity}" for f in overlap)
        added.append(sug)
    if added:
        analysis = dict(analysis)
        analysis["candidates"] = list(analysis["candidates"]) + added
        for idx, it in enumerate(analysis["candidates"]):
            it["change_id"] = f"c{idx}"
        analysis["blocked"] = list(analysis["blocked"]) + blocked
    elif blocked:
        analysis = dict(analysis)
        analysis["blocked"] = list(analysis["blocked"]) + blocked
    return analysis, added, blocked


def _rel_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _plan_hash(plan: dict) -> str:
    payload = {"root": plan["root"],
               "pipeline_fingerprint": plan["pipeline_fingerprint"],
               "max_risk": plan["max_risk"], "rule_ids": plan["rule_ids"],
               "items": [{k: plan_item[k] for k in
                          ("rule_id", "tier", "stage", "path", "span",
                           "before", "after")}
                         for plan_item in plan["items"]]}
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def item_hash(item: dict) -> str:
    canon = json.dumps({k: item[k] for k in
                        ("rule_id", "tier", "stage", "path", "span",
                         "before", "after", "reason")},
                       sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()