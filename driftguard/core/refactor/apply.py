"""Refactor plan application (API_SPEC §3.11): span-guarded edits, backups,
NOOP detection, out-dir or in-place modes.
"""
from __future__ import annotations

from pathlib import Path

from driftguard.core.parser.dialects.dbt import DbtParser
from driftguard.core.refactor.planner import item_hash, read_plan


def apply_plan(plan_path: Path, mode: str, out_dir: Path | None,
               no_backup: bool = False,
               skip_hashes: set[str] | None = None) -> dict:
    """Apply a plan. `mode` is "in_place" or "out_dir".

    Pre-validates every span against the current bytes before writing
    anything (all-or-nothing per plan). Idempotent: items in `skip_hashes`
    (already recorded in the DB) and items whose bytes already equal
    `after` (crash-resume) are skipped with warnings, not errors. Returns a
    results dict; raises ApplyError for stale spans (exit 2).
    """
    skip = set(skip_hashes or ())
    plan = read_plan(plan_path)
    items = plan["items"]
    if not items:
        return {"plan": plan, "applied": [], "noop": [], "skipped": [],
                "backups": [], "fingerprints": {}}

    root = Path(plan["root"])
    files: dict[Path, str] = {}
    for it in items:
        p = root / it["path"]
        if p not in files:
            files[p] = p.read_text(encoding="utf-8")

    # 1. validate all spans (all-or-nothing); already-applied bytes are skips
    to_skip: set[int] = set()
    for idx, it in enumerate(items):
        text = files[root / it["path"]]
        s, e = it["span"]
        if it.get("item_hash") in skip:
            to_skip.add(idx)
            continue
        if text[s:e] == it["after"]:
            to_skip.add(idx)  # crash-resume: this edit already landed
            continue
        if text[s:e] != it["before"]:
            raise ApplyError(
                f"plan_error: stale span for {it['rule_id']} in "
                f"{it['path']}:{s}:{e}: expected {it['before']!r} at "
                f"[{s},{e}] but found {text[s:e]!r}")

    # 2. write backups
    backups: list[dict] = []
    if not no_backup:
        for p in files:
            bak = Path(str(p) + ".orig")
            bak.write_bytes(p.read_bytes())
            backups.append({"path": str(p), "backup": str(bak)})

    # 3. apply bottom-up per file (later spans first so earlier stay valid)
    by_file: dict[Path, list] = {}
    for idx, it in enumerate(items):
        if idx in to_skip:
            continue
        by_file.setdefault(root / it["path"], []).append((idx, it))
    edited: dict[Path, str] = {}
    for p, its in by_file.items():
        text = files[p]
        for idx, it in sorted(its, key=lambda pair: pair[1]["span"][0],
                              reverse=True):
            s, e = it["span"]
            text = text[:s] + it["after"] + text[e:]
        edited[p] = text

    # 4. write files
    written: dict[Path, str] = {}
    for p, text in edited.items():
        if mode == "out_dir":
            dest = out_dir / p.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            dest = p
        dest.write_text(text, encoding="utf-8")
        written[dest] = text

    # 5. fingerprints after (re-parse the modified tree)
    parse_root = out_dir if mode == "out_dir" else root
    try:
        pipeline = DbtParser().parse_project(parse_root)
        fps = {s.name: s.fingerprint for s in pipeline.stages}
    except Exception:
        fps = {}

    applied, noop, skipped = [], [], []
    for idx, it in enumerate(items):
        it["item_hash"] = item_hash(it)
        fb = it.get("fingerprint_before") or ""
        fa = fps.get(it["stage"]) or ""
        if idx in to_skip:
            status = "skipped"
        elif fa and fa == fb:
            status = "noop"
        else:
            status = "applied"
        row = {"item_hash": it["item_hash"], "change_id": it.get("change_id")
               or f"c{idx}", "rule_id": it["rule_id"], "stage": it["stage"],
               "path": it["path"], "fingerprint_before": fb,
               "fingerprint_after": fa or None, "status": status}
        (applied if status == "applied"
         else noop if status == "noop" else skipped).append(row)

    return {"plan": plan, "applied": applied, "noop": noop,
            "skipped": skipped, "backups": backups, "fingerprints": fps}


class ApplyError(Exception):
    """Stale span / plan inconsistency (exit 2 plan_error)."""


def plan_summary(result: dict) -> dict:
    return {"applied": len(result["applied"]),
            "noop": len(result["noop"]),
            "skipped": len(result["skipped"]),
            "backups": len(result["backups"])}