"""Trusted-code rule plugin loader (ARCHITECTURE §2.1 plugin seam, Phase 5).

``--rules-dir DIR`` loads every ``*.py`` module in DIR (sorted by filename,
deterministic) and registers module-level objects that implement the ``Rule``
protocol (id / version / tier / description / analyze).

Loading executes plugin code — a deliberate, documented trust decision at
the same trust level as the tool itself (the operator chooses the directory;
CI pins its own plugin files). Invalid plugins are rejected with a stderr
warning and never break a run; id collisions with built-in rules are
rejected the same way (no silent shadowing).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from driftguard.core.refactor.catalog import RULES, RULES_BY_ID
from driftguard.core.refactor.model import TIER_RANK

BUILTIN_IDS = frozenset(RULES_BY_ID)

_REQUIRED_ATTRS = ("id", "version", "tier", "description", "analyze")


def _looks_like_rule(obj) -> bool:
    return all(hasattr(obj, name) for name in _REQUIRED_ATTRS)


def _warn(warn, message: str) -> None:
    if warn is not None:
        warn(message)


def load_rules(rules_dir: Path | None = None,
               warn=None) -> list:
    """Built-in rules plus (optionally) plugin rules from ``rules_dir``.

    Plugins load in sorted filename order; their rules are registered in
    sorted (id) order. A plugin that fails to import is rejected with a
    warning and skipped — the run proceeds with the remaining rules.
    """
    rules = list(RULES)
    if rules_dir is None:
        return rules
    if not rules_dir.is_dir():
        _warn(warn, f"rules-dir: {rules_dir} is not a directory; ignored")
        return rules
    seen: dict[str, object] = dict(RULES_BY_ID)
    for path in sorted(rules_dir.glob("*.py")):
        module = _import_plugin(path)
        if module is None:
            _warn(warn, f"rules-dir: rejected {path.name} (import failed)")
            continue
        for obj in _module_rules(module):
            if not _valid_rule(obj):
                _warn(warn, f"rules-dir: {path.name} has an object missing "
                            f"{', '.join(_REQUIRED_ATTRS)} or with a bad "
                            "field; skipped")
                continue
            rid = obj.id
            if rid in seen:
                _warn(warn, f"rules-dir: {path.name} defines {rid!r} which "
                            "collides with an existing rule; skipped")
                continue
            seen[rid] = obj
            rules.append(obj)
    return rules


def _import_plugin(path: Path):
    """Import one plugin file as a standalone module; None on failure."""
    module_name = f"_driftguard_plugin_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _module_rules(module):
    """Module-level objects implementing the Rule protocol, sorted by id."""
    out = []
    for name in dir(module):
        if name.startswith("_"):
            continue
        try:
            obj = getattr(module, name)
        except Exception:
            continue
        if _looks_like_rule(obj):
            out.append(obj)
    return sorted(out, key=lambda r: r.id)


def _valid_rule(rule) -> bool:
    if not isinstance(rule.id, str) or not rule.id:
        return False
    if not isinstance(rule.version, int) or rule.version < 1:
        return False
    if rule.tier not in TIER_RANK:
        return False
    if not isinstance(rule.description, str):
        return False
    if not callable(getattr(rule, "analyze", None)):
        return False
    return True