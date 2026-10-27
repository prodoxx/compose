import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_costevent, fc_ledger

TASKS = [{"id": "001", "linear": "XX-1"}, {"id": "002", "linear": "XX-2", "kind": "design"}, {"id": "003", "linear": "XX-3"}]
ROUTE = "## Route\n\n* workflow: test-first\n* depth: FAST\n"


class VerifyEvents(unittest.TestCase):
    def project(self, rounds):
        proj = Path(tempfile.mkdtemp()) / ".compose" / "p"
        (proj / "results").mkdir(parents=True)
        (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": TASKS, "issues": {"XX-1": {"description": ROUTE}, "XX-2": {"description": ROUTE}}}))
        if rounds is not None: (proj / "results" / "verify.json").write_text(json.dumps({"rounds": rounds}))
        return proj

    def events(self, rounds, passed=True):
        return fc_costevent.verify_events(self.project(rounds), TASKS, passed, 0.25, "run-1", "abc123")

    def numbers(self, e):
        return [e["task_ids"], e["workflow"], e["depth"], e["attempts"], e["escalations"], e["fix_rounds"], e["cost"]]

    def test_task_events_then_verify_event(self):
        rounds = [{"fix_tasks": ["001"]}, {}, {"fix_tasks": ["001", "003"]}, {"fix_tasks": []}]
        evs = self.events(rounds)
        self.assertEqual([self.numbers(e) for e in evs], [
            [["001"], "test-first", "FAST", 0, 0, 2, 0.0],
            [["003"], "-", "-", 0, 0, 1, 0.0],
            [["001", "003"], "-", "-", 0, 0, 3, 0.25]])
        for e in evs:
            self.assertEqual((e["phase"], e["source"], e["result"], e["run_id"], e["commit_sha"], e["criterion_id"]), ("cost", "verify", "pass", "run-1", "abc123", []))

    def test_failed_verify_records_fail(self):
        self.assertEqual({e["result"] for e in self.events([], passed=False)}, {"fail"})

class RunEvent(unittest.TestCase):
    NUMBERS = {"attempts": 2, "escalations": 1, "fix_rounds": 0, "cost": 0.01234}
    SIX = ["workflow", "depth", "attempts", "escalations", "fix_rounds", "cost"]

    def final(self, result="pass"):
        return {"event_id": "e1", "run_id": "run-1", "commit_sha": "abc123", "task_ids": ["001"], "criterion_id": ["C1", "C2"], "phase": "red", "source": "gate", "result": result, "fails_on_base": True, "detail": "x"}

    def project(self, route=True):
        proj = Path(tempfile.mkdtemp()) / ".compose" / "p"
        (proj / "results").mkdir(parents=True)
        issues = {"XX-1": {"description": "## Route\n\n* workflow: implement-then-test\n* depth: DEEP\n"}} if route else {}
        (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": TASKS, "issues": issues}))
        return proj

    def test_none_without_final(self):
        for status in ("passed", "failed", "already_green", None):
            self.assertIsNone(fc_costevent.run_event(self.project(), TASKS[0], {"status": status, "attempts": 1}, None))

    def test_ran_task_carries_route_and_result_numbers(self):
        final = self.final("fail")
        result = {"status": "passed", "attempts": 3, "cost": 0.42, "log": [{"attempt": 1}, {"attempt": 2, "fallback": {"from": "a", "to": "b"}}]}
        ev = fc_costevent.run_event(self.project(), TASKS[0], result, final)
        self.assertEqual([ev[k] for k in ("task_ids", "criterion_id", "run_id", "commit_sha", "result", "phase", "source")], [["001"], ["C1", "C2"], "run-1", "abc123", "fail", "cost", "run"])
        self.assertEqual([ev[k] for k in self.SIX], ["implement-then-test", "DEEP", 3, 1, 0, 0.42])
        self.assertEqual(final["source"], "gate")

    def test_already_green_has_zero_numbers_and_unrouted_dashes(self):
        final = self.final("pass")
        ev = fc_costevent.run_event(self.project(route=False), TASKS[0], {"status": "already_green"}, final)
        self.assertEqual([ev[k] for k in self.SIX], ["-", "-", 0, 0, 0, 0.0])
        self.assertEqual((ev["result"], ev["criterion_id"]), ("pass", ["C1", "C2"]))


class RunBatch(unittest.TestCase):
    ROUTE = "## Route\n\n* workflow: fix-then-regression-test\n* depth: DEEP\n"
    PASSED = {"status": "passed", "attempts": 3, "cost": 0.42, "branch": "b", "log": [{"attempt": 1, "test_exit": 1}, {"attempt": 2, "fallback": {"from": "a", "to": "b"}}, {"attempt": 3, "test_exit": 0}]}

    def setUp(self):
        self.repo, self.proj = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [], "issues": {"XX-1": {"description": self.ROUTE}}}))
        self.addCleanup(fc_ledger.UNSAVED.clear)

    def run_tasks(self, tasks, results, hold=None, saved=True):
        """The (repo, proj, meta, events) of each record_task call, and what the run said."""
        if hold: (self.proj / "results" / "hold.json").write_text(json.dumps({t: {"reason": "x", "at": "t"} for t in hold}))
        tasks = [{"id": t, "model": "pi:x/glm", "body": "## Acceptance criteria\n* works\n", **kv} for t, kv in tasks.items()]
        run_task = lambda repo, proj, meta, attempts: {"id": meta["id"], "title": "t", "attempts": 1, "seconds": 0.0, "cost": 0.0, "branch": "-", **results[meta["id"]]}
        patches = [mock.patch.object(fc.fc_implement, "run_task", run_task), mock.patch.object(fc.fc_run, "preflight", lambda m: None), mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None),
                   mock.patch.object(fc.fc_run, "triage", lambda *x: None), mock.patch.object(fc.fc_core, "say"), mock.patch("sys.stdout", io.StringIO())]
        if saved: patches.append(mock.patch.object(fc_ledger, "record_task", return_value=True))
        else: patches += [mock.patch.object(fc.fc_linear, "linear_ready", return_value=True), mock.patch.object(fc.fc_linear, "linear_issue", return_value={}), mock.patch.object(fc.fc_linear, "linear_reconcile"),
                          mock.patch.object(fc.fc_linear, "linear_comment", side_effect=RuntimeError("down")), mock.patch("time.sleep")]
        with contextlib.ExitStack() as st:
            mocks = [st.enter_context(p) for p in patches]
            with self.assertRaises(SystemExit) as e:
                fc.fc_run.run_tasks(self.repo, self.proj, tasks, SimpleNamespace(trunk="main", parallel=1, attempts=1, no_stack=True))
        return (mocks[-1].call_args_list if saved else [], mocks[4].call_args_list, e.exception.code)

    def test_finished_task_batch_is_its_run_events_then_one_cost_event(self):
        (call,), _, _ = self.run_tasks({"001": {"linear": "XX-1"}}, {"001": self.PASSED})
        self.assertEqual(call.args[:3], (self.repo, self.proj, call.args[2]))
        *run, cost = call.args[3]
        self.assertEqual([e["phase"] for e in run], ["attempt", "attempt", "final"])
        self.assertEqual([cost[k] for k in ("phase", "result", "criterion_id", "run_id", "task_ids")], ["cost", run[-1]["result"], run[-1]["criterion_id"], run[-1]["run_id"], ["001"]])
        self.assertEqual([cost[k] for k in ("workflow", "depth", "attempts", "escalations", "fix_rounds", "cost")], ["fix-then-regression-test", "DEEP", 3, 1, 0, 0.42])

    def test_already_green_and_unrouted_task_batch_is_red_then_zero_cost_event(self):
        (call,), _, _ = self.run_tasks({"001": {}}, {"001": {"status": "already_green", "attempts": 0, "cost": 0.0}})
        self.assertEqual([e["phase"] for e in call.args[3]], ["red", "cost"])
        red, cost = call.args[3]
        self.assertEqual((cost["result"], cost["criterion_id"]), (red["result"], red["criterion_id"]))
        self.assertEqual([cost[k] for k in ("workflow", "depth", "attempts", "escalations", "fix_rounds", "cost")], ["-", "-", 0, 0, 0, 0.0])

    def test_held_tasks_are_never_recorded(self):
        tasks = {"001": {"linear": "XX-1"}, "002": {"waits_on": ["XX-9"]}, "003": {}}
        calls, _, _ = self.run_tasks(tasks, {"003": self.PASSED}, hold=["001"])
        self.assertEqual([c.args[2]["id"] for c in calls], ["003"])
        self.assertEqual(self.run_tasks({k: tasks[k] for k in ("001", "002")}, {}, hold=["001"])[0], [])

    def test_unsaved_batch_is_named_by_the_ledgers_warning_and_the_run_finishes(self):
        _, said, code = self.run_tasks({"001": {"linear": "XX-1"}}, {"001": self.PASSED}, saved=False)
        self.assertEqual((code, [r["phase"] for _, r in fc_ledger.UNSAVED]), (0, ["attempt", "attempt", "final", "cost"]))
        self.assertTrue(any(fc_ledger.UNSAVED[-1][1]["event_id"] in str(c) and "not saved" in str(c) for c in said))


