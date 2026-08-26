"""Session orchestration: state transitions + audit rows in one transaction.

Every transition writes audit (action/from/to/args/result/exit_code) and
persists the new state; a crash between writes leaves the prior state, and a
re-run of the same command resumes (API_SPEC §4).
"""
from __future__ import annotations

import json

from driftguard.core.refactor import state as fsm
from driftguard.core.refactor.planner import PLAN_SCHEMA, PLAN_VERSION, item_hash
from driftguard.core.security.redact import redact
from driftguard.store import Store


class SessionError(Exception):
    pass


def _redact_args(args: dict) -> str:
    """args_json must never carry secrets (API_SPEC §3.13)."""
    return redact(json.dumps(args, ensure_ascii=False))


def create_session(store: Store, repo_fingerprint: str, rules: list[str],
                   max_risk: str, llm_used: bool = False,
                   base_commit: str | None = None,
                   rules_dir: str | None = None) -> dict:
    sid = store.create_session(repo_fingerprint, rules, max_risk, llm_used,
                               base_commit, rules_dir)
    store.audit_log(sid, "CREATE", None, "start",
                    _redact_args({"rules": rules, "max_risk": max_risk,
                                  "llm_used": llm_used,
                                  "rules_dir": rules_dir}), None, 0)
    return store.get_session(sid)


def mark_parsed(store: Store, session: dict, run_id: int | None) -> dict:
    _go(store, session, "parsed", "PARSE", run_id=run_id)
    return store.get_session(session["session_id"])


def mark_analyzed(store: Store, session: dict, run_id: int | None,
                  result: dict | None = None) -> dict:
    _go(store, session, "analyzed", "ANALYZE", run_id=run_id,
        result=_summary(result))
    return store.get_session(session["session_id"])


def mark_planned(store: Store, session: dict, plan_path: str,
                 plan_hash: str, run_id: int | None,
                 n_items: int, n_blocked: int) -> dict:
    _go(store, session, "planned", "PLAN", run_id=run_id,
        args={"plan_path": plan_path, "plan_hash": plan_hash},
        result={"items": n_items, "blocked": n_blocked})
    store.set_session_plan(session["session_id"], plan_path, plan_hash)
    return store.get_session(session["session_id"])


def mark_approved(store: Store, session: dict, source: str = "cli") -> dict:
    _go(store, session, "approved", "APPROVE",
        args={"source": source}, result={"source": source})
    return store.get_session(session["session_id"])


def mark_applied(store: Store, session: dict, plan_hash: str,
                 summary: dict, run_id: int | None) -> dict:
    _go(store, session, "applied", "APPLY", run_id=run_id,
        args={"plan_hash": plan_hash}, result=summary)
    return store.get_session(session["session_id"])


def mark_verified(store: Store, session: dict, ok: bool) -> dict:
    target = "verified" if ok else "approved"
    _go(store, session, target, "VERIFY",
        result={"ok": ok})
    return store.get_session(session["session_id"])


def mark_aborted(store: Store, session: dict, action: str,
                 exit_code: int, reason: str) -> None:
    try:
        store.audit_log(session["session_id"], action, session["state"],
                        "aborted", None,
                        json.dumps({"error": reason}), exit_code)
        store.set_session_state(session["session_id"], "aborted")
    except Exception:
        pass  # audit must never mask the original failure


def persist_plan(store: Store, session_id: int, run_id: int | None,
                 plan: dict) -> int:
    items = []
    for it in plan["items"]:
        it["item_hash"] = item_hash(it)
        items.append(dict(it))
    return store.save_plan(session_id, run_id, plan["plan_hash"],
                           f"{PLAN_SCHEMA} v{PLAN_VERSION}", items)


def require_state(session: dict, state: str) -> None:
    if session is None or session["state"] != state:
        raise SessionError(
            f"state_error: session requires state {state!r}, found "
            f"{session['state'] if session else None!r}")


def _go(store: Store, session: dict, target: str, action: str,
        run_id: int | None = None, args: dict | None = None,
        result: dict | None = None) -> None:
    fsm.validate_state(target)
    new_state = fsm.transition(session["state"], target)
    store.audit_log(session["session_id"], action, session["state"],
                    new_state,
                    _redact_args(args) if args else None,
                    json.dumps(result) if result else None, 0,
                    run_id=run_id)
    store.set_session_state(session["session_id"], new_state, run_id)


def _summary(result: dict | None) -> dict | None:
    if result is None:
        return None
    return {k: v for k, v in result.items()
            if k in ("candidates", "blocked", "findings")}