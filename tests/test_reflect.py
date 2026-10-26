import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, subprocess, tempfile, threading, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

MAP = """## Destination
Drafts survive a reload.

## Decisions so far
- Store drafts in localStorage
- ~~Autosave every keystroke~~ (C1)
- Autosave every 5 seconds (C1: keystrokes flooded the API, see ENG-9)
1. Show a toast on save

## Not yet specified
- Conflict handling
"""


def project(description=MAP):
    """A project dir whose last sync holds `description` as the feature's map, synced just now."""
    proj = Path(tempfile.mkdtemp()) / "hw"
    (proj / "results").mkdir(parents=True)
    (proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
    (proj / "results" / "tasks.json").write_text(json.dumps({"synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "feature": {"id": "ENG-1", "title": "Drafts", "description": description}, "tasks": [], "issues": {}}))
    return proj


def answers(**choices):
    """A Jev reply: answers(d0=("BROKEN", 0.8)) puts 0.8 on BROKEN for question d0."""
    return {"model": "jev-test", "answers": {k: {"choice": c, "probabilities": {c: p}} for k, (c, p) in choices.items()}}


class Reflect(unittest.TestCase):
    def setUp(self):
        for mod, name, value in ((fc.fc_config, "ENV_FILE", Path(tempfile.mkdtemp()) / "none.env"), (fc.fc_core, "say", lambda m: None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ts-test"}); p.start(); self.addCleanup(p.stop)

    def test_decision_lines_are_the_live_items_of_their_section(self):
        self.assertEqual(fc.fc_change.decision_lines(MAP), ["Store drafts in localStorage",
                                                 "Autosave every 5 seconds (C1: keystrokes flooded the API, see ENG-9)",
                                                 "Show a toast on save"])
        self.assertEqual(fc.fc_change.decision_lines("## Destination\nx\n"), [])
        self.assertEqual(fc.fc_change.map_section(MAP, "Destination"), "Drafts survive a reload.")
        self.assertEqual(fc.fc_change.decision_lines("## Decisions so far:\n- a\n"), ["a"])
        self.assertEqual(fc.fc_change.decision_lines("## Decisions so far (C1)\n- b\n## Next\n- c\n"), ["b"])

    def test_one_question_per_line_and_flags_at_the_threshold(self):
        proj = project()
        reply = answers(d0=("BROKEN", 0.82), d1=("HOLDS", 0.9), d2=("UNCLEAR", 0.6), d3=("UNCLEAR", 0.59))
        with mock.patch.object(fc.fc_models, "jev_ask", return_value=reply) as ask:
            path, rec = fc.fc_change.reflect(proj, "task:003", "localStorage is wiped by the sandbox",
                                   against=["Task 003 brief: use localStorage.setItem"])
        state, questions = ask.call_args.args[2], ask.call_args.args[3]
        self.assertEqual(len(questions), 4)  # three live decisions plus the quoted line
        self.assertEqual(questions["d3"]["instructions"]["line"], "Task 003 brief: use localStorage.setItem")
        self.assertEqual(state["destination"], "Drafts survive a reload.")
        self.assertIn("localStorage is wiped", state["evidence"])
        self.assertEqual([v["line"] for v in rec["flagged"]], ["Store drafts in localStorage", "Show a toast on save"])
        self.assertEqual(json.loads(path.read_text())["trigger"], "task:003")

    def test_a_brainstormed_design_without_a_destination_screens_against_its_intent(self):
        proj = project(MAP.replace("## Destination\nDrafts survive a reload.", "## Intent\nDrafts outlive a crash."))
        with mock.patch.object(fc.fc_models, "jev_ask", return_value=answers()) as ask:
            fc.fc_change.reflect(proj, "verify", "a finding")
        self.assertEqual(ask.call_args.args[2]["destination"], "Drafts outlive a crash.")

    def test_no_decisions_and_nothing_against_asks_nothing(self):
        proj = project("## Destination\nx\n")
        with mock.patch.object(fc.fc_models, "jev_ask") as ask:
            path, rec = fc.fc_change.reflect(proj, "verify", "a finding")
        ask.assert_not_called()
        self.assertEqual((rec["verdicts"], rec["flagged"], rec.get("error")), ([], [], None))
        self.assertTrue(path.exists())

    def test_a_screen_that_cannot_run_records_why_and_never_raises(self):
        proj = project()
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            _, rec = fc.fc_change.reflect(proj, "verify", "x")
        self.assertIn(f"Jev reflect needs a TypeSafe key, and {fc.fc_config.AUTH_PATH} has none", rec["error"])  # keys live in auth.json, not .env
        self.assertIn("--typesafe", rec["error"])
        with mock.patch.object(fc.fc_models, "jev_ask", side_effect=fc.fc_models.PaymentRequired("typesafe", "out of credits")):
            _, rec = fc.fc_change.reflect(proj, "verify", "x")
        self.assertIn("out of credits", rec["error"])
        (proj / "results" / "tasks.json").write_text("{not json")
        path, rec = fc.fc_change.reflect(proj, "verify", "x")
        self.assertTrue(rec["error"])
        self.assertTrue(path.exists())

    def test_jev_without_a_key_says_why_instead_of_a_key_error(self):
        """Under `compose timed` the scratch config holds no keys by design: a Jev call says so and sends nothing."""
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", Path(fc.fc_core.scratch_config())), \
                mock.patch("urllib.request.urlopen") as sent, self.assertRaises(RuntimeError) as e:
            fc.fc_models.jev_ask({}, {}, {}, {}, "Jev acceptance")
        sent.assert_not_called()
        self.assertEqual(str(e.exception), "Jev acceptance needs a TypeSafe key, but runs on a scratch config with no workspace and no keys, "
                                           "as every command under `compose timed` does: run compose's own commands directly")

    def test_parallel_screens_neither_wait_on_each_other_nor_share_a_file(self):
        proj, recs = project(), []
        meet = threading.Barrier(6, timeout=5)  # every screen is inside its Jev call at once: none holds a lock across it
        def ask(*a):
            meet.wait()
            return answers()
        with mock.patch.object(fc.fc_models, "jev_ask", ask):
            threads = [threading.Thread(target=lambda i=i: recs.append(fc.fc_change.reflect(proj, f"task:00{i}", "x")[1])) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual([r.get("error") for r in recs], [None] * 6)
        self.assertEqual(len(list((proj / "results").glob("reflect-*.json"))), 6)

    def test_evidence_builds_up_until_a_change_closes(self):
        proj = project()
        rows = [{"at": "2026-10-11 10:00:00", "trigger": "task:001", "text": "first failure"},
                {"at": "2026-10-11 10:05:00", "trigger": "task:002", "text": "second failure"}]
        (proj / "results" / "evidence.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        with mock.patch.object(fc.fc_models, "jev_ask", return_value=answers()) as ask:
            fc.fc_change.reflect(proj, "task:003", "third failure")
        ev = ask.call_args.args[2]["evidence"]
        self.assertLess(ev.index("third"), ev.index("second"))  # newest first
        self.assertIn("first failure", ev)
        fc.fc_change.changes_dir(proj).mkdir(parents=True)
        (fc.fc_change.changes_dir(proj) / "C1.json").write_text(json.dumps({"id": "C1", "done_at": "2026-10-11 10:03:00"}))
        with mock.patch.object(fc.fc_models, "jev_ask", return_value=answers()) as ask:
            fc.fc_change.reflect(proj, "task:004", "fourth failure")
        ev = ask.call_args.args[2]["evidence"]
        self.assertNotIn("first failure", ev)  # a closed change already acted on it
        self.assertIn("second failure", ev)

    def test_a_dismissal_cuts_the_evidence_like_a_closed_change(self):
        proj = project()
        (proj / "results" / "evidence.jsonl").write_text(json.dumps({"at": "2026-10-11 10:00:00", "trigger": "task:001", "text": "first failure"}) + "\n")
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", lambda a: proj), redirect_stdout(out):
            fc.fc_change.cmd_reflect(SimpleNamespace(evidence=None, dismiss="the sandbox flake, not the plan", against=None))
        self.assertIn("dismissed", out.getvalue())
        f = proj / "results" / "evidence.jsonl"; *rest, cut = f.read_text().splitlines(); cut = json.loads(cut)
        self.assertEqual(cut["cut"], True)
        cut["at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1))  # the next screen is a second later than the dismissal
        f.write_text("".join(l + "\n" for l in [*rest, json.dumps(cut)]))
        with mock.patch.object(fc.fc_models, "jev_ask", return_value=answers()) as ask:
            fc.fc_change.reflect(proj, "task:002", "second failure")
        ev = ask.call_args.args[2]["evidence"]
        self.assertNotIn("first failure", ev)
        self.assertNotIn("the sandbox flake", ev)
        self.assertIn("second failure", ev)

    def test_a_dismissal_needs_no_sync(self):
        proj = project()
        (proj / "results" / "tasks.json").unlink()  # a contradiction reported during groundwork, before any task exists
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", lambda a: proj), redirect_stdout(out):
            fc.fc_change.cmd_reflect(SimpleNamespace(evidence=None, dismiss="a false alarm", against=None))
        self.assertIn("dismissed", out.getvalue())

    def test_the_command_takes_evidence_or_a_dismissal(self):
        proj = project()
        with mock.patch.object(fc.fc_core, "project_dir", lambda a: proj), redirect_stdout(io.StringIO()):
            for kw in ({"evidence": None, "dismiss": None}, {"evidence": "x", "dismiss": "y"}):
                with self.assertRaises(SystemExit):
                    fc.fc_change.cmd_reflect(SimpleNamespace(against=None, **kw))

    def test_reflects_in_the_same_second_get_their_own_files(self):
        proj = project()
        with mock.patch.object(fc.fc_models, "jev_ask", return_value=answers()):
            paths = {fc.fc_change.reflect(proj, f"task:00{i}", "x")[0] for i in range(3)}
        self.assertEqual(len(paths), 3)

    def test_the_command_prints_each_verdict(self):
        proj = project()
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", lambda a: proj), \
                mock.patch.object(fc.fc_models, "jev_ask", return_value=answers(d0=("BROKEN", 0.9))), \
                mock.patch("sys.stdin", io.StringIO("product owner: drafts must sync across devices\n")), redirect_stdout(out):
            fc.fc_change.cmd_reflect(SimpleNamespace(evidence="-", dismiss=None, against=None))
        self.assertIn("BROKEN", out.getvalue())
        self.assertIn("Store drafts in localStorage", out.getvalue())
        self.assertIn("flagged 1 of 3", out.getvalue())

    def test_a_flagged_decision_prints_the_tasks_that_rest_on_it(self):
        proj = project(MAP.replace("1. Show a toast on save\n", "1. Show a toast on save\n- D3: one plan\n"))
        with_tasks(proj, [{**task("003"), "body": "## Goal\nFollow D3."}])
        rec = {"verdicts": [{"line": "D3: one plan", "choice": "BROKEN", "p_broken": 0.9, "p_unclear": 0.0}], "flagged": [{"line": "D3: one plan"}]}
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", lambda a: proj), mock.patch.object(fc.fc_change, "reflect", return_value=(Path("/p/r.json"), rec)), redirect_stdout(out):
            fc.fc_change.cmd_reflect(SimpleNamespace(evidence="x", dismiss=None, against=None))
        self.assertIn("rests on D3: 003 (cites D3)", out.getvalue())


class WatchReflect(unittest.TestCase):
    def snap(self, reflects):
        return {"tasks": {}, "agents": {}, "run": False, "verify": None, "crew": {}, "reflects": reflects}

    def test_flagged_screens_and_errors_wake_the_team_lead_once(self):
        after = self.snap({"/p/r1.json": {"trigger": "task:003", "error": None, "counts": (1, 0)},
                           "/p/r2.json": {"trigger": "verify", "error": None, "counts": (0, 0)},
                           "/p/r3.json": {"trigger": "task:004", "error": "no TYPESAFE_API_KEY", "counts": (0, 0)}})
        ev = fc.fc_watch.watch_events(self.snap({}), after)
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0], "event reflect=/p/r1.json broken=1 unclear=0 trigger=task:003")
        self.assertTrue(ev[1].startswith("event reflect=/p/r3.json error='no TYPESAFE_API_KEY'"))
        self.assertEqual(fc.fc_watch.watch_events(after, after), [])

    def test_a_pause_wakes_the_team_lead_and_counts_as_held(self):
        t = lambda state: {"state": state, "attempts": 1, "cost": 0.0, "note": "", "linear": "", "title": "t"}
        before, after = self.snap({}), self.snap({})
        before["tasks"], after["tasks"] = {"002": t("running")}, {"002": t("paused")}
        self.assertIn("task=002 state=paused", fc.fc_watch.watch_events(before, after)[0])
        snap = {**after, "agents": {}, "crew": {}, "run": False, "verify": None, "last": (1000.0, "x")}
        ev = fc.fc_watch.watch_stalls(project(), snap, 240, now_s=5000.0)
        self.assertTrue(any("stall=quiet" in e and "held 1" in e for e in ev), ev)

    def test_the_engineering_manager_does_not_get_screens(self):
        ev = ["event reflect=/p/r1.json broken=1 unclear=0 trigger=task:003"]
        self.assertEqual(fc.fc_watch.watch_filter(ev, "hw", ["hw-orchestrator"]), [])
        self.assertEqual(fc.fc_watch.watch_filter(ev, "hw", []), ev)

    def test_the_snapshot_drops_screens_a_closed_change_answered(self):
        proj = project()
        with mock.patch.object(fc.fc_models, "jev_ask", return_value=answers(d0=("BROKEN", 0.9))), mock.patch.object(fc.fc_core, "say", lambda m: None), \
                mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ts-test"}):
            path, _ = fc.fc_change.reflect(proj, "task:001", "x")
        with mock.patch.object(fc.fc_herdr, "herdr", return_value={}):
            self.assertEqual(fc.fc_watch.watch_snapshot(Path("."), proj)["reflects"][str(path)]["counts"], (1, 0))
            fc.fc_change.changes_dir(proj).mkdir(parents=True)
            (fc.fc_change.changes_dir(proj) / "C1.json").write_text(json.dumps({"id": "C1", "done_at": "2999-01-01 00:00:00"}))
            self.assertEqual(fc.fc_watch.watch_snapshot(Path("."), proj)["reflects"], {})

    def test_the_snapshot_drops_screens_a_dismissal_answered(self):
        proj = project()
        with mock.patch.object(fc.fc_models, "jev_ask", return_value=answers(d0=("BROKEN", 0.9))), mock.patch.object(fc.fc_core, "say", lambda m: None), \
                mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ts-test"}):
            fc.fc_change.reflect(proj, "task:001", "x")
        with open(proj / "results" / "evidence.jsonl", "a") as f:
            f.write(json.dumps({"at": "2999-01-01 00:00:00", "trigger": "dismissed", "text": "a false alarm", "cut": True}) + "\n")
        with mock.patch.object(fc.fc_herdr, "herdr", return_value={}):
            self.assertEqual(fc.fc_watch.watch_snapshot(Path("."), proj)["reflects"], {})

    def test_a_screen_written_between_two_watches_is_reported_by_the_next(self):
        proj = project()
        def watch(now):
            out, a = [], SimpleNamespace(repo=".", now=now, from_=None, timeout=0.05, interval=0.01, stall=10_000)
            with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "herdr", return_value={}), \
                    mock.patch("builtins.print", out.append), mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}):
                try:
                    fc.fc_watch.cmd_watch(a)
                except SystemExit:
                    pass
            return out
        watch(now=True)
        with mock.patch.object(fc.fc_models, "jev_ask", return_value=answers(d0=("BROKEN", 0.9))), mock.patch.object(fc.fc_core, "say", lambda m: None), \
                mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ts-test"}):
            path, _ = fc.fc_change.reflect(proj, "task:001", "x")  # while no watch runs
        out = watch(now=False)
        self.assertTrue(any(f"event reflect={path} broken=1" in line for line in out), out)
        self.assertIn("event timeout", watch(now=False)[-1])  # reported once

    def test_an_unreadable_change_file_does_not_hide_the_closed_ones(self):
        proj = project()
        fc.fc_change.changes_dir(proj).mkdir(parents=True)
        (fc.fc_change.changes_dir(proj) / "C1.json").write_text("{not json")
        (fc.fc_change.changes_dir(proj) / "C2.json").write_text(json.dumps({"id": "C2", "done_at": "2026-10-11 10:03:00"}))
        self.assertEqual(fc.fc_change.last_change_done(proj), "2026-10-11 10:03:00")


def task(tid, after=""):
    return {"id": tid, "title": f"task {tid}", "files": [f"{tid}.txt"], "protected": [], "test": "true", "model": "pi:x/y",
            "after": after, "linear": f"ENG-{int(tid)}", "linear_state": "Todo", "body": "## Goal\nx"}


def with_tasks(proj, tasks):
    cache = json.loads((proj / "results" / "tasks.json").read_text())
    cache["tasks"] = tasks
    (proj / "results" / "tasks.json").write_text(json.dumps(cache))


def run_all(proj, status=None, during=None, reflect=None, parallel=1):
    """run_tasks over every synced task with run_task faked: each passes unless `status` says otherwise, and `during(id)` runs
    inside it. Returns (ids run in order, the printed table, the reflect mock)."""
    calls, reflect = [], reflect or mock.MagicMock(return_value=(Path("/p/reflect.json"), {}))
    def fake(repo, proj_, meta, attempts):
        calls.append(meta["id"])
        if during:
            during(meta["id"])
        st = (status or {}).get(meta["id"], "passed")
        log = [{"contradiction": "the brief says x / the code shows y, a.txt:1"}] if st == "contradiction" else \
              [{"test_output": "AssertionError: expected 2, got 3"}]
        return {"id": meta["id"], "title": meta["title"], "status": st, "attempts": 1, "seconds": 0.0, "cost": 0.0, "branch": "-", "log": log}
    out = io.StringIO()
    with mock.patch.object(fc.fc_implement, "run_task", fake), mock.patch.object(fc.fc_run, "preflight", lambda m: None), \
            mock.patch.object(fc.fc_worktree, "stack_order", lambda r, p: []), mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None), \
            mock.patch.object(fc.fc_run, "triage", lambda *x: None), mock.patch.object(fc.fc_change, "reflect", reflect), redirect_stdout(out):
        try:
            fc.fc_run.run_tasks(Path("."), proj, fc.fc_tasks.load_tasks(proj), SimpleNamespace(trunk="main", parallel=parallel, attempts=1, no_stack=True))
        except SystemExit:
            pass
    return calls, out.getvalue(), reflect


class Holds(unittest.TestCase):
    def setUp(self):
        self.proj = project()
        with_tasks(self.proj, [task("001"), task("002", "001"), task("003", "002"), task("004")])
        self.said, self.notes = [], []
        fc.fc_tasks._hold_warned.clear()
        for mod, name, value in ((fc.fc_core, "say", self.said.append), (fc.fc_core, "project_dir", lambda a: self.proj),
                            (fc.fc_linear, "linear_note", lambda meta, body: self.notes.append((meta["id"], body)))):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def hold(self, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            fc.fc_change.cmd_hold(SimpleNamespace(**{"only": None, "rests_on": None, "reason": None, "release": None, **kw}))
        return out.getvalue()

    def test_hold_and_release_write_the_file_and_comment_on_linear(self):
        self.hold(only="002,003", reason="D2 broken")
        self.assertEqual(set(fc.fc_tasks.held(self.proj)), {"002", "003"})
        self.assertEqual(self.notes, [("002", "Held: D2 broken"), ("003", "Held: D2 broken")])
        self.hold(release="002")
        self.assertEqual(set(fc.fc_tasks.held(self.proj)), {"003"})
        self.hold(release="all")
        self.assertEqual(fc.fc_tasks.held(self.proj), {})
        self.assertEqual(self.notes[-2:], [("002", "Released"), ("003", "Released")])

    def test_holding_a_running_task_says_the_run_pauses_it(self):
        (self.proj / "results" / "002.json").write_text(json.dumps({"id": "002", "status": "running"}))
        (self.proj / "results" / "003.json").write_text(json.dumps({"id": "003", "status": "running"}))  # its run died
        late = "; it is running now: the run pauses it within seconds and keeps its work"
        with mock.patch.object(fc.fc_tasks, "run_alive", lambda p: True):
            out = self.hold(only="001,002", reason="D2 broken")
        with mock.patch.object(fc.fc_tasks, "run_alive", lambda p: False):
            out += self.hold(only="003", reason="D2 broken")
        self.assertEqual(out.splitlines(), ["001 held  D2 broken", "002 held  D2 broken" + late, "003 held  D2 broken"])
        self.assertEqual(self.notes, [("001", "Held: D2 broken"), ("002", "Held: D2 broken" + late), ("003", "Held: D2 broken")])
        self.assertEqual(set(fc.fc_tasks.held(self.proj)), {"001", "002", "003"})

    def test_hold_is_on_disk_before_it_reads_whether_the_task_runs(self):
        # run_task writes `running` and then reads the holds; hold writes the hold and then reads `running`. Either the task
        # sees the hold and never starts, or hold sees it running and says so: never a plain "held" for a task that starts.
        (self.proj / "results" / "002.json").write_text(json.dumps({"id": "002", "status": "running"}))
        seen = []
        with mock.patch.object(fc.fc_tasks, "run_alive", lambda p: seen.append(set(fc.fc_tasks.held(p))) or True):
            self.hold(only="002", reason="D2 broken")
        self.assertEqual(seen, [{"002"}])

    def test_a_task_held_while_the_run_prepares_it_never_starts_and_goes_back_to_pending(self):
        root = Path(tempfile.mkdtemp())
        repo = root / "repo"
        repo.mkdir()
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (repo / "001.txt").write_text("no\n")
        git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        before = json.dumps({"id": "001", "status": "failed", "attempts": 3})  # its last run's result
        (self.proj / "results" / "001.json").write_text(before)
        ready, turns, moves = fc.fc_worktree.ready_checkout, [], []
        def install(*a, **kw):  # the hold lands while the run clones and installs, after it submitted the task
            ready(*a, **kw)
            self.hold(only="001", reason="D2 broken")
        with mock.patch.object(fc.fc_worktree, "ready_checkout", install), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_implement, "implementer", lambda *a, **kw: turns.append(a)), \
                mock.patch.object(fc.fc_linear, "linear_event", lambda proj, meta, event, result=None: moves.append(event)):
            r = fc.fc_implement.run_task(repo, self.proj, {**task("001"), "test": "grep -q yes 001.txt"}, 3)
        self.assertEqual((r["status"], turns, moves), ("held", [], []))
        self.assertEqual((self.proj / "results" / "001.json").read_text(), before)
        self.assertFalse((self.proj / "worktrees" / "001").exists())
        self.assertIn("001 held    before its first turn: D2 broken", self.said)
        self.assertNotIn("001 start", " ".join(self.said))

    def test_a_task_handed_back_held_waits_like_any_held_task(self):
        calls, table, _ = run_all(self.proj, status={"004": "held"}, during=lambda tid: tid == "004" and self.hold(only="004", reason="x"))
        self.assertEqual(calls, ["001", "002", "003", "004"])
        self.assertIn("004 held    x", self.said)
        self.assertNotIn("004", table)  # not failed, not blocked: left pending for the next run
        self.assertIn("run done   3/3 passed", " ".join(self.said))

    def test_hold_needs_ids_and_a_reason(self):
        with self.assertRaises(SystemExit):
            self.hold(only="002")

    def test_a_run_skips_held_tasks_and_leaves_them_pending(self):
        self.hold(only="002", reason="D2 broken")
        calls, table, _ = run_all(self.proj)
        self.assertEqual(calls, ["001", "004"])
        self.assertIn("002 held    D2 broken", self.said)
        self.assertIn("003 held    after held 002", self.said)
        self.assertNotIn("blocked_by", table)

    def test_a_run_holds_a_task_whose_blocker_outside_the_feature_is_not_finished(self):
        with_tasks(self.proj, [task("001"), {**task("002", "001"), "waits_on": ["ENG-90 (In Progress)"]}, task("003", "002"), task("004")])
        calls, table, _ = run_all(self.proj)
        self.assertEqual(calls, ["001", "004"])
        self.assertIn("002 held    waits on ENG-90 (In Progress)", self.said)
        self.assertIn("003 held    after held 002", self.said)
        self.assertNotIn("blocked_by", table)
        with mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(returncode=1, stdout="")):
            row = fc.fc_status.task_state(Path("."), self.proj, fc.fc_tasks.load_tasks(self.proj)[1], "HEAD")
        self.assertEqual((row["state"], row["note"]), ("held", "waits on ENG-90 (In Progress)"))

    def test_a_failure_still_blocks_what_waits_on_it(self):
        self.hold(only="004", reason="x")
        _, table, _ = run_all(self.proj, status={"001": "failed"})
        self.assertIn("blocked_by_001", table)
        self.assertIn("004 held    x", self.said)

    def test_a_release_during_the_run_lets_the_task_start(self):
        self.hold(only="004", reason="x")
        calls, _, _ = run_all(self.proj, during=lambda tid: tid == "001" and self.hold(release="004"))
        self.assertIn("004", calls)

    def test_a_hold_stops_a_task_waiting_for_a_free_slot(self):
        with_tasks(self.proj, [task("001"), task("002"), task("003"), task("004")])
        placed = threading.Event()
        def during(tid):
            if tid == "001":
                self.hold(only="004", reason="x")
                placed.set()
            else:
                placed.wait(5)  # 002 and 003 keep their slots until the hold is placed
        calls, _, _ = run_all(self.proj, during=during, parallel=3)
        self.assertNotIn("004", calls)
        self.assertIn("004 held    x", self.said)

    def test_an_unreadable_hold_file_holds_nothing(self):
        (self.proj / "results" / "hold.json").write_text("{not json")
        self.assertEqual(fc.fc_tasks.held(self.proj), {})
        self.assertEqual(len(self.said), 1)
        self.assertTrue(self.said[0].startswith("hold.json unreadable"))
        fc.fc_tasks.held(self.proj)
        self.assertEqual(len(self.said), 1)

    def test_a_missing_hold_file_is_not_a_warning(self):
        self.assertEqual(fc.fc_tasks.held(self.proj), {})
        self.assertEqual(self.said, [])

    def test_hold_leaves_no_temp_file_and_a_parseable_hold_json(self):
        self.hold(only="002", reason="x")
        self.assertFalse((self.proj / "results" / "hold.json.tmp").exists())
        self.assertIn("002", json.loads((self.proj / "results" / "hold.json").read_text()))

    def test_status_shows_held_with_the_reason(self):
        self.hold(only="002", reason="D2 broken")
        with mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(returncode=1, stdout="")):
            row = fc.fc_status.task_state(Path("."), self.proj, task("002", "001"), "HEAD")
        self.assertEqual((row["state"], row["note"]), ("held", "D2 broken"))


class ImplementerContradiction(unittest.TestCase):
    def run_with(self, reply):
        """One task whose test can never pass, with an implementer that always answers `reply`. Returns (result, prompts)."""
        root = Path(tempfile.mkdtemp())
        repo, proj = root / "repo", root / "proj"
        repo.mkdir(); proj.mkdir()
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (repo / "a.txt").write_text("no\n")
        git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        meta = {"id": "001", "title": "answer", "files": ["a.txt"], "protected": [], "test": "grep -q yes a.txt",
                "model": "pi:p/primary", "after": "", "linear": "ENG-1", "body": "## Goal\nCall answer() from a.txt."}
        prompts = []
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            prompts.append(prompt)
            return "s", reply, 1, {"tokens_in": 1, "tokens_out": 1, "cost": 0.0}
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_core, "say", lambda m: None), mock.patch.object(fc.fc_config, "TEST_READER", None), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            return fc.fc_implement.run_task(repo, proj, meta, 3), prompts

    def test_a_contradiction_ends_the_task_without_more_attempts(self):
        found = "the brief says a.txt exports answer() / a.txt is plain text, a.txt:1"
        r, prompts = self.run_with(f"Read a.txt.\nCONTRADICTION: {found}")
        self.assertEqual((r["status"], r["attempts"], len(prompts)), ("contradiction", 1, 1))
        self.assertEqual(r["log"][-1]["contradiction"], found)
        self.assertIn("CONTRADICTION: <what the brief says>", prompts[0])
        self.assertIn("contradiction", fc.fc_watch.FINAL_STATES)

    def test_echoing_the_template_is_not_a_contradiction(self):
        r, prompts = self.run_with("Rules noted.\nCONTRADICTION: <what the brief says> / <what the code shows, file:line>")
        self.assertEqual((r["status"], len(prompts)), ("failed", 3))

    def test_only_a_line_that_starts_with_it_counts(self):
        self.assertIsNone(fc.fc_implement.contradiction("I would write CONTRADICTION: x / y if I found one"))
        self.assertEqual(fc.fc_implement.contradiction("a\nCONTRADICTION: first / one\nCONTRADICTION: last / two"), "last / two")

    def test_markdown_around_the_line_still_counts(self):
        found = "brief says x / code shows y, a.txt:1"
        for line in (f"`CONTRADICTION: {found}`", f"**CONTRADICTION:** {found}", f"- CONTRADICTION: {found}", f"  CONTRADICTION: {found}"):
            self.assertEqual(fc.fc_implement.contradiction(f"Read a.txt.\n{line}\n"), found, line)
        self.assertIsNone(fc.fc_implement.contradiction("`CONTRADICTION: <what the brief says> / <what the code shows, file:line>`"))

    def test_a_field_that_says_there_is_none_is_not_a_contradiction(self):
        for none in ("none", "None.", "none - the brief and the code agreed.", "NONE: all matched", "n/a", "N/A - nothing", "no contradiction", "No contradiction found.", "none\u2014agreed"):
            self.assertIsNone(fc.fc_implement.contradiction(f"Done.\nCONTRADICTION: {none}\n"), none)
            self.assertIsNone(fc.fc_implement.contradiction(f"Done.\n**CONTRADICTION:** {none}\n"), none)
        for real in ("the brief says none exist / a.txt:1 defines one", "nonexistent file a.txt / brief names it", "none of the exports the brief lists exist / a.txt:1",
                     "brief says no contradiction is possible / code shows one, a.txt:1", "n/aplan / x"):
            self.assertEqual(fc.fc_implement.contradiction(f"CONTRADICTION: {real}"), real, real)