class VerifyBatch(unittest.TestCase):
    TASKS = [{"id": "001"}]

    def setUp(self):
        self.proj, self.repo = Path(tempfile.mkdtemp()) / "feat", Path(tempfile.mkdtemp())
        (self.proj / "results").mkdir(parents=True)
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main"); git("commit", "-q", "--allow-empty", "-m", "one")
        self.sha = git("rev-parse", "HEAD")
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        (self.proj / "results" / "verify.json").write_text('{"rounds": []}')  # the last verify's, to be replaced

    def verify(self, passed=True, lost=False, **kw):
        """(exit code, said, calls in order); a `lost` cost row stays unsaved, which add_row cannot fix"""
        said, calls, unsaved = [], [], []
        def events(*args):
            calls.append(("events", args, json.loads((self.proj / "results" / "verify.json").read_text())["rounds"])); return [{"event_id": "c1"}]
        def record(issue, evs):
            calls.append(("record", issue, evs)); unsaved.extend((issue, e) for e in evs if lost); return not lost
        def add(issue, rows):
            calls.append(("flush", issue)); return False
        with mock.patch.object(fc.fc_verify, "run_stages", return_value=(passed, {"checks": []}, "" if passed else "finding")), mock.patch.object(fc_ledger, "verify_record"), \
                mock.patch.object(fc_ledger, "UNSAVED", unsaved), mock.patch.object(fc_ledger, "add_row", add), mock.patch.object(fc_ledger, "record", record), \
                mock.patch.object(fc_costevent, "verify_events", events), mock.patch.object(fc.fc_tasks, "load_tasks", return_value=self.TASKS), \
                mock.patch.object(fc.fc_core.LEDGER, "snapshot", return_value={"total": 0.37}), mock.patch.object(fc.fc_verify, "verify_failed"), mock.patch.object(fc.fc_linear, "linear_reconcile"), \
                mock.patch.object(fc.fc_core, "say", said.append), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            fc.fc_verify.verify_rounds(self.repo, self.proj, {"review_base": "main"}, SimpleNamespace(fix=0, skip_review=True, review_only=kw.pop("review_only", False)), [{"stage": "earlier"}], 0, **kw)
        return cm.exception.code, said, calls

    def test_full_verify_records_one_batch_built_from_the_rounds_just_written(self):
        for passed in (True, False):
            code, said, calls = self.verify(passed)
            self.assertEqual(calls, [("events", (self.proj, self.TASKS, passed, 0.37, "", self.sha), [{"stage": "earlier"}, {"checks": [], "sufficiency": None}]), ("record", "ENG-1", [{"event_id": "c1"}])])
            self.assertEqual((code, json.loads((self.proj / "results" / "verify.json").read_text())["passed"]), (0, True) if passed else (1, False))
            self.assertFalse([s for s in said if "not saved" in s])

    def test_unsaved_cost_event_is_flushed_after_the_record_and_fails_verify(self):
        code, said, calls = self.verify(lost=True)
        self.assertEqual([c[0] for c in calls], ["events", "record", "flush"])
        self.assertEqual((code, json.loads((self.proj / "results" / "verify.json").read_text())["passed"]), (1, False))
        self.assertIn("verify FAIL  evidence not saved on Linear: c1", said)

    def test_smoke_and_review_only_record_no_cost_events(self):
        for kw in ({"left_out": ("e2e",)}, {"review_only": True}):
            self.assertEqual(self.verify(**kw)[2], [], kw)

    def test_no_linear_json_records_on_no_issue(self):
        (self.proj / "linear.json").unlink()
        self.assertEqual(self.verify()[2][1:], [("record", None, [{"event_id": "c1"}])])
