"""LLM suggestion channel tests (API_SPEC §7, ADR-008).

Covers: client contract, prompt hygiene (redaction), suggestion
validation, offline degradation, the single exit-2 exception, the
deterministic-path rule, and the security block overlay on LLM items.
"""
import io
import json
import pathlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from driftguard.core.ir.model import Column, Pipeline, RefEdge, Stage
from driftguard.core.refactor.planner import analyze_pipeline, merge_suggestions
from driftguard.core.security.findings import Finding
from driftguard.llm import (LlmClient, LlmUnavailable, build_prompt,
                            request_suggestions, validate_suggestions)

ROOT = Path(__file__).resolve().parent.parent


def _pipeline() -> Pipeline:
    raw = "select id, name from orders"
    stage = Stage(
        name="m", path=Path("m.sql"), raw=raw, refs=[RefEdge("orders",
                                                              "ref")],
        columns=[Column("id", "id", None, (8, 10)),
                 Column("name", "name", None, (11, 15))])
    return Pipeline(root=Path("."), stages=[stage])


def _sug(span, before, after, confidence=0.9, stage="m", path="m.sql",
          rationale="test"):
    return {"stage": stage, "path": path, "span": span, "before": before,
            "after": after, "confidence": confidence, "rationale": rationale}


_VALID_SPAN = [0, 15]  # "select id, name" in the fixture raw


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class ClientTest(unittest.TestCase):
    def test_generate_parses_response_field(self):
        with mock.patch("driftguard.llm.ollama.urllib.request.urlopen",
                        return_value=FakeResponse(json.dumps(
                            {"model": "m", "response": "{\"ok\": 1}",
                             "done": True}).encode())) as uo:
            client = LlmClient(base_url="http://x:1", model="m")
            self.assertEqual(client.generate("hi"), "{\"ok\": 1}")
            body = json.loads(uo.call_args.args[0].data.decode())
            self.assertEqual(body["model"], "m")
            self.assertFalse(body["stream"])
            self.assertEqual(body["format"], "json")
            self.assertEqual(body["options"]["temperature"], 0.2)
            self.assertIn("/api/generate", uo.call_args.args[0].full_url)

    def test_unreachable_raises(self):
        import urllib.error
        with mock.patch("driftguard.llm.ollama.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            client = LlmClient(base_url="http://localhost:1")
            with self.assertRaises(LlmUnavailable):
                client.generate("hi")

    def test_empty_response_raises(self):
        with mock.patch("driftguard.llm.ollama.urllib.request.urlopen",
                        return_value=FakeResponse(json.dumps(
                            {"response": "  "}).encode())):
            with self.assertRaises(LlmUnavailable):
                LlmClient().generate("hi")


class PromptTest(unittest.TestCase):
    def test_redaction_of_secrets(self):
        secret = "sk-" + "a" * 30
        cand = {"rule_id": "REF-001", "stage": "m", "path": "m.sql",
                "span": [0, 10], "before": f"select '{secret}'",
                "after": "select 1", "reason": "x"}
        prompt = build_prompt(_pipeline(), [cand], [])
        self.assertNotIn(secret, prompt)
        self.assertIn("<redacted>", prompt)

    def test_context_and_instructions(self):
        prompt = build_prompt(_pipeline(), [], [])
        self.assertIn(_pipeline().fingerprint, prompt)
        self.assertIn("Output JSON only", prompt)
        self.assertIn("never invent file paths", prompt)


class ValidateTest(unittest.TestCase):
    def test_accepts_valid(self):
        pipe = _pipeline()
        raw = json.dumps({"suggestions": [_sug(
            _VALID_SPAN, "select id, name", "select id")]})
        out = validate_suggestions(raw, Path("."), pipe, [], 0.7, 50)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["rule_id"], "LLM-1")
        self.assertEqual(out[0]["tier"], "suggested")
        self.assertEqual(out[0]["after"], "select id")
        self.assertEqual(out[0]["span"], _VALID_SPAN)

    def test_sequential_ids(self):
        pipe = _pipeline()
        raw = json.dumps({"suggestions": [
            _sug(_VALID_SPAN, "select id, name", "select id"),
            _sug([0, 6], "select", "SELECT", 0.8),
        ]})
        out = validate_suggestions(raw, Path("."), pipe, [], 0.7, 50)
        self.assertEqual([s["rule_id"] for s in out], ["LLM-1", "LLM-2"])

    def test_low_confidence_dropped(self):
        pipe = _pipeline()
        raw = json.dumps({"suggestions": [
            _sug(_VALID_SPAN, "select id, name", "select id",
                 confidence=0.5)]})
        warns = []
        out = validate_suggestions(raw, Path("."), pipe, [], 0.7, 50,
                                   warn=warns.append)
        self.assertEqual(out, [])
        self.assertTrue(any("confidence" in w for w in warns))

    def test_before_mismatch_dropped(self):
        pipe = _pipeline()
        raw = json.dumps({"suggestions": [
            _sug([0, 7], "SELECT id, name", "SELECT id")]})
        warns = []
        out = validate_suggestions(raw, Path("."), pipe, [], 0.7, 50,
                                   warn=warns.append)
        self.assertEqual(out, [])
        self.assertTrue(any("does not match" in w for w in warns))

    def test_out_of_bounds_dropped(self):
        pipe = _pipeline()
        raw = json.dumps({"suggestions": [
            _sug([0, 9999], "select id, name", "select id")]})
        warns = []
        out = validate_suggestions(raw, Path("."), pipe, [], 0.7, 50,
                                   warn=warns.append)
        self.assertEqual(out, [])
        self.assertTrue(any("out of bounds" in w for w in warns))

    def test_duplicate_of_rule_candidate_dropped(self):
        pipe = _pipeline()
        existing = [{"before": "select id, name", "after": "select id"}]
        raw = json.dumps({"suggestions": [
            _sug(_VALID_SPAN, "select id, name", "select id")]})
        warns = []
        out = validate_suggestions(raw, Path("."), pipe, existing, 0.7, 50,
                                   warn=warns.append)
        self.assertEqual(out, [])
        self.assertTrue(any("duplicates" in w for w in warns))

    def test_noop_dropped(self):
        pipe = _pipeline()
        raw = json.dumps({"suggestions": [
            _sug(_VALID_SPAN, "select id, name", "select id, name")]})
        out = validate_suggestions(raw, Path("."), pipe, [], 0.7, 50)
        self.assertEqual(out, [])

    def test_cap_excess_dropped(self):
        pipe = _pipeline()
        warns = []
        raw = json.dumps({"suggestions": [
            _sug(_VALID_SPAN, "select id, name", "select id"),
            _sug([0, 6], "select", "SELECT", 0.8),
        ]})
        out = validate_suggestions(raw, Path("."), pipe, [], 0.7, 1,
                                   warn=warns.append)
        self.assertEqual(len(out), 1)
        self.assertTrue(any("max-llm-suggestions" in w for w in warns))

    def test_malformed_json_degrades(self):
        warns = []
        out = validate_suggestions("not json", Path("."), _pipeline(), [],
                                   0.7, 50, warn=warns.append)
        self.assertEqual(out, [])
        self.assertTrue(any("not JSON" in w for w in warns))


