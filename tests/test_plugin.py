"""Trusted-code rule plugin loader tests (ARCHITECTURE §2.1, Phase 5)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from driftguard.core.refactor.loader import load_rules
from driftguard.core.refactor.planner import analyze_pipeline

ROOT = Path(__file__).resolve().parent.parent


def _write(d, name, text):
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


class LoaderTest(unittest.TestCase):
    def test_no_rules_dir_is_builtins_only(self):
        rules = load_rules(None)
        ids = [r.id for r in rules]
        self.assertIn("REF-001", ids)
        self.assertNotIn("PLUG-001", ids)

    def test_loads_valid_plugin_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write(d, "z_last.py", (
                "from driftguard.core.refactor.model import RewriteCandidate\n"
                "class A:\n"
                "    id='PLUG-002'; version=1; tier='safe'\n"
                "    description='second'\n"
                "    def analyze(self, stage, ctx):\n"
                "        return []\n"
                "RULE2 = A()\n"))
            _write(d, "a_first.py", (
                "from driftguard.core.refactor.model import RewriteCandidate\n"
                "class B:\n"
                "    id='PLUG-001'; version=1; tier='suggested'\n"
                "    description='first'\n"
                "    def analyze(self, stage, ctx):\n"
                "        return []\n"
                "RULE1 = B()\n"))
            rules = load_rules(d)
            ids = [r.id for r in rules]
            self.assertEqual(ids, ["REF-001", "REF-002", "REF-003",
                                   "REF-004", "REF-005", "REF-006",
                                   "REF-007",
                                   "PLUG-001", "PLUG-002"])

    def test_builtin_id_collision_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write(d, "evil.py", (
                "class R:\n"
                "    id='REF-001'; version=1; tier='safe'\n"
                "    description='shadows builtin'\n"
                "    def analyze(self, stage, ctx):\n"
                "        return []\n"
                "RULE = R()\n"))
            warns = []
            rules = load_rules(d, warn=warns.append)
            ids = [r.id for r in rules]
            self.assertEqual(ids.count("REF-001"), 1)
            self.assertTrue(any("collides" in w for w in warns))

    def test_invalid_plugin_warned_and_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write(d, "bad.py", (
                "class R:\n"
                "    id='PLUG-BAD'\n"
                "    version='nope'\n"
                "    tier='safe'\n"
                "    description='bad version type'\n"
                "    def analyze(self, stage, ctx):\n"
                "        return []\n"
                "RULE = R()\n"))
            warns = []
            rules = load_rules(d, warn=warns.append)
            ids = [r.id for r in rules]
            self.assertNotIn("PLUG-BAD", ids)
            self.assertTrue(any("bad" in w for w in warns))

    def test_import_failure_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write(d, "broken.py", "import nonexistent_module_xyz\n")
            warns = []
            rules = load_rules(d, warn=warns.append)
            self.assertNotIn("PLUG", [r.id for r in rules])
            self.assertTrue(any("import failed" in w for w in warns))


class AnalyzePipelinePluginTest(unittest.TestCase):
    def test_plugin_rule_fires_via_rules_dir(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "m.sql").write_text("select id from orders",
                                     encoding="utf-8")
            plugin_dir = ROOT / "examples" / "plugins"
            analysis = analyze_pipeline(d, rules=[], max_risk="suggested",
                                        rules_dir=plugin_dir)
            self.assertIn("PLUG-001", analysis["rules"])
            ids = {c["rule_id"] for c in analysis["candidates"]}
            self.assertIn("PLUG-001", ids)

    def test_plugin_rule_gated_by_max_risk(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "m.sql").write_text("select id from orders",
                                     encoding="utf-8")
            plugin_dir = ROOT / "examples" / "plugins"
            analysis = analyze_pipeline(d, rules=[], max_risk="safe",
                                        rules_dir=plugin_dir)
            ids = {c["rule_id"] for c in analysis["candidates"]}
            self.assertNotIn("PLUG-001", ids)

    def test_rules_flag_filters_plugin(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "m.sql").write_text("select id from orders",
                                     encoding="utf-8")
            plugin_dir = ROOT / "examples" / "plugins"
            analysis = analyze_pipeline(d, rules=["REF-001"],
                                        max_risk="suggested",
                                        rules_dir=plugin_dir)
            self.assertNotIn("PLUG-001", analysis["rules"])

    def test_unknown_rule_still_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "m.sql").write_text("select 1", encoding="utf-8")
            plugin_dir = ROOT / "examples" / "plugins"
            with self.assertRaises(ValueError):
                analyze_pipeline(d, rules=["PLUG-999"],
                                 max_risk="suggested",
                                 rules_dir=plugin_dir)


class CliPluginTest(unittest.TestCase):
    def _run(self, args, cwd):
        e = dict(os.environ)
        e["PYTHONPATH"] = str(ROOT)
        proc = subprocess.run([sys.executable, "-m", "driftguard"] + args,
                              capture_output=True, text=True, cwd=str(cwd),
                              env=e, encoding="utf-8", errors="replace")
        return proc.returncode, proc.stdout + proc.stderr

    def test_rules_dir_plan_and_session_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "m.sql").write_text("select id from orders",
                                     encoding="utf-8")
            shutil.copytree(ROOT / "examples" / "plugins", d / "plugins")
            code, out = self._run(
                ["refactor", "plan", ".", "--max-risk", "suggested",
                 "--rules-dir", "plugins", "--db", "t.db", "--json"], d)
            self.assertEqual(code, 0, out)
            self.assertIn("PLUG-001", out)
            code, out = self._run(
                ["session", "show", "1", "--db", "t.db"], d)
            self.assertEqual(code, 0, out)
            self.assertIn("rules_dir: plugins", out)

    def test_rules_dir_plan_without_plugins_uses_builtins(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "m.sql").write_text("select id from orders",
                                     encoding="utf-8")
            code, out = self._run(
                ["refactor", "plan", ".", "--max-risk", "suggested",
                 "--db", "t.db", "--json"], d)
            self.assertEqual(code, 0, out)
            self.assertNotIn("PLUG-001", out)


if __name__ == "__main__":
    unittest.main()