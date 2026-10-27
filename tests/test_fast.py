import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, tempfile, unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_fast  # noqa: E402


WHY = "no expected behavior: add a Reproduction section or acceptance criteria"
FIX, IMPL = "fix-then-regression-test", "implement-then-test"
FLOWS = {"001": FIX, "002": FIX, "003": FIX, "004": FIX, "005": IMPL, "006": "test-first", "008": FIX, "009": IMPL}  # 007 has no route
CTX = {"001": ([], 0, 0, 0), "002": (["c"], 0, 0, 0), "003": ([], 0, 1, 0), "004": ([], 0, 0, 1), "008": ([], 1, 0, 0)}  # criteria, label, documented, confirmed


class RunHolds(unittest.TestCase):
    """run holds a FAST task with no expected behavior, and prints that an implement-then-test task runs as test-first."""
    def setUp(self):
        self.routectx, self.routerec = (importlib.import_module(m) for m in ("fc_routectx", "fc_routerec"))  # the foundation's modules, not on every branch
        self.repo = Path(tempfile.mkdtemp()).resolve(); self.proj = self.repo / ".compose" / "p"; (self.proj / "results").mkdir(parents=True)
        (self.proj / "verify.json").write_text('{"task_prefix": "compose/p-"}')
        self.tasks = [{"id": t, "title": t, "files": [], "protected": [], "test": "true", "body": "no route here", "model": "m", "after": "", "linear": f"ENG-{t}", "waits_on": []} for t in sorted([*FLOWS, "007"])]
        self.issues = {f"ENG-{t}": {"description": f"## Route\n\n* workflow: {FLOWS[t]}" if t in FLOWS else "## Goal\nx"} for t in sorted([*FLOWS, "007"])}
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": self.tasks, "issues": self.issues}))
        self.routes = {t: {"workflow": w} for t, w in reversed(FLOWS.items())}
        ctx = lambda repo, proj, meta, tasks, issues: {"criteria": CTX[meta["id"]][0], "bug": dict(zip(("label", "documented", "confirmed"), map(bool, CTX[meta["id"]][1:])))}
        self.ctx = mock.patch.object(self.routectx, "route_context", side_effect=ctx).start()  # a KeyError says it was asked about a task that is not FAST
        self.say = mock.patch.object(fc.fc_core, "say").start()
        mock.patch.object(self.routerec, "parse_route", side_effect=lambda d: {"workflow": d.split("workflow: ")[1]} if "workflow: " in d else None).start()
        self.addCleanup(mock.patch.stopall)

    def test_holds_a_fast_task_with_no_reproduction_confirmed_case_or_criterion_and_says_implement_then_test_runs_as_test_first(self):
        held = fc_fast.run_holds(self.repo, self.proj, self.tasks, self.routes)
        self.assertEqual(held, {"001": WHY, "008": WHY})  # a Bug label alone is no expected behavior
        self.assertEqual([c.args for c in self.say.call_args_list], [(f"{t} route   implement-then-test is not implemented yet: it runs as test-first",) for t in ("005", "009")])
        self.assertEqual(self.ctx.call_args_list[0].args, (self.repo, self.proj, self.tasks[0], self.tasks, self.issues))

    def test_run_waits_the_held_task_on_the_route_and_still_dispatches_every_task_unchanged(self):
        a = mock.Mock(repo=str(self.repo), project="p", only=None, redo=False, unproven=True, no_stack=True)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=self.repo), \
             mock.patch.object(fc.fc_route, "route_tasks", return_value=(self.routes, {"006": "boom"})), mock.patch.object(fc.fc_run, "require_committed"), mock.patch.object(fc.fc_briefs, "test_open", return_value=False), mock.patch.object(fc.fc_run, "run_tasks") as run_tasks:
            fc.fc_run.cmd_run(a)
        metas = run_tasks.call_args.args[2]
        self.assertEqual({m["id"]: m["waits_on"] for m in metas}, {t: {"001": [f"the route ({WHY})"], "008": [f"the route ({WHY})"], "006": ["the route (boom)"]}.get(t, []) for t in sorted([*FLOWS, "007"])})

    def test_workflow_reads_the_cache_description_and_is_none_without_a_route_or_an_issue(self):
        t = self.tasks
        got = [fc_fast.workflow(self.proj, m) for m in (t[0], t[4], t[6], {**t[0], "linear": "ENG-x"}, {"id": "010"})]
        self.assertEqual(got, [FIX, IMPL, None, None, None])


