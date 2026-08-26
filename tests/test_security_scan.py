"""Phase 4 security scanner tests: corpus regression (100% TP / 0 FP),
redaction, suppression, scan CLI envelope + exit codes, persistence."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from driftguard.core.security import (Finding, RULES, at_least, parse_suppressions,
                                      redact, scan_file, scan_root)
from driftguard.store import Store

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(__file__).parent / "security_corpus"


class RedactTest(unittest.TestCase):
    def test_known_prefix_scrubbed(self):
        self.assertNotIn("sk-", redact('key = "sk-1234567890abcdef1234567890abcdef"'))
        self.assertNotIn("ghp_", redact("token = ghp_abcdefghijklmnopqrstuvwxyz1234"))

    def test_key_assignment_scrubbed(self):
        out = redact('password = "hunter2secret"')
        self.assertIn("password", out)
        self.assertNotIn("hunter2secret", out)

    def test_redact_leaves_plain_text(self):
        text = "SELECT id FROM users WHERE email = 'x@y.z'"
        self.assertEqual(redact(text), text)


class SuppressionTest(unittest.TestCase):
    def test_parse_suppressions_line_and_file(self):
        text = ("-- driftguard:off SEC-002\n"
                "SELECT 1;\n"
                "# driftguard:off SEC-001,SEC-003\n"
                "x = 1\n"
                "-- driftguard:off-all\n")
        file_all, by_line = parse_suppressions(text)
        self.assertTrue(file_all)
        self.assertEqual(by_line, {1: {"SEC-002"}, 3: {"SEC-001", "SEC-003"}})

    def test_suppressed_finding_never_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "creds.py"
            p.write_text("api_key = 'sk-1234567890abcdef1234567890abcdef'  # driftguard:off SEC-001\n",
                         encoding="utf-8")
            findings, _ = scan_file(p, Path(tmp))
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].status, "suppressed")
            # the gate only considers open findings (severity itself is
            # unchanged — suppression never downgrades the finding)
            gated = [f for f in findings if f.status == "open"
                     and at_least(f.severity, "high")]
            self.assertEqual(gated, [])

    def test_file_scoped_suppression(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "creds.py"
            p.write_text("-- driftguard:off-all\n"
                         "api_key = 'sk-1234567890abcdef1234567890abcdef'\n",
                         encoding="utf-8")
            findings, _ = scan_file(p, Path(tmp))
            self.assertEqual(findings[0].status, "suppressed")

    def test_line_scoped_only_same_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "creds.py"
            p.write_text("# driftguard:off SEC-001\n"
                         "api_key = 'sk-1234567890abcdef1234567890abcdef'\n",
                         encoding="utf-8")
            findings, _ = scan_file(p, Path(tmp))
            self.assertEqual(findings[0].status, "open")


class SecurityCorpusTest(unittest.TestCase):
    def test_positive_corpus_hits_every_rule(self):
        findings, files, capped = scan_root(CORPUS / "positive")
        self.assertFalse(capped)
        self.assertGreaterEqual(files, 6)
        self.assertEqual({f.rule_id for f in findings},
                         {"SEC-001", "SEC-002", "SEC-003", "SEC-004", "SEC-005"})

    def test_positive_corpus_all_findings_open_and_redacted(self):
        findings, _, _ = scan_root(CORPUS / "positive")
        self.assertTrue(findings)
        for f in findings:
            self.assertEqual(f.status, "open")
            self.assertNotIn("sk-1234567890", f.snippet_redacted)
            self.assertNotIn("supersecret", f.snippet_redacted)

    def test_negative_corpus_zero_false_positives(self):
        findings, files, capped = scan_root(CORPUS / "negative")
        self.assertFalse(capped)
        self.assertEqual(files, 2)
        self.assertEqual(findings, [])

    def test_catalog_is_complete(self):
        self.assertEqual({r.id for r in RULES},
                         {"SEC-001", "SEC-002", "SEC-003", "SEC-004", "SEC-005"})


class ScanCliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "driftguard", *args],
                              capture_output=True, text=True,
                              env={"PYTHONPATH": str(ROOT),
                                   "DRIFTGUARD_DB": str(ROOT / "driftguard.db")})

    def test_scan_json_envelope_and_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "scan.db"
            r = self.run_cli("scan", str(CORPUS / "positive"), "--json", "--db", str(db))
            self.assertEqual(r.returncode, 1, r.stderr)
            env = json.loads(r.stdout)
            self.assertEqual(env["schema"], "driftguard.scan.v1")
            self.assertEqual(env["gate"], "failed")
            self.assertIsInstance(env["run_id"], int)
            self.assertEqual(env["counts"]["critical"], 1)
            self.assertGreater(env["counts"]["high"], 0)
            for f in env["findings"]:
                self.assertEqual(f["status"], "open")
                self.assertNotIn("sk-1234567890", f["snippet_redacted"])

    def test_clean_root_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_cli("scan", str(CORPUS / "negative"), "--json", "--no-persist")
            self.assertEqual(r.returncode, 0)
            env = json.loads(r.stdout)
            self.assertEqual(env["gate"], "passed")
            self.assertEqual(env["findings"], [])

    def test_fail_on_severity_none_disables_gate(self):
        r = self.run_cli("scan", str(CORPUS / "positive"), "--json",
                         "--fail-on-severity", "none", "--no-persist")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["gate"], "passed")

    def test_severity_filter_hides_low_severity(self):
        r = self.run_cli("scan", str(CORPUS / "positive"), "--json",
                         "--severity", "critical", "--no-persist")
        self.assertEqual(r.returncode, 1)
        env = json.loads(r.stdout)
        self.assertTrue(env["findings"])
        self.assertTrue(all(f["severity"] == "critical" for f in env["findings"]))

    def test_security_scan_alias(self):
        r = self.run_cli("security-scan", str(CORPUS / "positive"),
                         "--json", "--no-persist")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["schema"], "driftguard.scan.v1")

    def test_max_findings_cap_exits_5(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(6):
                (Path(tmp) / f"f{i}.py").write_text(
                    "api_key = 'sk-1234567890abcdef1234567890abcdef'\n",
                    encoding="utf-8")
            r = self.run_cli("scan", tmp, "--max-findings", "5", "--no-persist")
            self.assertEqual(r.returncode, 5)
            self.assertIn("resource_limit", r.stderr)

    def test_no_input_exits_2(self):
        r = self.run_cli("scan", str(CORPUS / "does-not-exist"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("no_input", r.stderr)

    def test_scan_persists_and_reconstructs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "scan.db"
            self.run_cli("scan", str(CORPUS / "positive"), "--json", "--db", str(db))
            store = Store(db)
            try:
                runs = store.recent_runs()
                self.assertEqual(len(runs), 1)
                findings = store.scan_findings(runs[0]["id"])
                self.assertGreater(len(findings), 0)
                self.assertNotIn("supersecret", findings[0].snippet_redacted)
            finally:
                store.close()

    def test_text_output_redacted(self):
        r = self.run_cli("scan", str(CORPUS / "positive"), "--no-persist")
        self.assertEqual(r.returncode, 1)
        self.assertIn("SEC-004", r.stdout)
        self.assertNotIn("supersecret", r.stdout)
        self.assertNotIn("sk-1234567890", r.stdout)


class ScanUnitTest(unittest.TestCase):
    def test_at_least_ordering(self):
        self.assertTrue(at_least("critical", "high"))
        self.assertTrue(at_least("medium", "medium"))
        self.assertFalse(at_least("low", "medium"))
        self.assertFalse(at_least("high", "none"))

    def test_sql_only_rule_skipped_for_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "auth.py"
            p.write_text("CREATE USER reader IDENTIFIED BY 'x';\n", encoding="utf-8")
            findings, _ = scan_file(p, Path(tmp))
            self.assertEqual(findings, [])

    def test_finding_to_dict_shape(self):
        f = Finding("SEC-001", "high", "a.py", 1, 1, (0, 10), "x", "hint")
        self.assertEqual(f.to_dict(), {"rule_id": "SEC-001", "severity": "high",
                                       "path": "a.py", "line": 1, "col": 1,
                                       "span": [0, 10], "snippet_redacted": "x",
                                       "hint": "hint", "status": "open"})


if __name__ == "__main__":
    unittest.main()