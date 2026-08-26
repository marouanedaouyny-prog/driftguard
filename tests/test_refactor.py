"""Phase 4 milestone 2: refactor engine tests.

Golden tests per rule (REF-001..006), idempotency apply(apply(x)) ==
apply(x), security block overlay, state machine, and CLI E2E
(plan -> approve -> apply -> verify -> audit).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from driftguard.core.ir.model import Column, Pipeline, Span, Stage
from driftguard.core.refactor import state as fsm
from driftguard.core.refactor.apply import ApplyError, apply_plan
from driftguard.core.refactor.catalog import RULES_BY_ID
from driftguard.core.refactor.model import AnalysisContext
from driftguard.core.refactor.planner import analyze_pipeline, item_hash
from driftguard.store import Store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def stage(name: str, raw: str) -> Stage:
    return Stage(name=name, path=Path(f"{name}.sql"), raw=raw)


def ctx_with(columns_by_name: dict[str, list[str]]) -> AnalysisContext:
    stages = [stage(n, f"select {', '.join(cols)} from raw_{n}")
              for n, cols in columns_by_name.items()]
    for s in stages:
        s.columns = [Column(name=c, source_expr=c, alias=None,
                            span=Span(0, len(c))) for c in columns_by_name[s.name]]
    pipe = Pipeline(root=Path("."), stages=stages)
    return AnalysisContext.build(pipe)


class SqlutilTest(unittest.TestCase):
    def test_cte_definitions_multiple_with_comma(self):
        from driftguard.core.refactor import sqlutil as su
        raw = "WITH a AS (select 1), b AS (select 2) select * from b"
        tokens, pref = su.structure(raw)
        defs, with_start = su.cte_definitions(tokens, pref)
        self.assertEqual([d.name for d in defs], ["a", "b"])
        self.assertEqual(with_start, 0)
        self.assertEqual(defs[0].name_start, 5)
        self.assertIsNotNone(defs[0].comma_end)
        self.assertIsNone(defs[1].comma_end)
        self.assertEqual(raw[defs[0].body_start:defs[0].body_end], "select 1")

    def test_cte_definitions_skips_unclosed_body(self):
        from driftguard.core.refactor import sqlutil as su
        raw = "WITH a AS (select 1"
        tokens, pref = su.structure(raw)
        defs, _ = su.cte_definitions(tokens, pref)
        self.assertEqual(defs, [])

    def test_projection_items(self):
        from driftguard.core.refactor import sqlutil as su
        raw = "select id, name as n, count(*) c from t"
        tokens, pref = su.structure(raw)
        items = su.projection_items(tokens, pref, raw)
        self.assertEqual([it[2] for it in items],
                         ["id", "name as n", "count(*) c"])

    def test_from_items_and_aliases(self):
        from driftguard.core.refactor import sqlutil as su
        raw = "select id from orders o join customers c on o.id = c.id"
        tokens, pref = su.structure(raw)
        items = su.from_items(tokens, pref, raw)
        self.assertEqual([it[2] for it in items],
                         ["orders o", "customers c"])
        self.assertEqual(su.item_alias("orders o"), "o")
        self.assertEqual(su.item_alias("orders AS o"), "o")
        self.assertIsNone(su.item_alias("orders"))
        self.assertIsNone(su.item_alias("schema.orders o"))
        self.assertEqual(su.from_target("{{ ref('orders') }}"), "orders")
        self.assertEqual(su.from_target("schema.orders"), "schema")


class Ref001Test(unittest.TestCase):
    def _analyze(self, raw):
        rule = RULES_BY_ID["REF-001"]
        return rule.analyze(stage("m", raw), AnalysisContext.build(
            Pipeline(root=Path("."), stages=[])))

    def test_drops_middle_cte(self):
        raw = "WITH dead AS (select id from t), keep AS (select id from t2) select id from keep"
        cands = self._analyze(raw)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.rule_id, "REF-001")
        self.assertEqual(c.before, "dead AS (select id from t), ")
        self.assertEqual(c.after, "")
        self.assertEqual(raw[:c.span.start] + c.after + raw[c.span.end:],
                         "WITH keep AS (select id from t2) select id from keep")

    def test_drops_last_cte(self):
        raw = ("WITH used AS (select 1), b AS (select 2) "
               "select 2 from used")
        cands = self._analyze(raw)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].before, ", b AS (select 2)")
        self.assertEqual(raw[:cands[0].span.start] + cands[0].after
                         + raw[cands[0].span.end:],
                         "WITH used AS (select 1) select 2 from used")

    def test_drops_only_cte(self):
        raw = "WITH a AS (select 1) select 1"
        cands = self._analyze(raw)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].before, "WITH a AS (select 1)")
        self.assertEqual(cands[0].after, "")
        self.assertEqual(raw[:cands[0].span.start] + cands[0].after
                         + raw[cands[0].span.end:], " select 1")

    def test_keeps_referenced_cte(self):
        raw = "WITH used AS (select id from t) select id from used"
        self.assertEqual(self._analyze(raw), [])

    def test_side_effect_guard(self):
        raw = ("WITH sink AS (insert into t select id from u) "
               "select 1")
        self.assertEqual(self._analyze(raw), [])


class Ref002Test(unittest.TestCase):
    def _analyze(self, raw):
        rule = RULES_BY_ID["REF-002"]
        return rule.analyze(stage("m", raw), AnalysisContext.build(
            Pipeline(root=Path("."), stages=[])))

    def test_drops_duplicate_keeping_first(self):
        raw = "select id, name, id from t"
        cands = self._analyze(raw)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].before, ", id")
        self.assertEqual(raw[:cands[0].span.start] + cands[0].after
                         + raw[cands[0].span.end:], "select id, name from t")

    def test_alias_distinguishes(self):
        raw = "select a, a as b from t"
        self.assertEqual(self._analyze(raw), [])

    def test_no_duplicates(self):
        self.assertEqual(self._analyze("select a, b from t"), [])

    def test_duplicate_aliased(self):
        raw = "select id as x, id as x from t"
        cands = self._analyze(raw)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].before, ", id as x")


class Ref003Test(unittest.TestCase):
    def _analyze(self, raw):
        rule = RULES_BY_ID["REF-003"]
        return rule.analyze(stage("m", raw), AnalysisContext.build(
            Pipeline(root=Path("."), stages=[])))

    def test_inlines_single_use(self):
        raw = "WITH t1 AS (select id from raw) select id from t1"
        cands = self._analyze(raw)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].before, "t1")
        self.assertEqual(cands[0].after, "(select id from raw)")
        self.assertEqual(raw[:cands[0].span.start] + cands[0].after
                         + raw[cands[0].span.end:],
                         "WITH t1 AS (select id from raw) select id from (select id from raw)")

    def test_multi_use_skipped(self):
        raw = ("WITH t1 AS (select id from raw) "
               "select a.id from t1 a join t1 b on a.id = b.id")
        self.assertEqual(self._analyze(raw), [])

    def test_recursive_body_skipped(self):
        raw = "WITH t1 AS (select id from t1) select id from t1"
        self.assertEqual(self._analyze(raw), [])

    def test_order_by_body_skipped(self):
        raw = "WITH t1 AS (select id from raw order by id) select id from t1"
        self.assertEqual(self._analyze(raw), [])

    def test_tier_is_suggested(self):
        self.assertEqual(RULES_BY_ID["REF-003"].tier, "suggested")


class Ref004Test(unittest.TestCase):
    def test_lowercases_identifiers_not_keywords(self):
        rule = RULES_BY_ID["REF-004"]
        raw = "select ID, NAME from ORDERS"
        cands = rule.analyze(stage("m", raw), AnalysisContext.build(
            Pipeline(root=Path("."), stages=[])))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].before, "ID, NAME from ORDERS")
        self.assertEqual(cands[0].after, "id, name from orders")

    def test_jinja_untouched(self):
        rule = RULES_BY_ID["REF-004"]
        raw = "select {{ ref('ORDERS') }}.ID from ORDERS"
        cands = rule.analyze(stage("m", raw), AnalysisContext.build(
            Pipeline(root=Path("."), stages=[])))
        self.assertEqual(len(cands), 1)
        rewritten = (raw[:cands[0].span.start] + cands[0].after
                     + raw[cands[0].span.end:])
        self.assertIn("{{ ref('ORDERS') }}", rewritten)
        self.assertIn(".id from orders", rewritten)

    def test_already_canonical_no_candidate(self):
        rule = RULES_BY_ID["REF-004"]
        cands = rule.analyze(stage("m", "select id from t"), AnalysisContext.build(
            Pipeline(root=Path("."), stages=[])))
        self.assertEqual(cands, [])


class Ref005Test(unittest.TestCase):
    def test_expands_star(self):
        rule = RULES_BY_ID["REF-005"]
        ctx = ctx_with({"orders": ["id", "name"]})
        cands = rule.analyze(stage("m", "select * from orders"), ctx)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].before, "* ")
        self.assertEqual(cands[0].after, "id, name")

    def test_expands_qualified_star(self):
        rule = RULES_BY_ID["REF-005"]
        ctx = ctx_with({"orders": ["id"]})
        cands = rule.analyze(stage("m", "select o.* from orders o"), ctx)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].after, "o.id")

    def test_unknown_source_skipped(self):
        rule = RULES_BY_ID["REF-005"]
        ctx = ctx_with({})
        self.assertEqual(rule.analyze(stage("m", "select * from ghost"), ctx),
                         [])

    def test_multi_from_bare_star_skipped(self):
        rule = RULES_BY_ID["REF-005"]
        ctx = ctx_with({"a": ["id"], "b": ["x"]})
        self.assertEqual(
            rule.analyze(stage("m", "select * from a join b on a.id = b.id"),
                         ctx), [])

    def test_tier_is_risky(self):
        self.assertEqual(RULES_BY_ID["REF-005"].tier, "risky")


class Ref006Test(unittest.TestCase):
    def _analyze(self, raw):
        rule = RULES_BY_ID["REF-006"]
        return rule.analyze(stage("m", raw), AnalysisContext.build(
            Pipeline(root=Path("."), stages=[])))

    def test_drops_unused_alias(self):
        raw = "select id from orders o"
        cands = self._analyze(raw)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].before, "o")
        self.assertEqual(raw[:cands[0].span.start] + cands[0].after
                         + raw[cands[0].span.end:], "select id from orders ")

    def test_drops_unused_as_alias(self):
        raw = "select id from orders AS o"
        cands = self._analyze(raw)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].before, " AS o")
        self.assertEqual(raw[:cands[0].span.start] + cands[0].after
                         + raw[cands[0].span.end:], "select id from orders")

    def test_keeps_used_alias(self):
        raw = "select o.id from orders o where o.id > 1"
        self.assertEqual(self._analyze(raw), [])


class Ref007Test(unittest.TestCase):
    def _analyze(self, raw):
        rule = RULES_BY_ID["REF-007"]
        return rule.analyze(stage("m", raw), AnalysisContext.build(
            Pipeline(root=Path("."), stages=[])))

    def _rewritten(self, raw):
        cands = self._analyze(raw)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.before, raw[c.span.start:c.span.end])
        return raw[:c.span.start] + c.after + raw[c.span.end:]

    def test_drops_derived_table_order_by(self):
        result = self._rewritten(
            "select a from (select b from t order by b) x")
        self.assertEqual(result, "select a from (select b from t) x")

    def test_drops_cte_body_order_by(self):
        result = self._rewritten(
            "with c as (select 1 as a order by a) select a from c")
        self.assertEqual(result,
                         "with c as (select 1 as a) select a from c")

    def test_drops_in_subquery_order_by_with_comment(self):
        result = self._rewritten(
            "select id from o where id in "
            "(select id from old order by id /* stale */)")
        self.assertEqual(result,
                         "select id from o where id in (select id from old)")

    def test_keeps_order_by_with_limit(self):
        raw = "select a from (select b from t order by b limit 5) x"
        self.assertEqual(self._analyze(raw), [])

    def test_keeps_order_by_with_offset(self):
        raw = "select a from (select b from t order by b offset 2) x"
        self.assertEqual(self._analyze(raw), [])

    def test_keeps_union_subquery_order_by(self):
        raw = ("select a from (select b from t union all "
               "select c from u order by b) x")
        self.assertEqual(self._analyze(raw), [])

    def test_keeps_window_over_order_by(self):
        raw = "select sum(x) over (order by y) from t"
        self.assertEqual(self._analyze(raw), [])

    def test_keeps_aggregate_order_by(self):
        raw = "select string_agg(x order by x) from t"
        self.assertEqual(self._analyze(raw), [])

    def test_keeps_top_level_order_by(self):
        raw = "select b from t order by b"
        self.assertEqual(self._analyze(raw), [])

    def test_skips_template_region_inside_subquery(self):
        raw = ("select a from (select b from {{ ref('orders') }} "
               "order by b) x")
        self.assertEqual(self._analyze(raw), [])

    def test_skips_side_effect_statements(self):
        raw = ("delete from t where id in "
               "(select id from old order by id)")
        self.assertEqual(self._analyze(raw), [])

    def test_nested_subqueries_both_fire_disjoint_spans(self):
        raw = ("select a from (select b from t where b in "
               "(select z from u order by z) order by b) x")
        cands = sorted(self._analyze(raw), key=lambda c: c.span.start)
        self.assertEqual(len(cands), 2)
        self.assertLess(cands[0].span.end, cands[1].span.start)
        for c in reversed(cands):
            raw = raw[:c.span.start] + c.after + raw[c.span.end:]
        self.assertEqual(raw, "select a from (select b from t where b in "
                              "(select z from u)) x")

    def test_idempotent_reanalysis_is_clean(self):
        once = self._rewritten(
            "with c as (select 1 as a order by a limit 3) select * from c"
            .replace(" limit 3", ""))
        self.assertNotIn("order by", once.lower())


class ApplyTest(unittest.TestCase):
    def _write(self, d: Path, name: str, text: str) -> None:
        (d / name).write_text(text, encoding="utf-8")

    def _plan_file(self, d: Path, items: list[dict], root: Path) -> Path:
        plan = {"schema": "driftguard.plan.v1", "version": 1,
                "session_id": 1, "root": str(root),
                "pipeline_fingerprint": "fp", "max_risk": "safe",
                "rule_ids": [], "items": items, "plan_hash": "ph"}
        p = d / "plan.json"
        p.write_text(json.dumps(plan), encoding="utf-8")
        return p

    def _item(self, stage_name: str, span: list[int], before: str,
              after: str) -> dict:
        return {"rule_id": "REF-001", "rule_version": 1, "tier": "safe",
                "stage": stage_name, "path": f"{stage_name}.sql",
                "span": span, "before": before, "after": after,
                "reason": "test", "security_note": None,
                "fingerprint_before": "fb"}

    def test_idempotent_reapply(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            raw = "WITH dead AS (select 1), keep AS (select 2) select 2"
            self._write(d, "m.sql", raw)
            before = "dead AS (select 1), "
            start = raw.index("dead")
            items = [self._item("m", [start, start + len(before)], before, "")]
            items[0]["item_hash"] = item_hash(items[0])
            plan = self._plan_file(d, items, d)
            r1 = apply_plan(plan, "in_place", None)
            self.assertEqual(len(r1["applied"]), 1)
            self.assertEqual(len(r1["noop"]), 0)
            self.assertEqual(len(r1["skipped"]), 0)
            first = (d / "m.sql").read_text(encoding="utf-8")
            self.assertEqual(first, "WITH keep AS (select 2) select 2")
            r2 = apply_plan(plan, "in_place", None,
                            skip_hashes={items[0]["item_hash"]})
            self.assertEqual(len(r2["applied"]), 0)
            self.assertEqual(len(r2["skipped"]), 1)
            self.assertEqual((d / "m.sql").read_text(encoding="utf-8"), first)

    def test_stale_span_raises(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write(d, "m.sql", "SELECT 1 FROM t")
            plan = self._plan_file(d, [self._item(
                "m", [0, 4], "WITH", "X")], d)
            with self.assertRaises(ApplyError):
                apply_plan(plan, "in_place", None)

    def test_out_dir_keeps_original(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            raw = "WITH a AS (select 1), b AS (select 2) select 2"
            self._write(d, "m.sql", raw)
            before = "a AS (select 1), "
            start = len("WITH ")
            items = [self._item("m", [start, start + len(before)], before, "")]
            plan = self._plan_file(d, items, d)
            out = d / "out"
            result = apply_plan(plan, "out_dir", out)
            self.assertEqual(len(result["applied"]), 1)
            self.assertEqual((d / "m.sql").read_text(encoding="utf-8"), raw)
            self.assertEqual((out / "m.sql").read_text(encoding="utf-8"),
                             "WITH b AS (select 2) select 2")

    def test_backup_written(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            raw = "WITH a AS (select 1) select 1"
            self._write(d, "m.sql", raw)
            plan = self._plan_file(d, [self._item("m", [0, len(raw)],
                                                  raw, "")], d)
            result = apply_plan(plan, "in_place", None, no_backup=False)
            self.assertEqual(len(result["backups"]), 1)
            self.assertTrue((d / "m.sql.orig").exists())


class PlannerTest(unittest.TestCase):
    def _seed(self, d: Path, files: dict[str, str]) -> None:
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")

    def test_security_block_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            secret = "sk-" + "a" * 30
            self._seed(d, {
                "m.sql": (f"WITH dead AS (select '{secret}' as k), "
                          "keep AS (select 1) select 1"),
            })
            analysis = analyze_pipeline(d)
            self.assertEqual(len(analysis["blocked"]), 1)
            self.assertEqual(analysis["blocked"][0]["rule_id"], "REF-001")
            self.assertIn("SEC-001", analysis["blocked"][0]["block_reason"])
            self.assertEqual(analysis["candidates"], [])
            analysis2 = analyze_pipeline(d, allow_on_finding=True)
            self.assertEqual(len(analysis2["candidates"]), 1)
            self.assertIn("SEC-001", analysis2["candidates"][0]
                          ["security_note"])

    def test_max_risk_gate(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._seed(d, {
                "orders.sql": "select id from raw_orders",
                "m.sql": ("WITH t1 AS (select id from orders) "
                          "select id from t1"),
                "n.sql": "select * from orders",
            })
            safe = analyze_pipeline(d, max_risk="safe")
            self.assertNotIn("REF-003", {c["rule_id"]
                                         for c in safe["candidates"]})
            self.assertNotIn("REF-005", {c["rule_id"]
                                         for c in safe["candidates"]})
            suggested = analyze_pipeline(d, max_risk="suggested")
            self.assertIn("REF-003", {c["rule_id"]
                                      for c in suggested["candidates"]})
            self.assertNotIn("REF-005", {c["rule_id"]
                                         for c in suggested["candidates"]})
            risky = analyze_pipeline(d, max_risk="risky")
            self.assertIn("REF-005", {c["rule_id"]
                                      for c in risky["candidates"]})

    def test_rule_filter(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._seed(d, {"m.sql": "WITH a AS (select 1) select 1"})
            analysis = analyze_pipeline(d, rules=["REF-002"])
            self.assertEqual(analysis["candidates"], [])


class StateMachineTest(unittest.TestCase):
    def test_happy_path(self):
        s = "start"
        for target in ("parsed", "analyzed", "planned", "approved",
                       "applied", "verified", "done"):
            s = fsm.transition(s, target)
        self.assertEqual(s, "done")

    def test_illegal_transition(self):
        with self.assertRaises(fsm.TransitionError):
            fsm.transition("start", "applied")

    def test_self_transition_noop(self):
        self.assertEqual(fsm.transition("planned", "planned"), "planned")


class CliE2ETest(unittest.TestCase):
    """Full chain against the real CLI in a throwaway workspace."""

    def _run(self, args: list[str], cwd: Path, env: dict) -> tuple[int, str]:
        e = dict(os.environ)
        e["PYTHONPATH"] = str(ROOT)
        e.update(env or {})
        proc = subprocess.run([sys.executable, "-m", "driftguard"] + args,
                              capture_output=True, text=True, cwd=str(cwd),
                              env=e, encoding="utf-8", errors="replace")
        return proc.returncode, proc.stdout + proc.stderr

    def _seed(self, d: Path) -> None:
        (d / "orders.sql").write_text(
            "select id, name from raw_orders", encoding="utf-8")
        (d / "m.sql").write_text(
            "WITH dead AS (select 1), keep AS (select id, name from orders) "
            "select id, name, id from keep", encoding="utf-8")

    def test_full_chain(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._seed(d)
            env = {"DRIFTGUARD_DB": str(d / "test.db")}

            code, out = self._run(["refactor", "plan", str(d)], d, env)
            self.assertEqual(code, 0, out)
            plan_file = d / "refactor_plan.json"
            self.assertTrue(plan_file.exists())
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
            self.assertEqual(plan["schema"], "driftguard.plan.v1")
            self.assertGreaterEqual(len(plan["items"]), 1)
            rules = {it["rule_id"] for it in plan["items"]}
            self.assertIn("REF-001", rules)
            self.assertIn("REF-002", rules)
            session_id = plan["session_id"]

            code, out = self._run(["refactor", "approve",
                                   "--session", str(session_id)], d, env)
            self.assertEqual(code, 0, out)

            code, out = self._run(["refactor", "apply",
                                   "--plan", "refactor_plan.json",
                                   "--in-place"], d, env)
            self.assertEqual(code, 0, out)
            self.assertIn("applied", out)
            text = (d / "m.sql").read_text(encoding="utf-8")
            self.assertNotIn("dead AS", text)
            self.assertNotIn(", id, name", text)
            self.assertTrue((d / "m.sql.orig").exists())

            code, out = self._run(["refactor", "verify",
                                   "--session", str(session_id)], d, env)
            self.assertEqual(code, 0, out)
            self.assertIn("verified and closed", out)

            code, out = self._run(["refactor", "apply",
                                   "--plan", "refactor_plan.json",
                                   "--in-place"], d, env)
            self.assertEqual(code, 0, out)
            self.assertIn("already applied, skipped", out)
            self.assertEqual((d / "m.sql").read_text(encoding="utf-8"), text)

            code, out = self._run(["session", "show", str(session_id)],
                                  d, env)
            self.assertEqual(code, 0, out)
            self.assertIn("state=done", out)

            code, out = self._run(["audit", "--session", str(session_id)],
                                  d, env)
            self.assertEqual(code, 0, out)
            for action in ("CREATE", "PARSE", "ANALYZE", "PLAN", "APPROVE",
                           "APPLY", "VERIFY", "CLOSE"):
                self.assertIn(action, out)

            code, out = self._run(["audit", "--json"], d, env)
            self.assertEqual(code, 0, out)
            envelope = json.loads(out[out.index("{\n"):])
            self.assertEqual(envelope["schema"], "driftguard.audit.v1")

    def test_apply_requires_approval(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._seed(d)
            env = {"DRIFTGUARD_DB": str(d / "test.db")}
            code, out = self._run(["refactor", "plan", str(d)], d, env)
            self.assertEqual(code, 0, out)
            code, out = self._run(["refactor", "apply",
                                   "--plan", "refactor_plan.json",
                                   "--in-place"], d, env)
            self.assertEqual(code, 2, out)
            self.assertIn("state_error", out)

    def test_apply_requires_exactly_one_mode(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._seed(d)
            env = {"DRIFTGUARD_DB": str(d / "test.db")}
            code, out = self._run(["refactor", "plan", str(d)], d, env)
            self.assertEqual(code, 0, out)
            code, out = self._run(["refactor", "apply",
                                   "--plan", "refactor_plan.json"], d, env)
            self.assertEqual(code, 2, out)
            self.assertIn("usage", out)

    def test_analyze_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._seed(d)
            env = {"DRIFTGUARD_DB": str(d / "test.db")}
            code, out = self._run(["refactor", "analyze", str(d), "--json"],
                                  d, env)
            self.assertEqual(code, 0, out)
            start = out.index("{\n")
            envelope = json.loads(out[start:])
            self.assertEqual(envelope["schema"], "driftguard.analysis.v1")
            self.assertIn("baseline_scan", envelope)
            self.assertIn("candidates", envelope)
            self.assertIn("blocked", envelope)
            self.assertIn("lineage", envelope)

    def test_verify_regression_returns_to_approved(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "orders.sql").write_text("select id from raw_orders",
                                          encoding="utf-8")
            (d / "m.sql").write_text(
                "WITH t1 AS (select id from orders) select id from t1",
                encoding="utf-8")
            env = {"DRIFTGUARD_DB": str(d / "test.db")}
            code, out = self._run(["refactor", "plan", str(d),
                                   "--max-risk", "suggested"], d, env)
            self.assertEqual(code, 0, out)
            plan = json.loads((d / "refactor_plan.json").read_text(
                encoding="utf-8"))
            self.assertTrue(all(it["rule_id"] == "REF-003"
                                for it in plan["items"]))
            sid = plan["session_id"]
            code, out = self._run(["refactor", "approve",
                                   "--session", str(sid)], d, env)
            self.assertEqual(code, 0, out)
            code, out = self._run(["refactor", "apply",
                                   "--plan", "refactor_plan.json",
                                   "--in-place"], d, env)
            self.assertEqual(code, 0, out)
            code, out = self._run(["refactor", "verify",
                                   "--session", str(sid)], d, env)
            self.assertEqual(code, 1, out)
            self.assertIn("FAILED", out)
            store = Store(d / "test.db")
            try:
                self.assertEqual(store.get_session(sid)["state"], "approved")
            finally:
                store.close()

    def test_ci_apply(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._seed(d)
            env = {"DRIFTGUARD_DB": str(d / "test.db")}
            code, out = self._run(["refactor", "plan", str(d)], d, env)
            self.assertEqual(code, 0, out)
            code, out = self._run(["refactor", "apply",
                                   "--plan", "refactor_plan.json",
                                   "--in-place", "--ci"], d, env)
            self.assertEqual(code, 0, out)
            code, out = self._run(["audit", "--json"], d, env)
            envelope = json.loads(out[out.index("{\n"):])
            approve_rows = [r for r in envelope["rows"]
                            if r["action"] == "APPROVE"]
            self.assertEqual(len(approve_rows), 1)


if __name__ == "__main__":
    unittest.main()