class UnprovenLinear(unittest.TestCase):
    """An unproven task stays In Progress on Linear with its comment, and watch wakes on unproven and broken_base."""
    RESULT = {"status": "unproven", "branch": "fast/p/001", "attempts": 2, "cost": 0.0123, "model": "pi:x/glm", "log": []}

    def event(self, event, result=None, parent="ENG-9"):
        """Sends `event` for task ENG-3 and returns the calls to (move, comment, roll_up) as lists of args."""
        issue = {"identifier": "ENG-3", "parent": {"identifier": parent} if parent else None}
        with mock.patch.object(fc.fc_linear, "linear_ready", return_value=True), mock.patch.object(fc.fc_linear, "linear_issue", return_value=issue), \
             mock.patch.object(fc.fc_linear, "linear_move") as move, mock.patch.object(fc.fc_linear, "linear_comment") as comment, mock.patch.object(fc.fc_linear, "linear_roll_up") as roll:
            fc.fc_linear.linear_event(Path("proj"), {"id": "001", "linear": "ENG-3"}, event, result)
        self.moved = [c.kwargs for c in roll.call_args_list]
        return [[c.args[1:] for c in m.call_args_list] for m in (move, comment, roll)]

    def test_unproven_keeps_the_task_in_progress_with_its_one_line_comment_and_rolls_up_its_parent_as_in_progress(self):
        move, comment, roll = self.event("unproven", self.RESULT)
        self.assertEqual(move, [("In Progress",)])
        self.assertEqual(comment, [("Unproven: branch `fast/p/001` passes its tests after 2 attempt(s), $0.0123, model `pi:x/glm`; its regression test is not proven yet.",)])
        self.assertEqual(roll, [("ENG-9",)])
        self.assertEqual(self.moved, [{"moved": {"ENG-3": "In Progress"}}])
        self.assertEqual(self.event("unproven", self.RESULT, parent=None)[2], [])

    def test_watch_wakes_on_unproven_and_broken_base_and_keeps_its_other_final_states(self):
        self.assertTrue({"unproven", "broken_base", "passed", "failed", "hand_done"} <= set(fc.fc_watch.FINAL_STATES))


