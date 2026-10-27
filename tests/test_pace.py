import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_pace  # noqa: E402  the module compose.py just loaded (bin/ is on sys.path after it)

even = lambda gap: (lambda s, now: (0, gap))  # a policy: no earliest moment, one call every `gap` seconds


class Pace(unittest.TestCase):
    """One pace per service for every compose process on the machine: a state file beside the config in use."""
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        p = mock.patch.dict(os.environ, {"COMPOSE_CONFIG": str(self.home / ".compose" / "config.json")}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("COMPOSE_NO_PACE", None)  # the suite's switch: off here, this is the pace's own test

    def state(self, service="svc"):
        return json.loads((self.home / ".compose" / "pace" / f"{service}.json").read_text())

    def test_claims_follow_each_other_by_the_policys_spacing_and_the_state_lives_beside_the_config(self):
        self.assertEqual(fc_pace.pace_dir(), self.home / ".compose" / "pace")
        self.assertEqual(fc_pace.claim("svc", even(2)), 0)
        self.assertAlmostEqual(fc_pace.claim("svc", even(2)), 2, delta=0.1)
        self.assertAlmostEqual(fc_pace.claim("svc", even(2)), 4, delta=0.1)
        self.assertAlmostEqual(self.state()["next_at"], time.time() + 6, delta=0.2)
        self.assertEqual(fc_pace.claim("other", even(2)), 0)  # another service paces by itself

    def test_the_policy_names_the_earliest_moment_and_a_hold_from_the_service_wins_over_both(self):
        self.assertAlmostEqual(fc_pace.claim("svc", lambda s, now: (now + 5, 1)), 5, delta=0.1)
        fc_pace.hold("svc", 30)
        self.assertAlmostEqual(fc_pace.claim("svc", even(1)), 30, delta=0.1)
        fc_pace.hold("svc", 3)  # the latest word from the service wins, shorter too
        self.assertAlmostEqual(fc_pace.claim("svc", even(1)), 31, delta=0.1)  # behind the turn claimed before the hold

    def test_a_turn_past_the_limit_is_not_claimed(self):
        fc_pace.claim("svc", even(40))
        before = self.state()["next_at"]
        self.assertAlmostEqual(fc_pace.claim("svc", even(40), limit=10), 40, delta=0.1)
        self.assertEqual(self.state()["next_at"], before)  # the slot is still free for a caller that can wait
        self.assertAlmostEqual(fc_pace.claim("svc", even(40), limit=60), 40, delta=0.1)
        self.assertAlmostEqual(self.state()["next_at"], before + 40, delta=0.1)

    def test_learn_keeps_what_a_reply_said_and_averages_the_cost(self):
        fc_pace.learn("svc", cost=100, limit=2500, left=900, reset=None)
        self.assertEqual(self.state(), {"cost": 100, "limit": 2500, "left": 900})
        fc_pace.learn("svc", cost=200, left=800)
        self.assertEqual(self.state(), {"cost": 120, "limit": 2500, "left": 800})
        fc_pace.learn("svc", left=700)
        self.assertEqual(self.state()["cost"], 120)

    def test_wait_sleeps_its_turn_says_a_long_one_and_refuses_one_past_the_limit(self):
        with mock.patch("time.sleep") as nap, mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(fc_pace.wait("svc", even(30), 120, "Linear"), 0)
            nap.assert_not_called()
            self.assertAlmostEqual(fc_pace.wait("svc", even(30), 120, "Linear"), 30, delta=0.1)
            self.assertAlmostEqual(nap.call_args.args[0], 30, delta=0.1)
        self.assertIn("Linear: waiting 30s for a turn (every compose process paces its Linear calls to stay under the limit)", err.getvalue())
        with mock.patch("time.sleep") as nap, self.assertRaises(RuntimeError) as e:
            fc_pace.wait("svc", even(30), 10, "Linear")
        self.assertEqual(str(e.exception), "Linear lets calls through again in about 60s; run it again then")
        nap.assert_not_called()
        with mock.patch("time.sleep") as nap, mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertAlmostEqual(fc_pace.wait("svc", even(1), 120, "Linear"), 60, delta=0.1)  # the refused turn was not claimed
        self.assertIn("waiting 60s", err.getvalue())
        fc_pace.claim("quiet", even(1)); fc_pace.claim("quiet", even(1))
        with mock.patch("time.sleep") as nap, mock.patch("sys.stderr", io.StringIO()) as err:
            fc_pace.wait("quiet", even(1), 120, "Linear")
        self.assertEqual(err.getvalue(), "")  # a short wait is silent
        self.assertAlmostEqual(nap.call_args.args[0], 2, delta=0.1)

    def test_compose_no_pace_switches_the_wait_off_for_the_suite(self):
        with mock.patch.dict(os.environ, {"COMPOSE_NO_PACE": "1"}), mock.patch("time.sleep") as nap:
            for _ in range(3):
                self.assertEqual(fc_pace.wait("svc", even(30), 1, "Linear"), 0)
        nap.assert_not_called()
        self.assertFalse((self.home / ".compose" / "pace").exists())

    def test_the_state_is_shared_with_another_process(self):
        code = "import sys; sys.path.insert(0, %r); import fc_pace; print(fc_pace.claim('svc', lambda s, now: (0, 20)))" % str(Path(fc.__file__).parent)
        for expected in (0, 20):
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=os.environ, check=True).stdout
            self.assertAlmostEqual(float(out), expected, delta=0.5)
        self.assertAlmostEqual(fc_pace.claim("svc", even(20)), 40, delta=0.5)

    def test_about_and_retry_after(self):
        self.assertEqual([fc_pace.about(s) for s in (0, 45, 89, 90, 1800, 5399, 5400, 7200, 129599, 129600, 300000)],
                         ["0s", "45s", "89s", "2m", "30m", "90m", "2h", "2h", "36h", "2d", "3d"])
        self.assertEqual(fc_pace.retry_after({"Retry-After": "7"}, 60), 7)
        self.assertEqual(fc_pace.retry_after({"Retry-After": "0"}, 60), 1)  # "now" still gets a second
        self.assertEqual(fc_pace.retry_after({"Retry-After": "soon"}, 60), 60)
        self.assertEqual(fc_pace.retry_after({}, 60), 60)
        self.assertEqual(fc_pace.retry_after(None, 5), 5)

    def test_a_damaged_state_file_is_started_over(self):
        d = self.home / ".compose" / "pace"; d.mkdir(parents=True)
        for text in ("", "{not json", "[1, 2]"):
            (d / "svc.json").write_text(text)
            self.assertEqual(fc_pace.claim("svc", even(2)), 0)


if __name__ == "__main__":
    unittest.main()
