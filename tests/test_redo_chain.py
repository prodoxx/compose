import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, subprocess, tempfile, time, unittest
from pathlib import Path
from unittest import mock
import sys
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

RUN_TASKS = fc.fc_run.run_tasks  # the tests that stub it out keep the real one for the ones that run it

ENV = {"HERDR_ENV": "", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class KeepFinishedLayers(unittest.TestCase):
    """A plain `run` re-enters every task: a layer that passed, or that a teammate finished by hand on top of its pass, keeps its
    branch. Rebuilding a hand-done layer threw away the teammate's commit, so the team lead could not record it."""
    def sh(self, cmd, *a, **k):  # run_task prunes stale clones first; nothing else may ask git
        self.assertEqual(cmd, ["git", "worktree", "prune"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        os.environ.pop("COMPOSE_REDO", None)
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True); (self.proj / "worktrees").mkdir()
        for p in (mock.patch.object(fc.fc_core, "sh", self.sh), mock.patch.object(fc.fc_run, "require_committed"), mock.patch.object(fc.fc_run.fc_briefs, "test_open", return_value=False),
                  mock.patch.object(fc.fc_worktree, "task_branch", lambda repo, proj, tid: f"fast/p/{tid}"), mock.patch.object(fc.fc_run.fc_prove, "unproven", return_value=None),
                  mock.patch.object(fc.fc_run.fc_linear, "refresh_tasks"), mock.patch.object(fc.fc_run.fc_route, "route_tasks", return_value=({}, {})), mock.patch.object(fc.fc_run.fc_fast, "run_holds", return_value={})):
            p.start(); self.addCleanup(p.stop)

    def test_a_passed_or_hand_done_layer_keeps_its_branch(self):
        for status in ("passed", "hand_done"):
            (self.proj / "results" / "001.json").write_text(json.dumps({"id": "001", "status": status, "branch": "fast/p/001"}))
            with mock.patch.object(fc.fc_worktree, "replace_branch", side_effect=AssertionError(f"a {status} layer was rebuilt")), mock.patch.object(fc.fc_core, "say"):
                r = fc.fc_implement.run_task(self.repo, self.proj, {"id": "001", "title": "A", "files": [], "body": ""}, 1)
            self.assertEqual(r["status"], status)

    def test_a_hand_done_task_without_a_branch_is_kept_and_never_moves_linear_unless_redone(self):
        # its work was committed on the project branch, as groundwork: no branch
        (self.proj / "results" / "001.json").write_text(json.dumps({"id": "001", "status": "hand_done"}))
        task = {"id": "001", "title": "A", "files": [], "body": ""}
        with mock.patch.object(fc.fc_worktree, "replace_branch", side_effect=AssertionError("a hand_done task was rebuilt")), \
             mock.patch.object(fc.fc_linear, "linear_event", side_effect=AssertionError("a hand_done task moved Linear")), mock.patch.object(fc.fc_core, "say"):
            self.assertEqual(fc.fc_implement.run_task(self.repo, self.proj, task, 1)["status"], "hand_done")
        with mock.patch.dict(os.environ, {"COMPOSE_REDO": "1"}), mock.patch.object(fc.fc_worktree, "replace_branch", side_effect=RuntimeError("rebuilt")):
            with self.assertRaisesRegex(RuntimeError, "rebuilt"):
                fc.fc_implement.run_task(self.repo, self.proj, task, 1)

    def test_run_and_resume_schedule_only_the_tasks_that_need_work(self):
        states = {"001": "hand_done", "003": "passed", "005": "failed"}
        tasks = [{"id": t, "title": t, "files": [], "protected": [], "test": "true", "body": "", "model": "m",
                  **({"linear_state": "Canceled"} if t == "002" else {})} for t in ("001", "002", "003", "004", "005")]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        for t, st in states.items():
            (self.proj / "results" / f"{t}.json").write_text(json.dumps({"id": t, "status": st}))
        ran = []
        a = lambda **kw: mock.Mock(repo=str(self.repo), project="p", only=None, redo=False, **kw)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=self.repo), \
             mock.patch.object(fc.fc_run, "run_tasks", lambda repo, proj, ts, a: ran.append([m["id"] for m in ts])), mock.patch.object(fc.fc_core, "say"):
            fc.fc_run.cmd_run(a())
            (self.proj / "results" / "run.json").write_text(json.dumps({"pid": 999999, "started_at": "x", "tasks": [t["id"] for t in tasks], "args": {}}))
            fc.fc_run.cmd_resume(a())
            fc.fc_run.cmd_run(mock.Mock(repo=str(self.repo), project="p", only=None, redo=True))
        os.environ.pop("COMPOSE_REDO", None)
        self.assertEqual(ran, [["004", "005"], ["004", "005"], ["001", "003", "004", "005"]])  # Canceled stays out even on --redo

    def test_run_and_resume_take_the_flag_then_the_config_then_their_default(self):
        tasks = [{"id": "002", "title": "B", "files": [], "protected": [], "test": "true", "body": "", "model": "m"}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        halt = {"command": "run", "remaining": ["002"], "halted_at": "x", "provider": "p", "args": {"attempts": 3, "parallel": 4, "trunk": None, "no_stack": False}}
        got = []
        def go(cmd, flag, config):
            (self.proj / "results" / "resume.json").write_text(json.dumps(halt))
            with mock.patch.object(fc.fc_config, "PARALLEL", config):
                getattr(fc.fc_run, f"cmd_{cmd}")(mock.Mock(repo=str(self.repo), project="p", only=None, redo=False, parallel=flag))
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_core, "say"), \
             mock.patch.object(fc.fc_run, "run_tasks", lambda repo, proj, ts, a: got.append(a.parallel)):
            for cmd in ("run", "resume"):
                for flag, config in ((None, None), (None, 20), (7, 20)):
                    go(cmd, flag, config)
        self.assertEqual(got, [3, 20, 7, 4, 20, 7])  # resume with neither keeps the halted run's own value

    def test_resume_lists_only_the_tasks_it_runs(self):
        tasks = [{"id": t, "title": t, "files": [], "protected": [], "test": "true", "body": "", "model": "m"} for t in ("002", "003", "004")]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        (self.proj / "results" / "003.json").write_text(json.dumps({"id": "003", "status": "passed"}))  # passed after the halt was saved
        (self.proj / "results" / "resume.json").write_text(json.dumps({"command": "run", "remaining": ["002", "003", "004"], "halted_at": "x", "provider": "p",
                                                                       "args": {"attempts": 3, "parallel": 3, "trunk": None, "no_stack": False}}))
        said = []
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_core, "say", said.append), \
             mock.patch.object(fc.fc_run, "run_tasks", lambda repo, proj, ts, a: None):
            fc.fc_run.cmd_resume(mock.Mock(repo=str(self.repo), project="p"))
        self.assertIn("resume 2 task(s): 002, 004", said)