class UnprovenRun(unittest.TestCase):
    """An unproven task's dependents wait, and its run events carry no red event."""
    def setUp(self):
        self.ledger = importlib.import_module("fc_ledger")  # the foundation's module, not on every branch
        self.costevent = importlib.import_module("fc_costevent")
        keys = ("run_id", "commit_sha", "task_ids", "criterion_id", "phase", "source", "result", "fails_on_base", "detail")
        mock.patch.object(self.ledger, "event", side_effect=lambda *a: dict(zip(keys, a))).start()
        self.ev = lambda phase, result="passed", detail="d": self.ledger.event("r1", "abc", ["001"], "001.1:x", phase, "run", result, None, detail)
        mock.patch.object(self.ledger, "run_events", side_effect=lambda proj, repo, meta, r: [self.ev(p, r["status"] if p == "final" else "passed") for p in ("red", "green", "final")]).start()
        self.addCleanup(mock.patch.stopall)

    def test_a_fast_task_has_no_red_event_and_an_unproven_one_ends_with_a_final_unproven_event_while_other_workflows_keep_every_event(self):
        events = lambda flow, status: (mock.patch.object(fc_fast, "workflow", return_value=flow).start(), fc_fast.run_events(Path("p"), Path("r"), {"id": "001"}, {"status": status, "attempts": 3}))[1]
        for status in ("passed", "failed"):
            self.assertEqual(events(FIX, status), [self.ev("green"), self.ev("final", status)])
        self.assertEqual(events(FIX, "unproven"), [self.ev("green"), self.ev("final", "unproven", "unproven after 3 attempts")])
        for flow in ("test-first", IMPL, None):
            self.assertEqual(events(flow, "unproven"), [self.ev(p, "unproven" if p == "final" else "passed") for p in ("red", "green", "final")])

    def run_tasks(self, specs, statuses, on_start=None):
        """Runs tasks (id, after) whose run_task answers `statuses` and calls on_start(proj, task id) first; returns (say lines, {id: status} of the rows, exit code, record_task (issue, events) calls)."""
        repo = Path(tempfile.mkdtemp()).resolve(); proj = repo / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        self.started = []
        tasks = [{"id": t, "title": t, "after": after, "files": [], "protected": [], "test": "true", "model": "m", "linear": f"ENG-{t}"} for t, after in specs]
        def run_task(repo, proj, task, attempts):
            self.started.append(task["id"])
            on_start and on_start(proj, task["id"])
            return {"id": task["id"], "title": task["id"], "status": statuses[task["id"]], "attempts": 1, "seconds": 0.0, "cost": 0.0, "branch": "b", "log": []}
        a = mock.Mock(trunk="trunk", attempts=1, parallel=1, no_stack=True, redo=False)
        with mock.patch.object(fc.fc_core, "say") as say, mock.patch.object(fc.fc_run, "preflight", lambda m: None), mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None), \
             mock.patch.object(fc.fc_run, "triage", lambda *x: None), mock.patch.object(fc.fc_run, "reflect_task"), mock.patch.object(fc.fc_linear, "linear_reconcile"), \
             mock.patch.object(fc.fc_implement, "run_task", run_task), mock.patch.object(fc_fast, "run_events", side_effect=lambda proj, repo, meta, r: [meta["id"], r["status"]]), \
             mock.patch.object(self.costevent, "run_event", return_value=None), mock.patch.object(self.ledger, "record_task") as record,mock.patch("builtins.print") as out, self.assertRaises(SystemExit) as e:
            fc.fc_run.run_tasks(repo, proj, tasks, a)
        rows = {c.args[0].split()[0]: c.args[0].split()[1] for c in out.call_args_list if c.args and c.args[0][:1].isdigit()}
        return [c.args[0] for c in say.call_args_list], rows, e.exception.code, sorted((c.args[2]["linear"], c.args[3]) for c in record.call_args_list)

    def test_dependents_of_an_unproven_task_are_held_with_no_row_and_a_failed_tasks_dependents_stay_blocked_and_the_run_exits_1(self):
        specs = [("001", ""), ("007", ""), ("002", "007,001"), ("003", "002"), ("004", ""), ("005", "004"), ("010", "005"), ("006", "")]
        say, rows, code, record = self.run_tasks(specs, {"001": "unproven", "007": "unproven", "004": "failed", "006": "passed"})
        self.assertEqual(rows, {"001": "unproven", "007": "unproven", "004": "failed", "005": "blocked_by_004", "010": "blocked_by_005", "006": "passed"})
        self.assertEqual([l for l in say if " held " in l], ["002 held    after unproven 001,007", "003 held    after held 002"])
        self.assertEqual(record, [(f"ENG-{t}", [t, s]) for t, s in (("001", "unproven"), ("004", "failed"), ("006", "passed"), ("007", "unproven"))])  # fc_fast.run_events' events
        self.assertEqual(self.run_tasks([("001", ""), ("002", "001")], {"001": "unproven"})[2], 1)

    def test_a_task_queued_on_the_live_run_joins_it_and_starts_once_its_after_task_has_passed(self):
        def queue(proj, tid):
            if tid == "001":  # `compose run --only` hands these over while 001 runs
                (proj / "results" / "run-queue").mkdir(exist_ok=True)
                for t, after in (("002", "001"), ("003", "")):
                    fc.fc_core.atomic_write(proj / "results" / "run-queue" / f"{t}.json", json.dumps({"id": t, "title": t, "after": after, "files": [], "protected": [], "test": "true", "model": "m", "linear": f"ENG-{t}"}))
        mock.patch.object(fc.fc_run, "QUEUE_POLL", 0.05).start()
        say, rows, code, _ = self.run_tasks([("001", "")], {"001": "passed", "002": "passed", "003": "passed"}, queue)
        self.assertEqual((rows, code), ({"001": "passed", "002": "passed", "003": "passed"}, 0))
        self.assertLess(self.started.index("001"), self.started.index("002"))
        self.assertEqual([l for l in say if " joined " in l], ["002 joined  added to the run while it goes on", "003 joined  added to the run while it goes on"])

    def test_run_only_on_a_project_with_a_live_run_queues_its_tasks_there_and_the_lock_stays_for_everything_else(self):
        repo = Path(tempfile.mkdtemp()).resolve(); proj = repo / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        a = SimpleNamespace(cmd="run", repo=str(repo), project="p", only="004")
        mock.patch.object(fc.fc_core, "project_dir", lambda a: proj).start()
        marker = lambda **kw: (proj / "results" / "run.json").write_text(json.dumps({"pid": os.getppid(), "tasks": ["001"], "reads_queue": True, **kw}))
        self.assertFalse(fc.fc_run.joins_live_run(a))  # no run
        marker()
        self.assertTrue(fc.fc_run.joins_live_run(a))
        for other in (dict(only=None), dict(cmd="verify"), dict(cmd="resume")):
            self.assertFalse(fc.fc_run.joins_live_run(SimpleNamespace(**{**vars(a), **other})))
        marker(closed=True)  # the run is ending: it takes no more
        self.assertFalse(fc.fc_run.joins_live_run(a))
        marker()
        tasks = [{"id": "001", "model": "m"}, {"id": "004", "model": "m"}]
        with mock.patch.object(fc.fc_run, "preflight") as pre, mock.patch.object(fc.fc_core, "say") as say:
            fc.fc_run.run_tasks(repo, proj, tasks, a)
        self.assertEqual(sorted(f.name for f in (proj / "results" / "run-queue").glob("*.json")), ["004.json"])  # 001 is in the run already
        self.assertEqual(pre.call_args.args[0] - {None}, {"m", fc.fc_models.fallback_for("m")} - {None})
        self.assertIn("001 keep    already in the live run", say.call_args_list[0].args[0])

    def beside_live_run(self, ended, **marker):
        """`compose run --only 052` beside a live run (this test's parent process) that has ended the tasks `ended` {id: status}; returns (say lines, queued ids, SystemExit or None)."""
        repo = Path(tempfile.mkdtemp()).resolve(); proj = repo / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        self.proj = proj
        for tid, status in ended.items():
            (proj / "results" / f"{tid}.json").write_text(json.dumps({"id": tid, "status": status}))
        self.marker = lambda **kw: (proj / "results" / "run.json").write_text(json.dumps({"pid": os.getppid(), "tasks": ["001", "052"], "reads_queue": True, **kw}))
        self.marker(**marker)
        mock.patch.object(fc.fc_core, "project_dir", lambda a: proj).start()
        mock.patch.object(fc.fc_run, "QUEUE_POLL", 0.01).start()
        a = SimpleNamespace(cmd="run", repo=str(repo), project="p", only="052")
        stop = None
        with mock.patch.object(fc.fc_run, "preflight"), mock.patch.object(fc.fc_core, "say") as say:
            try:
                fc.fc_run.run_tasks(repo, proj, [{"id": "052", "model": "m"}], a)
            except SystemExit as e:
                stop = e
        return [c.args[0] for c in say.call_args_list], sorted(f.stem for f in (proj / "results" / "run-queue").glob("*.json")), stop

    def test_a_task_the_live_run_ended_without_passing_is_queued_on_it_again_and_one_that_passed_there_is_kept(self):
        say, queued, _ = self.beside_live_run({"052": "contradiction"}, open=["001"])
        self.assertEqual((queued, [l.split("(")[0] for l in say]), (["052"], ["052 queued  added to the live run "]))
        say, queued, _ = self.beside_live_run({"052": "passed"}, open=["001"])
        self.assertEqual((queued, say), ([], [f"052 keep    its result is passed in the live run (pid {os.getppid()})"]))

    def test_a_task_the_live_run_still_has_open_makes_run_only_say_it_waits_for_that_pid_and_then_decide_by_how_the_run_ended_it(self):
        def ends(status):
            def sleep(_):
                (self.proj / "results" / "052.json").write_text(json.dumps({"id": "052", "status": status}))
                self.marker(open=[])
            return sleep
        for status, queued in (("contradiction", ["052"]), ("passed", [])):
            with mock.patch.object(fc.fc_run.time, "sleep", ends(status)):
                say, got, _ = self.beside_live_run({}, open=["052"])
            self.assertEqual(say[0], f"052 waits   for the live run (pid {os.getppid()}) to finish it; it runs again if it does not pass")
            self.assertEqual(got, queued)

    def test_a_task_waited_for_stops_with_the_start_again_line_when_the_live_run_ends_first(self):
        with mock.patch.object(fc.fc_run.time, "sleep", lambda _: self.marker(open=[], closed=True)):
            say, queued, stop = self.beside_live_run({}, open=["052"])
        self.assertEqual((queued, say[0].split("(")[0]), ([], "052 waits   for the live run "))
        self.assertIn("start it again: compose run --project p --only 052", str(stop))

    def test_run_only_refuses_a_live_run_that_does_not_read_the_queue_and_writes_no_queue_file(self):
        repo = Path(tempfile.mkdtemp()).resolve(); proj = repo / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        a = SimpleNamespace(cmd="run", repo=str(repo), project="p", only="001,004")
        mock.patch.object(fc.fc_core, "project_dir", lambda a: proj).start()
        (proj / "results" / "run.json").write_text(json.dumps({"pid": os.getppid(), "tasks": ["002"]}))  # started before run --only could join
        with mock.patch.object(fc.fc_run, "preflight"), self.assertRaises(SystemExit) as e:
            fc.fc_run.run_tasks(repo, proj, [{"id": "001", "model": "m"}, {"id": "004", "model": "m"}], a)
        self.assertIn(f"live run (pid {os.getppid()}) predates", str(e.exception))
        self.assertIn("wait for it to end or stop it", str(e.exception))
        self.assertFalse((proj / "results" / "run-queue").exists())

    def test_a_task_queued_in_the_instant_the_run_closes_is_named_in_the_log_and_not_run(self):
        real = fc.fc_core.atomic_write
        def write(path, text, *args, **kw):
            real(path, text, *args, **kw)
            if path.name == "run.json" and json.loads(text)["closed"]:  # `compose run --only` lands after the loop's last look at the queue
                (path.parent / "run-queue").mkdir(exist_ok=True)
                real(path.parent / "run-queue" / "009.json", json.dumps({"id": "009", "after": "", "model": "m"}))
        with mock.patch.object(fc.fc_core, "atomic_write", write):
            say, rows, code, _ = self.run_tasks([("001", "")], {"001": "passed"})
        self.assertEqual((rows, code), ({"001": "passed"}, 0))
        self.assertEqual([l for l in say if " not run " in l], ["009 not run  queued as the run ended; start it again: compose run --project p --only 009"])

    def test_a_task_queued_again_after_the_run_ended_it_without_passing_runs_again_and_one_that_is_running_or_passed_is_left_alone(self):
        def queue(proj, tid):
            if tid == "002":  # `compose run --only 001` hands 001 over after the run ended it as a contradiction
                (proj / "results" / "run-queue").mkdir(exist_ok=True)
                for t in ("001", "002"):
                    fc.fc_core.atomic_write(proj / "results" / "run-queue" / f"{t}.json", json.dumps({"id": t, "title": t, "after": "", "files": [], "protected": [], "test": "true", "model": "m", "linear": f"ENG-{t}"}))
        mock.patch.object(fc.fc_run, "QUEUE_POLL", 0.05).start()
        say, rows, code, _ = self.run_tasks([("001", ""), ("002", "")], {"001": "contradiction", "002": "passed"}, queue)
        self.assertEqual(sorted(self.started), ["001", "001", "002"])  # 002 is running when it is queued: not started twice
        self.assertEqual([l for l in say if " joined " in l], ["001 joined  added to the run while it goes on"])


