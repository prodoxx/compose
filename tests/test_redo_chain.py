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


class RedoMidChain(unittest.TestCase):
    """Rebuilding a layer in the middle of the stack: the layers above still carry its old commit, and the chain rebase drops it."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start()
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.git("init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("base\n")
        self.git("add", "-A"); self.git("commit", "-qm", "init")
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True); (self.proj / "worktrees").mkdir()
        (self.repo / ".git" / "info" / "exclude").write_text("/.compose/\n")
        after = {"011": "010", "012": "011", "005": "012"}
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": t, "after": after.get(t, "")} for t in ("010", "011", "012", "005")]}))
        for t, base in (("010", "main"), ("011", "fast/p/010"), ("012", "fast/p/011"), ("005", "fast/p/012")):
            self.layer(t, base, "c.txt" if t == "011" else f"{t}.txt", "old\n")

    def tearDown(self):
        self.env.stop()

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def layer(self, tid, base, name, text):
        self.git("checkout", "-q", "-b", f"fast/p/{tid}", base)
        (self.repo / name).write_text(text)
        self.git("add", "-A"); self.git("commit", "-qm", f"task {tid}")
        self.git("checkout", "-q", "main")

    def test_layers_above_a_rebuilt_layer_replay_onto_it_without_its_old_commit(self):
        fc.fc_worktree.replace_branch(self.repo, self.proj, "fast/p/011")
        self.layer("011", "fast/p/010", "c.txt", "new\n")
        chain = fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.assertEqual(chain, [f"fast/p/{t}" for t in ("010", "011", "012", "005")])
        self.assertEqual(self.git("show", "fast/p/005:c.txt"), "new")
        self.assertEqual(self.git("rev-list", "--count", "main..fast/p/005"), "4")  # one commit per layer, the old 011 is gone
        self.assertEqual(self.git("for-each-ref", "refs/compose/replaced/"), "")  # nothing carries the old tip any more

    def test_every_layer_above_a_layer_rewritten_by_hand_replays_only_its_own_commits(self):
        # 011 rebased or reset by hand before this pass: no replaced ref records its old tip, only its reflog does
        self.git("checkout", "-q", "-B", "fast/p/011", "fast/p/010"); (self.repo / "c.txt").write_text("new\n")
        self.git("add", "-A"); self.git("commit", "-qm", "task 011 redone"); self.git("checkout", "-q", "main")
        chain = fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.assertEqual(fc.fc_layers.STACK_STOP, "")
        self.assertEqual(chain, [f"fast/p/{t}" for t in ("010", "011", "012", "005")])
        self.assertEqual([self.git("rev-list", "--count", f"main..fast/p/{t}") for t in ("012", "005")], ["3", "4"])
        self.assertEqual(self.git("show", "fast/p/005:c.txt"), "new")

    def test_a_commit_moved_by_hand_from_a_layer_to_the_one_above_it_survives_the_chain_rebase(self):
        # 011's commit folded onto 012 by hand: 011 reset back to 010, so its reflog still holds the commit 012 now owns
        self.git("branch", "-f", "fast/p/011", "fast/p/010")
        self.git("checkout", "-q", "main"); (self.repo / "m.txt").write_text("moved\n")
        self.git("add", "-A"); self.git("commit", "-qm", "main moves")
        chain = fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.assertEqual(fc.fc_layers.STACK_STOP, "")
        self.assertEqual(chain, [f"fast/p/{t}" for t in ("010", "011", "012", "005")])
        self.assertEqual(self.git("log", "--format=%s", "main..fast/p/005"), "task 005\ntask 012\ntask 011\ntask 010")

    def test_a_layer_above_one_resolved_by_hand_between_runs_replays_only_its_own_commits(self):
        # 011 rewritten, then 012 rebased onto it by hand after a stopped run: 005 still carries the old 011 and the old 012
        self.git("checkout", "-q", "-B", "fast/p/011", "fast/p/010"); (self.repo / "c.txt").write_text("new\n")
        self.git("add", "-A"); self.git("commit", "-qm", "task 011 redone")
        self.git("checkout", "-q", "-B", "fast/p/012", "fast/p/011"); (self.repo / "012.txt").write_text("old\n")
        self.git("add", "-A"); self.git("commit", "-qm", "task 012"); self.git("checkout", "-q", "main")
        chain = fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.assertEqual(fc.fc_layers.STACK_STOP, "")
        self.assertEqual(chain, [f"fast/p/{t}" for t in ("010", "011", "012", "005")])
        self.assertEqual(self.git("rev-list", "--count", "main..fast/p/005"), "4")
        self.assertEqual(self.git("show", "fast/p/005:c.txt"), "new")

    def test_a_layer_built_on_a_chained_layer_below_its_new_neighbour_replays_only_its_own_commits(self):
        # 012 rebuilt from a clone, as run writes it: its reflog holds only its tip. 005 still sits on 011, which stays chained.
        self.git("branch", "-D", "fast/p/012", "fast/p/005"); self.layer("005", "fast/p/011", "005.txt", "old\n")
        clone = Path(tempfile.mkdtemp()).resolve() / "c"
        subprocess.run(["git", "clone", "-q", "--shared", str(self.repo), str(clone)], check=True)
        cgit = lambda *a: subprocess.run(["git", *a], cwd=clone, check=True, capture_output=True, text=True)
        cgit("checkout", "-q", "-b", "fast/p/012", "origin/fast/p/011"); (clone / "c.txt").write_text("twelve\n"); cgit("commit", "-qam", "task 012 edits c")
        self.git("fetch", "-q", str(clone), "fast/p/012:fast/p/012")
        chain = fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.assertEqual(fc.fc_layers.STACK_STOP, "")
        self.assertEqual(chain, [f"fast/p/{t}" for t in ("010", "011", "012", "005")])
        self.assertEqual(self.git("rev-list", "--count", "main..fast/p/005"), "4")
        self.assertEqual(self.git("show", "fast/p/005:c.txt"), "twelve")

    def test_a_stacked_task_rebases_only_the_layers_through_its_dependency_and_leaves_the_ones_above_for_the_redone_layer(self):
        # 013 sits on 012 without needing it (its id keeps it above 012: a lower id would put it right above 010).
        # run --only 011,012 --redo --no-stack: while 012 is rebuilt on the new 011, 013 keeps the old 011 and 012 (moving it onto 011 would leave it off 012 for good, as no stack pass follows)
        self.git("branch", "-m", "fast/p/005", "fast/p/013")
        tasks = json.loads((self.proj / "results" / "tasks.json").read_text())
        tasks["tasks"] = [{**t, "id": "013" if t["id"] == "005" else t["id"], "after": "010" if t["id"] == "005" else t["after"]} for t in tasks["tasks"]]
        (self.proj / "results" / "tasks.json").write_text(json.dumps(tasks))
        fc.fc_worktree.replace_branch(self.repo, self.proj, "fast/p/011")
        self.layer("011", "fast/p/010", "c.txt", "new\n")
        fc.fc_worktree.replace_branch(self.repo, self.proj, "fast/p/012")
        above = self.git("rev-parse", "fast/p/013")
        base, _ = fc.fc_implement.task_base(self.repo, self.proj, {"id": "012", "after": "011"})
        self.assertEqual(base, "fast/p/011")
        self.assertEqual(self.git("rev-parse", "fast/p/013"), above)
        self.layer("012", "fast/p/011", "012.txt", "new\n")
        chain = fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.assertEqual(fc.fc_layers.STACK_STOP, "")
        self.assertEqual(chain, [f"fast/p/{t}" for t in ("010", "011", "012", "013")])
        self.assertEqual(self.git("log", "--format=%s", "main..fast/p/013"), "task 005\ntask 012\ntask 011\ntask 010")
        self.assertEqual(self.git("for-each-ref", "refs/compose/replaced/"), "")

    def test_layers_above_a_replaced_layer_that_moved_up_the_chain_drop_its_old_commit(self):
        # a re-plan put 011 after 005, then --redo replaced it: 012 and 005 still sit on the old 011 while it waits for 005
        tasks = json.loads((self.proj / "results" / "tasks.json").read_text())
        tasks["tasks"] = [{**t, "after": {"011": "005", "012": "010"}.get(t["id"], t["after"])} for t in tasks["tasks"]]
        (self.proj / "results" / "tasks.json").write_text(json.dumps(tasks))
        fc.fc_worktree.replace_branch(self.repo, self.proj, "fast/p/011")
        chain = fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.assertEqual(fc.fc_layers.STACK_STOP, "")
        self.assertEqual(chain, [f"fast/p/{t}" for t in ("010", "012", "005")])
        self.assertEqual(self.git("log", "--format=%s", "main..fast/p/005"), "task 005\ntask 012\ntask 010")
        self.assertEqual(self.git("for-each-ref", "refs/compose/replaced/"), "")

    def test_a_layer_above_two_redone_layers_replays_only_its_own_commit(self):
        # 013 sits on 012 without needing it. run --redo of 011 and 012: 013 is chained while 012 runs, then once 012 is rebuilt
        self.git("branch", "-D", "fast/p/005")
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [
            {"id": "010", "after": ""}, {"id": "011", "after": "010"}, {"id": "012", "after": "011"}, {"id": "013", "after": "011"}]}))
        self.layer("013", "fast/p/012", "013.txt", "old\n")
        fc.fc_worktree.replace_branch(self.repo, self.proj, "fast/p/011")
        self.layer("011", "fast/p/010", "c.txt", "new\n")
        fc.fc_worktree.replace_branch(self.repo, self.proj, "fast/p/012")
        fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.layer("012", "fast/p/011", "012.txt", "new\n")
        chain = fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.assertEqual(fc.fc_layers.STACK_STOP, "")
        self.assertEqual(chain, [f"fast/p/{t}" for t in ("010", "011", "012", "013")])
        self.assertEqual(self.git("log", "--format=%s", "main..fast/p/013"), "task 013\ntask 012\ntask 011\ntask 010")
        self.assertEqual(self.git("show", "fast/p/013:012.txt"), "new")

    def test_a_layer_built_on_layers_restacked_since_replays_only_its_own_commit_onto_the_layer_below_it(self):
        # 014 (after 012; its id keeps it above 013, which a lower id would not) was built while 010-012 sat on main; a later pass put 004 below them and 013 above 012, then 014 passed
        self.git("branch", "-m", "fast/p/005", "held")
        tasks = json.loads((self.proj / "results" / "tasks.json").read_text())
        tasks["tasks"] = [{**t, "id": "014" if t["id"] == "005" else t["id"]} for t in tasks["tasks"]]
        tasks["tasks"] += [{"id": "004", "after": ""}, {"id": "013", "after": "012"}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps(tasks))
        self.layer("004", "main", "004.txt", "new\n")
        fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.layer("013", "fast/p/012", "012.txt", "thirteen\n")
        self.git("branch", "-m", "held", "fast/p/014")
        chain = fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.assertEqual(fc.fc_layers.STACK_STOP, "")
        self.assertEqual(chain, [f"fast/p/{t}" for t in ("004", "010", "011", "012", "013", "014")])
        self.assertEqual(self.git("log", "--format=%s", "fast/p/013..fast/p/014"), "task 005")

    def test_a_task_built_on_a_low_layer_goes_to_its_own_place_and_moves_no_layer(self):
        # 006 after 010 starts on 010, as a run bases it, so it counts as many commits as 011; its place is still after 005
        tasks = json.loads((self.proj / "results" / "tasks.json").read_text())
        tasks["tasks"].append({"id": "006", "after": "010"})
        (self.proj / "results" / "tasks.json").write_text(json.dumps(tasks))
        tips = {t: self.git("rev-parse", f"fast/p/{t}") for t in ("010", "011", "012", "005")}
        base, _ = fc.fc_implement.task_base(self.repo, self.proj, {"id": "006", "after": "010"})
        self.layer("006", base, "006.txt", "new\n")
        chain = fc.fc_layers.linearize(self.repo, self.proj, "main", fc.fc_worktree.stack_worktree(self.repo, self.proj, "main"))
        self.assertEqual(fc.fc_layers.STACK_STOP, "")
        self.assertEqual(chain, [f"fast/p/{t}" for t in ("010", "011", "012", "005", "006")])
        self.assertEqual({t: self.git("rev-parse", f"fast/p/{t}") for t in tips}, tips)
        self.assertEqual(self.git("log", "--format=%s", "fast/p/005..fast/p/006"), "task 006")


class RedoLayersTheTrunkNowClashesWith(unittest.TestCase):
    """--redo rebuilds layers whose old commits clash with the trunk or with each other; no rebase before they are rebuilt may stop the run."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {**ENV, "COMPOSE_REDO": "1"}); self.env.start()
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.git("init", "-q", "-b", "trunk")
        (self.repo / "a.txt").write_text("base\n")
        self.git("add", "-A"); self.git("commit", "-qm", "init")
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True); (self.proj / "worktrees").mkdir()
        (self.repo / ".git" / "info" / "exclude").write_text("/.compose/\n")

    def tearDown(self):
        self.env.stop()

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def layer(self, tid, base, name, text):
        self.git("checkout", "-q", "-b", f"fast/p/{tid}", base)
        (self.repo / name).write_text(text)
        self.git("add", "-A"); self.git("commit", "-qm", f"task {tid}")
        self.git("checkout", "-q", "trunk")

    def run_tasks(self, ids, after, add, also=()):
        """run_tasks over `ids`, each rebuilt from its dependencies' tip as run_task does (add: the file it writes; also: layers outside the run, still in the plan)."""
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": t, "after": after.get(t, "")} for t in sorted([*ids, *also])]}))
        built = []
        def run_task(repo, proj, meta, attempts):
            tid = meta["id"]
            base, _ = fc.fc_implement.task_base(repo, proj, meta)
            fc.fc_worktree.replace_branch(repo, proj, f"fast/p/{tid}")
            self.layer(tid, base, add[tid], "rebuilt\n"); built.append(tid)
            return {"id": tid, "title": tid, "status": "passed", "attempts": 1, "seconds": 1.0, "cost": 0.0, "branch": f"fast/p/{tid}"}
        tasks = [{"id": t, "title": t, "after": after.get(t, ""), "files": [], "protected": [], "test": "true", "model": "m", "body": ""} for t in ids]
        a = mock.Mock(trunk="trunk", attempts=1, parallel=1, no_stack=False)
        with mock.patch.object(fc.fc_core, "say") as say, mock.patch.object(fc.fc_run, "preflight", lambda m: None), mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None), \
             mock.patch.object(fc.fc_implement, "run_task", run_task), mock.patch.object(fc.fc_run, "triage", lambda *x: None), mock.patch.object(fc.fc_stack, "register_stack"), \
             mock.patch("builtins.print"), self.assertRaises(SystemExit) as e:
            fc.fc_run.run_tasks(self.repo, self.proj, tasks, a)
        said = [str(c.args[0]) for c in say.call_args_list]
        self.assertFalse(any("STOP" in l for l in said), said)
        self.assertEqual((e.exception.code, built), (0, ids))

    def test_the_layer_about_to_be_rebuilt_is_not_rebased_onto_a_trunk_it_clashes_with(self):
        self.layer("010", "trunk", "h.txt", "task\n"); self.layer("011", "fast/p/010", "011.txt", "x\n")
        self.git("checkout", "-q", "trunk"); (self.repo / "h.txt").write_text("trunk\n"); self.git("add", "-A"); self.git("commit", "-qm", "trunk adds h")
        self.run_tasks(["010"], {"011": "010"}, {"010": "010.txt"}, also=["011"])
        self.assertEqual(self.git("rev-list", "--count", "trunk..fast/p/011"), "1")  # the layer above lost the old 010 commit

    def test_a_redo_layer_waiting_to_start_does_not_unchain_a_task_stacked_above_it(self):
        # 011 is rebuilt later; 014 starts first on 013, which adds the file 011's old commit also adds
        self.layer("011", "trunk", "x.txt", "old\n")
        self.run_tasks(["013", "014", "011"], {"014": "013"}, {"013": "x.txt", "014": "014.txt", "011": "011.txt"})


