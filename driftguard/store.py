"""SQLite persistence: stages, lineage edges, drift, scans, run history."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from driftguard.core.drift import Drift, drift_to_dict
from driftguard.core.security.findings import Finding
from driftguard.lineage import Lineage
from driftguard.parser import Stage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stages (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    refs_json TEXT NOT NULL,
    columns_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lineage_edges (
    run_id INTEGER NOT NULL,
    producer TEXT NOT NULL,
    consumer TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'ref',
    PRIMARY KEY (run_id, producer, consumer)
);
CREATE TABLE IF NOT EXISTS drifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    producer TEXT NOT NULL,
    consumer TEXT NOT NULL,
    added_json TEXT NOT NULL,
    removed_json TEXT NOT NULL,
    renamed_json TEXT NOT NULL,
    breaking INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    stages INTEGER NOT NULL,
    drifts INTEGER NOT NULL,
    breaking INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    col INTEGER NOT NULL,
    span_json TEXT NOT NULL,
    snippet_redacted TEXT NOT NULL,
    hint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id),
    repo_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'start',
    plan_path TEXT,
    plan_hash TEXT,
    rule_ids_json TEXT NOT NULL DEFAULT '["all"]',
    max_risk TEXT NOT NULL DEFAULT 'safe',
    llm_used INTEGER NOT NULL DEFAULT 0,
    rules_dir TEXT,
    base_commit TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refactor_plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    plan_hash TEXT NOT NULL,
    schema_ver TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL,
    UNIQUE (session_id, plan_hash)
);
CREATE TABLE IF NOT EXISTS refactor_plan_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    item_hash TEXT NOT NULL,
    change_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL DEFAULT 1,
    tier TEXT NOT NULL,
    stage TEXT NOT NULL,
    path TEXT NOT NULL,
    span_start INTEGER NOT NULL,
    span_end INTEGER NOT NULL,
    before TEXT NOT NULL,
    after TEXT NOT NULL,
    reason TEXT NOT NULL,
    security_note TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    fingerprint_before TEXT,
    fingerprint_after TEXT,
    applied_at TEXT,
    UNIQUE (plan_id, item_hash),
    UNIQUE (plan_id, change_id)
);
CREATE TABLE IF NOT EXISTS audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(session_id),
    run_id INTEGER REFERENCES runs(id),
    ts TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'cli',
    action TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    args_json TEXT,
    result_json TEXT,
    exit_code INTEGER
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        # Durability + concurrency contract (ZERO_COST_STRATEGY §105):
        # WAL allows a concurrent `oarl loop`-style watcher to read history
        # while a run writes; NORMAL fsync keeps writes cheap.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def _migrate(self) -> None:
        """Phase 2: lineage_edges becomes per-run (run_id, kind columns).

        Old seed DBs have the scratch lineage_edges (producer, consumer) —
        the table is fully rebuilt on every run, so drop-and-recreate is
        lossless.
        """
        cols = {r[1] for r in
                self.conn.execute("PRAGMA table_info(lineage_edges)")}
        if cols and "run_id" not in cols:
            self.conn.execute("DROP TABLE lineage_edges")
        sess_cols = {r[1] for r in
                     self.conn.execute("PRAGMA table_info(sessions)")}
        if sess_cols and "rules_dir" not in sess_cols:
            self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN rules_dir TEXT")

    def close(self) -> None:
        self.conn.close()

    def save_run(self, root: str, stages: list[Stage], lineage: Lineage,
                 drifts: list[Drift]) -> int:
        self.conn.execute("DELETE FROM stages")
        self._upsert_stages(stages)
        breaking = sum(1 for d in drifts if d.breaking)
        run_id = self._insert_run(root, len(stages), len(drifts), breaking)
        self._upsert_edges(run_id, lineage)
        self.conn.executemany(
            "INSERT INTO drifts (run_id, producer, consumer, added_json, "
            "removed_json, renamed_json, breaking) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(run_id, d.producer, d.consumer, json.dumps(d.added),
              json.dumps(d.removed), json.dumps(d.renamed), int(d.breaking))
             for d in drifts])
        self.conn.commit()
        return run_id

    def save_lineage(self, root: str, stages: list[Stage],
                     lineage: Lineage) -> int:
        """Persist a lineage snapshot with per-run edges (Phase 2)."""
        self.conn.execute("DELETE FROM stages")
        self._upsert_stages(stages)
        run_id = self._insert_run(root, len(stages), 0, 0)
        self._upsert_edges(run_id, lineage)
        self.conn.commit()
        return run_id

    def save_stages(self, root: str, stages: list[Stage]) -> int:
        """Persist a parsed pipeline snapshot (parse/inspect commands)."""
        self.conn.execute("DELETE FROM stages")
        self._upsert_stages(stages)
        run_id = self._insert_run(root, len(stages), 0, 0)
        self.conn.commit()
        return run_id

    def save_scan(self, root: str, findings: list[Finding]) -> int:
        """Persist a security scan snapshot (Phase 4)."""
        run_id = self._insert_run(root, 0, 0, 0)
        self.conn.executemany(
            "INSERT INTO scans (run_id, rule_id, severity, path, line, col, "
            "span_json, snippet_redacted, hint, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(run_id, f.rule_id, f.severity, f.path, f.line, f.col,
              json.dumps(list(f.span)), f.snippet_redacted, f.hint, f.status)
             for f in findings])
        self.conn.commit()
        return run_id

    def scan_findings(self, run_id: int) -> list[Finding]:
        """Reconstruct the findings of one scan run (history query)."""
        rows = self.conn.execute(
            "SELECT rule_id, severity, path, line, col, span_json, "
            "snippet_redacted, hint, status FROM scans WHERE run_id = ? "
            "ORDER BY id", (run_id,))
        return [Finding(rule_id=r[0], severity=r[1], path=r[2], line=r[3],
                        col=r[4], span=tuple(json.loads(r[5])),
                        snippet_redacted=r[6], hint=r[7], status=r[8])
                for r in rows]

    def _upsert_edges(self, run_id: int, lineage: Lineage) -> None:
        kinds = getattr(lineage, "edge_kinds", {})
        self.conn.executemany(
            "INSERT OR REPLACE INTO lineage_edges (run_id, producer, consumer, kind) "
            "VALUES (?, ?, ?, ?)",
            [(run_id, a, b, kinds.get((a, b), "ref")) for a, b in lineage.edges])

    def _upsert_stages(self, stages: list[Stage]) -> None:
        for stage in stages:
            self.conn.execute(
                "INSERT OR REPLACE INTO stages (name, path, refs_json, columns_json) "
                "VALUES (?, ?, ?, ?)",
                (stage.name, str(stage.path), json.dumps(stage.refs),
                 json.dumps(stage.columns)))

    def _insert_run(self, root: str, stages: int, drifts: int,
                    breaking: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (root, checked_at, stages, drifts, breaking) "
            "VALUES (?, datetime('now'), ?, ?, ?)",
            (root, stages, drifts, breaking))
        return cur.lastrowid

    def recent_runs(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, root, checked_at, stages, drifts, breaking FROM runs "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return [{"id": r[0], "root": r[1], "checked_at": r[2], "stages": r[3],
                 "drifts": r[4], "breaking": r[5]} for r in rows]

    def run_drifts(self, run_id: int) -> list[Drift]:
        """Reconstruct the Drift findings of one run (Phase 3 history query)."""
        rows = self.conn.execute(
            "SELECT producer, consumer, added_json, removed_json, renamed_json "
            "FROM drifts WHERE run_id = ? ORDER BY id", (run_id,))
        return [Drift(r[0], r[1], added=json.loads(r[2]),
                      removed=json.loads(r[3]),
                      renamed=[tuple(pair) for pair in json.loads(r[4])])
                for r in rows]

    def drift_history(self, limit: int = 10) -> list[dict]:
        """Recent runs with their drift findings (Phase 3 history query)."""
        return [{"id": r["id"], "root": r["root"], "checked_at": r["checked_at"],
                 "stages": r["stages"], "drifts": r["drifts"],
                 "breaking": r["breaking"],
                 "findings": [drift_to_dict(d) for d in self.run_drifts(r["id"])]}
                for r in self.recent_runs(limit)]

    # ---- Phase 4 refactoring sessions, plans, audit ---------------------------

    def create_session(self, repo_fingerprint: str, rules: list[str],
                       max_risk: str, llm_used: bool = False,
                       base_commit: str | None = None,
                       rules_dir: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions (repo_fingerprint, state, rule_ids_json, "
            "max_risk, llm_used, rules_dir, base_commit, created_at, "
            "updated_at) "
            "VALUES (?, 'start', ?, ?, ?, ?, ?, datetime('now'), "
            "datetime('now'))",
            (repo_fingerprint, json.dumps(rules), max_risk, int(llm_used),
             rules_dir, base_commit))
        self.conn.commit()
        return cur.lastrowid

    def get_session(self, session_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT session_id, run_id, repo_fingerprint, state, plan_path, "
            "plan_hash, rule_ids_json, max_risk, llm_used, rules_dir, "
            "base_commit, created_at, updated_at FROM sessions "
            "WHERE session_id = ?",
            (session_id,)).fetchone()
        if row is None:
            return None
        return {"session_id": row[0], "run_id": row[1],
                "repo_fingerprint": row[2], "state": row[3],
                "plan_path": row[4], "plan_hash": row[5],
                "rule_ids": json.loads(row[6]), "max_risk": row[7],
                "llm_used": bool(row[8]), "rules_dir": row[9],
                "base_commit": row[10],
                "created_at": row[11], "updated_at": row[12]}

    def set_session_state(self, session_id: int, state: str,
                          run_id: int | None = None) -> None:
        self.conn.execute(
            "UPDATE sessions SET state = ?, run_id = COALESCE(?, run_id), "
            "updated_at = datetime('now') WHERE session_id = ?",
            (state, run_id, session_id))
        self.conn.commit()

    def set_session_plan(self, session_id: int, plan_path: str,
                         plan_hash: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET plan_path = ?, plan_hash = ?, "
            "updated_at = datetime('now') WHERE session_id = ?",
            (plan_path, plan_hash, session_id))
        self.conn.commit()

    def save_plan(self, session_id: int, run_id: int | None, plan_hash: str,
                  schema_ver: str, items: list[dict]) -> int:
        cur = self.conn.execute(
            "INSERT INTO refactor_plans (session_id, run_id, plan_hash, "
            "schema_ver, item_count, created_at) VALUES (?, ?, ?, ?, ?, "
            "datetime('now'))",
            (session_id, run_id, plan_hash, schema_ver, len(items)))
        plan_id = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO refactor_plan_items (plan_id, item_hash, change_id, "
            "rule_id, rule_version, tier, stage, path, span_start, span_end, "
            "before, after, reason, security_note, state, fingerprint_before) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            [(plan_id, it["item_hash"], it["change_id"], it["rule_id"],
              it["rule_version"], it["tier"], it["stage"], it["path"],
              it["span"][0], it["span"][1], it["before"], it["after"],
              it["reason"], it["security_note"], it["fingerprint_before"])
             for it in items])
        self.conn.commit()
        return plan_id

    def get_plan(self, plan_id: int | None = None,
                 session_id: int | None = None) -> dict | None:
        if plan_id is not None:
            row = self.conn.execute(
                "SELECT plan_id, session_id, run_id, plan_hash, schema_ver, "
                "item_count, status FROM refactor_plans WHERE plan_id = ?",
                (plan_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT plan_id, session_id, run_id, plan_hash, schema_ver, "
                "item_count, status FROM refactor_plans "
                "WHERE session_id = ? ORDER BY plan_id DESC LIMIT 1",
                (session_id,)).fetchone()
        if row is None:
            return None
        items = self.conn.execute(
            "SELECT item_hash, change_id, rule_id, rule_version, tier, stage, "
            "path, span_start, span_end, before, after, reason, security_note, "
            "state, fingerprint_before, fingerprint_after FROM "
            "refactor_plan_items WHERE plan_id = ? ORDER BY change_id",
            (row[0],)).fetchall()
        return {"plan_id": row[0], "session_id": row[1], "run_id": row[2],
                "plan_hash": row[3], "schema_ver": row[4],
                "item_count": row[5], "status": row[6],
                "items": [{"item_hash": i[0], "change_id": i[1],
                           "rule_id": i[2], "rule_version": i[3],
                           "tier": i[4], "stage": i[5], "path": i[6],
                           "span": [i[7], i[8]], "before": i[9],
                           "after": i[10], "reason": i[11],
                           "security_note": i[12], "state": i[13],
                           "fingerprint_before": i[14],
                           "fingerprint_after": i[15]}
                          for i in items]}

    def update_plan_item(self, plan_id: int, item_hash: str, state: str,
                         fingerprint_after: str | None = None) -> None:
        self.conn.execute(
            "UPDATE refactor_plan_items SET state = ?, fingerprint_after = "
            "COALESCE(?, fingerprint_after), applied_at = "
            "CASE WHEN ? = 'applied' THEN datetime('now') ELSE applied_at END "
            "WHERE plan_id = ? AND item_hash = ?",
            (state, fingerprint_after, state, plan_id, item_hash))
        self.conn.commit()

    def set_plan_status(self, plan_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE refactor_plans SET status = ? WHERE plan_id = ?",
            (status, plan_id))
        self.conn.commit()

    def audit_log(self, session_id: int | None, action: str,
                  from_state: str | None, to_state: str | None,
                  args_json: str | None, result_json: str | None,
                  exit_code: int | None = None,
                  run_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit (session_id, run_id, ts, action, from_state, "
            "to_state, args_json, result_json, exit_code) "
            "VALUES (?, ?, datetime('now'), ?, ?, ?, ?, ?, ?)",
            (session_id, run_id, action, from_state, to_state, args_json,
             result_json, exit_code))
        self.conn.commit()

    def audit_rows(self, session_id: int | None = None,
                   limit: int = 100) -> list[dict]:
        q = "SELECT audit_id, session_id, run_id, ts, action, from_state, " \
            "to_state, args_json, result_json, exit_code FROM audit"
        params: tuple = ()
        if session_id is not None:
            q += " WHERE session_id = ?"
            params = (session_id,)
        q += " ORDER BY audit_id DESC LIMIT ?"
        rows = self.conn.execute(q, params + (limit,)).fetchall()
        return [{"id": r[0], "session_id": r[1], "run_id": r[2], "ts": r[3],
                 "action": r[4], "from_state": r[5], "to_state": r[6],
                 "args_json": r[7], "result_json": r[8], "exit_code": r[9]}
                for r in rows]