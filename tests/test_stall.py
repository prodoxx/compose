import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

import time
NOW = 1_800_000_000.0
at = lambda ago: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - ago))
S = fc.fc_watch.fc_stall
ORCH, WORKER = "hw-orchestrator", "hw-builder"


class Lines(unittest.TestCase):
    def test_a_retry_loop_or_a_limit_banner_matches_and_prose_about_one_does_not(self):
        for line in ("⎿  API Error · Retrying in 4s… (attempt 3/10) · 401 Unauthorized", "Claude usage limit reached. Your limit will reset at 3pm",
                     "You've hit the 5-hour limit - try again at 3:00 PM", "request failed: rate limited, 429", "Error: 402 Payment Required", "status: 402",
                     "Retrying... HTTP 503"):
            self.assertEqual(S.limit_line(f"working\n{line}\n"), line.strip(), line)
        for line in ("def rate_limit(self):", "we should add a rate limit to the endpoint", "retrying the build with 12 files", "fixed 402 lines of CSS", "all tests passed"):
            self.assertIsNone(S.limit_line(f"{line}\n"), line)

    def test_only_the_bottom_of_the_pane_counts(self):
        pane = "usage limit reached\n" + "\n".join(f"line {i}" for i in range(20))
        self.assertIsNone(S.limit_line(pane))


class Stuck(unittest.TestCase):
    """A pane stuck on a retry loop or a limit is its own event, naming the pane and the matched line, once per spell."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        self.pane = "ok\n"

    def snap(self, state="working", status="working"):
        return {"tasks": {"001": {"state": "pending"}}, "agents": {ORCH: "idle", WORKER: status},
                "crew": {WORKER: [{"at": at(30), "state": state, "message": "m"}], ORCH: [{"at": at(30), "state": "working", "message": "parallel: started m"}]},
                "run": False, "verify": None, "run_end": 0, "last": (NOW - 10, "teammate x reported working")}

    def stalls(self, ago, snap=None, **kw):
        with mock.patch.object(fc.fc_herdr, "herdr_text", side_effect=lambda *a, **k: self.pane if a[2] == WORKER else "ok\n"):
            return fc.fc_watch.watch_stalls(self.proj, snap or self.snap(), 240, now_s=NOW - ago, **kw)

    def test_a_pane_that_keeps_showing_a_retry_line_fires_once_naming_pane_and_line(self):
        self.pane = "reading\nAPI Error · Retrying in 8s (attempt 4/10) · 401\n"
        self.assertEqual(self.stalls(100), [])  # the first look: it may be scrolling past
        self.assertEqual(self.stalls(70), [])
        ev = self.stalls(40)  # the same kind of line for 60 s
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("stall=limit agent=hw-builder", ev[0])
        self.assertIn("Retrying in 8s (attempt 4/10) · 401", ev[0])
        self.assertIn("herdr agent read hw-builder --lines 60", ev[0])
        self.assertEqual(self.stalls(10), [])  # once per spell

    def test_a_line_that_scrolls_away_ends_the_spell_and_a_later_spell_fires_again(self):
        self.pane = "usage limit reached\n"
        self.stalls(200)
        self.assertEqual(len(self.stalls(140)), 1)  # the first spell fires after 60 s
        self.pane = "all fine\n"
        self.assertEqual(self.stalls(100), [])  # gone: the spell ended
        self.pane = "usage limit reached\n"
        self.assertEqual(self.stalls(80), [])  # a new spell starts from this look
        ev = self.stalls(20)
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("stall=limit agent=hw-builder line='usage limit reached'", ev[0])

    def test_a_finished_or_closed_teammate_is_not_read(self):
        self.pane = "usage limit reached\n"
        for s in (self.snap(state="done"), dict(self.snap(), closed=[WORKER])):
            self.stalls(100, s)
            self.assertFalse([e for e in self.stalls(0, s) if "stall=limit" in e])

    def test_the_stall_reaches_the_engineering_manager_whoever_it_names(self):
        ev = ["event stall=limit agent=hw-builder line='usage limit reached' x"]
        self.assertEqual(fc.fc_watch.watch_filter(ev, "hw", [ORCH]), ev)


class Silent(unittest.TestCase):
    """A teammate herdr shows working for 30 minutes with no report and no commit is stuck."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results" / "crew").mkdir(parents=True)
        self.commit = 0

    def snap(self, state="working", status="working", report_ago=3000):
        return {"tasks": {"001": {"state": "pending"}}, "agents": {WORKER: status},
                "crew": {WORKER: [{"at": at(report_ago), "state": state, "message": "m"}]},
                "run": False, "verify": None, "run_end": 0, "last": (NOW - 10, "x")}

    def stalls(self, snap=None, working_for=2000, commit_ago=None, **kw):
        seen = {WORKER: [snap["agents"][WORKER] if snap else "working", NOW - working_for]}
        with mock.patch.object(S, "last_commit", return_value=NOW - commit_ago if commit_ago is not None else 0):
            return [e for _, e in S.silent_stalls(self.proj, snap or self.snap(), seen, NOW, **kw)]

    def test_working_for_half_an_hour_without_report_or_commit_is_silent(self):
        ev = self.stalls()
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("silent agent=hw-builder", ev[0])
        self.assertIn("no report and no commit for 33m20.0s", ev[0])

    def test_a_report_a_commit_or_a_shorter_spell_means_it_is_not_stuck(self):
        self.assertEqual(self.stalls(self.snap(report_ago=600)), [])  # reported ten minutes ago
        self.assertEqual(self.stalls(commit_ago=300), [])  # committed five minutes ago
        self.assertEqual(self.stalls(working_for=1500), [])  # working only 25 minutes, whatever it reported before
        self.assertEqual(self.stalls(self.snap(status="idle")), [])
        self.assertEqual(self.stalls(self.snap(state="needs-user")), [])
        self.assertEqual(self.stalls(self.snap(state="done")), [])

    def test_time_asleep_does_not_count_and_the_limit_can_be_set(self):
        self.assertEqual(self.stalls(awake_from=NOW - 600), [])
        self.assertEqual(len(self.stalls(awake_from=NOW - 2400)), 1)
        self.assertEqual(self.stalls(limit=4000), [])

    def test_the_last_commit_is_read_from_the_teammates_worktree(self):
        repo = Path(tempfile.mkdtemp())
        git = lambda *a: fc.fc_core.sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], repo)
        git("init", "-q"); git("commit", "-q", "--allow-empty", "-m", "x")
        (self.proj / "results" / "crew" / f"{WORKER}.json").write_text(json.dumps({"name": WORKER, "worktree": str(repo)}))
        self.assertAlmostEqual(S.last_commit(self.proj, WORKER), time.time(), delta=60)
        self.assertEqual(S.last_commit(self.proj, "hw-nobody"), 0)

if __name__ == "__main__":
    unittest.main()
