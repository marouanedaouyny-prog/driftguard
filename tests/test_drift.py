import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from driftguard.core.drift.diff import schema_diff
from driftguard.core.drift.similarity import find_rename, rename_score
from driftguard.drift import Drift, detect_drifts
from driftguard.lineage import build_lineage
from driftguard.parser import parse_sql_file
from driftguard.report import report_markdown
from driftguard.store import Store

from driftguard.__main__ import main

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "models"
GOLDEN = ROOT / "tests" / "golden" / "drift"


def _pipeline(spec: dict[str, str]):
    tmp = Path(tempfile.mkdtemp())
    stages = []
    for name, sql in spec.items():
        path = tmp / f"{name}.sql"
        path.write_text(sql, encoding="utf-8")
        stages.append(parse_sql_file(path))
    return build_lineage(stages)


class DriftTest(unittest.TestCase):

    def test_removed_column_is_breaking(self):
        lineage = _pipeline({
            "producer": "SELECT id FROM raw",
            "consumer": "SELECT id, name, email FROM {{ ref('producer') }}",
        })
        drifts = detect_drifts(lineage)
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0].removed, ["name", "email"])
        self.assertTrue(drifts[0].breaking)

    def test_added_column_is_non_breaking(self):
        lineage = _pipeline({
            "producer": "SELECT id, name, email FROM raw",
            "consumer": "SELECT id, name FROM {{ ref('producer') }}",
        })
        drifts = detect_drifts(lineage)
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0].added, ["email"])
        self.assertFalse(drifts[0].breaking)

    def test_rename_detected_by_similarity(self):
        lineage = _pipeline({
            "producer": "SELECT id, full_name FROM raw",
            "consumer": "SELECT id, fullname FROM {{ ref('producer') }}",
        })
        drifts = detect_drifts(lineage)
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0].renamed, [("fullname", "full_name")])
        self.assertTrue(drifts[0].breaking)

    def test_no_drift_when_schemas_agree(self):
        lineage = _pipeline({
            "producer": "SELECT id, name FROM raw",
            "consumer": "SELECT id, name FROM {{ ref('producer') }}",
        })
        self.assertEqual(detect_drifts(lineage), [])

    def test_aggregate_alias_treated_as_expected_column(self):
        # Honest MVP limitation: `COUNT(*) AS n` makes the consumer expect a
        # column `n` from its producer — the parser cannot know `n` is
        # computed, so a drift is reported (better a false positive than a
        # silently broken refactor).
        lineage = _pipeline({
            "producer": "SELECT id, name FROM raw",
            "consumer": "SELECT COUNT(*) AS n FROM {{ ref('producer') }}",
        })
        drifts = detect_drifts(lineage)
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0].removed, ["n"])
        self.assertTrue(drifts[0].breaking)


if __name__ == "__main__":
    unittest.main()


# ---- Phase 3: drift subcommand, threshold, diff preview, history -------------


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


class DriftGoldenTest(unittest.TestCase):
    """Golden drift cases: removed=breaking, renamed=breaking (similarity
    >= threshold), added=non-breaking, identical=clean (ARCH §Phase 3)."""

    CASES = {"removed": 1, "renamed": 1, "added": 0, "identical": 0}

    def test_golden_cases(self):
        for case, expected_exit in self.CASES.items():
            with self.subTest(case=case):
                case_dir = GOLDEN / case
                expected = json.loads(
                    (case_dir / "expected.json").read_text(encoding="utf-8"))
                old = os.getcwd()
                os.chdir(case_dir)
                try:
                    code, out = _run(["drift", "models", "--json",
                                      "--no-persist"])
                finally:
                    os.chdir(old)
                self.assertEqual(code, expected_exit)
                envelope = json.loads(out)
                self.assertEqual(envelope["schema"], "driftguard.drift.v1")
                self.assertIsNone(envelope["run_id"])
                self.assertIn("checked_at", envelope)
                self.assertEqual({k: envelope[k] for k in expected}, expected)

    def test_markdown_report_matches_golden(self):
        from driftguard.parser import parse_pipeline
        stages = parse_pipeline(EXAMPLES)
        lineage = build_lineage(stages)
        drifts = detect_drifts(lineage)
        self.assertEqual(
            report_markdown(stages, lineage, drifts),
            (GOLDEN / "report.md").read_text(encoding="utf-8"))


class ThresholdTest(unittest.TestCase):

    def test_rename_toggles_on_threshold(self):
        # rename_score("orderid", "order_identifier") ~= 0.61: renamed only
        # when the gate is below the similarity.
        lineage = _pipeline({
            "producer": "SELECT id, order_identifier FROM raw",
            "consumer": "SELECT id, orderid FROM {{ ref('producer') }}",
        })
        self.assertEqual(detect_drifts(lineage)[0].removed, ["orderid"])
        self.assertEqual(detect_drifts(lineage)[0].renamed, [])
        drifts = detect_drifts(lineage, threshold=0.5)
        self.assertEqual(drifts[0].renamed, [("orderid", "order_identifier")])
        self.assertEqual(drifts[0].removed, [])

    def test_threshold_out_of_range_exits_two(self):
        code, _ = _run(["drift", str(EXAMPLES), "--no-persist",
                        "--threshold", "1.5"])
        self.assertEqual(code, 2)
        code, _ = _run(["drift", str(EXAMPLES), "--no-persist",
                        "--threshold", "-0.1"])
        self.assertEqual(code, 2)

    def test_threshold_applied_by_cli(self):
        case_dir = GOLDEN / "renamed" / "models"
        code, out = _run(["drift", str(case_dir), "--json", "--no-persist",
                          "--threshold", "0.5"])
        self.assertEqual(code, 1)
        drifts = json.loads(out)["drifts"]
        self.assertEqual(drifts[0]["renamed"], [["user_email", "user_email_v2"]])
        self.assertTrue(drifts[0]["breaking"])

    def test_similarity_module(self):
        self.assertAlmostEqual(rename_score("full_name", "fullname"), 0.941, 2)
        self.assertIsNone(find_rename("email", ["id", "name"], {"id", "name"},
                                      0.75))
        self.assertEqual(find_rename("user_email", ["id", "user_email_v2"],
                                     {"id", "user_email"}, 0.75),
                         "user_email_v2")