class UnprovenWhy(unittest.TestCase):
    """The unproven comment says why the regression test is not proof once the proof has run."""
    RESULT = {"status": "unproven", "branch": "fast/p/001", "attempts": 2, "cost": 0.0123, "model": "pi:x/glm", "log": []}
    LINE = "Unproven: branch `fast/p/001` passes its tests after 2 attempt(s), $0.0123, model `pi:x/glm`; its regression test is not proven yet."

    def event(self, event, result):
        """Sends `event` for task ENG-3 and returns (the moves, the comments) as lists of args after the issue."""
        with mock.patch.object(fc.fc_linear, "linear_ready", return_value=True), mock.patch.object(fc.fc_linear, "linear_issue", return_value={"identifier": "ENG-3"}), \
             mock.patch.object(fc.fc_linear, "linear_move") as move, mock.patch.object(fc.fc_linear, "linear_comment") as comment, mock.patch.object(fc.fc_linear, "linear_roll_up"):
            fc.fc_linear.linear_event(Path("proj"), {"id": "001", "linear": "ENG-3"}, event, result)
        return [c.args[1:] for c in move.call_args_list], [c.args[1:] for c in comment.call_args_list]

    def test_the_comment_ends_with_why_and_the_last_log_errors_text_as_written_once_a_proof_ran_and_the_task_stays_in_progress(self):
        why = "the regression test passes on the base, so it proves nothing"
        move, comment = self.event("unproven", dict(self.RESULT, proof={"proven": False}, log=[{"error": "older"}, {"error": why}]))
        self.assertEqual((move, comment), ([("In Progress",)], [(f"{self.LINE} Why: {why}.",)]))
        long = "x" * 600 + "\nsecond line"
        self.assertEqual(self.event("unproven", dict(self.RESULT, proof={}, log=[{"error": long}]))[1], [(f"{self.LINE} Why: {long}.",)])

