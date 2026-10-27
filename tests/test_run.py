import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, sys, tempfile, threading, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_load

STANDARD = "claude:claude-sonnet-5-5"
OTHER = "claude:claude-opus-5-5"


def route(depth): return f"## Route\n\n* workflow: test-first\n* depth: {depth}\n"


class RunReadsTasksFresh(unittest.TestCase):
    def test_run_refreshes_the_tasks_from_linear_before_it_reads_them(self):
        order = []
        with mock.patch.object(fc.fc_core, "project_dir", return_value=Path("/p")), mock.patch.object(fc.fc_run, "require_committed"), \
                mock.patch.object(fc.fc_linear, "refresh_tasks", side_effect=lambda proj: order.append("refresh")), \
                mock.patch.object(fc.fc_tasks, "load_tasks", side_effect=lambda *a, **k: order.append("read") or []), self.assertRaises(SystemExit):
            fc.fc_run.cmd_run(SimpleNamespace(repo="/r", project="p", only=None, unproven=True, no_stack=True))
        self.assertEqual(order, ["refresh", "read"])


class RunStartsOnTier(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        issues = {"NEB-1": {"description": route("DEEP")}, "NEB-2": {"description": route("FAST")}}
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [], "issues": issues}))

    def run_tasks(self, *tasks):
        """Runs `run_tasks` up to preflight; returns the models preflight was asked about."""
        seen = []
        def stop(models): seen.append(set(models)); raise SystemExit("stop")
        with mock.patch.object(R, "preflight", side_effect=stop), mock.patch.object(R.fc_proc, "keep_awake"), self.assertRaises(SystemExit):
            fc.fc_run.run_tasks(self.proj.parent.parent, self.proj, list(tasks), SimpleNamespace(trunk=None, no_stack=True))
        return seen[0]

    def task(self, tid, linear, model=OTHER, auto=True):
        return {"id": tid, "linear": linear, "model": model, **({"model_auto": True} if auto else {})}

    def test_a_deep_auto_task_reaches_preflight_on_standard(self):
        t = self.task("001", "NEB-1")
        self.assertIn(STANDARD, self.run_tasks(t))
        self.assertEqual(t["model"], STANDARD)

    def test_a_fast_auto_task_reaches_preflight_on_the_default_implementer(self):
        t = self.task("001", "NEB-2")
        self.assertIn(fc.fc_config.DEFAULT_MODEL, self.run_tasks(t))
        self.assertEqual(t["model"], fc.fc_config.DEFAULT_MODEL)

    def test_a_task_with_its_own_model_keeps_it_whatever_the_route(self):
        t = self.task("001", "NEB-1", model="pi:a/b", auto=False)
        self.assertIn("pi:a/b", self.run_tasks(t))
        self.assertEqual(t["model"], "pi:a/b")

    def test_every_task_gets_its_own_start_model(self):
        deep, fast, own = self.task("001", "NEB-1"), self.task("002", "NEB-2"), self.task("003", "NEB-1", model="pi:a/b", auto=False)
        seen = self.run_tasks(deep, fast, own)
        self.assertEqual([t["model"] for t in (deep, fast, own)], [STANDARD, fc.fc_config.DEFAULT_MODEL, "pi:a/b"])
        self.assertLessEqual({STANDARD, fc.fc_config.DEFAULT_MODEL, "pi:a/b"}, seen)



R = fc.fc_run


class RunLoadGate(unittest.TestCase):
    def go(self, script, tasks=("001",), force=False, block=None, env=False):
        """Runs the queue loop; `script` is the held flag for each load reading (the last one repeats). Returns (starts, hold lines, readings)."""
        proj = Path(tempfile.mkdtemp()) / ".compose" / "p"
        (proj / "results").mkdir(parents=True)
        metas = [{"id": t, "model": STANDARD, **({"after": tasks[0]} if t != tasks[0] else {})} for t in tasks]
        calls, starts, lines = [], [], []
        def update(avg=None):
            calls.append(script[min(len(calls), len(script) - 1)])
            return {"held": calls[-1], "avg": 14.0}
        def run_task(repo, proj, m, attempts):
            starts.append((m["id"], calls[-1] if calls else "no load reading", len(calls)))
            for _ in range(300 if block and m["id"] == tasks[0] else 0):  # runs on until the load has been read `block` times
                if len(calls) >= block: break
                threading.Event().wait(0.01)
            return {"id": m["id"], "title": None, "status": "passed", "attempts": 1, "seconds": 0.0, "cost": 0.0, "branch": "b"}
        patches = [mock.patch.object(fc_load, "update", update), mock.patch.object(fc_load, "off", lambda force=False: force) if not env else mock.patch.dict(os.environ, {"COMPOSE_NO_LOAD": "1"}),
                   mock.patch("os.cpu_count", return_value=8), mock.patch.object(R, "QUEUE_POLL", 0.01),
                   mock.patch.object(R.fc_core, "say", lambda m, *a, **k: lines.append(m)), mock.patch.object(R.fc_implement, "run_task", run_task),
                   mock.patch.object(R.fc_credit, "check", return_value=None), mock.patch.object(R, "preflight"), mock.patch.object(R.fc_proc, "keep_awake"),
                   mock.patch.object(R.fc_blockers, "released", return_value=({}, {})), mock.patch.object(R.fc_herdr, "herdr_space"),
                   mock.patch.object(R.fc_history, "record"), mock.patch.object(R.fc_ledger, "record_task"), mock.patch.object(R, "triage"),
                   mock.patch.object(R.fc_reroute, "after_run", return_value=None), mock.patch.object(R.fc_core, "current_branch", return_value="main")]
        for p in patches: p.start()
        self.addCleanup(mock.patch.stopall)
        a = SimpleNamespace(trunk="main", no_stack=True, parallel=2, attempts=1, force=force)
        with self.assertRaises(SystemExit) as e:
            R.run_tasks(proj.parent.parent, proj, metas, a)
        self.assertEqual(e.exception.code, 0)
        return starts, [l for l in lines if "holding new task starts" in l], calls

    def test_a_held_run_with_nothing_running_waits_then_starts_after_the_release(self):
        starts, holds, calls = self.go([True, True, True, False])
        self.assertEqual(starts, [("001", False, 4)])
        self.assertEqual(len(holds), 1)
        self.assertRegex(holds[0], r"load 14\.0 on 8 cores: holding new task starts until it falls below \d+\.\d$")

    def test_a_running_task_finishes_while_held_and_the_next_starts_only_after_the_release(self):
        starts, holds, calls = self.go([False] + [True] * 6 + [False], ("001", "002"), block=4)
        self.assertEqual([s[:2] for s in starts], [("001", False), ("002", False)])
        self.assertGreaterEqual(starts[1][2], 8)
        self.assertEqual(len(holds), 1)

    def test_the_hold_line_is_said_once_per_hold(self):
        starts, holds, calls = self.go([True, True, False, True, True, False], ("001", "002"))
        self.assertEqual(len(holds), 2)
        self.assertEqual([s[:2] for s in starts], [("001", False), ("002", False)])