class DriftDiffTest(unittest.TestCase):

    def test_schema_diff_shape(self):
        d = Drift("stg", "fct", added=["email"], removed=["name"],
                  renamed=[("user", "user_v2")])
        lines = schema_diff(d).splitlines()
        self.assertEqual(lines[0], "--- a/stg (schema)")
        self.assertEqual(lines[1], "+++ b/fct (expected)")
        self.assertEqual(lines[2], "@@ -1,2 +1,2 @@")
        self.assertIn("-name", lines)
        self.assertIn("-user", lines)
        self.assertIn("+user_v2", lines)
        self.assertIn("+email", lines)

    def test_diff_subcommand_preview(self):
        case_dir = GOLDEN / "renamed" / "models"
        code, out = _run(["drift", "diff", str(case_dir), "--no-persist"])
        self.assertEqual(code, 1)
        self.assertIn("--- a/producer (schema)", out)
        self.assertIn("+++ b/consumer (expected)", out)
        self.assertIn("-user_email", out)
        self.assertIn("+user_email_v2", out)

    def test_diff_clean_exits_zero(self):
        case_dir = GOLDEN / "identical" / "models"
        code, out = _run(["drift", "diff", str(case_dir), "--no-persist"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")


class DriftHistoryTest(unittest.TestCase):

    def _pipeline(self, spec):
        tmp = Path(tempfile.mkdtemp())
        stages = []
        for name, sql in spec.items():
            path = tmp / f"{name}.sql"
            path.write_text(sql, encoding="utf-8")
            stages.append(parse_sql_file(path))
        return stages, build_lineage(stages)

    def test_history_correct_across_runs(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "h.db")
            stages, lineage = self._pipeline({
                "producer": "SELECT id, name FROM raw",
                "consumer": "SELECT id, name, email FROM {{ ref('producer') }}",
            })
            drifts = detect_drifts(lineage)
            r1 = store.save_run("breaking", stages, lineage, drifts)
            stages, lineage = self._pipeline({
                "producer": "SELECT id, name FROM raw",
                "consumer": "SELECT id, name FROM {{ ref('producer') }}",
            })
            r2 = store.save_run("clean", stages, lineage, detect_drifts(lineage))

            history = store.drift_history()
            self.assertEqual([h["id"] for h in history], [r2, r1])
            self.assertEqual(history[0]["breaking"], 0)
            self.assertEqual(history[0]["findings"], [])
            self.assertEqual(history[1]["breaking"], 1)
            self.assertEqual(history[1]["findings"][0]["removed"], ["email"])
            self.assertTrue(history[1]["findings"][0]["breaking"])

            rebuilt = store.run_drifts(r1)
            self.assertEqual(len(rebuilt), 1)
            self.assertEqual(rebuilt[0].removed, ["email"])
            self.assertEqual(rebuilt[0].renamed, [])
            store.close()

    def test_drift_cli_persists_and_reports_run(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "r.db")
            case_dir = GOLDEN / "removed" / "models"
            code, _ = _run(["drift", str(case_dir), "--json", "--db", db])
            self.assertEqual(code, 1)
            code, out = _run(["drift", str(case_dir), "--db", db])
            self.assertIn("(run #", out)
            self.assertIn(f"persisted to {db})", out)
            try:
                store = Store(db)
                runs = store.recent_runs()
                self.assertEqual(len(runs), 2)
                self.assertTrue(runs[0]["breaking"])
                self.assertTrue(runs[1]["breaking"])
            finally:
                store.close()


class CiExampleTest(unittest.TestCase):

    def test_ci_example_gates_drift(self):
        yml = (ROOT / "examples" / "ci" / "drift.yml").read_text(
            encoding="utf-8")
        self.assertIn("python -m driftguard drift models --threshold 0.75 "
                      "--json", yml)
        self.assertIn('exit 1', yml)
        self.assertIn("breaking schema drift", yml)

    def test_synthetic_pr_gates(self):
        # A synthetic PR: the same tree before/after a breaking schema change.
        tmp = Path(tempfile.mkdtemp())
        models = tmp / "models"
        models.mkdir()
        (models / "producer.sql").write_text(
            "SELECT id, name FROM raw", encoding="utf-8")
        (models / "consumer.sql").write_text(
            "SELECT id, name FROM {{ ref('producer') }}", encoding="utf-8")
        code, _ = _run(["drift", str(models), "--no-persist", "--json"])
        self.assertEqual(code, 0)
        (models / "producer.sql").write_text(
            "SELECT id FROM raw", encoding="utf-8")  # PR drops `name`
        code, out = _run(["drift", str(models), "--no-persist", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["drifts"][0]["removed"], ["name"])


class LegacyCompatTest(unittest.TestCase):

    def test_legacy_json_payload_unchanged(self):
        code, out = _run(["examples/models", "--json", "--no-persist"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(list(payload), ["stages", "edges", "cycles",
                                         "drifts"])
        self.assertEqual(payload["stages"], 3)
        self.assertEqual(payload["edges"], 2)