class AutoReflect(unittest.TestCase):
    def setUp(self):
        self.proj = project()
        with_tasks(self.proj, [task("001"), task("002")])
        self.said = []
        p = mock.patch.object(fc.fc_core, "say", self.said.append); p.start(); self.addCleanup(p.stop)

    def test_a_task_that_does_not_pass_is_screened(self):
        _, _, reflect = run_all(self.proj, status={"001": "failed"})
        self.assertEqual(reflect.call_count, 1)
        trigger, evidence = reflect.call_args.args[1:3]
        self.assertEqual(trigger, "task:001")
        self.assertIn("ended failed after 1 attempt(s): AssertionError: expected 2, got 3", evidence)

    def test_a_contradiction_is_screened_against_the_brief_line(self):
        _, _, reflect = run_all(self.proj, status={"002": "contradiction"})
        self.assertEqual(reflect.call_args.args[1], "task:002")
        self.assertEqual(reflect.call_args.kwargs["against"], ["the brief says x"])

    def test_a_screen_that_crashes_never_stops_the_run(self):
        calls, table, _ = run_all(self.proj, status={"001": "failed"}, reflect=mock.MagicMock(side_effect=RuntimeError("disk full")))
        self.assertEqual(calls, ["001", "002"])
        self.assertTrue(any(m.startswith("001 reflect failed (disk full)") for m in self.said), self.said)
        self.assertIn("002", table)

    def test_a_crew_contradiction_is_screened(self):
        reflect = mock.MagicMock(return_value=(Path("/p/reflect.json"), {"flagged": []}))
        msg = "the API returns a list; Linear says: returns a dict (ENG-7); evidence: api.py:40".split()
        with mock.patch.object(fc.fc_change, "reflect", reflect), mock.patch.object(fc.fc_core, "project_dir", lambda a: self.proj), \
                mock.patch.object(fc.fc_crew, "crew_attention", lambda *a: None), redirect_stdout(io.StringIO()):
            fc.fc_crew.cmd_report(SimpleNamespace(as_="hw-tests-003", state="contradiction", message=msg))
        self.assertEqual(reflect.call_args.args[1], "contradiction:hw-tests-003")
        self.assertEqual(reflect.call_args.kwargs["against"], ["returns a dict"])

    def test_a_screen_that_crashes_never_stops_a_report_or_a_verify(self):
        reflect, out = mock.MagicMock(side_effect=RuntimeError("disk full")), io.StringIO()
        with mock.patch.object(fc.fc_change, "reflect", reflect), mock.patch.object(fc.fc_core, "project_dir", lambda a: self.proj), \
                mock.patch.object(fc.fc_crew, "crew_attention", lambda *a: None), redirect_stdout(out):
            fc.fc_crew.cmd_report(SimpleNamespace(as_="hw-tests-003", state="contradiction", message=["x"]))
            fc.fc_verify.verify_failed(self.proj, [{}], "confirmed [high] s.go: a query per answer")
        self.assertIn("reported hw-tests-003 contradiction", out.getvalue())
        self.assertEqual(len([m for m in self.said if "reflect failed (disk full)" in m]), 2, self.said)

    def test_every_crew_brief_says_how_to_report_one(self):
        with mock.patch.object(fc.fc_herdr, "project_worktree", lambda p: Path("/wt")):
            text = fc.fc_crew.crew_preamble(self.proj, "hw-groundwork", "groundwork")
        self.assertIn('contradiction "<what you found>; Linear says: <quoted line> (<issue>); evidence:', text)

    def test_a_failed_verify_is_screened_with_its_findings(self):
        reflect = mock.MagicMock(return_value=(Path("/p/reflect-1.json"), {}))
        with mock.patch.object(fc.fc_change, "reflect", reflect):
            fc.fc_verify.verify_failed(self.proj, [{}, {}], "confirmed [high] s.go: a query per answer")
            self.assertEqual(reflect.call_args.args[1], "verify")
            self.assertIn("after 2 round(s)", reflect.call_args.args[2])
            self.assertIn("a query per answer", reflect.call_args.args[2])
            reflect.reset_mock()
            fc.fc_verify.verify_failed(self.proj, [{}], "  ")  # only tool errors failed: nothing about the app to screen
            reflect.assert_not_called()


