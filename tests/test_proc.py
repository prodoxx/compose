import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, threading, time, types, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


class Proc(unittest.TestCase):
    """Task 001 (NEB-918): fc_proc.run, kill_group, stop, Stopped and HOOKS."""

    def setUp(self):
        self.addCleanup(fc.fc_proc.STOP.clear); self.addCleanup(fc.fc_proc.HOOKS.clear); self.addCleanup(fc.fc_proc.RUNNING.clear)

    def sleeps(self, k):
        """A sleep length unique to this run and case (12 s and change, so a missed kill ends on its own), and its live pids."""
        n = f"12.{os.getpid()}{k}"
        pids = lambda: [int(p) for p in subprocess.run(["pgrep", "-f", f"sleep {n}( |$)"], capture_output=True, text=True).stdout.split()]
        self.addCleanup(lambda: [os.kill(p, 9) for p in pids()])
        return n, pids

    def test_returns_output_and_exit_code_from_a_session_of_its_own(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        code = "import os, sys; print(os.getpgid(0), os.getsid(0), os.getcwd(), os.environ['X'], repr(sys.stdin.read())); sys.exit('bad')"
        r = fc.fc_proc.run([sys.executable, "-c", code], cwd=d, env={"X": "y"}, timeout=30)
        pgid, sid, cwd, x, stdin = r.stdout.split()
        self.assertEqual((type(r), r.returncode, r.stderr, x, stdin, os.path.realpath(cwd)), (subprocess.CompletedProcess, 1, "bad\n", "y", "''", os.path.realpath(d)))
        self.assertEqual(pgid, sid); self.assertNotEqual(int(pgid), os.getpgid(0))
        self.assertEqual(fc.fc_proc.RUNNING, set())

    def test_stop_from_another_thread_kills_the_whole_group_within_6s(self):
        """The background sleep holds stdout open and every process ignores SIGTERM: only the group SIGKILL ends the run."""
        (n, pids), stopped = self.sleeps(0), []
        def stopper():
            deadline = time.time() + 5
            while len(pids()) < 3 and time.time() < deadline: time.sleep(0.05)
            stopped.append(time.time()); fc.fc_proc.stop()
        threading.Thread(target=stopper, daemon=True).start()
        try:
            fc.fc_proc.run(["sh", "-c", f"trap '' TERM; sleep {n} & sleep {n}"], timeout=60)
        except fc.fc_proc.Stopped:
            pass
        self.assertLess(time.time() - stopped[0], 6)
        time.sleep(0.5); self.assertEqual(pids(), [])

    def test_run_limited_turns_a_timeout_into_exit_124_with_the_output_so_far_and_the_limit(self):
        (n, pids), t0 = self.sleeps(2), time.time()
        p = fc.fc_proc.run_limited(["sh", "-c", f"echo before; sleep {n} & sleep {n}"], timeout=1)
        self.assertEqual((p.returncode, p.stdout), (124, "before\n"))
        self.assertIn("TIMEOUT: did not finish within 1s", p.stderr)
        self.assertLess(time.time() - t0, 8)
        time.sleep(0.5); self.assertEqual(pids(), [])

    def test_after_the_stop_run_raises_stopped_and_starts_nothing(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        fc.fc_proc.STOP.set()
        with self.assertRaises(fc.fc_proc.Stopped):
            fc.fc_proc.run(["touch", "started"], cwd=d)
        self.assertFalse(os.path.exists(os.path.join(d, "started")))

    def test_stop_sets_stop_and_calls_every_hook(self):
        called = []
        fc.fc_proc.HOOKS.update(esc=lambda: called.append("esc"), close=lambda: called.append("close"))
        fc.fc_proc.stop()
        self.assertTrue(fc.fc_proc.STOP.is_set()); self.assertEqual(sorted(called), ["close", "esc"])

    def test_killing_a_group_already_gone_is_not_an_error(self):
        p = subprocess.Popen(["true"], start_new_session=True); p.wait()
        fc.fc_proc.kill_group(p.pid)


class PauseTask(unittest.TestCase):
    """fc_proc.pause_task: one task's groups die, the rest of the run goes on."""

    def setUp(self):
        for f in (fc.fc_proc.PAUSED, fc.fc_proc.KILLED, fc.fc_proc.TASK_GROUPS, fc.fc_proc.HOOKS):
            f.clear(); self.addCleanup(f.clear)
        fc.fc_proc.STOP.clear(); self.addCleanup(fc.fc_proc.STOP.clear)
        self.addCleanup(setattr, fc.fc_proc.TASK, "id", None)

    def test_pausing_a_task_kills_only_its_own_group_and_raises_paused_in_its_thread(self):
        out = {}
        def worker(tid):
            fc.fc_proc.TASK.id = tid
            try:
                out[tid] = fc.fc_proc.run(["sleep", "30"]).returncode
            except fc.fc_proc.Paused:
                out[tid] = "paused"
        a, b = (threading.Thread(target=worker, args=(t,)) for t in ("004", "005"))
        a.start(); b.start(); time.sleep(0.5)
        self.assertTrue(fc.fc_proc.pause_task("004")); a.join(6)
        self.assertEqual(out, {"004": "paused"})  # within 6 s, the kill_group limit
        self.assertTrue(b.is_alive())  # the unrelated task keeps going
        fc.fc_proc.pause_task("005"); b.join(6)

    def test_a_paused_task_starts_nothing_until_resumed_and_a_second_pause_says_false(self):
        fc.fc_proc.TASK.id = "004"
        fc.fc_proc.pause_task("004")
        self.assertFalse(fc.fc_proc.pause_task("004"))
        self.assertTrue(fc.fc_proc.task_paused("004"))
        with self.assertRaises(fc.fc_proc.Paused):
            fc.fc_proc.run(["true"])
        fc.fc_proc.resume_task("004")
        self.assertEqual(fc.fc_proc.run(["true"]).returncode, 0)
        self.assertEqual(fc.fc_proc.TASK_GROUPS.get("004", set()), set())

    def test_a_group_killed_by_a_pause_raises_paused_even_when_resumed_at_once(self):
        out = {}
        def worker():
            fc.fc_proc.TASK.id = "004"
            try:
                out["r"] = fc.fc_proc.run(["sleep", "30"]).returncode
            except fc.fc_proc.Paused:
                out["r"] = "paused"
        real = fc.fc_proc.kill_group
        def kill_then_resume(pid):
            real(pid); fc.fc_proc.resume_task("004")
        t = threading.Thread(target=worker); t.start(); time.sleep(0.5)
        with mock.patch.object(fc.fc_proc, "kill_group", kill_then_resume):
            fc.fc_proc.pause_task("004"); t.join(6)
        self.assertEqual(out, {"r": "paused"})
        self.assertEqual(fc.fc_proc.KILLED, set())

    def test_a_pause_landing_right_after_the_spawn_kills_the_new_group_and_raises_paused(self):
        real, spawned = subprocess.Popen, []
        def popen(*a, **k):
            p = real(*a, **k); spawned.append(p.pid)
            fc.fc_proc.PAUSED.add("004")  # the pause lands after the spawn, before the group is registered
            return p
        fc.fc_proc.TASK.id = "004"
        with mock.patch.object(fc.fc_proc.subprocess, "Popen", popen), self.assertRaises(fc.fc_proc.Paused):
            fc.fc_proc.run(["sleep", "30"])
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.killpg(spawned[0], 0)
        self.assertEqual((fc.fc_proc.KILLED, fc.fc_proc.TASK_GROUPS.get("004", set())), (set(), set()))

    def test_pause_calls_the_named_hook_only(self):
        called = []
        fc.fc_proc.HOOKS.update({"p-004": lambda: called.append("004"), "p-005": lambda: called.append("005")})
        fc.fc_proc.pause_task("004", "p-004")
        self.assertEqual(called, ["004"])

    def test_a_thread_with_no_task_is_never_paused(self):
        fc.fc_proc.TASK.id = None
        fc.fc_proc.pause_task("004")
        self.assertEqual(fc.fc_proc.run(["true"]).returncode, 0)


class Slots(unittest.TestCase):
    """fc_proc.slot lanes: a scoped run never queues behind a suite."""

    def test_scoped_tells_a_slice_of_the_tests_from_a_suite(self):
        yes = ("pytest -k foo", "go test ./... -run TestX", "npx vitest src/a.test.ts", "python3 -m unittest tests.test_proc", "pytest tests/test_a.py::T::t",
               "python3 -m unittest discover -s tests -p 'test_x.py'", "cd app && npx jest --testNamePattern=foo", "pytest -k a | tail", "npm test -- -t foo")
        no = ("python3 -m unittest discover -s tests", "python3 -m unittest discover -s tests -p \"test_*.py\"", "go test ./...", "npm test", "make test", "",
              "pytest -k a; pytest", "cd app")
        for c in yes: self.assertTrue(fc.fc_proc.scoped(c), c)
        for c in no: self.assertFalse(fc.fc_proc.scoped(c), c)

    def test_only_a_whole_suite_takes_a_suite_slot(self):
        suites = ("python3 -m unittest discover -s tests", "pytest", "npm test", "make test", "go test ./...", "cd app && npm run test", "cargo nextest run",
                  "uv run pytest tests", "pytest -k a; pytest", "python3 -m tests.run", "cd compose && python3 -m tests.run -j 4")
        others = ("python3 bin/compose.py --help", "bp help", "uv sync --locked", "git status", "npm install test-pkg", "cd app", "",
                  "pytest -k foo", "node --test test/bp.test.mjs", "pytest tests/test_a.py tests/test_b.py", "npm test -- -t foo",
                  "python3 -m tests.run test_ship.py", "python3 -m tests.run test_ship test_layers")
        for c in suites: self.assertTrue(fc.fc_proc.suite(c), c)
        for c in others: self.assertFalse(fc.fc_proc.suite(c), c)

    def test_suite_slots_default_to_one_on_any_core_count_and_the_env_overrides_them(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("COMPOSE_TEST_SLOTS", None)
            for cores in (None, 1, 4, 16, 64):
                with mock.patch("os.cpu_count", return_value=cores):
                    self.assertEqual(fc.fc_proc.suite_slots(), 1, cores)
            with mock.patch("os.cpu_count", return_value=64):
                for env, want in (("3", 3), ("1", 1), ("0", 1), ("", 1)):
                    os.environ["COMPOSE_TEST_SLOTS"] = env
                    self.assertEqual(fc.fc_proc.suite_slots(), want, env)

    def test_a_second_suite_waits_for_the_first_by_default_while_a_quick_run_does_not(self):
        with mock.patch.dict(os.environ, {"COMPOSE_QUICK_SLOTS": "1"}), mock.patch("os.cpu_count", return_value=16):
            os.environ.pop("COMPOSE_TEST_SLOT", None); os.environ.pop("COMPOSE_TEST_SLOTS", None)
            ready, got = threading.Event(), []
            def another():
                os.environ.pop("COMPOSE_TEST_SLOT", None)
                with fc.fc_proc.slot() as n: got.append(n); ready.set()
            with fc.fc_proc.slot() as first:
                th = threading.Thread(target=another); th.start()
                time.sleep(0.6); self.assertFalse(ready.is_set())  # the second suite waits on the first
                with fc.fc_proc.slot(None, True) as quick: self.assertEqual((first, quick), ("0", "q0"))
            th.join(timeout=5); self.assertEqual(got, ["0"])  # it takes the slot the first one released

    def test_a_command_that_is_not_a_suite_runs_while_every_suite_slot_is_held(self):
        with mock.patch.dict(os.environ, {"COMPOSE_TEST_SLOTS": "1", "COMPOSE_QUICK_SLOTS": "1"}):
            os.environ.pop("COMPOSE_TEST_SLOT", None)
            with fc.fc_proc.slot():
                t0 = time.time()
                r = fc.fc_implement.timed("echo hi", os.getcwd(), 10)
                self.assertEqual((r.returncode, r.stdout.strip()), (0, "hi"))
                self.assertLess(time.time() - t0, 3)

    def test_a_quick_run_gets_a_slot_while_every_suite_slot_is_held_and_waits_only_on_quick_runs(self):
        env = {"COMPOSE_TEST_SLOTS": "1", "COMPOSE_QUICK_SLOTS": "1"}
        said = []
        with mock.patch.dict(os.environ, env), mock.patch.object(fc.fc_proc, "SLOT_SAY_AFTER", 0.2):
            os.environ.pop("COMPOSE_TEST_SLOT", None)
            with fc.fc_proc.slot() as full:
                t0 = time.time()
                with fc.fc_proc.slot(said.append, True) as quick:
                    self.assertLess(time.time() - t0, 1)
                    self.assertEqual((full, quick), ("0", "q0"))
                    t1 = time.time()
                    ready = threading.Event()
                    def another():
                        os.environ.pop("COMPOSE_TEST_SLOT", None)
                        with fc.fc_proc.slot(said.append, True) as n: ready.set(); said.append(n)
                    th = threading.Thread(target=another); th.start()
                    time.sleep(0.6); self.assertFalse(ready.is_set())  # a second quick run waits on the first
                th.join(timeout=5); self.assertTrue(ready.is_set())
        self.assertTrue(any("quick-run slot" in x for x in said), said)
        self.assertEqual(sorted(x.name for x in (Path(os.environ["HOME"]) / ".compose" / "test-slots").iterdir()), ["0", "q0"])

    def test_a_process_already_under_a_slot_takes_none_in_either_lane(self):
        with mock.patch.dict(os.environ, {"COMPOSE_TEST_SLOT": "q1", "COMPOSE_QUICK_SLOTS": "1"}):
            with fc.fc_proc.slot(None, True) as a, fc.fc_proc.slot(None, True) as b, fc.fc_proc.slot() as c:
                self.assertEqual((a, b, c), ("q1", "q1", "q1"))
class KeepAwake(unittest.TestCase):
    """fc_proc.keep_awake holds one `caffeinate -dims -w <pid>` per process, and nothing where there is no caffeinate."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "caffeinate").write_text(f"#!/bin/sh\necho \"$@\" >> {self.dir}/args\nexec sleep 30\n")
        (self.dir / "caffeinate").chmod(0o755)
        for p in (mock.patch.dict(os.environ, {"PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}", "COMPOSE_NO_AWAKE": ""}), mock.patch.object(sys, "platform", "darwin"),
                  mock.patch.dict(fc.fc_proc.AWAKE, clear=True)):
            p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: [(h.terminate(), h.wait()) for h in list(fc.fc_proc.AWAKE.values())])

    def args(self, count):
        deadline = time.time() + 5
        while time.time() < deadline and not ((self.dir / "args").exists() and len((self.dir / "args").read_text().splitlines()) >= count):
            time.sleep(0.02)
        return sorted((self.dir / "args").read_text().splitlines())

    def test_it_holds_the_display_idle_disk_and_system_assertions_for_the_pid_and_only_once(self):
        held = fc.fc_proc.keep_awake()
        self.assertIs(fc.fc_proc.keep_awake(), held)  # run, verify and an e2e stage may each ask: one assertion
        self.assertIsNone(held.poll())
        self.assertEqual(self.args(1), [f"-dims -w {os.getpid()}"])
        other = fc.fc_proc.keep_awake(4242)  # a teammate's session
        self.assertIsNot(other, held)
        self.assertEqual(self.args(2), sorted([f"-dims -w {os.getpid()}", "-dims -w 4242"]))

    def test_it_asks_again_when_the_hold_ended(self):
        held = fc.fc_proc.keep_awake()
        held.terminate(); held.wait()
        self.assertIsNot(fc.fc_proc.keep_awake(), held)

    def test_it_does_nothing_without_caffeinate_or_off_macos(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(fc.fc_proc.keep_awake())
        (self.dir / "caffeinate").unlink()
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(fc.fc_proc.keep_awake())
        self.assertEqual(fc.fc_proc.AWAKE, {})

    def test_it_does_nothing_under_compose_no_awake(self):
        with mock.patch.dict(os.environ, {"COMPOSE_NO_AWAKE": "1"}):
            self.assertIsNone(fc.fc_proc.keep_awake())
        self.assertEqual(fc.fc_proc.AWAKE, {})


class Slept(unittest.TestCase):
    """fc_proc.clock and fc_proc.slept: the seconds a Mac slept, the wall clock's against the awake one (monotonic stops in sleep)."""

    def at(self, wall, awake):
        return mock.patch.object(fc.fc_proc, "time", types.SimpleNamespace(time=lambda: wall, monotonic=lambda: awake))

    def test_it_is_the_wall_time_the_awake_clock_did_not_see(self):
        with self.at(1000.0, 50.0):
            since = fc.fc_proc.clock()
        with self.at(1000.0 + 5400, 50.0 + 180):
            self.assertEqual(fc.fc_proc.slept(since), 5220)

    def test_a_short_gap_or_a_clock_set_back_is_no_sleep(self):
        with self.at(1000.0, 50.0):
            since = fc.fc_proc.clock()
        for wall, awake in ((1000.0 + 30, 50.0), (1000.0 + 600, 50.0 + 590), (1000.0 - 3600, 50.0 + 10)):
            with self.subTest(wall=wall), self.at(wall, awake):
                self.assertEqual(fc.fc_proc.slept(since), 0)

    def test_it_says_the_minutes(self):
        self.assertEqual(fc.fc_proc.slept_text(5220), "the machine slept for 87 minutes")
        self.assertEqual(fc.fc_proc.slept_text(90), "the machine slept for 2 minutes")


class NiceFloor(unittest.TestCase):
    """fc_proc.nice_floor: raise to the floor, never lower, inherit, a missing pid is None; main() calls it first. A niceness cannot
    be lowered again, so each case runs in a child; under a niced test run the child starts at `base`, so the raising cases are relative to it."""

    def child(self, body):
        code = f"import json, os, subprocess, sys\nsys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'bin')!r})\nimport fc_proc\nnice = lambda pid=0: os.getpriority(os.PRIO_PROCESS, pid)\nbase = nice()\n{body}"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_it_raises_to_the_floor_and_returns_it(self):
        base, ret, now = self.child("print(json.dumps([base, fc_proc.nice_floor(floor=base + 3), nice()]))")
        self.assertEqual((ret, now), (base + 3, base + 3))

    def test_the_default_floor_is_10_and_a_higher_niceness_is_never_lowered(self):
        base, ret, now = self.child("print(json.dumps([base, fc_proc.nice_floor(), nice()]))")
        self.assertEqual((ret, now), (max(base, 10),) * 2)
        ret, now = self.child("os.setpriority(os.PRIO_PROCESS, 0, 15)\nprint(json.dumps([fc_proc.nice_floor(), nice()]))")
        self.assertEqual((ret, now), (15, 15))

    def test_a_child_started_afterwards_inherits_it(self):
        ret, kid = self.child("r = fc_proc.nice_floor()\nprint(json.dumps([r, int(subprocess.run([sys.executable, '-c', 'import os;print(os.getpriority(os.PRIO_PROCESS, 0))'], capture_output=True, text=True).stdout)]))")
        self.assertEqual((kid, ret >= 10), (ret, True))

    def test_it_raises_another_process_and_leaves_this_one(self):
        other = subprocess.Popen(["sleep", "30"]); self.addCleanup(other.kill)
        mine = os.getpriority(os.PRIO_PROCESS, 0)
        self.assertEqual(fc.fc_proc.nice_floor(other.pid), max(mine, 10))
        self.assertEqual((os.getpriority(os.PRIO_PROCESS, other.pid), os.getpriority(os.PRIO_PROCESS, 0)), (max(mine, 10), mine))

    def test_a_pid_that_does_not_exist_is_none(self):
        gone = subprocess.Popen(["true"]); gone.wait()
        self.assertIsNone(fc.fc_proc.nice_floor(gone.pid))

    def test_main_calls_it_before_anything_else(self):
        with mock.patch.object(fc.fc_proc, "nice_floor", side_effect=RuntimeError("floor")), mock.patch.object(fc.fc_proc, "on_termination") as hook, \
                mock.patch.object(sys, "argv", ["compose", "timed", "true"]):
            with self.assertRaisesRegex(RuntimeError, "floor"):
                fc.main()
        hook.assert_not_called()


import fc_load


class TimedLoadWait(unittest.TestCase):
    """fc_implement.timed waits at the load gate before taking a suite slot, and a quick run or a held slot never waits."""

    def run_timed(self, cmd, limit=10, wait=None):
        """Run timed(cmd) with fc_load.wait patched; returns the result and the ordered calls to wait and slot."""
        events, real = [], fc.fc_proc.slot
        def spy(say=None, quick=False): events.append(("slot", quick, say)); return real(say, quick)
        def waited(say, force=False): events.append(("wait", say)); wait and wait()
        with mock.patch.object(fc_load, "wait", side_effect=waited), mock.patch.object(fc.fc_proc, "slot", spy):
            return fc.fc_implement.timed(cmd, os.getcwd(), limit), events

    def test_a_full_suite_waits_with_the_slots_say_before_it_takes_the_slot_and_then_runs(self):
        r, events = self.run_timed("echo ran; true || pytest")
        self.assertEqual([e[0] for e in events], ["wait", "slot"])
        self.assertEqual(events[0][1], events[1][2])
        self.assertIs(events[1][1], False)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ran"))

    def test_a_held_machine_keeps_the_suite_from_starting_until_the_load_falls_below_low(self):
        marker = Path(tempfile.mkdtemp()) / "started"; self.addCleanup(shutil.rmtree, marker.parent, True)
        load, said, loads = Path.home() / ".compose" / "load.json", [], []
        self.addCleanup(lambda: load.unlink(missing_ok=True)); load.unlink(missing_ok=True)
        def avg():
            loads.append(marker.exists())  # every read of the load: has the suite started yet
            return (13.0 if len(loads) < 4 else 7.0,) * 3
        env = {k: v for k, v in os.environ.items() if k not in ("COMPOSE_NO_LOAD", "COMPOSE_TEST_SLOT")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(fc.fc_config, "LOAD", {"high": 12, "low": 8}), mock.patch.object(fc_load, "memory_level", return_value=None), \
                mock.patch.object(fc_load, "WAIT_POLL", 0.01), mock.patch("os.getloadavg", avg), mock.patch.object(fc.fc_core, "say", said.append):
            r = fc.fc_implement.timed(f"echo ran > {marker}; true || pytest", os.getcwd(), 10)
        self.assertEqual((loads, marker.exists(), r.returncode), ([False] * 4, True, 0))  # four reads held or just released, none after the start
        waits = [m for m in said if "waiting until" in m]
        self.assertEqual(len(waits), 1); self.assertIn("is above 12.0: waiting until it falls below 8.0", waits[0])

    def test_a_quick_run_does_not_wait(self):
        for cmd in ("echo pytest -k foo", "echo hi"):
            r, events = self.run_timed(cmd)
            self.assertEqual([e[0] for e in events], ["slot"], cmd)
            self.assertIs(events[0][1], True, cmd)

    def test_a_process_already_under_a_slot_does_not_wait(self):
        with mock.patch.dict(os.environ, {"COMPOSE_TEST_SLOT": "0"}):
            r, events = self.run_timed("true || pytest")
        self.assertNotIn("wait", [e[0] for e in events])
        self.assertEqual(r.returncode, 0)

    def test_the_time_limit_starts_after_the_wait(self):
        r, events = self.run_timed("true || pytest", limit=1, wait=lambda: time.sleep(1.5))
        self.assertEqual([e[0] for e in events], ["wait", "slot"])
        self.assertEqual(r.returncode, 0)

import fc_started


class Recorded(unittest.TestCase):
    """A timed command and a started implementer agent are recorded in fc_started, and a timed command's record goes when it ends."""

    def timed(self, cmd, limit=10, events=None):
        events = [] if events is None else events
        def record(*a, **k): events.append(("record", a, k)); os.kill(a[0], 0)  # the process is alive when it is recorded
        with mock.patch.object(fc_started, "record", side_effect=record), mock.patch.object(fc_started, "forget", side_effect=lambda pid: events.append(("forget", pid))):
            t0 = time.time()
            r = fc.fc_implement.timed(cmd, os.getcwd(), limit)
        return r, events, t0

    def test_a_timed_command_is_recorded_with_its_waiter_and_limit_then_forgotten(self):
        r, events, t0 = self.timed("echo $$; sleep 0.3")
        pid = int(r.stdout.split()[0])
        self.assertEqual([e[0] for e in events], ["record", "forget"])
        self.assertEqual(events[0][1], (pid, "timed", None))
        self.assertEqual({k: v for k, v in events[0][2].items() if k != "until"}, {"waiter": os.getpid(), "cwd": os.getcwd()})
        self.assertTrue(t0 + 10 <= events[0][2]["until"] <= time.time() + 10)
        self.assertEqual(events[1], ("forget", pid))

    def test_the_record_goes_on_a_timeout_and_on_an_error_in_the_run(self):
        r, events, _ = self.timed("sleep 5", limit=1)
        self.assertEqual((r.returncode, [e[0] for e in events]), (124, ["record", "forget"]))
        self.assertEqual(events[1][1], events[0][1][0])
        events = []
        with mock.patch.object(fc.fc_core, "scrub", side_effect=RuntimeError("scrub")), self.assertRaises(RuntimeError):
            self.timed("true", events=events)
        self.assertEqual([e[0] for e in events], ["record", "forget"])

    def agent(self, pids=(412, 77, 90), start=None):
        root, record = Path(tempfile.mkdtemp()), mock.Mock()
        start = start or mock.Mock(return_value={})
        with mock.patch.object(fc_started, "record", record), mock.patch.object(fc.fc_herdr, "herdr", return_value={}), mock.patch.object(fc.fc_herdr, "herdr_agent_start", start), \
                mock.patch.object(fc.fc_herdr, "agent_pids", return_value=list(pids)) as ap, mock.patch.object(fc.fc_herdr, "herdr_implementer_pane", return_value="p1"), \
                mock.patch.object(fc.fc_herdr, "herdr_error", return_value=(None, "stopped")), self.assertRaises(RuntimeError):
            fc.fc_implement.herdr_pi(str(root / "wt"), "openrouter/x/y", "hi", None, {"meta": {"id": "028", "title": "t"}, "proj": root / "shop", "space": "w9"})
        return record, ap, root

    def test_a_started_implementer_is_recorded_with_its_lowest_pid_and_project_dir(self):
        record, ap, root = self.agent()
        ap.assert_called_with("p1")
        record.assert_called_once()
        self.assertEqual(record.call_args.args, (77, "implementer", str(root / "shop")))
        self.assertEqual(record.call_args.kwargs["waiter"], os.getpid())
        self.assertEqual(str(record.call_args.kwargs["cwd"]), str(root / "wt"))

    def test_no_pid_or_a_failed_start_records_nothing(self):
        record, ap, _ = self.agent(pids=())
        ap.assert_called_with("p1")
        record.assert_not_called()
        record, ap, _ = self.agent(start=mock.Mock(side_effect=RuntimeError("herdr agent start: agent_pane_busy")))
        record.assert_not_called()
        ap.assert_not_called()


class Detached(unittest.TestCase):
    """A command that starts a child in a group of its own (Playwright's webServer does) leaves nothing running when it is killed."""

    PARENT = ("import subprocess, time; "
              "print(subprocess.Popen(['sleep', '60'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True).pid, flush=True); time.sleep(60)")

    def gone(self, pid):
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        return False

    def test_a_timed_command_that_times_out_takes_its_detached_child_with_it(self):
        r = fc.fc_implement.timed(f"{sys.executable} -c \"{self.PARENT}\"", os.getcwd(), 1)
        child = int(r.stdout.split()[0])
        self.addCleanup(lambda: self.gone(child) or os.kill(child, 9))
        self.assertEqual(r.returncode, 124)
        self.assertTrue(self.gone(child), "the child in its own group is still running")


if __name__ == "__main__":
    unittest.main()
