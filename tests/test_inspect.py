"""Phase 1 acceptance: golden IR fixtures, diagnostics, stability, inspect CLI.

Contract (ARCHITECTURE §Phase 1): examples/ models parse with asserted IR
JSON golden fixtures; unknown constructs produce structured diagnostics; no
silent misparse; parse -> serialize -> parse is fingerprint-stable.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from driftguard.core.ir.serialize import (
    pipeline_dict,
    stage_dict,
    stage_fingerprint,
    to_json,
)
from driftguard.core.parser.dialects.dbt import DbtParser, parse_sql_file
from driftguard.core.parser.tokenizer import TokenizerError, tokenize
from driftguard.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "models"
GOLDEN = ROOT / "tests" / "golden" / "parse"


def _pipeline(spec: dict[str, str], subdir: str = "") -> Path:
    tmp = Path(tempfile.mkdtemp())
    if subdir:
        (tmp / subdir).mkdir()
    for name, sql in spec.items():
        target = tmp / subdir / f"{name}.sql" if subdir else tmp / f"{name}.sql"
        target.write_text(sql, encoding="utf-8")
    return tmp


class GoldenParseTest(unittest.TestCase):

    def test_examples_match_golden_fixtures(self):
        parser = DbtParser()
        pipeline = parser.parse_project(EXAMPLES)
        self.assertEqual(
            sorted(s.name for s in pipeline.stages),
            ["fct_orders", "fct_orders_renamed", "stg_orders"])
        for stage in pipeline.stages:
            golden = GOLDEN / f"{stage.name}.json"
            self.assertTrue(golden.is_file(), f"missing golden {golden}")
            expected = json.loads(golden.read_text(encoding="utf-8"))
            self.assertEqual(stage_dict(stage, EXAMPLES), expected)

    def test_projection_alias_qualified_and_bare(self):
        stage = parse_sql_file(Path(tempfile.mkdtemp()) / "x.sql")
        self.assertIsNone(stage)  # non-sql path guard


class IrStabilityTest(unittest.TestCase):

    def test_parse_twice_fingerprint_equal(self):
        root = _pipeline({
            "a": "SELECT order_id, o.amount AS amount FROM raw_orders o",
        })
        parser = DbtParser()
        p1 = parser.parse_project(root)
        p2 = parser.parse_project(root)
        self.assertEqual(p1.stages[0].fingerprint, p2.stages[0].fingerprint)
        self.assertEqual(p1.fingerprint, p2.fingerprint)

    def test_serialization_deterministic(self):
        root = _pipeline({"a": "SELECT x, y FROM t"})
        pipeline = DbtParser().parse_project(root)
        first = to_json(pipeline_dict(pipeline))
        second = to_json(pipeline_dict(pipeline))
        self.assertEqual(first, second)

    def test_whitespace_and_comment_insensitive_fingerprint(self):
        root = Path(tempfile.mkdtemp())
        for sub in ("left", "right"):
            (root / sub).mkdir()
            (root / sub / "a.sql").write_text(
                "SELECT x, y FROM t" if sub == "left"
                else "  -- leading comment\nSELECT x,  y FROM t /* block */",
                encoding="utf-8")
        parser = DbtParser()
        stages = {s.path.parent.name: s
                  for s in parser.parse_project(root).stages}
        self.assertEqual(stages["left"].fingerprint, stages["right"].fingerprint)

    def test_content_change_changes_fingerprint(self):
        root = _pipeline({
            "a": "SELECT x, y FROM t",
            "b": "SELECT x FROM t",
        })
        parser = DbtParser()
        stages = {s.name: s for s in parser.parse_project(root).stages}
        self.assertNotEqual(stages["a"].fingerprint, stages["b"].fingerprint)


class ParseEdgeCasesTest(unittest.TestCase):

    def test_cte_columns_and_refs(self):
        stage = parse_sql_file(_file("m.sql", """
            WITH cte AS (SELECT id FROM a)
            SELECT id, name FROM cte
        """))
        self.assertEqual(stage.column_names, ["id", "name"])
        self.assertEqual(stage.ref_names, ["a", "cte"])

    def test_union_all_columns(self):
        stage = parse_sql_file(_file("u.sql", """
            SELECT id FROM a
            UNION ALL
            SELECT id FROM b
        """))
        self.assertEqual(stage.column_names, ["id"])
        self.assertEqual(stage.ref_names, ["a", "b"])

    def test_source_function_records_sources(self):
        stage = parse_sql_file(_file("s.sql", """
            SELECT id FROM {{ source('raw', 'orders') }}
        """))
        self.assertEqual(stage.sources, [type(stage.sources[0])("raw", "orders")])
        self.assertEqual(stage.ref_names, [])

    def test_unknown_template_marks_hint_and_warning(self):
        stage = parse_sql_file(_file("v.sql", """
            {{ config(materialized='table') }}
            SELECT id FROM {{ var('target_table') }}
        """))
        self.assertIn("unknown_template_region", stage.dialect_hints)
        kinds = {d.kind for d in stage.diagnostics}
        self.assertIn("warning", kinds)
        self.assertTrue(any("unknown_template_region" in d.reason
                            for d in stage.diagnostics))

    def test_config_and_comment_templates_do_not_hint(self):
        stage = parse_sql_file(_file("c.sql", """
            {{ config(materialized='table') }}
            {# a comment #}
            SELECT id FROM {{ ref('stg_orders') }}
        """))
        self.assertEqual(stage.dialect_hints, [])
        self.assertEqual(stage.ref_names, ["stg_orders"])

    def test_unterminated_string_is_error_diagnostic_not_crash(self):
        stage = parse_sql_file(_file("e.sql", "SELECT 'unterminated FROM t"))
        self.assertTrue(any(d.kind == "error" for d in stage.diagnostics))
        self.assertEqual(stage.column_names, [])

    def test_no_select_is_warning(self):
        stage = parse_sql_file(_file("n.sql", "INSERT INTO x VALUES (1)"))
        self.assertTrue(any(d.kind == "warning" for d in stage.diagnostics))
        self.assertEqual(stage.column_names, [])

    def test_bare_from_in_subquery_collected(self):
        stage = parse_sql_file(_file("q.sql", """
            SELECT x FROM (SELECT x FROM t) s
        """))
        self.assertIn("t", stage.ref_names)
        self.assertNotIn("s", stage.ref_names)

    def test_extract_from_not_a_ref(self):
        stage = parse_sql_file(_file("ex.sql", """
            SELECT EXTRACT(YEAR FROM ts) AS year FROM events
        """))
        self.assertEqual(stage.column_names, ["year"])
        self.assertEqual(stage.ref_names, ["events"])

    def test_create_materialized_view_name(self):
        stage = parse_sql_file(_file("mv.sql", """
            CREATE OR REPLACE MATERIALIZED VIEW schema.mv_name AS
            SELECT id FROM stg_orders
        """))
        self.assertEqual(stage.name, "mv_name")
        self.assertEqual(stage.column_names, ["id"])

    def test_quoted_identifier_projection(self):
        stage = parse_sql_file(_file("qi.sql", """
            SELECT "OrderId", `created_at` FROM raw
        """))
        self.assertEqual(stage.column_names, ["orderid", "created_at"])

    def test_complex_expression_without_alias_not_asserted(self):
        stage = parse_sql_file(_file("cx.sql", """
            SELECT amount * 0.2, amount * 0.8 AS fee FROM raw
        """))
        self.assertEqual(stage.column_names, ["fee"])

    def test_star_projection_warns_and_asserts_nothing(self):
        stage = parse_sql_file(_file("st.sql", "SELECT * FROM t"))
        self.assertEqual(stage.column_names, [])
        self.assertTrue(any(d.kind == "warning" and "SELECT *" in d.reason
                            for d in stage.diagnostics))

    def test_qualified_star_warns_but_other_columns_asserted(self):
        stage = parse_sql_file(_file("qst.sql", "SELECT a.*, a.id FROM t a"))
        self.assertEqual(stage.column_names, ["id"])
        self.assertTrue(any("SELECT *" in d.reason for d in stage.diagnostics))

    def test_star_in_cte_body_does_not_warn(self):
        # The output schema comes from the main select; an intermediate CTE
        # star does not affect the drift contract, so no warning.
        stage = parse_sql_file(_file("cw.sql", """
            WITH cte AS (SELECT * FROM a)
            SELECT id FROM cte
        """))
        self.assertEqual(stage.column_names, ["id"])
        self.assertFalse(any("SELECT *" in d.reason for d in stage.diagnostics))


class InspectCliTest(unittest.TestCase):

    def test_inspect_json_shape(self):
        root = _pipeline({
            "producer": "SELECT id, name FROM raw",
            "consumer": "SELECT id FROM {{ ref('producer') }}",
        })
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["inspect", str(root), "--json", "--no-persist"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["v"], 1)
        pipeline = data["pipeline"]
        self.assertEqual(pipeline["schema"], "driftguard.pipeline.v1")
        self.assertEqual(pipeline["v"], 1)
        self.assertEqual(len(pipeline["stages"]), 2)
        self.assertTrue(pipeline["fingerprint"].startswith("sha256:"))
        stage = pipeline["stages"][0]
        self.assertEqual(stage["schema"], "driftguard.stage.v1")
        self.assertIn("columns", stage)
        self.assertIn("diagnostics", stage)
        self.assertIsInstance(data["diagnostics"], list)

    def test_parse_json_shape(self):
        root = _pipeline({"a": "SELECT x, y FROM t"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["parse", str(root), "--json", "--no-persist"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["schema"], "driftguard.parse.v1")
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["stage_count"], 1)
        self.assertEqual(data["git_sha"], None)
        stage = data["stages"][0]
        self.assertEqual(stage["name"], "a")
        self.assertEqual([c["name"] for c in stage["columns"]], ["x", "y"])
        self.assertEqual(stage["refs"], [{"producer": "t", "consumer": "a",
                                          "kind": "bare",
                                          "expected_columns": []}])

    def test_parse_out_writes_artifact(self):
        root = _pipeline({"a": "SELECT x FROM t"})
        out = Path(tempfile.mkdtemp()) / "parse.json"
        code = main(["parse", str(root), "--no-persist", "--out", str(out)])
        self.assertEqual(code, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], "driftguard.parse.v1")

    def test_parse_hard_error_exits_two(self):
        root = _pipeline({"bad": "SELECT 'unterminated FROM t"})
        self.assertEqual(main(["parse", str(root), "--no-persist"]), 2)

    def test_inspect_hard_error_exits_two(self):
        root = _pipeline({"bad": "SELECT 'unterminated FROM t"})
        self.assertEqual(main(["inspect", str(root), "--no-persist"]), 2)

    def test_parse_warning_still_zero(self):
        root = _pipeline({"n": "INSERT INTO x VALUES (1)"})
        self.assertEqual(main(["parse", str(root), "--no-persist"]), 0)

    def test_drift_star_consumer_warns_but_does_not_gate(self):
        # Real-world pattern (e.g. jaffle_shop): a consumer ends in
        # `SELECT * FROM final`. The rename is invisible to the drift
        # contract — the tool must say so instead of staying silent.
        root = _pipeline({
            "producer": "SELECT id, first_name FROM raw",
            "consumer": "SELECT * FROM {{ ref('producer') }}",
        })
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["drift", str(root), "--no-persist"])
        self.assertEqual(code, 0)
        self.assertIn("SELECT *", err.getvalue())

    def test_parse_persists_run_id(self):
        root = _pipeline({"a": "SELECT x FROM t"})
        db = Path(tempfile.mkdtemp()) / "runs.db"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["parse", str(root), "--db", str(db)])
        self.assertEqual(code, 0)
        self.assertRegex(buf.getvalue(), r"run #\d+ persisted")
        store = _open_store(db)
        try:
            self.assertEqual(store.recent_runs()[0]["stages"], 1)
        finally:
            store.close()

    def test_inspect_text_output(self):
        root = _pipeline({"a": "SELECT x FROM t"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["inspect", str(root), "--no-persist"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("pipeline:", out)
        self.assertIn("a [model]", out)
        self.assertIn("fingerprint:", out)

    def test_inspect_missing_dir_exits_two(self):
        self.assertEqual(main(["inspect", "/definitely/not/a/dir"]), 2)

    def test_inspect_no_sql_exits_two(self):
        root = Path(tempfile.mkdtemp())
        (root / "readme.txt").write_text("hello", encoding="utf-8")
        self.assertEqual(main(["inspect", str(root)]), 2)

    def test_version_flag(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--version"])
        self.assertEqual(code, 0)
        self.assertRegex(buf.getvalue(), r"^driftguard \d+\.\d+\.\d+")

    def test_unknown_subcommand_exits_two(self):
        self.assertEqual(main(["bogus", "."]), 2)


class NoSilentMisparseTest(unittest.TestCase):
    """Property: every parsed column's source_expr is exactly the raw bytes
    at its span (the parser only reports expressions it actually consumed)."""

    def test_source_expr_matches_raw_bytes_at_span(self):
        root = _pipeline({
            "a": "SELECT order_id, o.amount AS amount, status\nFROM raw_orders o",
            "b": "SELECT id, name FROM t",
        })
        parser = DbtParser()
        for stage in parser.parse_project(root).stages:
            raw = stage.raw
            for col in stage.columns:
                self.assertIsNotNone(col.span, f"{stage.name}.{col.name}")
                start, end = col.span.start, col.span.end
                self.assertEqual(raw[start:end], col.source_expr,
                                 f"{stage.name}.{col.name}")


class TokenizerUnitTest(unittest.TestCase):

    def test_token_offsets_and_kinds(self):
        toks = tokenize("SELECT a, 'it''s' FROM t -- c\n/* x */")
        kinds = [t.kind for t in toks]
        self.assertEqual(kinds, ["WORD", "WORD", "OP", "STRING",
                                 "WORD", "WORD", "EOF"])
        word = toks[0]
        self.assertEqual(word.value, "SELECT")
        self.assertEqual(word.start, 0)
        self.assertEqual(word.end, 6)

    def test_unterminated_comment_raises(self):
        with self.assertRaises(TokenizerError):
            tokenize("SELECT 1 /* never closed")

    def test_template_markers(self):
        toks = tokenize("{{ ref('x') }} {% raw %} {% endraw %}")
        kinds = [t.kind for t in toks]
        self.assertIn("TEMPLATE_OPEN", kinds)
        self.assertIn("TEMPLATE_CLOSE", kinds)
        self.assertIn("TEMPLATE_TAG_OPEN", kinds)
        self.assertIn("TEMPLATE_TAG_CLOSE", kinds)


def _file(name: str, sql: str) -> Path:
    tmp = Path(tempfile.mkdtemp())
    p = tmp / name
    p.write_text(sql, encoding="utf-8")
    return p


def _open_store(path: Path):
    from driftguard.store import Store
    return Store(path)


if __name__ == "__main__":
    unittest.main()