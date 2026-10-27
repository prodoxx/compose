import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

T = time.time()
stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
hm = lambda t: time.strftime("%H:%M:%S", time.localtime(t))
S = fc.fc_watch.fc_sleep


class Wake(unittest.TestCase):
    """A wall-clock gap far past the 2 s poll is a sleep, and the wake is one event naming the window."""
    def test_a_normal_tick_is_not_a_sleep_and_a_long_gap_is(self):
        self.assertIsNone(S.wake_window(T, T + 2.5))
        self.assertIsNone(S.wake_window(T, T + 30))  # a slow herdr call is not a sleep
        self.assertIsNone(S.wake_window(None, T))
        self.assertEqual(S.wake_window(T, T + 1800), (T, T + 1800))

    def test_the_wake_is_one_event_naming_the_window(self):
        ev, woke = S.wake_events(Path("."), Path(tempfile.mkdtemp()) / "hw", None, T - 1800, T)
        self.assertEqual(len(ev), 1, ev)
        self.assertIn(f"event sleep=woke from={hm(T - 1800)} to={hm(T)} slept=30m00.0s", ev[0])
        self.assertEqual(woke, [T - 1800, T])
        self.assertEqual(S.wake_events(Path("."), Path(tempfile.mkdtemp()) / "hw", woke, T, T + 2), ([], woke))  # no new gap: no new event


class Lost(unittest.TestCase):
    """A run the sleep killed is resumed by itself when that is safe, else reported with the exact command."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        self.window = [T - 600, T - 20]
        self.pid = (p := subprocess.Popen([sys.executable, "-c", "pass"])).pid
        p.wait()
        self.marker = {"pid": self.pid, "started_at": stamp(T - 3600), "tasks": ["001"], "args": {}}
        self.log = self.proj / "results" / "run.log"
        self.log.write_text("working\n")
        os.utime(self.log, (T - 700, T - 700))  # written just before the sleep began
        self.save()

    def save(self, **kv):
        (self.proj / "results" / "run.json").write_text(json.dumps(self.marker | kv))

    def after(self, window=None):
        with mock.patch.object(S.subprocess, "Popen") as popen:
            ev = S.run_after_wake(Path("/r"), self.proj, window or self.window, T)
        return ev, popen

    def test_a_run_that_died_with_the_sleep_is_resumed_once(self):
        ev, popen = self.after()
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("event sleep=resumed", ev[0])
        self.assertIn(f"the Mac slept {hm(T - 600)} to {hm(T - 20)}", ev[0])
        cmd = popen.call_args[0][0]
        self.assertEqual([cmd[0].endswith("bin/compose"), *cmd[1:]], [True, "resume", "--repo", "/r", "--project", "hw"])
        self.assertTrue(popen.call_args[1]["start_new_session"])
        ev2, popen2 = self.after()
        self.assertEqual(ev2, [])  # a second watcher, or the next tick, does not resume it again
        popen2.assert_not_called()

    def test_a_payment_halt_is_the_product_owners_call(self):
        (self.proj / "results" / "resume.json").write_text("{}")
        ev, popen = self.after()
        popen.assert_not_called()
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("event sleep=run-lost", ev[0])
        self.assertIn("product owner", ev[0])
        self.assertIn("compose resume --repo /r --project hw", ev[0])

    def test_a_run_that_died_long_before_the_sleep_is_reported_not_resumed(self):
        os.utime(self.log, (T - 7200, T - 7200))
        ev, popen = self.after()
        popen.assert_not_called()
        self.assertIn("event sleep=run-lost", ev[0])
        self.assertIn("died of something else", ev[0])
        self.assertIn("compose resume --repo /r --project hw", ev[0])

    def test_ctrl_c_a_live_run_and_a_late_crash_are_left_alone(self):
        self.save(interrupted=True)  # stopped on purpose
        self.assertEqual(self.after()[0], [])
        self.save(interrupted=False, pid=os.getpid())  # still alive
        self.assertEqual(self.after()[0], [])
        self.save(pid=self.pid)
        self.assertEqual(S.run_after_wake(Path("/r"), self.proj, self.window, T + 3600), [])  # an hour after the wake: not the sleep's


class Loop(unittest.TestCase):
    """The watch loop wires it in: one event for the wake, and no quiet alarm for the silence the sleep made."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)

    def snap(self, last_ago):
        return {"tasks": {"001": {"state": "pending", "attempts": 0, "cost": 0.0, "note": "", "linear": "", "title": "t"}}, "agents": {}, "crew": {}, "run": False,
                "ended": None, "verify": None, "halt": None, "run_end": 0, "last": [T - last_ago, "teammate x reported working"]}

    def test_the_watch_loop_emits_the_wake_and_waits_out_the_silence(self):
        clock, naps = [T], []
        def nap(seconds):  # the third nap is the Mac sleeping: the wall clock jumps, the loop's own wait does not
            naps.append(seconds)
            clock[0] += 2000 if len(naps) == 3 else 2
        out = []
        a = SimpleNamespace(repo=".", now=False, from_=None, timeout=0, interval=0.0, stall=240, rewake=False)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=self.snap(10)), \
                mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}), mock.patch.object(fc.fc_proc, "keep_awake"), mock.patch("builtins.print", out.append), \
                mock.patch.object(fc.fc_watch.time, "time", side_effect=lambda: clock[0]), mock.patch.object(fc.fc_watch.time, "sleep", nap):
            with contextlib.suppress(SystemExit):
                fc.fc_watch.cmd_watch(a)
        self.assertEqual(len(naps), 3, out)
        self.assertEqual(len([l for l in out if "event sleep=woke" in l]), 1, out)
        self.assertFalse([l for l in out if "stall=quiet" in l], out)  # 2000 s of silence, all of it asleep
        self.assertEqual(json.loads((self.proj / "results" / "watch-orchestrator.json").read_text())["woke"], [T + 4, T + 2004])

if __name__ == "__main__":
    unittest.main()