class KeepFinishedLayers(unittest.TestCase):
    """A plain `run` re-enters every task: a layer that passed, or that a teammate finished by hand on top of its pass, keeps its
    branch. Rebuilding a hand-done layer threw away the teammate's commit, so the team lead could not record it."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        os.environ.pop("COMPOSE_REDO", None)
        self.repo = Path(tempfile.mkdtemp()).resolve()
        git = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main"); (self.repo / "a.txt").write_text("base\n"); git("add", "-A"); git("commit", "-qm", "init")
        git("checkout", "-q", "-b", "fast/p/001"); (self.repo / "b.txt").write_text("teammate\n"); git("add", "-A"); git("commit", "-qm", "teammate's commit")
        git("checkout", "-q", "main")
        self.tip = git("rev-parse", "fast/p/001")
        self.git = git
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True); (self.proj / "worktrees").mkdir()

    def test_a_passed_or_hand_done_layer_keeps_its_branch(self):
        for status in ("passed", "hand_done"):
            (self.proj / "results" / "001.json").write_text(json.dumps({"id": "001", "status": status, "branch": "fast/p/001"}))
            with mock.patch.object(fc.fc_worktree, "replace_branch", side_effect=AssertionError(f"a {status} layer was rebuilt")), mock.patch.object(fc.fc_core, "say"):
                r = fc.fc_implement.run_task(self.repo, self.proj, {"id": "001", "title": "A", "files": [], "body": ""}, 1)
            self.assertEqual(r["status"], status)
            self.assertEqual(self.git("rev-parse", "fast/p/001"), self.tip)

    def test_a_hand_done_task_without_a_branch_is_kept_and_never_moves_linear_unless_redone(self):
        self.git("branch", "-D", "fast/p/001")  # its work was committed on the project branch, as groundwork
        (self.proj / "results" / "001.json").write_text(json.dumps({"id": "001", "status": "hand_done"}))
        task = {"id": "001", "title": "A", "files": [], "body": ""}
        with mock.patch.object(fc.fc_worktree, "replace_branch", side_effect=AssertionError("a hand_done task was rebuilt")), \
             mock.patch.object(fc.fc_linear, "linear_event", side_effect=AssertionError("a hand_done task moved Linear")), mock.patch.object(fc.fc_core, "say"):
            self.assertEqual(fc.fc_implement.run_task(self.repo, self.proj, task, 1)["status"], "hand_done")
        with mock.patch.dict(os.environ, {"COMPOSE_REDO": "1"}), mock.patch.object(fc.fc_worktree, "replace_branch", side_effect=RuntimeError("rebuilt")):
            with self.assertRaisesRegex(RuntimeError, "rebuilt"):
                fc.fc_implement.run_task(self.repo, self.proj, task, 1)

    def test_run_and_resume_schedule_only_the_tasks_that_need_work(self):
        (self.repo / ".git" / "info" / "exclude").write_text("/.compose/\n")
        self.git("branch", "-D", "fast/p/001")
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
        (self.repo / ".git" / "info" / "exclude").write_text("/.compose/\n")
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
        (self.repo / ".git" / "info" / "exclude").write_text("/.compose/\n")
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
        git = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main")
        (self.repo / ".claude").mkdir()
        for name in ("settings.json", "settings.local.json"):
            (self.repo / ".claude" / name).write_text('{"statusLine": {}}\n')
        (self.repo / "a.txt").write_text("base\n")
        git("add", "-A"); git("commit", "-qm", "init")
        (self.repo / ".git" / "info" / "exclude").write_text("/.compose/\n")
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        tasks = [{"id": "001", "title": "A", "files": [], "protected": [], "test": "true", "body": "", "model": "m"}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        self.ran = []
        for p in (mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_core, "say"),
                  mock.patch.object(fc.fc_run, "run_tasks", lambda repo, proj, ts, a: self.ran.append([m["id"] for m in ts]))):
            p.start(); self.addCleanup(p.stop)

    def go(self, cmd):
        if cmd == "resume":
            (self.proj / "results" / "resume.json").write_text(json.dumps({"command": "run", "remaining": ["001"], "halted_at": "x", "provider": "p",
                                                                           "args": {"attempts": 3, "parallel": 3, "trunk": None, "no_stack": False}}))
        getattr(fc.fc_run, f"cmd_{cmd}")(mock.Mock(repo=str(self.repo), project="p", only=None, redo=False, parallel=None))

    def test_a_rewritten_agent_wiring_file_does_not_block_run_or_resume(self):
        for name in ("settings.json", "settings.local.json"):
            (self.repo / ".claude" / name).write_text("{}\n")  # what graft's SessionStart hook does
        self.go("run"); self.go("resume")
        self.assertEqual(self.ran, [["001"], ["001"]])

    def test_the_tools_own_leftovers_do_not_block_run_or_resume(self):
        """What graft and trail leave behind (a graph folder, wiring in a repo that had none, a stray .ignore) is not the user's change."""
        for rel in ("graft/map.json", ".trail/cache/x.json", ".compose/q/notes.md", ".ignore"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("{}\n")
        (self.repo / ".claude" / "settings.local.json").write_text("{}\n")
        self.go("run"); self.go("resume")
        self.assertEqual(self.ran, [["001"], ["001"]])
        self.assertEqual(fc.fc_worktree.dirty_paths(self.repo), [])
        (self.repo / "sub").mkdir(); (self.repo / "sub" / "graft").write_text("x\n")
        self.assertEqual(fc.fc_worktree.dirty_paths(self.repo), ["sub/graft"])  # only the tools' own folders at the root

    def test_run_and_resume_name_the_dirty_files_they_refuse_on(self):
        (self.repo / ".claude" / "settings.json").write_text("{}\n")
        subprocess.run(["git", "mv", "a.txt", "b.txt"], cwd=self.repo, check=True)  # a rename lists only its new path
        (self.repo / "new_test.py").write_text("x\n")
        for cmd in ("run", "resume"):
            with self.assertRaises(SystemExit) as e:
                self.go(cmd)
            self.assertIn("uncommitted changes (b.txt, new_test.py)", str(e.exception))
        self.assertEqual(self.ran, [])


class ResumeAfterRedo(unittest.TestCase):
    """`resume` of a `run --redo` that stopped early rebuilds the tasks that had not started: their results still read passed
    from before the redo, but their branches were set aside."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        self.repo = Path(tempfile.mkdtemp()).resolve()
        git = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("base\n")
        git("add", "-A"); git("commit", "-qm", "init")
        (self.repo / ".git" / "info" / "exclude").write_text("/.compose/\n")
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
                  mock.patch.object(fc.fc_run, "run_tasks", run_tasks)):
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


class RewrittenTrunk(unittest.TestCase):
    """The trunk itself was rebased onto a moved main: the chain still carries the trunk's old commits, which must not replay."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start()
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.git("init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("five\n")
        self.git("add", "-A"); self.git("commit", "-qm", "init")
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True); (self.proj / "worktrees").mkdir()
        (self.repo / ".git" / "info" / "exclude").write_text("/.compose/\n")
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": "010", "after": ""}, {"id": "011", "after": "010"}]}))
        self.git("checkout", "-q", "-b", "trunk")
        (self.repo / "a.txt").write_text("FIVE\n"); self.git("commit", "-qam", "trunk work")
        self.layer("010", "trunk", "010.txt", "x\n"); self.layer("011", "fast/p/010", "011.txt", "x\n")
        self.git("checkout", "-q", "main"); (self.repo / "a.txt").write_text("5\n"); self.git("commit", "-qam", "main moves")
        # the trunk rebased onto the moved main, its one commit resolved by hand: the chain's copy of it conflicts with this one
        self.git("checkout", "-q", "-B", "trunk", "main")
        (self.repo / "a.txt").write_text("5 FIVE\n"); self.git("commit", "-qam", "trunk work")

    def tearDown(self):
        self.env.stop()

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def layer(self, tid, base, name, text):
        self.git("checkout", "-q", "-b", f"fast/p/{tid}", base)
        (self.repo / name).write_text(text)
        self.git("add", "-A"); self.git("commit", "-qm", f"task {tid}")

    def test_the_chain_replays_only_its_own_commits_onto_the_rewritten_trunk(self):
        chain = fc.fc_layers.linearize(self.repo, self.proj, "trunk", fc.fc_worktree.stack_worktree(self.repo, self.proj, "trunk"))
        self.assertEqual(chain, ["fast/p/010", "fast/p/011"])
        self.assertEqual(self.git("rev-list", "--count", "trunk..fast/p/010"), "1")
        self.assertEqual(self.git("rev-list", "--count", "trunk..fast/p/011"), "2")
        self.assertEqual(self.git("show", "fast/p/011:a.txt"), "5 FIVE")

    def test_a_layer_that_cannot_be_rebased_stops_the_run_with_one_line(self):
        self.git("checkout", "-q", "fast/p/011"); (self.repo / "a.txt").write_text("eleven\n"); self.git("commit", "-qam", "task 011 edits a")
        self.git("checkout", "-q", "trunk")
        a = mock.Mock(trunk="trunk", attempts=1, parallel=1, no_stack=False)
        with mock.patch.object(fc.fc_core, "say") as say, mock.patch.object(fc.fc_run, "preflight", lambda m: None), mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None), \
             self.assertRaises(SystemExit) as e:
            fc.fc_run.run_tasks(self.repo, self.proj, [{"id": "012", "after": "", "files": [], "protected": [], "test": "true", "model": "m"}], a)
        self.assertEqual(self.git("rev-list", "--count", "trunk..fast/p/010"), "1")  # the layer below is kept
        self.assertIn("fast/p/011", str(e.exception)); self.assertIn("git rebase --onto fast/p/010 ", str(e.exception))
        self.assertIn("(the old base recorded in this pass)", str(e.exception))  # the hint says where the old base came from
        self.assertIn("fast/p/011", "".join(str(c) for c in say.call_args_list))

    def test_no_stack_dispatches_without_rebasing_the_existing_layers(self):
        self.git("checkout", "-q", "fast/p/011"); (self.repo / "a.txt").write_text("eleven\n"); self.git("commit", "-qam", "task 011 edits a")
        self.git("checkout", "-q", "trunk")
        tips = [self.git("rev-parse", f"fast/p/{t}") for t in ("010", "011")]
        a = mock.Mock(trunk="trunk", attempts=1, parallel=1, no_stack=True)
        task = {"id": "012", "title": "012", "after": "", "files": [], "protected": [], "test": "true", "model": "m", "body": ""}
        with mock.patch.object(fc.fc_core, "say") as say, mock.patch.object(fc.fc_run, "preflight", lambda m: None), mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None), \
             mock.patch.object(fc.fc_run, "triage", lambda *x: None), mock.patch("builtins.print"), self.assertRaises(SystemExit):
            fc.fc_run.run_tasks(self.repo, self.proj, [task], a)
        said = [str(c.args[0]) for c in say.call_args_list]
        self.assertFalse(any("STOP" in l for l in said), said)
        self.assertTrue(any(l.startswith("run done   0/1 passed") for l in said))  # 012 ran: its test was already green
        self.assertEqual([self.git("rev-parse", f"fast/p/{t}") for t in ("010", "011")], tips)

    def test_a_task_whose_chain_stops_mid_run_is_unchained_and_the_run_still_ends_with_its_summary(self):
        after = {"011": "010", "012": "011", "013": "012", "014": "013"}
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": t, "after": after.get(t, "")} for t in ("010", "011", "012", "013", "014", "015")]}))
        self.git("checkout", "-q", "trunk")
        real = fc.fc_implement.run_task
        def run_task(repo, proj, meta, attempts):
            if meta["id"] != "012":
                return real(repo, proj, meta, attempts)
            self.layer("012", "fast/p/010", "011.txt", "twelve\n"); self.git("checkout", "-q", "trunk")  # passes on 010, clashing with 011
            return {"id": "012", "title": "012", "status": "passed", "attempts": 1, "seconds": 1.0, "cost": 0.0, "branch": "fast/p/012"}
        tasks = [{"id": t, "title": t, "after": after.get(t, ""), "files": [], "protected": [], "test": "true", "model": "m", "body": ""} for t in ("012", "013", "014", "015")]
        a = mock.Mock(trunk="trunk", attempts=1, parallel=1, no_stack=True)
        with mock.patch.object(fc.fc_core, "say") as say, mock.patch.object(fc.fc_run, "preflight", lambda m: None), mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None), \
             mock.patch.object(fc.fc_implement, "run_task", run_task), mock.patch.object(fc.fc_run, "triage", lambda *x: None), \
             mock.patch("builtins.print") as out, self.assertRaises(SystemExit) as e:
            fc.fc_run.run_tasks(self.repo, self.proj, tasks, a)
        said = [str(c.args[0]) for c in say.call_args_list]
        rows = {str(c.args[0]).split()[0]: str(c.args[0]).split()[1] for c in out.call_args_list if c.args and str(c.args[0])[:3].isdigit()}
        self.assertEqual(e.exception.code, 1)
        self.assertEqual(rows, {"012": "passed", "013": "unchained", "014": "blocked_by_013", "015": "already_green"})
        self.assertTrue(any(l.startswith("013 STOP  after: fast/p/012 could not be chained: fast/p/012 conflicts") for l in said))
        self.assertTrue(any(l.startswith("run done   1/4 passed") for l in said))
        self.assertEqual(json.loads((self.proj / "results" / "013.json").read_text())["status"], "unchained")
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

