"""Phase 2 acceptance (ARCHITECTURE §Phase 2): lineage graph, topological
order, sources.yml resolution, per-run edge persistence, lineage CLI golden.

Known-answer tests: diamond, cycle, missing ref, source ref; topo order
validated on a synthetic DAG; `lineage --json` matches the golden fixture.
"""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from driftguard.__main__ import main
from driftguard.core.lineage import Lineage, build_lineage
from driftguard.core.parser.dialects.dbt import (
    DbtParser,
    find_sources,
    parse_sources_yml,
)
from driftguard.parser import parse_sql_file

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "models"
GOLDEN = ROOT / "tests" / "golden" / "lineage" / "examples.json"


def _stage(name: str, sql: str):
    tmp = Path(tempfile.mkdtemp())
    path = tmp / f"{name}.sql"
    path.write_text(sql, encoding="utf-8")
    return parse_sql_file(path)


def _core_stages(spec: dict[str, str]) -> list:
    root = Path(tempfile.mkdtemp())
    for name, sql in spec.items():
        (root / f"{name}.sql").write_text(sql, encoding="utf-8")
    return DbtParser().parse_project(root).stages


class KnownAnswerLineageTest(unittest.TestCase):

    def test_diamond_topology(self):
        a = _stage("a", "SELECT x FROM raw")
        b = _stage("b", "SELECT x FROM {{ ref('a') }}")
        c = _stage("c", "SELECT x FROM {{ ref('a') }}")
        d = _stage("d", "SELECT x FROM {{ ref('b') }} JOIN {{ ref('c') }} ON 1=1")
        lineage = build_lineage([a, b, c, d])
        self.assertEqual(sorted(lineage.edges),
                         [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")])
        order = lineage.topo_order
        self.assertEqual(order[0], "a")
        self.assertEqual(order[-1], "d")
        for producer, consumer in lineage.edges:
            self.assertLess(order.index(producer), order.index(consumer))

    def test_cycle_detected_and_topo_deterministic(self):
        a = _stage("a", "SELECT x FROM {{ ref('b') }}")
        b = _stage("b", "SELECT x FROM {{ ref('c') }}")
        c = _stage("c", "SELECT x FROM {{ ref('a') }}")
        lineage = build_lineage([a, b, c])
        self.assertEqual(len(lineage.cycles), 1)
        cycle = lineage.cycles[0]
        self.assertEqual(cycle[0], cycle[-1])  # closed walk
        self.assertEqual(sorted(set(cycle)), ["a", "b", "c"])
        self.assertEqual(len(cycle), 4)
        self.assertEqual(lineage.topo_order, ["a", "b", "c"])

    def test_missing_ref_tracked(self):
        a = _stage("a", "SELECT x FROM {{ ref('ghost') }}")
        lineage = build_lineage([a])
        self.assertEqual(lineage.missing, [("ghost", "a")])

    def test_source_ref_resolved_via_sources_yml(self):
        stages = _core_stages({"orders": "SELECT id FROM "
                                        "{{ source('raw', 'orders') }}"})
        root = Path(tempfile.mkdtemp())
        (root / "sources.yml").write_text(
            "sources:\n"
            "  - name: raw\n"
            "    tables:\n"
            "      - name: orders\n", encoding="utf-8")
        source_tables = find_sources(root)
        self.assertEqual(source_tables, {"raw.orders"})
        lineage = build_lineage(stages, source_tables)
        self.assertEqual(lineage.edges, [("raw.orders", "orders")])
        self.assertEqual(lineage.kind("raw.orders", "orders"), "source")
        self.assertEqual(lineage.missing, [])

    def test_unresolved_source_ref_is_missing(self):
        stages = _core_stages({"orders": "SELECT id FROM "
                                        "{{ source('raw', 'orders') }}"})
        lineage = build_lineage(stages, set())
        self.assertEqual(lineage.missing, [("raw.orders", "orders")])

    def test_duplicate_refs_deduped(self):
        a = _stage("a", "SELECT x FROM {{ ref('p') }} JOIN {{ ref('p') }} "
                        "ON 1=1")
        p = _stage("p", "SELECT x FROM raw")
        lineage = build_lineage([a, p])
        self.assertEqual(lineage.edges, [("p", "a")])

    def test_source_edges_do_not_drift(self):
        stages = _core_stages({
            "orders": "SELECT id, status FROM {{ source('raw', 'orders') }}",
        })
        lineage = build_lineage(stages, {"raw.orders"})
        from driftguard.core.lineage import Lineage as L
        from driftguard.drift import detect_drifts
        self.assertEqual(detect_drifts(lineage), [])


class SourcesYmlTest(unittest.TestCase):

    def test_parse_sources_yml_subset(self):
        path = Path(tempfile.mkdtemp()) / "sources.yml"
        path.write_text(
            "version: 2\n"
            "sources:\n"
            "  - name: raw\n"
            "    database: dw\n"
            "    schema: landing\n"
            "    tables:\n"
            "      - name: orders\n"
            "      - name: customers\n"
            "  - name: app\n"
            "    tables:\n"
            "      - name: events\n", encoding="utf-8")
        sources = parse_sources_yml(path)
        self.assertEqual(sources, [
            {"source": "raw", "tables": ["orders", "customers"]},
            {"source": "app", "tables": ["events"]},
        ])

    def test_find_sources_skips_missing_files(self):
        self.assertEqual(find_sources(Path(tempfile.mkdtemp())), set())


class TopoOrderPropertyTest(unittest.TestCase):

    def test_topo_order_respects_all_edges(self):
        stages = _core_stages({
            "a": "SELECT x FROM raw",
            "b": "SELECT x FROM {{ ref('a') }}",
            "c": "SELECT x FROM {{ ref('a') }}",
            "d": "SELECT x FROM {{ ref('b') }}",
            "e": "SELECT x FROM {{ ref('c') }}",
            "f": "SELECT x FROM {{ ref('d') }} JOIN {{ ref('e') }} ON 1=1",
        })
        lineage = build_lineage(stages)
        order = lineage.topo_order
        self.assertEqual(len(order), 6)
        for producer, consumer in lineage.edges:
            self.assertLess(order.index(producer), order.index(consumer),
                            f"edge {producer}->{consumer} violates topo order")


class LineageCliTest(unittest.TestCase):

    def _pipeline(self, spec: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp())
        for name, sql in spec.items():
            (root / f"{name}.sql").write_text(sql, encoding="utf-8")
        return root

    def test_lineage_json_matches_golden(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["lineage", str(EXAMPLES), "--json", "--no-persist"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["schema"], "driftguard.lineage.v1")
        self.assertEqual(data["version"], 1)
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        for field in ("edges", "cycles", "missing", "topo_order",
                      "pipeline_fingerprint"):
            self.assertEqual(data[field], golden[field], field)
        self.assertEqual(data["source_tables"], [])

    def test_lineage_exit_zero_with_findings(self):
        root = self._pipeline({"a": "SELECT x FROM {{ ref('ghost') }}"})
        self.assertEqual(main(["lineage", str(root), "--no-persist"]), 0)

    def test_lineage_no_input_exits_two(self):
        self.assertEqual(main(["lineage", "/definitely/not/a/dir"]), 2)

    def test_lineage_hard_parse_error_exits_two(self):
        root = self._pipeline({"bad": "SELECT 'unterminated FROM t"})
        self.assertEqual(main(["lineage", str(root), "--no-persist"]), 2)

    def test_drift_and_lineage_agree_on_sources(self):
        root = self._pipeline({"orders": "SELECT id, status FROM "
                                         "{{ source('raw', 'orders') }}"})
        (root / "sources.yml").write_text(
            "sources:\n"
            "  - name: raw\n"
            "    tables:\n"
            "      - name: orders\n", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["lineage", str(root), "--json", "--no-persist"])
        self.assertEqual(code, 0)
        lineage = json.loads(buf.getvalue())
        self.assertEqual(lineage["missing"], [])
        self.assertEqual(
            [e for e in lineage["edges"] if e["producer"] == "raw.orders"],
            [{"producer": "raw.orders", "consumer": "orders", "kind": "source"}])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["drift", str(root), "--json", "--no-persist"])
        self.assertEqual(code, 0)
        drift = json.loads(buf.getvalue())
        self.assertEqual(drift["edges"], 1)
        self.assertEqual(drift["cycles"], 0)
        self.assertEqual(drift["drifts"], [])
        self.assertFalse(drift["breaking"])


class PerRunEdgePersistenceTest(unittest.TestCase):

    def test_edges_persist_per_run(self):
        from driftguard.store import Store
        db = Path(tempfile.mkdtemp()) / "runs.db"
        store = Store(db)
        stages = [_stage("a", "SELECT x FROM raw"),
                  _stage("b", "SELECT x FROM {{ ref('a') }}")]
        lineage = build_lineage(stages)
        run1 = store.save_lineage(str(Path(".")), stages, lineage)
        lineage2 = build_lineage([stages[0]])
        run2 = store.save_lineage(str(Path(".")), [stages[0]], lineage2)
        rows = store.conn.execute(
            "SELECT run_id, producer, consumer, kind FROM lineage_edges "
            "ORDER BY run_id, producer, consumer").fetchall()
        self.assertEqual(rows, [(run1, "a", "b", "ref")])
        store.close()

    def test_old_schema_db_migrated(self):
        db = Path(tempfile.mkdtemp()) / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE lineage_edges (producer TEXT NOT NULL, "
            "consumer TEXT NOT NULL, PRIMARY KEY (producer, consumer));")
        conn.execute("INSERT INTO lineage_edges VALUES ('a', 'b')")
        conn.commit()
        conn.close()
        from driftguard.store import Store
        store = Store(db)
        cols = {r[1] for r in
                store.conn.execute("PRAGMA table_info(lineage_edges)")}
        self.assertIn("run_id", cols)
        self.assertIn("kind", cols)
        store.close()


if __name__ == "__main__":
    unittest.main()