class RunDirtyCheck(unittest.TestCase):
    """`run` and `resume` need the tests committed, so they refuse a dirty project worktree and name what is dirty. A graph
    tool's session hook rewriting a tracked .claude/settings.json is no change of the user's and blocks neither."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        tasks = [{"id": "001", "title": "A", "files": [], "protected": [], "test": "true", "body": "", "model": "m"}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        self.ran, self.dirty = [], []
        for p in (mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_core, "say"),
                  mock.patch.object(fc.fc_worktree, "dirty_paths", lambda repo: self.dirty), mock.patch.object(fc.fc_init, "start_problems", lambda repo, tree, hint, proj: (
                      [f"uncommitted changes ({', '.join(fc.fc_worktree.dirty_paths(repo))}): {hint}"] if tree and fc.fc_worktree.dirty_paths(repo) else [], [])),
                  mock.patch.object(fc.fc_init.fc_checks, "battery_stop", return_value=None), mock.patch.dict(os.environ, {"COMPOSE_NO_PREFLIGHT": "1"}),
                  mock.patch.object(fc.fc_run.fc_briefs, "test_open", return_value=False), mock.patch.object(fc.fc_worktree, "task_branch", lambda repo, proj, tid: f"fast/p/{tid}"),
                  mock.patch.object(fc.fc_run.fc_prove, "unproven", return_value=None), mock.patch.object(fc.fc_run.fc_linear, "refresh_tasks"),
                  mock.patch.object(fc.fc_run.fc_route, "route_tasks", return_value=({}, {})), mock.patch.object(fc.fc_run.fc_fast, "run_holds", return_value={}),
                  mock.patch.object(fc.fc_run, "run_tasks", lambda repo, proj, ts, a: self.ran.append([m["id"] for m in ts]))):
            p.start(); self.addCleanup(p.stop)

    def go(self, cmd):
        if cmd == "resume":
            (self.proj / "results" / "resume.json").write_text(json.dumps({"command": "run", "remaining": ["001"], "halted_at": "x", "provider": "p",
                                                                           "args": {"attempts": 3, "parallel": 3, "trunk": None, "no_stack": False}}))
        getattr(fc.fc_run, f"cmd_{cmd}")(mock.Mock(repo=str(self.repo), project="p", only=None, redo=False, parallel=None))

    def test_run_and_resume_name_the_dirty_files_they_refuse_on(self):
        self.dirty = ["b.txt", "new_test.py"]
        for cmd in ("run", "resume"):
            with self.assertRaises(SystemExit) as e:
                self.go(cmd)
            self.assertIn("uncommitted changes (b.txt, new_test.py)", str(e.exception))
        self.assertEqual(self.ran, [])

    def test_run_and_resume_go_ahead_when_nothing_of_the_users_is_dirty(self):
        self.go("run"); self.go("resume")
        self.assertEqual(self.ran, [["001"], ["001"]])


class DirtyPaths(unittest.TestCase):
    """What `git status` shows, minus the agent wiring and the tools' own leftovers (a graph folder, wiring in a repo that had none,
    a stray .ignore): those are not the user's change."""
    def paths(self, *entries):
        out = subprocess.CompletedProcess([], 0, "".join(e + "\0" for e in entries), "")
        with mock.patch.object(fc.fc_core, "sh", return_value=out):
            return fc.fc_worktree.dirty_paths(Path("."))

    def test_a_rewritten_agent_wiring_file_is_not_dirty(self):
        self.assertEqual(self.paths(" M .claude/settings.json", " M .claude/settings.local.json"), [])

    def test_the_tools_own_leftovers_are_not_dirty_but_only_at_the_root(self):
        self.assertEqual(self.paths("?? graft/map.json", "?? .trail/cache/x.json", "?? .compose/q/notes.md", "?? .ignore"), [])
        self.assertEqual(self.paths("?? sub/graft"), ["sub/graft"])

    def test_a_rename_lists_only_its_new_path(self):
        self.assertEqual(self.paths("R  b.txt", "a.txt", "?? new_test.py"), ["b.txt", "new_test.py"])


