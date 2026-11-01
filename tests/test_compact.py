import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, sys, unittest
from pathlib import Path
from unittest import mock
# tests/test_modules.py fails a test file without the next line
os.environ["HERDR_ENV"] = ""
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_config


class Policy(unittest.TestCase):
    def test_questions_match_compact_adviser_byte_for_byte(self):
        self.assertEqual(fc.fc_compact.QUESTIONS["done"]["criteria"]["finished"],
                         "Finished and reported, including a question, choice, or blocker fully stated and handed to whoever must act next.")
        self.assertEqual(sorted(fc.fc_compact.QUESTIONS["shape"]["criteria"]), ["coordinating", "hands_on", "unclear"])
        self.assertTrue(fc.fc_compact.QUESTIONS["done"]["instructions"].startswith("Decide whether the assistant's latest unit of work"))

    def test_score_and_floor_follow_the_curve(self):
        self.assertAlmostEqual(fc.fc_compact.score(0.8, 0.5, 0.5), 0.6)
        self.assertEqual(fc.fc_compact.score(0.8, 0.0, 0.0), 0.8)
        self.assertEqual(fc.fc_compact.floor_for(0.05), 0.9)
        self.assertEqual(fc.fc_compact.floor_for(0.5), 0.7)
        self.assertEqual(fc.fc_compact.floor_for(0.95), 0.5)
        self.assertEqual(fc.fc_compact.floor_for(None), 0.9)
        self.assertEqual(fc.fc_compact.floor_for(float("nan")), 0.9)
        self.assertTrue(fc.fc_compact.qualifies(0.7, 0.5, fc.fc_compact.PROFILES["lead"]["floors"]))
        self.assertFalse(fc.fc_compact.qualifies(0.69, 0.5, fc.fc_compact.PROFILES["lead"]["floors"]))

    def test_budget_applies_only_below_the_limit(self):
        self.assertEqual(fc.fc_compact.effective_budget(200000, 150000), 150000)
        self.assertEqual(fc.fc_compact.effective_budget(100000, 150000), 100000)
        self.assertEqual(fc.fc_compact.effective_budget(None, 150000), 150000)
        self.assertEqual(fc.fc_compact.context_pressure(75000, 200000, 150000), 0.5)
        self.assertIsNone(fc.fc_compact.context_pressure(75000, None, None))

    def test_parse_judgment_rejects_a_malformed_reply(self):
        good = {"model": "jev-latest", "usage": {"input_tokens": 10, "output_tokens": 2}, "answers": {
            "done": {"type": "choice", "choice": "finished", "probabilities": {"finished": 0.8, "not_finished": 0.1, "unclear": 0.1}, "confidence": 0.8},
            "shape": {"type": "choice", "choice": "coordinating", "probabilities": {"hands_on": 0.2, "coordinating": 0.7, "unclear": 0.1}, "confidence": 0.7}}}
        j = fc.fc_compact.parse_judgment(good)
        self.assertEqual((j["done"]["choice"], j["input_tokens"]), ("finished", 10))
        for bad in ({}, {**good, "model": 123}, {**good, "answers": {"done": good["answers"]["done"]}},
                    {**good, "answers": {**good["answers"], "done": {**good["answers"]["done"], "probabilities": {"finished": 0.5}}}}):
            with self.assertRaises(fc.fc_compact.JudgeError) as c: fc.fc_compact.parse_judgment(bad)
            self.assertEqual(c.exception.kind, "response")

    def test_error_kind_classifies_failures(self):
        kind = fc.fc_compact.error_kind
        self.assertEqual(kind(RuntimeError("HTTP 401 unauthorized")), "authentication")
        self.assertEqual(kind(RuntimeError("HTTP 403 forbidden")), "authentication")
        self.assertEqual(kind(RuntimeError("HTTP 402 payment")), "billing")
        self.assertEqual(kind(RuntimeError("PaymentRequired")), "billing")
        self.assertEqual(kind(RuntimeError("HTTP 429 slow down")), "rate-limit")
        self.assertEqual(kind(RuntimeError("HTTP 503 unavailable")), "server")
        self.assertEqual(kind(RuntimeError("the read timed out")), "timeout")
        self.assertEqual(kind(TimeoutError()), "timeout")
        self.assertEqual(kind(OSError("refused")), "network")
        self.assertEqual(kind(fc.fc_compact.JudgeError("input")), "input")
        self.assertEqual(kind(ValueError("odd")), "response")

    def test_backoff_doubles_and_caps(self):
        self.assertEqual(fc.fc_compact.backoff(0, 1000.0), (1, 1010.0))
        self.assertEqual(fc.fc_compact.backoff(5, 1000.0), (6, 1300.0))
        self.assertEqual(fc.fc_compact.backoff(6, 1000.0), (6, 1300.0))

    def test_cooldown_reasons(self):
        self.assertEqual(fc.fc_compact.cooldown_reason({"retry_after": 2000.0}, 90000, 5, 1000.0), "TypeSafe backoff")
        self.assertEqual(fc.fc_compact.cooldown_reason({"baseline": 50000}, 60000, 5, 1000.0), "Settling after a compaction")
        self.assertEqual(fc.fc_compact.cooldown_reason({"baseline": 50000}, 80000, 2, 1000.0), "Settling after a compaction")
        self.assertEqual(fc.fc_compact.cooldown_reason({"last_asked_at": 950.0, "last_asked_tokens": 10000}, 90000, 5, 1000.0), "Asked recently")
        self.assertEqual(fc.fc_compact.cooldown_reason({"last_asked_at": 0.0, "last_asked_tokens": 88000}, 90000, 5, 1000.0), "Context barely grew")
        self.assertIsNone(fc.fc_compact.cooldown_reason({"baseline": 50000, "last_asked_at": 0.0, "last_asked_tokens": 0}, 80000, 3, 1000.0))
        self.assertIsNone(fc.fc_compact.cooldown_reason({}, 90000, 5, 1000.0))


if __name__ == "__main__":
    unittest.main()