class RequestTest(unittest.TestCase):
    def _fake(self, raw):
        return FakeResponse(json.dumps(
            {"model": "m", "response": raw, "done": True}).encode())

    def test_network_failure_on_first_call_raises(self):
        import urllib.error
        pipe = _pipeline()
        with mock.patch("driftguard.llm.ollama.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("down")):
            client = LlmClient(base_url="http://localhost:1")
            with self.assertRaises(LlmUnavailable):
                request_suggestions(client, Path("."), pipe, [], [])

    def test_malformed_retries_then_degrades(self):
        pipe = _pipeline()
        calls = {"n": 0}

        def urlopen(req, timeout=None):
            calls["n"] += 1
            return self._fake("garbage")

        warns = []
        with mock.patch("driftguard.llm.ollama.urllib.request.urlopen",
                        side_effect=urlopen), \
                mock.patch("driftguard.llm.ollama.time.sleep"):
            client = LlmClient(base_url="http://localhost:1")
            out, raw = request_suggestions(client, Path("."), pipe, [], [],
                                           warn=warns.append)
        self.assertEqual(out, [])
        self.assertEqual(calls["n"], 2)
        self.assertTrue(any("malformed" in w or "not JSON" in w
                            for w in warns))

    def test_valid_suggestions_flow(self):
        pipe = _pipeline()
        payload = json.dumps({"suggestions": [
            _sug(_VALID_SPAN, "select id, name", "select id")]})
        with mock.patch("driftguard.llm.ollama.urllib.request.urlopen",
                        return_value=self._fake(payload)):
            client = LlmClient(base_url="http://localhost:1")
            out, raw = request_suggestions(client, Path("."), pipe, [], [],
                                           warn=print)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["rule_id"], "LLM-1")
        self.assertEqual(out[0]["confidence"], 0.9)