class CheckOnlyEvents(unittest.TestCase):
    """A check-only run's rows name the check, end with a skipped regression-test row, and a held one has none."""
    TEST = "python3 -m unittest tests/test_a.py"
    META = {"id": "001", "test": TEST}

    def setUp(self):
        self.ledger = importlib.import_module("fc_ledger")  # the foundation's module, not on every branch
        self.ev = lambda phase, result, detail, sha="abc", crit=("001.1:x", "001.2:y"): self.ledger.event("r1", sha, ["001"], list(crit), phase, "run", result, True if phase == "red" else None, detail)

    def events(self, flow, rows, result=None):
        with mock.patch.object(fc_fast, "workflow", return_value=flow), mock.patch.object(self.ledger, "run_events", return_value=rows):
            return fc_fast.run_events(Path("p"), Path("r"), self.META, result or {"status": "passed"})

    def named(self, e):  # the row rebuilt with the check in its detail
        return self.ledger.event(e["run_id"], e["commit_sha"], e["task_ids"], e["criterion_id"], e["phase"], e["source"], e["result"], e["fails_on_base"], f"check-only: {self.TEST}: {e['detail']}")

    def skipped(self):
        return self.ledger.event("r1", "", ["001"], ["001.1:x", "001.2:y"], "red", "run", "skipped", None, "regression test skipped (check-only)")

    def test_every_row_names_the_check_with_new_ids_and_one_skipped_red_row_ends_the_run(self):
        rows = [self.ev("red", "fail_expected", "test fails before the change"), self.ev("attempt", "fail", "attempt 1 failed", "def"), self.ev("final", "pass", "passed after 2 attempts", "ghi")]
        got = self.events("check-only", rows)
        self.assertEqual(got, [*map(self.named, rows), self.skipped()])
        self.assertEqual(got[0]["detail"], f"check-only: {self.TEST}: test fails before the change")
        self.assertTrue(all(g["event_id"] != r["event_id"] for g, r in zip(got, rows)))

    def test_an_already_green_run_gives_its_pass_row_and_the_skipped_row(self):
        row = self.ev("red", "pass", "test already passes before the change")
        self.assertEqual(self.events("check-only", [row], {"status": "already_green"}), [self.named(row), self.skipped()])

    def test_a_held_run_has_no_rows(self):
        self.assertEqual(self.events("check-only", [], {"status": "held"}), [])