BLOCK = "\n\n```compose\nid: {tid}\nfiles: {tid}.txt\ntest: true\n```\n"


def issue(tid, goal, status="Todo", blocked=()):
    return {"title": f"task {tid}", "description": f"## Goal\n{goal}" + BLOCK.format(tid=tid), "status": status, "blockedBy": sorted(blocked)}


BEFORE = {"ENG-3": issue("003", "Save to localStorage."), "ENG-4": issue("004", "Read from localStorage.", blocked=["ENG-3"])}
AFTER = {"ENG-3": issue("003", "Save to the server."), "ENG-4": issue("004", "Read from localStorage.", status="Canceled"),
         "ENG-5": issue("005", "Read from the server.", blocked=["ENG-3"])}
MAP_AFTER = MAP.replace("- Store drafts in localStorage\n",
                        "- ~~Store drafts in localStorage~~ (C1)\n- Store drafts on the server (C1: the sandbox wipes localStorage)\n")


def synced(proj, description, issues, ahead=0):
    """Sync the feature and `issues` (id -> issue) the way the team lead does, stamped `ahead` seconds from now."""
    fc.fc_tasks.sync_tasks(proj, {"feature": {"id": "ENG-1", "title": "Drafts", "description": description},
                         "issues": [{"id": i, **v} for i, v in issues.items()]})
    cache = json.loads((proj / "results" / "tasks.json").read_text())
    cache["synced_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + ahead))
    (proj / "results" / "tasks.json").write_text(json.dumps(cache))


def git_repo(branches):
    """A repo with one commit and a task branch fast/hw/<id> for each id. Returns (path, git)."""
    repo = Path(tempfile.mkdtemp())
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("1\n")
    git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    for b in branches:
        git("branch", f"fast/hw/{b}")
    return repo, git


def change(proj, repo, action, cid=None, why=None, files=None):
    out = io.StringIO()
    with mock.patch.object(fc.fc_core, "project_dir", lambda a: proj), redirect_stdout(out):
        fc.fc_change.cmd_change(SimpleNamespace(repo=str(repo), action=action, id=cid, why=why, files=files))
    return out.getvalue()


class Changes(unittest.TestCase):
    def setUp(self):
        self.proj = project()
        self.repo, self.git = git_repo(("003", "004"))
        for mod, name, value in ((fc.fc_core, "say", lambda m: None), (fc.fc_config, "LINEAR_KEY", None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        synced(self.proj, MAP, BEFORE)

    def test_sync_keeps_every_issue_whole(self):
        issues = fc.fc_tasks.linear_cache(self.proj)["issues"]
        self.assertEqual(issues["ENG-4"], BEFORE["ENG-4"])
        synced(self.proj, MAP, dict(BEFORE, **{"ENG-6": {"title": "story", "description": "A story.", "status": None, "blockedBy": []}}))
        self.assertEqual(fc.fc_tasks.linear_cache(self.proj)["issues"]["ENG-6"]["status"], "")  # a null status never reaches .lower()

    def test_start_snapshots_linear_and_saves_each_branch_tip(self):
        self.assertIn("C1 open: localStorage is wiped", change(self.proj, self.repo, "start", why="localStorage is wiped"))
        rec = fc.fc_change.load_change(self.proj, "C1")
        self.assertEqual(rec["before"]["issues"]["ENG-4"]["blockedBy"], ["ENG-3"])
        tip = self.git("rev-parse", "fast/hw/003")
        self.assertEqual(rec["tips"]["003"], tip)
        self.assertEqual(self.git("rev-parse", "refs/compose/hw/C1/003"), tip)
        self.assertEqual(fc.fc_change.open_change(self.proj), "C1")
        self.assertEqual(fc.fc_change.change_line(self.proj), "  CHANGE C1 open: localStorage is wiped")

    def test_start_needs_a_fresh_sync_and_no_open_change(self):
        synced(self.proj, MAP, BEFORE, ahead=-600)
        with self.assertRaises(SystemExit):
            change(self.proj, self.repo, "start", why="x")
        synced(self.proj, MAP, BEFORE)
        change(self.proj, self.repo, "start", why="x")
        with self.assertRaises(SystemExit):
            change(self.proj, self.repo, "start", why="y")

    def test_done_records_what_changed_and_why(self):
        change(self.proj, self.repo, "start", why="localStorage is wiped by the sandbox.")
        with self.assertRaises(SystemExit):
            change(self.proj, self.repo, "done")  # no sync since the start
        synced(self.proj, MAP_AFTER, AFTER, ahead=2)
        out = change(self.proj, self.repo, "done")
        self.assertIn("C1: localStorage is wiped by the sandbox. Superseded: Store drafts in localStorage. "
                      "Canceled 004. New 005. Edited 003. Evidence: none recorded.", out)
        self.assertIn("post the comment above on ENG-1 through Linear", out)
        self.assertIsNone(fc.fc_change.open_change(self.proj))
        self.assertTrue(fc.fc_change.load_change(self.proj, "C1")["diff"]["feature"])

    def test_keeping_the_plan_is_recorded_too(self):
        change(self.proj, self.repo, "start", why="the product owner keeps localStorage")
        synced(self.proj, MAP, BEFORE, ahead=2)
        self.assertIn("C1: the product owner keeps localStorage. No changes: the plan stays as it was.", change(self.proj, self.repo, "done"))

    def test_a_task_the_run_only_moved_is_not_an_edit(self):
        change(self.proj, self.repo, "start", why="x")
        synced(self.proj, MAP, dict(BEFORE, **{"ENG-3": dict(BEFORE["ENG-3"], status="In Review")}), ahead=2)
        out = change(self.proj, self.repo, "done")
        self.assertNotIn("Edited", out)
        self.assertIn("No changes: the plan stays as it was.", out)

    def test_the_route_and_evidence_the_cli_writes_are_not_an_edit(self):
        change(self.proj, self.repo, "start", why="x")
        stamped = dict(BEFORE["ENG-3"], description=BEFORE["ENG-3"]["description"].replace(
            "\n\n```compose", "\n\n## Route\n\nrerouted after a failed run\n\n```compose") + "\n## Evidence\n\nVerdict: passed\n")
        synced(self.proj, MAP, dict(BEFORE, **{"ENG-3": stamped}), ahead=2)
        out = change(self.proj, self.repo, "done")
        self.assertNotIn("Edited", out)
        self.assertEqual(fc.fc_change.load_change(self.proj, "C1")["diff"]["edited"], [])
        edited = dict(stamped, description=stamped["description"].replace("localStorage", "the server"))
        self.assertEqual(fc.fc_change.change_diff({"feature": {}, "issues": BEFORE}, {"feature": {}, "issues": {**BEFORE, "ENG-3": edited}})["edited"], ["ENG-3"])

    def test_a_canceled_issue_the_sync_no_longer_lists_still_counts(self):
        change(self.proj, self.repo, "start", why="x")
        synced(self.proj, MAP, {"ENG-3": BEFORE["ENG-3"]}, ahead=2)
        change(self.proj, self.repo, "done")
        self.assertEqual(fc.fc_change.load_change(self.proj, "C1")["diff"]["canceled"], ["ENG-4"])

    def test_a_canceled_task_is_no_task_and_no_layer(self):
        for t in ("003", "004"):  # both layers were built
            self.git("branch", "-f", f"fast/hw/{t}", self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", t))
        synced(self.proj, MAP, dict(BEFORE, **{"ENG-4": dict(BEFORE["ENG-4"], status="Canceled")}))  # canceled, block and all
        self.assertEqual([m["id"] for m in fc.fc_tasks.load_tasks(self.proj)], ["003"])
        self.assertIn("ENG-4", fc.fc_tasks.linear_cache(self.proj)["issues"])  # still stored whole: undo can bring it back
        self.assertEqual(fc.fc_worktree.stack_order(self.repo, self.proj), ["fast/hw/003"])
        (self.proj / "results" / "tasks.json").unlink()
        self.assertEqual(fc.fc_worktree.stack_order(self.repo, self.proj), ["fast/hw/003", "fast/hw/004"])  # nothing synced: every branch

    def test_records_are_written_atomically(self):
        d = fc.fc_change.changes_dir(self.proj)
        change(self.proj, self.repo, "start", why="x")
        self.assertEqual([f.name for f in d.iterdir()], ["C1.json"])
        synced(self.proj, MAP_AFTER, AFTER, ahead=2)
        change(self.proj, self.repo, "done")
        self.assertEqual([f.name for f in d.iterdir()], ["C1.json"])
        self.assertTrue(fc.fc_change.load_change(self.proj, "C1")["done_at"])

    def test_stray_files_do_not_break_numbering(self):
        d = fc.fc_change.changes_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / "C1.json.tmp").write_text('{"id": "C1", "wh')
        (d / "Cx.json").write_text("{}")
        self.assertIn("C1 open", change(self.proj, self.repo, "start", why="x"))
        self.assertEqual(fc.fc_change.change_ids(self.proj), ["C1"])

    def test_status_survives_an_unreadable_change_record(self):
        d = fc.fc_change.changes_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / "C1.json").write_text('{"id": "C1", "wh')
        self.assertTrue(fc.fc_change.change_line(self.proj).startswith("  CHANGE records unreadable"))


STATE_TYPES = {"Todo": "unstarted", "In Progress": "started", "In Review": "started", "Done": "completed", "Canceled": "canceled"}


class SettlePaused(unittest.TestCase):
    def setUp(self):
        self.proj = project()
        self.repo, self.git = git_repo(("004", "005", "006"))
        for mod, name, value in ((fc.fc_core, "say", lambda m: None), (fc.fc_config, "LINEAR_KEY", None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        synced(self.proj, MAP, {f"ENG-{n}": issue(t, "Goal.") for n, t in ((4, "004"), (5, "005"), (6, "006"))})
        for t in ("004", "005", "006"):
            self.write(t, {"id": t, "status": "paused", "paused": {"ref": f"refs/compose/hw/paused/{t}", "base": "abc", "files": ["a.txt", "b.txt"]}})

    def write(self, tid, data):
        (self.proj / "results" / f"{tid}.json").write_text(json.dumps(data))

    def result(self, tid):
        return json.loads((self.proj / "results" / f"{tid}.json").read_text())

    def test_done_refuses_until_every_paused_task_is_settled(self):
        change(self.proj, self.repo, "start", why="D2 changed")
        with self.assertRaises(SystemExit) as e:
            change(self.proj, self.repo, "done")
        self.assertIn("paused and not settled: 004, 005, 006", str(e.exception))

    def test_keep_part_and_discard_are_recorded_on_the_change_and_the_result(self):
        change(self.proj, self.repo, "start", why="D2 changed")
        self.assertEqual(fc.fc_change.settle(self.proj, "004", "keep"), "004 keep  2 files")
        self.assertEqual(fc.fc_change.settle(self.proj, "005", "part", ("a.txt",)), "005 part  a.txt")
        fc.fc_change.settle(self.proj, "006", "discard")
        rec = fc.fc_change.open_change(self.proj)
        rec = fc.fc_change.load_change(self.proj, rec)
        self.assertEqual({t: s["choice"] for t, s in rec["settled"].items()}, {"004": "keep", "005": "part", "006": "discard"})
        self.assertEqual(self.result("005")["paused"]["keep"], ["a.txt"])
        self.assertEqual(self.result("004")["paused"]["choice"], "keep")
        synced(self.proj, MAP, {f"ENG-{n}": issue(t, "Goal.") for n, t in ((4, "004"), (5, "005"), (6, "006"))}, ahead=2)
        out = change(self.proj, self.repo, "done")
        self.assertIn("Paused work: 004 kept, 005 part (a.txt), 006 discarded.", out)

    def test_cmd_change_maps_the_actions_and_checks_files(self):
        change(self.proj, self.repo, "start", why="D2 changed")
        self.assertIn("005 part  a.txt", change(self.proj, self.repo, "part", cid="005", files="a.txt"))
        for action, files in (("part", None), ("keep", "a.txt")):
            with self.assertRaises(SystemExit):
                change(self.proj, self.repo, action, cid="004", files=files)

    def test_part_refuses_a_file_the_paused_work_did_not_touch_and_settle_needs_an_open_change(self):
        with self.assertRaises(SystemExit) as e:
            fc.fc_change.settle(self.proj, "004", "keep")
        self.assertIn("no change is open", str(e.exception))
        change(self.proj, self.repo, "start", why="D2 changed")
        with self.assertRaises(SystemExit) as e:
            fc.fc_change.settle(self.proj, "005", "part", ("x.txt",))
        self.assertIn("005 paused with a.txt, b.txt; x.txt is not among them", str(e.exception))
        with self.assertRaises(SystemExit):
            fc.fc_change.settle(self.proj, "007", "keep")
        self.assertNotIn("settled", fc.fc_change.load_change(self.proj, "C1"))

    def test_a_task_an_earlier_change_settled_does_not_block_a_later_one(self):
        every = {f"ENG-{n}": issue(t, "Goal.") for n, t in ((4, "004"), (5, "005"), (6, "006"))}
        change(self.proj, self.repo, "start", why="D2 changed")
        for t in ("004", "005", "006"):
            fc.fc_change.settle(self.proj, t, "discard")
        synced(self.proj, MAP, every, ahead=2)
        change(self.proj, self.repo, "done")
        change(self.proj, self.repo, "start", why="unrelated")
        synced(self.proj, MAP, every, ahead=4)
        self.assertIn("C2:", change(self.proj, self.repo, "done"))

    def test_undo_leaves_a_task_that_paused_again_under_a_later_change(self):
        every = {f"ENG-{n}": issue(t, "Goal.") for n, t in ((4, "004"), (5, "005"), (6, "006"))}
        change(self.proj, self.repo, "start", why="D2 changed")
        for t in ("004", "005", "006"):
            fc.fc_change.settle(self.proj, t, "discard")
        synced(self.proj, MAP, every, ahead=2)
        change(self.proj, self.repo, "done")
        again = {"ref": "refs/compose/hw/paused/004", "base": "new", "files": ["a.txt"], "change": "C7"}
        self.write("004", {"id": "004", "status": "paused", "paused": again})
        synced(self.proj, MAP, every, ahead=3)
        out = change(self.proj, self.repo, "undo", cid="C1")
        self.assertEqual(self.result("004")["paused"], again)
        self.assertIn("004 ran since C1", out)

    def test_undo_puts_the_paused_records_back_unsettled(self):
        change(self.proj, self.repo, "start", why="D2 changed")
        for t in ("004", "005", "006"):
            fc.fc_change.settle(self.proj, t, "discard")
        synced(self.proj, MAP, {f"ENG-{n}": issue(t, "Goal.") for n, t in ((4, "004"), (5, "005"), (6, "006"))}, ahead=2)
        change(self.proj, self.repo, "done")
        self.write("006", {"id": "006", "status": "passed"})
        synced(self.proj, MAP, {f"ENG-{n}": issue(t, "Goal.") for n, t in ((4, "004"), (5, "005"), (6, "006"))}, ahead=3)
        out = change(self.proj, self.repo, "undo", cid="C1")
        self.assertNotIn("choice", self.result("004")["paused"])
        self.assertEqual(self.result("006")["status"], "passed")
        self.assertIn("006 ran since C1; its paused work stays on refs/compose/", out)


    def release(self, ids):
        if not (self.proj / "results" / "hold.json").exists():
            (self.proj / "results" / "hold.json").write_text(json.dumps({t: {"reason": "D2?", "at": "x"} for t in ("004", "005", "006")}))
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", lambda a: self.proj), mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: None), \
                mock.patch.object(fc.fc_linear, "linear_note", lambda *a: None), redirect_stdout(out):
            fc.fc_change.cmd_hold(SimpleNamespace(only=None, rests_on=None, reason=None, release=ids))

    def test_a_task_released_with_no_change_open_does_not_block_done(self):
        self.release("004")  # a false alarm: released before any change opened, it keeps all its work
        change(self.proj, self.repo, "start", why="unrelated")
        self.release("005")  # released while a change is open: that change settles it
        synced(self.proj, MAP, {f"ENG-{n}": issue(t, "Goal.") for n, t in ((4, "004"), (5, "005"), (6, "006"))}, ahead=2)
        with self.assertRaises(SystemExit) as e:
            change(self.proj, self.repo, "done")
        self.assertIn("paused and not settled: 005, 006:", str(e.exception))

    def test_settle_writes_the_result_before_the_change_record(self):
        change(self.proj, self.repo, "start", why="D2 changed")
        with mock.patch.object(fc.fc_change, "write_change", side_effect=OSError("disk full")), self.assertRaises(OSError):
            fc.fc_change.settle(self.proj, "006", "discard")
        self.assertEqual(self.result("006")["paused"]["choice"], "discard")  # the rerun never keeps what was discarded


class LinearIssues:
    """Linear's GraphQL API in memory, enough for undo: issues with a description, a state, and their blockers."""
    def __init__(self, issues, feature_description):
        self.issues = {i: {"description": v["description"], "status": v["status"], "blockedBy": list(v["blockedBy"])} for i, v in issues.items()}
        self.issues["ENG-1"] = {"description": feature_description, "status": "In Progress", "blockedBy": []}
        self.fail_on, self.comments = None, []

    def __call__(self, query, v):
        if "inverseRelations" in query:
            i = v["id"]
            return {"issue": {"inverseRelations": {"nodes": [{"id": f"{b}|{i}", "type": "blocks", "issue": {"id": b, "identifier": b}}
                                                             for b in self.issues[i]["blockedBy"]]}}}
        if "issueRelationDelete" in query:
            b, i = v["id"].split("|")
            self.issues[i]["blockedBy"].remove(b)
            return {"issueRelationDelete": {"success": True}}
        if "issueRelationCreate" in query:
            self.issues[v["b"]]["blockedBy"].append(v["a"])
            return {"issueRelationCreate": {"success": True}}
        if "issueUpdate" in query:
            if v["id"] == self.fail_on:
                raise RuntimeError("Linear is down")
            if "in" in v:
                self.issues[v["id"]].update(v["in"])
            else:
                self.issues[v["id"]]["status"] = v["s"][2:]
            return {"issueUpdate": {"success": True}}
        if "commentCreate" in query:
            self.comments.append((v["id"], v["b"]))
            return {"commentCreate": {"success": True}}
        i = v["id"]
        return {"issue": {"id": i, "identifier": i, "state": {"name": self.issues[i]["status"], "type": STATE_TYPES[self.issues[i]["status"]]},
                          "team": {"states": {"nodes": [{"id": f"s-{n}", "name": n, "type": t} for n, t in STATE_TYPES.items()]}},
                          "parent": None, "children": {"nodes": []}}}


class Undo(unittest.TestCase):
    def setUp(self):
        self.proj = project()
        self.repo, self.git = git_repo(("003", "004"))
        self.lin = LinearIssues(AFTER, MAP_AFTER)
        for mod, name, value in ((fc.fc_core, "say", lambda m: None), (fc.fc_config, "LINEAR_KEY", "lin_test"), (fc.fc_linear, "linear_gql", self.lin),
                            (fc.fc_stack, "register_stack", lambda *a, **k: None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        synced(self.proj, MAP, BEFORE)
        change(self.proj, self.repo, "start", why="localStorage is wiped by the sandbox")
        rebuilt = self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "rebuilt")
        self.git("branch", "-f", "fast/hw/003", rebuilt)  # the re-plan redid layer 003
        synced(self.proj, MAP_AFTER, AFTER, ahead=2)
        change(self.proj, self.repo, "done")
        synced(self.proj, MAP_AFTER, AFTER, ahead=3)

    def test_undo_puts_back_every_issue_and_layer(self):
        old = self.git("rev-parse", "refs/compose/hw/C1/003")
        out = change(self.proj, self.repo, "undo", cid="C1")
        self.assertEqual(self.lin.issues["ENG-3"]["description"], BEFORE["ENG-3"]["description"])
        self.assertEqual((self.lin.issues["ENG-4"]["status"], self.lin.issues["ENG-4"]["blockedBy"]), ("Todo", ["ENG-3"]))
        self.assertEqual(self.lin.issues["ENG-5"]["status"], "Canceled")
        self.assertEqual(self.lin.issues["ENG-1"]["description"], MAP)
        self.assertEqual(self.git("rev-parse", "fast/hw/003"), old)
        self.assertEqual(fc.fc_change.load_change(self.proj, "C2")["why"], "undo C1")
        self.assertIn("C2 open: undo C1", out)

    def test_undo_refuses_when_something_it_touched_moved_since(self):
        synced(self.proj, MAP_AFTER, dict(AFTER, **{"ENG-3": issue("003", "Save to the server, batched.")}), ahead=3)
        with self.assertRaises(SystemExit) as e:
            change(self.proj, self.repo, "undo", cid="C1")
        self.assertIn("003", str(e.exception))
        self.assertIsNone(fc.fc_change.open_change(self.proj))

    def test_the_runs_own_state_moves_do_not_block_the_undo(self):
        for i in ("ENG-3", "ENG-5"):  # the run dispatched them after `change done`
            self.lin.issues[i]["status"] = "In Review"
        synced(self.proj, MAP_AFTER, {**AFTER, **{i: dict(AFTER[i], status="In Review") for i in ("ENG-3", "ENG-5")}}, ahead=3)
        self.assertIn("C2 open: undo C1", change(self.proj, self.repo, "undo", cid="C1"))
        self.assertEqual(self.lin.issues["ENG-5"]["status"], "Canceled")
        self.assertEqual(self.lin.issues["ENG-3"]["description"], BEFORE["ENG-3"]["description"])
        self.assertEqual(self.lin.issues["ENG-3"]["status"], "In Review")  # content goes back; the run's state move is the run's

    def test_a_canceled_task_brought_back_blocks_the_undo(self):
        synced(self.proj, MAP_AFTER, dict(AFTER, **{"ENG-4": dict(AFTER["ENG-4"], status="Todo")}), ahead=3)
        with self.assertRaises(SystemExit) as e:
            change(self.proj, self.repo, "undo", cid="C1")
        self.assertIn("004", str(e.exception))

    def sync_linear(self, ahead):
        """Sync Linear as it now is, the way the team lead does after an undo."""
        synced(self.proj, self.lin.issues["ENG-1"]["description"],
               {i: {"title": AFTER[i]["title"], **v} for i, v in self.lin.issues.items() if i != "ENG-1"}, ahead=ahead)

    def test_the_task_an_undo_cancels_leaves_the_stack(self):
        self.git("branch", "fast/hw/005", self.git("rev-parse", "fast/hw/003"))  # 005 was built on the re-plan
        self.assertIn("fast/hw/005", fc.fc_worktree.stack_order(self.repo, self.proj))
        change(self.proj, self.repo, "undo", cid="C1")
        self.sync_linear(4)
        self.assertNotIn("005", [m["id"] for m in fc.fc_tasks.load_tasks(self.proj)])
        self.assertNotIn("fast/hw/005", fc.fc_worktree.stack_order(self.repo, self.proj))

    def test_undoing_the_undo_puts_the_change_back(self):
        rebuilt = self.git("rev-parse", "fast/hw/003")
        change(self.proj, self.repo, "undo", cid="C1")
        self.sync_linear(4)
        change(self.proj, self.repo, "done")
        self.assertEqual(fc.fc_change.load_change(self.proj, "C2")["diff"]["new"], ["ENG-4"])  # back from Canceled: new to the plan again
        self.sync_linear(5)
        change(self.proj, self.repo, "undo", cid="C2")
        self.assertEqual({i: self.lin.issues[i]["status"] for i in AFTER}, {"ENG-3": "Todo", "ENG-4": "Canceled", "ENG-5": "Todo"})
        self.assertEqual(self.lin.issues["ENG-3"]["description"], AFTER["ENG-3"]["description"])
        self.assertEqual(self.lin.issues["ENG-1"]["description"], MAP_AFTER)
        self.assertEqual(self.git("rev-parse", "fast/hw/003"), rebuilt)

    def test_without_a_key_it_prints_the_edits(self):
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", None):
            out = change(self.proj, self.repo, "undo", cid="C1")
        self.assertIn("apply these through `compose linear update`, then sync:", out)
        self.assertNotIn("save_issue", out)
        self.assertIn('ENG-4: {"status": "Todo", "blockedBy": ["ENG-3"]}', out)
        self.assertEqual(self.lin.issues["ENG-4"]["status"], "Canceled")  # nothing was written
        self.assertEqual(fc.fc_change.open_change(self.proj), "C2")

    def test_linear_failing_midway_lists_what_is_left_and_leaves_the_undo_open(self):
        self.lin.fail_on = "ENG-3"
        out = change(self.proj, self.repo, "undo", cid="C1")
        self.assertEqual(self.lin.issues["ENG-5"]["status"], "Canceled")  # applied before the failure
        self.assertIn("  ENG-3: ", out)
        self.assertIn("  ENG-1: ", out)
        self.assertNotIn("  ENG-5: ", out)
        self.assertIn("apply these through `compose linear update`, then sync:", out)  # the leftovers after a failure, too
        self.assertNotIn("save_issue", out)
        self.assertEqual(fc.fc_change.open_change(self.proj), "C2")

    def test_a_branch_git_refuses_to_move_is_listed_and_the_undo_still_finishes(self):
        tip = self.git("rev-parse", "refs/compose/hw/C1/003")
        self.git("worktree", "add", str(Path(tempfile.mkdtemp()) / "wt"), "fast/hw/003")  # checked out elsewhere: `branch -f` refuses
        out = change(self.proj, self.repo, "undo", cid="C1")
        self.assertIn("reset these branches by hand, then sync:", out)
        self.assertIn(f"  git branch -f fast/hw/003 {tip}", out)
        self.assertEqual(self.lin.issues["ENG-4"]["status"], "Todo")  # the Linear step still ran
        self.assertIn("C2 open: undo C1", out)
        self.assertEqual(fc.fc_change.open_change(self.proj), "C2")

    def test_blockers_are_set_to_exactly_the_snapshot(self):
        self.lin.issues["ENG-4"]["blockedBy"] = ["ENG-3", "ENG-5"]
        fc.fc_linear.linear_blocked_by(fc.fc_linear.linear_issue("ENG-4"), ["ENG-3"])
        self.assertEqual(self.lin.issues["ENG-4"]["blockedBy"], ["ENG-3"])


class ChangeBrief(unittest.TestCase):
    def setUp(self):
        self.proj = project()
        self.repo, _ = git_repo(())
        p = mock.patch.object(fc.fc_core, "say", lambda m: None); p.start(); self.addCleanup(p.stop)
        synced(self.proj, MAP, BEFORE)

    def brief(self, cid, extra=None):
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", lambda a: self.proj), mock.patch.object(fc.fc_herdr, "project_worktree", lambda p: self.repo), \
                redirect_stdout(out):
            fc.fc_briefs.cmd_architect(SimpleNamespace(extra=extra, change=cid))
        return out.getvalue()

    def test_the_change_architect_asks_on_its_own_page(self):
        change(self.proj, self.repo, "start", why="localStorage is wiped by the sandbox")
        brief = self.brief("C1", extra="held: 003, 004")
        page = str(self.proj / "lavish" / "change-C1.html")
        self.assertIn("Text by default", brief)
        self.assertIn(f"npx -y lavish-axi poll {page}", brief)
        self.assertIn("localStorage is wiped by the sandbox", brief)
        self.assertIn("its `evidence` field", brief)
        self.assertIn("`compose decide --change C1`", brief)
        self.assertIn("struck through, `~~<old line>~~ (C1)`", brief)
        self.assertIn("an answer that keeps it strikes nothing", brief)
        self.assertIn("`- C1-1: <question> (replaces <the D id it would replace>) - Recommendation:", " ".join(brief.split()))
        self.assertIn("Do not touch tasks, stories, or the plan", brief)
        self.assertTrue(brief.rstrip().endswith("held: 003, 004"))

    def test_only_an_open_change_gets_a_brief(self):
        with self.assertRaises(SystemExit):
            self.brief("C9")


class BranchNames(unittest.TestCase):
    """status, done and change name task branches compose/hw-NNN in a new project and keep fast/hw/NNN in a legacy one."""
    def setUp(self):
        self.proj = project()
        self.repo, self.git = git_repo(())
        for mod, name, value in ((fc.fc_core, "say", lambda m: None), (fc.fc_config, "LINEAR_KEY", None),
                                 (fc.fc_stack, "register_stack", lambda *a, **k: None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        synced(self.proj, MAP, BEFORE)

    def branch(self, name, msg="work"):
        """Point `name` at a new commit on top of main, so it is unmerged. Returns the commit."""
        sha = self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", "main^{tree}", "-p", "main", "-m", msg)
        self.git("branch", "-f", name, sha)
        return sha

    def test_status_reads_the_task_branch_by_its_project_name(self):
        for config, name in ((None, "compose/hw-003"), ({"branch": "team/hw"}, "team/hw-003"), (None, "fast/hw/003")):
            with self.subTest(name):
                self.setUp()
                if config:
                    (self.proj / "verify.json").write_text(json.dumps(config))
                (self.proj / "results" / "003.json").write_text(json.dumps({"status": "passed"}))
                sha = self.branch(name)
                row = fc.fc_status.task_state(self.repo, self.proj, task("003"), self.git("rev-parse", "main"))
                self.assertEqual((row["state"], row["note"]), ("passed", name))
                self.assertEqual(fc.fc_status.task_state(self.repo, self.proj, task("003"), sha)["state"], "merged")

    def test_done_records_the_helper_branch_and_keeps_one_already_there(self):
        for legacy, name in ((False, "compose/hw-003"), (True, "fast/hw/003")):
            with self.subTest(name):
                self.setUp()
                if legacy:
                    self.branch("fast/hw/004")  # another task's legacy branch makes the whole project legacy
                (self.proj / "results" / "004.json").write_text(json.dumps({"status": "failed", "branch": "kept/004"}))
                with mock.patch.object(fc.fc_core, "project_dir", lambda a: self.proj), redirect_stdout(io.StringIO()):
                    fc.fc_status.cmd_done(SimpleNamespace(repo=str(self.repo), project="hw", only="003,004", note="by hand"))
                read = lambda t: json.loads((self.proj / "results" / f"{t}.json").read_text())
                self.assertEqual((read("003")["branch"], read("004")["branch"]), (name, "kept/004"))

    def test_a_change_snapshots_new_name_tips_and_its_undo_resets_them(self):
        old = self.branch("compose/hw-003", "first build")
        self.branch("compose/hw-004")
        change(self.proj, self.repo, "start", why="localStorage is wiped by the sandbox")
        rec = fc.fc_change.load_change(self.proj, "C1")
        self.assertEqual(rec["tips"], {"003": old, "004": self.git("rev-parse", "compose/hw-004")})
        self.assertEqual(self.git("rev-parse", "refs/compose/hw/C1/003"), old)
        self.branch("compose/hw-003", "rebuilt")  # the re-plan redid 003
        synced(self.proj, MAP_AFTER, AFTER, ahead=2)
        change(self.proj, self.repo, "done")
        synced(self.proj, MAP_AFTER, AFTER, ahead=3)
        change(self.proj, self.repo, "undo", cid="C1")
        self.assertEqual(self.git("rev-parse", "compose/hw-003"), old)
        self.assertNotIn("fast/hw/003", self.git("branch", "--list"))


class ClaudeCap(unittest.TestCase):
    """`claude_parallel` caps the Claude Code tasks `run` has at once, beside the pi ones, whatever `parallel` is."""

    def setUp(self):
        self.proj = project()
        with_tasks(self.proj, [{**task(t), "model": m} for t, m in (("001", "claude:claude-opus-5-5"), ("002", "claude"), ("003", "claude:claude-opus-5-5"),
                                                                     ("004", "pi:x/y"), ("005", "claude:claude-opus-5-5"), ("006", "pi:x/y"))])
        self.live, self.peak, self.lock = set(), {"claude": 0, "all": 0}, threading.Lock()
        for mod, name, value in ((fc.fc_core, "say", lambda m: None), (fc.fc_linear, "linear_note", lambda meta, body: None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def during(self, tid):
        with self.lock:
            self.live.add(tid)
            self.peak["all"] = max(self.peak["all"], len(self.live))
            self.peak["claude"] = max(self.peak["claude"], len([t for t in self.live if t in ("001", "002", "003", "005")]))
        time.sleep(0.3)
        with self.lock:
            self.live.discard(tid)

    def test_no_more_claude_tasks_run_at_once_than_the_cap_while_pi_tasks_fill_the_other_slots(self):
        with mock.patch.object(fc.fc_config, "CLAUDE_PARALLEL", 2):
            calls, _, _ = run_all(self.proj, during=self.during, parallel=6)
        self.assertEqual(sorted(calls), ["001", "002", "003", "004", "005", "006"])
        self.assertEqual(self.peak["claude"], 2)
        self.assertGreaterEqual(self.peak["all"], 4)  # the pi tasks did not wait for the Claude ones

    def test_claude_capped_counts_the_running_ones_and_keeps_the_order(self):
        metas = {t: {"model": m} for t, m in (("a", "claude"), ("b", "pi:x/y"), ("c", "claude:z"), ("d", "claude"))}
        with mock.patch.object(fc.fc_config, "CLAUDE_PARALLEL", 2):
            self.assertEqual(fc.fc_run.claude_capped(["a", "b", "c", "d"], [], 4, metas), ["a", "b", "c"])
            self.assertEqual(fc.fc_run.claude_capped(["a", "b", "c", "d"], ["claude"], 4, metas), ["a", "b"])
            self.assertEqual(fc.fc_run.claude_capped(["a", "b", "c", "d"], ["pi:x/y", "claude", "claude"], 3, metas), ["b"])
            self.assertEqual(fc.fc_run.claude_capped(["a", "b", "c"], [], 1, metas), ["a"])


if __name__ == "__main__":
    unittest.main()