class ResumeAfterRedo(unittest.TestCase):
    """`resume` of a `run --redo` that stopped early rebuilds the tasks that had not started: their results still read passed
    from before the redo, but their branches were set aside."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        tasks = [{"id": t, "title": t, "files": [], "protected": [], "test": "true", "body": "", "model": "m", **({"linear_state": "Done"} if t == "004" else {})}
                 for t in ("001", "002", "003", "004")]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        for t in ("001", "002", "003", "004"):  # all passed before the redo, an hour ago
            f = self.proj / "results" / f"{t}.json"
            f.write_text(json.dumps({"id": t, "status": "passed"}))
            os.utime(f, (time.time() - 3600,) * 2)
        self.ran = []
        def run_tasks(repo, proj, ts, a):
            self.ran.append(([m["id"] for m in ts], os.environ.get("COMPOSE_REDO")))
        for p in (mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_core, "say"),
                  mock.patch.object(fc.fc_run, "run_tasks", run_tasks), mock.patch.object(fc.fc_run, "require_committed")):
            p.start(); self.addCleanup(p.stop)

    def resume(self):
        fc.fc_run.cmd_resume(mock.Mock(repo=str(self.repo), project="p", parallel=None))

    def test_resume_after_a_payment_halt_rebuilds_every_task_the_banner_counted(self):
        halted = []
        results = lambda: {"001": {"id": "001", "status": "payment_required", "attempts": 1, "seconds": 0.0, "cost": 0.0, "branch": "-",
                                   "log": [{"payment_required": {"provider": "p", "message": "m"}}]}}
        real = fc.fc_run
        with mock.patch.object(real, "preflight"), mock.patch.object(real.fc_proc, "keep_awake"), \
             mock.patch.object(real.fc_blockers, "released", return_value=({}, {})), mock.patch.object(real.fc_herdr, "herdr_space"), \
             mock.patch.object(real.fc_implement, "run_task", lambda repo, proj, meta, attempts: results()[meta["id"]]), \
             mock.patch.dict(os.environ, {"COMPOSE_REDO": "1"}):
            tasks = [m for m in fc.fc_tasks.load_tasks(self.proj, required=True) if m["id"] != "004"]
            with self.assertRaises(SystemExit) as e:
                RUN_TASKS(self.repo, self.proj, tasks, mock.Mock(attempts=3, parallel=1, trunk="main", no_stack=True))
            self.assertEqual(e.exception.code, 3)
        self.assertEqual(json.loads((self.proj / "results" / "resume.json").read_text())["remaining"], ["001", "002", "003"])
        self.resume()
        self.assertEqual(self.ran, [(["001", "002", "003"], "1")])  # Done on Linear (004) stays out

    def test_resume_of_an_interrupted_redo_keeps_only_what_passed_in_that_run(self):
        started = time.time() - 600
        fresh = self.proj / "results" / "001.json"  # rebuilt and passed in the run, after it started
        fresh.write_text(json.dumps({"id": "001", "status": "passed"}))
        (self.proj / "results" / "run.json").write_text(json.dumps({"pid": 999999, "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "tasks": ["001", "002", "003"], "args": {"attempts": 3, "parallel": 3, "trunk": None, "no_stack": True, "redo": True}}))
        self.resume()
        self.assertEqual(self.ran, [(["002", "003"], "1")])

    def test_resume_without_redo_still_keeps_every_passed_task(self):
        (self.proj / "results" / "resume.json").write_text(json.dumps({"command": "run", "remaining": ["001", "002"], "halted_at": "x", "provider": "p",
            "args": {"attempts": 3, "parallel": 3, "trunk": None, "no_stack": True}}))
        with self.assertRaises(SystemExit):
            self.resume()
        self.assertEqual(self.ran, [])


class RunChain(unittest.TestCase):
    """What run_tasks does with the stack before it dispatches: the chain work (linearize, replace_branch) is faked at its function."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {**ENV, "COMPOSE_REDO": ""}); self.env.start(); self.addCleanup(self.env.stop)
        self.proj = Path(tempfile.mkdtemp()).resolve() / "p"; (self.proj / "results").mkdir(parents=True)
        self.calls, fc.fc_layers.STACK_STOP = [], ""
        self.addCleanup(setattr, fc.fc_layers, "STACK_STOP", "")
        self.ran = []
        def run_task(repo, proj, meta, attempts):
            self.ran.append(meta["id"])
            if meta["id"] in self.unchain:
                raise RuntimeError(f"after: fast/p/{meta['after']} could not be chained: fast/p/{meta['after']} conflicts")
            return {"id": meta["id"], "title": meta["id"], "status": "passed", "attempts": 1, "seconds": 1.0, "cost": 0.0, "branch": f"fast/p/{meta['id']}"}
        def linearize(*a, **k):
            self.calls.append("linearize")
            fc.fc_layers.STACK_STOP = self.stop
        self.stop, self.unchain = "", ()
        for p in (mock.patch.object(fc.fc_core, "say"), mock.patch.object(fc.fc_run, "preflight", lambda m: None), mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None),
                  mock.patch.object(fc.fc_core, "current_branch", return_value="trunk"), mock.patch.object(fc.fc_blockers, "released", return_value=({}, {})),
                  mock.patch.object(fc.fc_worktree, "stack_order", return_value=["fast/p/010"]), mock.patch.object(fc.fc_worktree, "stack_worktree"),
                  mock.patch.object(fc.fc_worktree, "task_branch", lambda repo, proj, tid: f"fast/p/{tid}"),
                  mock.patch.object(fc.fc_worktree, "replace_branch", lambda repo, proj, b: self.calls.append(f"replace {b}")),
                  mock.patch.object(fc.fc_layers, "linearize", linearize), mock.patch.object(fc.fc_implement, "run_task", run_task),
                  mock.patch.object(fc.fc_run, "triage", lambda *x: None), mock.patch.object(fc.fc_run.fc_fast, "run_events", return_value=[]), mock.patch.object(fc.fc_run.fc_ledger, "record_task"), mock.patch.object(fc.fc_stack, "register_stack"), mock.patch("builtins.print")):
            p.start(); self.addCleanup(p.stop)

    def run_ids(self, ids, after=None, no_stack=False):
        tasks = [{"id": t, "title": t, "after": (after or {}).get(t, ""), "files": [], "protected": [], "test": "true", "model": "m", "body": ""} for t in ids]
        with self.assertRaises(SystemExit) as e:
            fc.fc_run.run_tasks(self.proj.parent, self.proj, tasks, mock.Mock(trunk="trunk", attempts=1, parallel=1, no_stack=no_stack))
        return e.exception

    def test_a_task_the_run_rebuilds_is_set_aside_before_the_chain_is_rebased_and_a_finished_one_is_not(self):
        (self.proj / "results" / "011.json").write_text(json.dumps({"id": "011", "status": "passed"}))
        self.assertEqual(self.run_ids(["010", "011"]).code, 0)
        self.assertEqual(self.calls[:2], ["replace fast/p/010", "linearize"])  # 010's old commits may clash with the trunk: they are gone before the rebase

    def test_a_chain_that_stops_stops_the_run_with_one_line_before_any_task_starts(self):
        self.stop = "fast/p/011 conflicts when rebased onto fast/p/010 in a.txt; the layers below it are kept. Resolve by hand: git rebase --onto fast/p/010 abc"
        self.assertEqual(str(self.run_ids(["012"])), f"stack  STOP  {self.stop}")
        self.assertEqual(self.ran, [])

    def test_no_stack_dispatches_without_rebasing_the_existing_layers(self):
        self.assertEqual(self.run_ids(["012"], no_stack=True).code, 0)
        self.assertEqual((self.calls, self.ran), ([], ["012"]))

    def test_a_task_whose_chain_stops_mid_run_is_unchained_and_the_run_still_ends_with_its_summary(self):
        after = {"013": "012", "014": "013"}
        self.unchain = ("013",)
        out = []
        with mock.patch.object(fc.fc_core, "say", out.append):
            e = self.run_ids(["012", "013", "014"], after)
        self.assertEqual((e.code, self.ran), (1, ["012", "013"]))
        self.assertEqual({r: json.loads((self.proj / "results" / f"{r}.json").read_text())["status"] for r in ("013",)}, {"013": "unchained"})
        self.assertTrue(any(l.startswith("013 STOP  after: fast/p/012 could not be chained") for l in out), out)
        self.assertTrue(any(l.startswith("run done   1/3 passed") for l in out), out)
        self.assertFalse((self.proj / "results" / "run.json").exists())  # watch sees a finished run, not a crashed one


