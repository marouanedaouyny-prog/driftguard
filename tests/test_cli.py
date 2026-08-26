import json
import tempfile
import unittest
from pathlib import Path

from driftguard.__main__ import main


def _pipeline(spec: dict[str, str]) -> Path:
    tmp = Path(tempfile.mkdtemp())
    for name, sql in spec.items():
        (tmp / f"{name}.sql").write_text(sql, encoding="utf-8")
    return tmp


class CliTest(unittest.TestCase):

    def test_clean_pipeline_exits_zero(self):
        root = _pipeline({
            "producer": "SELECT id, name FROM raw",
            "consumer": "SELECT id, name FROM {{ ref('producer') }}",
        })
        code = main([str(root), "--no-persist"])
        self.assertEqual(code, 0)

    def test_breaking_drift_exits_one(self):
        root = _pipeline({
            "producer": "SELECT id FROM raw",
            "consumer": "SELECT id, name FROM {{ ref('producer') }}",
        })
        code = main([str(root), "--no-persist"])
        self.assertEqual(code, 1)

    def test_json_output(self):
        root = _pipeline({
            "producer": "SELECT id FROM raw",
            "consumer": "SELECT id, name FROM {{ ref('producer') }}",
        })
        main([str(root), "--json", "--no-persist"])
        code = main([str(root), "--json", "--no-persist"])
        self.assertEqual(code, 1)

    def test_json_shape(self):
        import io
        import contextlib
        root = _pipeline({
            "producer": "SELECT id FROM raw",
            "consumer": "SELECT id, name FROM {{ ref('producer') }}",
        })
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main([str(root), "--json", "--no-persist"])
        data = json.loads(buf.getvalue())
        self.assertEqual(data["stages"], 2)
        self.assertEqual(data["drifts"][0]["removed"], ["name"])
        self.assertTrue(data["drifts"][0]["breaking"])

    def test_missing_dir_exits_two(self):
        self.assertEqual(main(["/definitely/not/a/dir", "--no-persist"]), 2)

    def test_no_sql_exits_two(self):
        root = Path(tempfile.mkdtemp())
        (root / "readme.txt").write_text("hello", encoding="utf-8")
        self.assertEqual(main([str(root), "--no-persist"]), 2)


if __name__ == "__main__":
    unittest.main()