class MergeTest(unittest.TestCase):
    def _analysis(self):
        return {"pipeline": _pipeline(), "findings": [],
                "candidates": [{"rule_id": "REF-001", "stage": "m",
                                "path": "m.sql", "span": [0, 4],
                                "before": "WITH", "after": ""}],
                "blocked": []}

    def _llm_sug(self):
        return {"rule_id": "LLM-1", "tier": "suggested", "stage": "m",
                "path": "m.sql", "span": _VALID_SPAN,
                "before": "select id, name", "after": "select id",
                "confidence": 0.9, "rationale": "drop unused name",
                "reason": "LLM suggestion (conf 0.90)"}

    def test_safe_max_risk_never_includes_llm(self):
        analysis = self._analysis()
        merged, added, blocked = merge_suggestions(
            analysis, [self._llm_sug()], False, "safe")
        self.assertEqual(added, [])
        self.assertEqual(blocked, [])
        self.assertEqual(len(merged["candidates"]), 1)
        self.assertEqual(merged["candidates"][0]["rule_id"], "REF-001")

    def test_suggested_max_risk_merges_and_renumbers(self):
        analysis = self._analysis()
        sug = self._llm_sug()
        merged, added, blocked = merge_suggestions(analysis, [sug],
                                                   False, "suggested")
        self.assertEqual(len(added), 1)
        self.assertEqual([c["rule_id"] for c in merged["candidates"]],
                         ["REF-001", "LLM-1"])
        self.assertEqual([c["change_id"] for c in merged["candidates"]],
                         ["c0", "c1"])

    def test_security_block_overlay(self):
        analysis = self._analysis()
        analysis["findings"] = [Finding(
            rule_id="SEC-001", severity="critical", path="m.sql",
            line=1, col=0, span=(0, 8), snippet_redacted="<redacted>",
            hint="secret", status="open")]
        sug = self._llm_sug()
        merged, added, blocked = merge_suggestions(analysis, [sug],
                                                   False, "suggested")
        self.assertEqual(added, [])
        self.assertEqual(len(blocked), 1)
        self.assertIn("SEC-001", blocked[0]["block_reason"])
        self.assertEqual(len(merged["candidates"]), 1)

        merged2, added2, _ = merge_suggestions(
            analysis, [sug], True, "suggested")
        self.assertEqual(len(added2), 1)
        self.assertIn("SEC-001", added2[0]["security_note"])


class CliLlmTest(unittest.TestCase):
    def _run(self, args, cwd, env):
        import subprocess
        import sys
        e = dict(__import__("os").environ)
        e["PYTHONPATH"] = str(ROOT)
        e.update(env or {})
        proc = subprocess.run([sys.executable, "-m", "driftguard"] + args,
                              capture_output=True, text=True, cwd=str(cwd),
                              env=e, encoding="utf-8", errors="replace")
        return proc.returncode, proc.stdout + proc.stderr

    def test_llm_unreachable_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "m.sql").write_text("WITH a AS (select 1) select 1",
                                     encoding="utf-8")
            env = {"DRIFTGUARD_DB": str(d / "t.db")}
            code, out = self._run(["refactor", "analyze", str(d),
                                   "--llm-suggestions", "--llm-base-url",
                                   "http://127.0.0.1:1"], d, env)
            self.assertEqual(code, 2, out)
            self.assertIn("llm_unavailable", out)

    def test_no_flag_means_zero_llm(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "m.sql").write_text("WITH a AS (select 1) select 1",
                                     encoding="utf-8")
            env = {"DRIFTGUARD_DB": str(d / "t.db")}
            code, out = self._run(["refactor", "analyze", str(d), "--json",
                                   "--llm-base-url",
                                   "http://127.0.0.1:1"], d, env)
            self.assertEqual(code, 0, out)
            envelope = json.loads(out[out.index("{\n"):])
            self.assertEqual(envelope["llm"]["used"], False)

    def test_safe_max_risk_deprecated_alias_no_call(self):
        # --llm (deprecated alias) on a safe run: the channel is requested
        # but suggestions can never pass the tier gate; an unreachable
        # Ollama still exits 2 per R-7.
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "m.sql").write_text("WITH a AS (select 1) select 1",
                                     encoding="utf-8")
            env = {"DRIFTGUARD_DB": str(d / "t.db")}
            code, out = self._run(["refactor", "analyze", str(d), "--llm",
                                   "--llm-base-url",
                                   "http://127.0.0.1:1"], d, env)
            self.assertEqual(code, 2, out)
            self.assertIn("llm_unavailable", out)


if __name__ == "__main__":
    unittest.main()