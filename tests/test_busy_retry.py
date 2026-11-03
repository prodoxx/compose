import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, sys, unittest, urllib.error, urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_models  # noqa: E402

LIMITED = '{"error":{"message":"temporarily rate-limited upstream"}}'
GOOD = {"choices": [{"message": {"content": '{"confirmed": true, "evidence": "line 3"}'}, "finish_reason": "stop"}], "usage": {}}


def refused(code, body=LIMITED, retry_after=None):
    hdrs = Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://openrouter.ai/api/v1/chat/completions", code, "Too Many Requests", hdrs, io.BytesIO(body.encode()))


def answer(obj):
    return io.BytesIO(json.dumps(obj).encode())


class BusyProvider(unittest.TestCase):
    """A provider that turns a request away as busy (HTTP 429, or a 502/503/504) is asked again after a growing wait; only one
    that stays busy fails the call. A refusal that stays (a 400, a bad key) and a billing refusal are never asked again."""
    def ask(self, *replies):
        with mock.patch.object(urllib.request, "urlopen", side_effect=list(replies)) as op, mock.patch.object(fc_models.time, "sleep") as nap:
            try:
                out = fc_models.confirm_call({"TEXT_MODEL_API_KEY": "k"}, "s", "u")
            except Exception as e:  # noqa: BLE001
                out = e
        return out, op.call_count, [c.args[0] for c in nap.call_args_list]

    def test_one_429_on_the_confirm_call_is_waited_out_and_the_next_reply_counts(self):
        out, calls, naps = self.ask(refused(429), answer(GOOD))
        self.assertEqual((out, calls, naps), ((True, "line 3"), 2, [fc_models.BUSY_WAITS[0]]))

    def test_busy_answers_are_asked_again_with_growing_waits_then_the_error_stands(self):
        out, calls, naps = self.ask(*[refused(429) for _ in range(len(fc_models.BUSY_WAITS) + 1)])
        self.assertIsInstance(out, RuntimeError)
        self.assertIn("HTTP 429", str(out))
        self.assertIn("temporarily rate-limited upstream", str(out))
        self.assertEqual((calls, naps), (len(fc_models.BUSY_WAITS) + 1, list(fc_models.BUSY_WAITS)))
        self.assertEqual(naps, sorted(naps))

    def test_an_overloaded_upstream_is_asked_again_too(self):
        for code in (502, 503, 504):
            with self.subTest(code=code):
                out, calls, _ = self.ask(refused(code, "overloaded"), answer(GOOD))
                self.assertEqual((out, calls), ((True, "line 3"), 2))

    def test_retry_after_is_honoured_up_to_a_minute(self):
        _, _, naps = self.ask(refused(429, retry_after="7"), refused(429, retry_after="3600"), refused(429, retry_after="soon"), answer(GOOD))
        self.assertEqual(naps, [7, 60, fc_models.BUSY_WAITS[2]])

    def test_a_refusal_that_stays_is_not_asked_again(self):
        out, calls, naps = self.ask(refused(400, '{"error":{"message":"bad model id"}}'), answer(GOOD))
        self.assertIsInstance(out, RuntimeError)
        self.assertEqual((calls, naps), (1, []))

    def test_a_billing_refusal_raises_payment_required_at_once(self):
        out, calls, naps = self.ask(refused(402, '{"error":{"message":"insufficient credits"}}'), answer(GOOD))
        self.assertIsInstance(out, fc_models.PaymentRequired)
        self.assertEqual((calls, naps), (1, []))

    def test_a_200_whose_error_says_rate_limited_is_asked_again(self):
        out, calls, naps = self.ask(answer({"error": {"code": 429, "message": "temporarily rate-limited upstream"}}), answer(GOOD))
        self.assertEqual((out, calls, naps), ((True, "line 3"), 2, [fc_models.BUSY_WAITS[0]]))


if __name__ == "__main__":
    unittest.main()