class UnprovenRestart(unittest.TestCase):
    """Resume or run --only of an unproven task restarts only its regression test writer, never the fix."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.repo / ".compose" / "p"; (self.proj / "results").mkdir(parents=True)
        self.wt = self.repo / "wt"; self.wt.mkdir()

    def save(self, tid, status="unproven", worktree=None):
        (self.proj / "results" / f"{tid}.json").write_text(json.dumps({"id": tid, "status": status, "branch": f"fast/p/{tid}", "worktree": str(worktree or self.wt)}))
        return json.loads((self.proj / "results" / f"{tid}.json").read_text())

    def run_ids(self, ids, redo=False):
        tasks = [{"id": t, "title": t, "after": "", "files": [], "protected": [], "test": "true", "model": "m", "body": ""} for t in ids]
        done = lambda *x, **k: {"id": x[2]["id"], "title": "t", "status": "passed", "attempts": 1, "seconds": 0.0, "cost": 0.0, "branch": "b"}
        a = mock.Mock(trunk="trunk", attempts=3, parallel=1, no_stack=True, redo=redo)
        with mock.patch.object(fc.fc_core, "say") as say, mock.patch.object(fc.fc_run, "preflight", lambda m: None), mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None), \
             mock.patch.object(fc.fc_run, "triage", lambda *x: None), mock.patch("builtins.print"), \
             mock.patch.object(fc.fc_run.fc_fast, "run_events", return_value=[]), mock.patch.object(fc.fc_run.fc_ledger, "record_task"), \
             mock.patch.object(fc.fc_implement, "run_task", mock.Mock(side_effect=lambda r, p, m, n, **k: done(r, p, m))) as run_task, \
             mock.patch.object(fc.fc_run.fc_regress, "regress", mock.Mock(side_effect=lambda r, p, m, pr, n: done(r, p, m))) as regress, self.assertRaises(SystemExit):
            fc.fc_run.run_tasks(self.repo, self.proj, tasks, a)
        return run_task, regress, [str(c.args[0]) for c in say.call_args_list]

    def test_an_unproven_task_with_a_kept_worktree_restarts_only_its_writer(self):
        saved = self.save("001")
        run_task, regress, said = self.run_ids(["001"])
        run_task.assert_not_called()
        regress.assert_called_once(); args = regress.call_args.args
        self.assertEqual((args[0], args[1], args[2]["id"], args[3], args[4]), (self.repo, self.proj, "001", saved, 3))
        self.assertIn("001 regress  fix kept on fast/p/001; restarting its regression test writer", said)

    def test_redo_runs_the_fix_again(self):
        self.save("001")
        run_task, regress, said = self.run_ids(["001"], redo=True)
        regress.assert_not_called(); run_task.assert_called_once()
        self.assertFalse(any("regress " in l for l in said))

    def test_only_a_kept_unproven_task_restarts_its_writer(self):
        self.save("001", worktree=self.repo / "gone"); self.save("002", status="failed"); self.save("003"); self.save("004", worktree=self.repo / "gone2")
        run_task, regress, said = self.run_ids(["001", "002", "003", "004", "005"])
        self.assertEqual([c.args[2]["id"] for c in regress.call_args_list], ["003"])
        self.assertEqual(sorted(c.args[2]["id"] for c in run_task.call_args_list), ["001", "002", "004", "005"])
        self.assertEqual(len([l for l in said if " regress " in l]), 1)

