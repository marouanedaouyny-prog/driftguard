import tempfile
import unittest
from pathlib import Path

from driftguard.parser import parse_pipeline, parse_sql_file


class ParserTest(unittest.TestCase):

    def _write(self, name: str, sql: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        p = tmp / name
        p.write_text(sql, encoding="utf-8")
        return p

    def test_projection_aliases_qualified_and_bare(self):
        stage = parse_sql_file(self._write("orders.sql", """
            SELECT order_id,
                   o.customer_id,
                   o.amount AS amount,
                   status
            FROM raw_orders o
        """))
        self.assertEqual(stage.name, "orders")
        self.assertEqual(stage.columns, ["order_id", "customer_id", "amount", "status"])
        self.assertIn("raw_orders", stage.refs)

    def test_star_expanded_and_dotted_star_skipped(self):
        stage = parse_sql_file(self._write("wide.sql", """
            SELECT *, o.extra, o.* FROM raw_orders o
        """))
        self.assertEqual(stage.columns, ["extra"])

    def test_ref_function_and_dedup(self):
        stage = parse_sql_file(self._write("marts.sql", """
            SELECT id FROM {{ ref('stg_orders') }}
            UNION ALL
            SELECT id FROM ref('stg_orders')
        """))
        self.assertEqual(stage.refs, ["stg_orders"])

    def test_comments_stripped(self):
        stage = parse_sql_file(self._write("commented.sql", """
            -- SELECT ghost_col FROM elsewhere
            /* SELECT also_ghost FROM other */
            SELECT id, name FROM users
        """))
        self.assertEqual(stage.columns, ["id", "name"])
        self.assertEqual(stage.refs, ["users"])

    def test_create_table_overrides_stem_name(self):
        stage = parse_sql_file(self._write("model.sql", """
            CREATE TABLE mart.orders AS
            SELECT id FROM stg_orders
        """))
        self.assertEqual(stage.name, "orders")
        self.assertEqual(stage.columns, ["id"])

    def test_parse_pipeline_recurses(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "staging").mkdir()
        (tmp / "staging" / "a.sql").write_text("SELECT x FROM b", encoding="utf-8")
        (tmp / "b.sql").write_text("SELECT y FROM raw", encoding="utf-8")
        stages = parse_pipeline(tmp)
        self.assertEqual(sorted(s.name for s in stages), ["a", "b"])

    def test_non_sql_ignored(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "notes.txt").write_text("SELECT not sql", encoding="utf-8")
        self.assertEqual(parse_pipeline(tmp), [])


if __name__ == "__main__":
    unittest.main()