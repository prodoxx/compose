import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
import sys, threading, time, types
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""
os.environ["COMPOSE_E2E_HEADLESS"] = "1"  # testing the tool itself never opens a browser window; real projects keep theirs

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


PRIMARY, BACKUP = "pi:p/primary", "pi:b/backup"


class Fallback(unittest.TestCase):
    def run_once(self, first, reset_files=False, after=2, edit=None, impl=None):
        """The primary model behaves as `first` ("error", "miss", or "rate"); the fallback writes the answer. Returns (result, models called);
        the prompts land in self.prompts."""
        root = Path(tempfile.mkdtemp())
        repo, proj = root / "repo", root / "proj"
        repo.mkdir(); proj.mkdir()
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (repo / "a.txt").write_text("no\n")
        git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        task = {"id": "001", "title": "answer", "files": ["a.txt"], "protected": [], "test": "grep -q yes a.txt",
                "model": PRIMARY, "after": "", "linear": "ENG-1", "body": "## Goal\nWrite yes."}
        calls, self.prompts = [], []
        def run_impl(cwd, model, prompt, session=None, task=None, timeout=None):
            calls.append(model); self.prompts.append(prompt)
            if model == PRIMARY and first == "error":
                raise RuntimeError("provider refused the request")
            if model == PRIMARY and first == "rate":
                if edit:  # the turn edits, then the provider cuts it short
                    (Path(cwd) / "a.txt").write_text(edit)
                raise fc.fc_models.RateLimited({"cost": 0.0})
            (Path(cwd) / "a.txt").write_text("yes\n" if model == BACKUP else "nope\n")
            return "s", "", 1, {"tokens_in": 1, "tokens_out": 1, "cost": 0.0}
        with mock.patch.object(fc.fc_implement, "implementer", impl or run_impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_config, "DEFAULT_MODEL", PRIMARY), mock.patch.object(fc.fc_config, "FALLBACK_MODEL", BACKUP), \
                mock.patch.object(fc.fc_config, "FALLBACK_AFTER", after), mock.patch.object(fc.fc_config, "TEST_READER", None), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            return fc.fc_implement.run_task(repo, proj, task, 3, reset_files=reset_files), calls

    def test_turn_limit_has_no_room_for_a_test_run(self):
        root = Path(tempfile.mkdtemp())
        repo, proj = root / "repo", root / "proj"
        repo.mkdir(); proj.mkdir()
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (repo / "a.txt").write_text("no\n")
        git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        task = {"id": "001", "title": "answer", "files": ["a.txt"], "protected": [], "test": "grep -q yes a.txt", "test_timeout": 300,
                "model": PRIMARY, "after": "", "linear": "ENG-1", "body": "## Goal\nWrite yes."}
        limits = []
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            limits.append(timeout)
            (Path(cwd) / "a.txt").write_text("yes\n")
            return "s", "", 1, {"tokens_in": 1, "tokens_out": 1, "cost": 0.0}
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            fc.fc_implement.run_task(repo, proj, task, 3)
        self.assertEqual(limits[0], fc.fc_models.TURN_TIMEOUT)  # compose runs the test after the turn, never inside it

    def test_error_falls_back_after_two(self):
        r, calls = self.run_once("error")
        self.assertEqual(r["status"], "passed")
        self.assertEqual(calls, [PRIMARY, PRIMARY, BACKUP])
        self.assertEqual(r["model"], BACKUP)
        self.assertEqual(r["attempts"], 3)
        self.assertEqual(r["log"][1]["fallback"]["why"], "errored")

    def test_two_misses_then_the_fallback(self):
        r, calls = self.run_once("miss")
        self.assertEqual(r["status"], "passed")
        self.assertEqual(calls, [PRIMARY, PRIMARY, BACKUP])
        self.assertNotIn("fallback", r["log"][0])
        self.assertEqual(r["log"][1]["fallback"]["why"], "missed")
        self.assertEqual(self.run_once("miss", after=1)[1], [PRIMARY, BACKUP])  # models.fallback_after

    def test_rate_limit_switches_at_once(self):
        r, calls = self.run_once("rate")
        self.assertEqual((r["status"], calls), ("passed", [PRIMARY, BACKUP]))
        self.assertEqual(r["attempts"], 1)  # the refused turn cost no attempt

    def busy_run(self, refusals, said, slept, err=None, nap=lambda: None):
        """One task with one attempt allowed and no fallback, whose first `refusals` turns end as `err` (default: the provider turns
        them away as busy), `nap` running just before each one ends."""
        busy = iter([err or fc.fc_models.RateLimited({"cost": 0.0}, "provider busy: 503 overloaded_error")] * refusals)
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            err = next(busy, None)
            if err:
                nap()
                raise err
            (Path(cwd) / "a.txt").write_text("yes\n")
            return "s", "", 1, {"tokens_in": 1, "tokens_out": 1, "cost": 0.0}
        root = Path(tempfile.mkdtemp()); repo, proj = root / "repo", root / "proj"; repo.mkdir(); proj.mkdir()
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main"); (repo / "a.txt").write_text("no\n")
        git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        task = {"id": "001", "title": "answer", "files": ["a.txt"], "protected": [], "test": "grep -q yes a.txt",
                "model": PRIMARY, "after": "", "linear": "ENG-1", "body": "## Goal\nWrite yes."}
        me, real_sleep = threading.get_ident(), time.sleep
        record = lambda s: slept.append(s) if threading.get_ident() == me and s >= 1 else real_sleep(s)  # time is global: another thread's sleep is not the wait, nor is the 1 ms poll subprocess makes for a child it has not yet reaped
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_config, "FALLBACK_MODEL", None), mock.patch.object(fc.fc_config, "TEST_READER", None), \
                mock.patch.object(fc.fc_core, "say", said.append), mock.patch.object(fc.fc_implement.time, "sleep", record), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            return fc.fc_implement.run_task(repo, proj, task, 1), proj

    def test_a_busy_provider_without_a_fallback_waits_and_costs_no_attempt(self):
        said, slept = [], []
        r, proj = self.busy_run(1, said, slept)
        self.assertEqual((r["status"], slept), ("passed", [20]))  # one attempt allowed, and the busy turn did not spend it
        self.assertIn("001 provider busy: 503 overloaded_error; fresh session in 20s (wait 1/4)", said)
        # The wait is in the log, but the count every reader shows (the result, its json, the final line) is the one attempt spent.
        self.assertEqual((r["attempts"], len(r["log"])), (1, 2))
        self.assertEqual(json.loads((proj / "results" / "001.json").read_text())["attempts"], 1)
        self.assertTrue(any(s.startswith("001 passed") and "attempts=1 " in s for s in said), said)
        r, _ = self.busy_run(5, [], [])  # the provider never answers: four waits, then the task gives up having spent none
        self.assertEqual((r["status"], r["attempts"], len(r["log"])), ("failed", 0, 5))

    def test_a_turn_that_timed_out_across_a_sleep_costs_no_attempt(self):
        t = {"wall": 1000.0, "awake": 50.0}  # the lid closes in the first turn: 90 minutes pass on the wall clock, 3 on the awake one
        clock = types.SimpleNamespace(time=lambda: t["wall"], monotonic=lambda: t["awake"])
        nap = lambda: t.update(wall=t["wall"] + 5400, awake=t["awake"] + 180)
        said, slept = [], []
        with mock.patch.object(fc.fc_proc, "time", clock):
            r, _ = self.busy_run(1, said, slept, err=subprocess.TimeoutExpired("pi", 180), nap=nap)
        self.assertEqual((r["status"], r["attempts"]), ("passed", 1))  # one attempt allowed, and the turn the machine slept in did not spend it
        self.assertTrue(any(s.startswith("001 attempt 1 timeout") and "the machine slept for 87 minutes: it does not count" in s for s in said), said)
        self.assertEqual(r["log"][0]["slept"], 5220)

    def test_a_turn_cut_short_is_tested_on_the_edit_it_left(self):
        r, calls = self.run_once("rate", edit="yes\n")
        self.assertEqual((r["status"], calls), ("passed", [PRIMARY]))
        r, calls = self.run_once("rate", edit="maybe\n")
        self.assertEqual((r["status"], calls, r["attempts"]), ("passed", [PRIMARY, BACKUP], 1))  # the cut-short turn cost no attempt
        self.assertIn("A previous attempt left edits in the files and did not pass", self.prompts[1])
        self.assertIn("grep -q yes a.txt", self.prompts[1].split("did not pass")[1])

    def test_a_timed_out_turn_that_left_a_passing_edit_keeps_the_usage_it_spent(self):
        spent = {"cost": 0.5, "tokens_in": 100, "tokens_out": 20}
        def timed_out(cwd, model, prompt, session=None, task=None, timeout=None):
            (Path(cwd) / "a.txt").write_text("yes\n")
            e = subprocess.TimeoutExpired("herdr agent prompt", 180)
            e.tools, e.usage = 4, spent
            raise e
        r, _ = self.run_once("timeout", impl=timed_out)
        timeout, cut = r["log"]
        self.assertEqual((r["status"], timeout["tool_calls"], timeout["usage"], cut["cut_short"], r["cost"]), ("passed", 4, spent, True, 0.5))

    def test_fallback_has_no_fallback(self):
        with mock.patch.object(fc.fc_config, "FALLBACK_MODEL", BACKUP):
            self.assertIsNone(fc.fc_models.fallback_for(BACKUP))
            self.assertIsNone(fc.fc_models.fallback_for("claude"))


class Preflight(unittest.TestCase):
    def test_bad_model_stops_before_anything_runs(self):
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            if model == BACKUP:
                raise RuntimeError("400 z-ai/glm-5-3-flash is not a valid model ID")
            if model == "pi:r/limited":
                raise fc.fc_models.RateLimited({"cost": 0.0})
            return "s", "OK", 0, {"cost": 0.0}
        with mock.patch.object(fc.fc_implement, "implementer", impl):
            fc.fc_run.preflight({PRIMARY, "pi:r/limited", "claude", None})  # a rate limit proves the model resolved; claude is not probed
            with self.assertRaises(SystemExit) as cm:
                fc.fc_run.preflight({PRIMARY, BACKUP})
        self.assertIn(BACKUP, str(cm.exception))
        self.assertIn("not a valid model ID", str(cm.exception))

    def test_a_pi_auth_key_that_shadows_the_workspace_key_stops_before_any_probe(self):
        auth = Path(tempfile.mkdtemp()) / "auth.json"
        auth.write_text(json.dumps({"openrouter-fast": {"type": "api_key", "key": "k"}, "anthropic": {}}))
        probed = []
        impl = lambda cwd, model, *a, **kw: probed.append(model) or ("s", "OK", 0, {"cost": 0.0})
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_run.fc_pi, "PI_AUTH", auth), \
                mock.patch.object(fc.fc_config, "WORKSPACE", "personal"):
            fc.fc_run.preflight({"pi:openrouter/x", "claude"})  # no pi model of this run uses openrouter-fast
            with self.assertRaises(SystemExit) as cm:
                fc.fc_run.preflight({"pi:openrouter-fast/z-ai/glm", "pi:openrouter/x"})
        self.assertEqual(probed, ["pi:openrouter/x"])
        for words in (str(auth), "openrouter-fast", "'personal'"):
            self.assertIn(words, str(cm.exception))
        self.assertNotIn("model id", str(cm.exception))


class ReadyCheckout(unittest.TestCase):
    def test_clone_borrows_installs_and_stays_clean(self):
        root = Path(tempfile.mkdtemp())
        repo, wt = root / "repo", root / "wt"
        (repo / "client" / "node_modules" / "jest").mkdir(parents=True)
        (repo / "client" / "app.js").write_text("x\n")
        (repo / "a.py").write_text("def f():\n    return 1\n")
        (repo / ".gitignore").write_text("node_modules/\n")
        git = lambda cwd, *a: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        git(root, "clone", "-q", "--shared", str(repo), str(wt))
        fc.fc_worktree.ready_checkout(repo, wt)
        fc.fc_worktree.ready_checkout(repo, wt)  # idempotent: a re-run neither fails nor duplicates exclude lines
        self.assertTrue((wt / "client" / "node_modules").is_symlink())
        self.assertTrue((wt / "client" / "node_modules" / "jest").is_dir())
        for graph in (".trail", "graft"):  # what a `trail build` leaves, without the machine's own trail
            (wt / graph).mkdir(); (wt / graph / "graph.db").write_text("x")
        self.assertEqual(git(wt, "status", "--porcelain").stdout, "")
        exclude = (wt / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertEqual(exclude.count("/client/node_modules"), 1)

    def test_clone_borrows_ignored_env_files_only(self):
        root = Path(tempfile.mkdtemp())
        repo, wt = root / "repo", root / "wt"
        (repo / "app").mkdir(parents=True)
        (repo / "app" / ".env").write_text("A=1\n")
        (repo / "app" / ".env.local").write_text("B=2\n")
        (repo / "app" / ".env.example").write_text("A=\n")  # tracked: the clone has its own
        (repo / "app" / "env.ts").write_text("x\n")
        (repo / ".gitignore").write_text(".env\n.env.*\n!.env.example\n")
        git = lambda cwd, *a: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        git(root, "clone", "-q", "--shared", str(repo), str(wt))
        fc.fc_worktree.ready_checkout(repo, wt)
        fc.fc_worktree.ready_checkout(repo, wt)
        for name in (".env", ".env.local"):
            self.assertTrue((wt / "app" / name).is_symlink())
            self.assertEqual((wt / "app" / name).read_text(), (repo / "app" / name).read_text())
        self.assertFalse((wt / "app" / ".env.example").is_symlink())
        self.assertEqual(git(wt, "status", "--porcelain").stdout, "")

    def test_install_command_replaces_borrowing(self):
        root = Path(tempfile.mkdtemp())
        repo, wt = root / "repo", root / "wt"
        (repo / "node_modules" / "old").mkdir(parents=True)
        (repo / ".gitignore").write_text("node_modules/\n")
        git = lambda cwd, *a: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        git(root, "clone", "-q", "--shared", str(repo), str(wt))
        (wt / "node_modules").symlink_to(repo / "node_modules")  # a link left by an earlier borrow
        fc.fc_worktree.ready_checkout(repo, wt, install="mkdir -p node_modules/fresh")
        self.assertFalse((wt / "node_modules").is_symlink())  # the install lives in the checkout, so build output cannot depend on the project worktree's path
        self.assertTrue((wt / "node_modules" / "fresh").is_dir())
        self.assertFalse((repo / "node_modules" / "fresh").exists())
        self.assertEqual(git(wt, "status", "--porcelain").stdout, "")
        (wt / "bun.lock").write_text("new\n")  # a lockfile change is what makes the install run again
        git(wt, "add", "-A")
        git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "dep")
        with self.assertRaises(RuntimeError):
            fc.fc_worktree.ready_checkout(repo, wt, install="exit 3")

    def test_install_reruns_only_when_the_lockfile_changes(self):
        root = Path(tempfile.mkdtemp())
        repo, wt = root / "repo", root / "wt"
        repo.mkdir()
        (repo / ".gitignore").write_text("node_modules/\n")
        (repo / "bun.lock").write_text("one\n")
        git = lambda cwd, *a: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        git(root, "clone", "-q", "--shared", str(repo), str(wt))
        install = "mkdir -p node_modules && cat bun.lock >> node_modules/seen"
        seen = lambda: (wt / "node_modules" / "seen").read_text()
        fc.fc_worktree.ready_checkout(repo, wt, install=install)
        fc.fc_worktree.ready_checkout(repo, wt, install=install)
        self.assertEqual(seen(), "one\n")  # the same lockfile: no second install
        (wt / "bun.lock").write_text("two\n")
        git(wt, "add", "-A")
        git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "dep")
        fc.fc_worktree.ready_checkout(repo, wt, install=install)
        self.assertEqual(seen(), "one\ntwo\n")  # a changed lockfile installs again

    def test_lockfile_task_installs_on_its_own_and_hands_it_over(self):
        root = Path(tempfile.mkdtemp())
        repo, wt = root / "repo", root / "wt"
        (repo / "node_modules" / "old").mkdir(parents=True)
        (repo / ".gitignore").write_text("node_modules/\n")
        (repo / "a.js").write_text("x\n")
        git = lambda cwd, *a: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        git(root, "clone", "-q", "--shared", str(repo), str(wt))
        for f in (["bun.lock"], ["a.js", "package-lock.json"]):
            self.assertTrue(fc.fc_worktree.names_lockfile({"files": f}))
        self.assertFalse(fc.fc_worktree.names_lockfile({"files": ["a.js"]}))
        fc.fc_worktree.ready_checkout(repo, wt, borrow=False)
        self.assertFalse((wt / "node_modules").exists())
        (wt / "node_modules" / "new").mkdir(parents=True)  # the task's own install
        real, seen = shutil.rmtree, []
        def rmtree(path, *a, **k):
            real(path, *a, **k)
            seen.append((repo / "node_modules").exists())
        with mock.patch.object(shutil, "rmtree", rmtree):
            fc.fc_worktree.adopt_installs(repo, wt)
        self.assertTrue(seen and all(seen), "the install dir was missing during the swap")
        self.assertTrue((repo / "node_modules" / "new").is_dir())
        self.assertFalse((repo / "node_modules" / "old").exists())
        self.assertFalse((repo / "node_modules").is_symlink())


class FixScope(unittest.TestCase):
    def verify(self, writes, reply="", cfg=None, pack="", tasks=(), trunk="main", dirty=None, artifact=None, report=None, rerun=0, top=None, layer=None):
        """A stack with one layer changing a.go; the first stage run fails, the fixer writes `writes`, later runs pass.
        `self.scopes` holds the tests each stage run was given (None: the full checks). `dirty` (path: text) is in the worktree
        before verify starts, over a tracked .claude/settings.json; `artifact` is a file the first stage run leaves behind. The first
        stage run's `report` (by default one failing check, `c1`) is what the fix answers; the check exits `rerun` when verify runs it
        again. `top` is the task id of the stack's top layer. `layer` (path: text) is more of what the layer commits."""
        root = Path(tempfile.mkdtemp())
        repo, proj = root / "repo", root / "proj"
        repo.mkdir(); proj.mkdir()
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", trunk)
        (repo / "a.go").write_text("a\n"); (repo / "yarn.lock").write_text("lock\n")
        (repo / ".claude").mkdir(); (repo / ".claude" / "settings.json").write_text("{}\n")
        git("add", "-A"); git("commit", "-qm", "init")
        git("checkout", "-qb", "layer")
        (repo / "a.go").write_text("b\n")
        for f, text in (layer or {}).items():
            (repo / f).parent.mkdir(exist_ok=True); (repo / f).write_text(text)
        git("add", "-A"); git("commit", "-qm", "layer")
        for f, text in (dirty or {}).items():
            (repo / f).write_text(text)
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            self.prompt = prompt
            for f, text in writes.items():
                (Path(cwd) / f).write_text(text)
            return "s", reply, 1, {"cost": 0.0}
        stages, self.scopes, self.said = iter([(False, report or {"checks": [{"cmd": "c1", "exit": 1}]}, "finding"), (True, {}, "")]), [], []
        def run_stages(repo, proj, cfg, a, tests=None):
            if artifact and not self.scopes:
                (repo / artifact).write_text("out\n")
            self.scopes.append(tests)
            return next(stages)
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=pack), \
                mock.patch.object(fc.fc_verify, "run_stages", run_stages), mock.patch.object(fc.fc_verify, "review_inputs", return_value=(0, 0, 0, [])), \
                mock.patch.object(fc.fc_tasks, "load_tasks", return_value=list(tasks)), \
                mock.patch.object(fc.fc_change, "reflect", return_value=(Path("/p/reflect.json"), {})), \
                mock.patch.object(fc.fc_proc, "run", return_value=mock.Mock(returncode=rerun)), \
                mock.patch.object(fc.fc_worktree, "stack_order", return_value=[f"compose/p-{top}"] if top else []), \
                mock.patch.object(fc.fc_worktree, "task_prefix", return_value="compose/p-"), \
                mock.patch.object(fc.fc_core, "say", self.said.append):  # a failed verify screens: never call Jev
            with self.assertRaises(SystemExit) as cm:
                fc.fc_verify.verify_rounds(repo, proj, {"review_base": "main", **(cfg or {})}, type("A", (), {"fix": 2, "skip_review": True})(), [], time.time())
        return cm.exception.code, repo, json.loads((proj / "results" / "verify.json").read_text())

    def committed(self, repo):
        """The files the last commit changed, and what `git status` still shows."""
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True).stdout
        return git("show", "--name-only", "--format=", "HEAD").split(), git("status", "--porcelain").splitlines()

    def test_a_rewritten_wiring_file_neither_blocks_the_fix_nor_lands_in_its_commit(self):
        # A session hook rewrites the tracked .claude/settings.json whenever a session starts in the checkout.
        code, repo, res = self.verify({"a.go": "fixed\n"}, dirty={".claude/settings.json": '{"hooks": {}}\n'})
        self.assertEqual((code, res["passed"], len(res["rounds"])), (0, True, 2))
        self.assertEqual(self.committed(repo), (["a.go"], [" M .claude/settings.json"]))

    def test_the_fix_commit_holds_only_what_the_fixer_changed(self):
        # A check left a report behind and the fixer also rewrote the wiring: neither is the fix, and neither makes it a stray.
        code, repo, res = self.verify({"a.go": "fixed\n", ".claude/settings.json": '{"x": 1}\n'}, artifact="report.xml")
        self.assertEqual((code, res["passed"], res["rejected_fixes"]), (0, True, []))
        self.assertEqual(self.committed(repo), (["a.go"], [" M .claude/settings.json", "?? report.xml"]))

    def test_uncommitted_changes_of_the_user_still_skip_the_fix(self):
        code, repo, res = self.verify({"a.go": "fixed\n"}, dirty={"a.go": "mine\n"})
        self.assertEqual((code, len(res["rounds"])), (1, 1))
        self.assertEqual((repo / "a.go").read_text(), "mine\n")
        self.assertIn("fix    skip  repo has uncommitted changes (a.go); commit first so a bad fix can be reverted", self.said)

    def test_a_recorded_base_that_names_no_branch_scopes_the_fix_to_the_default_branch(self):
        # A project made before init recorded its start branch holds review_base "main", also in a repository whose trunk is master.
        code, repo, res = self.verify({"a.go": "fixed\n", "b.go": "new\n"}, trunk="master")
        self.assertEqual((code, res["rejected_fixes"]), (1, [{"round": 1, "files": ["b.go"]}]))
        self.assertEqual(((repo / "a.go").read_text(), (repo / "b.go").exists()), ("b\n", False))  # the round is reverted

    def test_a_recorded_base_that_names_no_branch_reviews_the_diff_against_the_default_branch(self):
        _, repo, _ = self.verify({"a.go": "fixed\n"}, trunk="master")
        proj = Path(tempfile.mkdtemp())
        with mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[{"id": "001", "title": "t", "body": "", "files": ["a.go"], "protected": []}]):
            self.assertEqual(fc.fc_verify.verify_root(repo, proj, {"review_base": "main"})[1], "master")
            diff, _, _, _ = fc.fc_verify.review_inputs(repo, proj, {"review_base": "main"})
        self.assertIn("+fixed", diff)  # the stack's diff, not every named file whole
        self.assertNotIn("==== a.go ====", diff)

    def test_a_fix_that_changes_no_failing_check_and_no_cited_line_is_discarded(self):
        code, repo, res = self.verify({"a.go": "fixed\n"}, rerun=1)  # c1 still fails after the fix
        self.assertEqual((code, len(res["rounds"]), res["rejected_fixes"]), (1, 1, [{"round": 1, "files": ["a.go"],
                         "why": "changed no failing check and removed no line a confirmed finding cites"}]))
        self.assertEqual((self.committed(repo)[0], (repo / "a.go").read_text()), (["a.go"], "b\n"))  # still the layer's own commit
        rv = lambda quote: {"review": {"pass": False, "issues": [], "confirmed": [{"evidence": quote}]}}
        code, repo, res = self.verify({"a.go": "fixed\n"}, report=rv("b"))  # it removes the line the finding quotes
        self.assertEqual((code, res["rejected_fixes"], (repo / "a.go").read_text()), (0, [], "fixed\n"))
        code, _, res = self.verify({"a.go": "fixed\n"}, report=rv("zzz"))
        self.assertEqual((code, len(res["rejected_fixes"])), (1, 1))  # it leaves that line alone
        code, _, res = self.verify({"a.go": "fixed\n"}, report={"e2e": [{"name": "n", "exit": 1}]})  # a failing e2e case is its own reproduction
        self.assertEqual((code, res["rejected_fixes"]), (0, []))

    GUARD = {"m.py": "def f(x):\n    return x\n", "tests/__init__.py": "",
             "tests/test_m.py": "import unittest\nfrom m import f\n\n\nclass T(unittest.TestCase):\n    def test_keeps(self):\n        self.assertEqual(f(1), 1)\n"}
    ADDED = "\n    def test_none(self):\n        self.assertEqual(f(None), 0)\n"
    GUARDED = "def f(x):\n    if x is None:\n        return 0\n    return x\n"
    CHECK = "python3 -m unittest discover -s tests -t ."

    def additive(self, writes, file="m.py", **kw):
        """A fix that only adds code, for a confirmed finding about `file`; no check failed, so nothing but its own tests could show it."""
        report = {"checks": [{"cmd": self.CHECK, "exit": 0}], "review": {"pass": False, "issues": [], "confirmed": [{"file": file, "evidence": "return x"}]}}
        return self.verify(writes, report=report, layer=self.GUARD, **kw)

    def test_an_additive_fix_for_a_confirmed_finding_is_kept_when_its_new_test_fails_without_it_and_passes_with_it(self):
        code, repo, res = self.additive({"m.py": self.GUARDED, "tests/test_m.py": self.GUARD["tests/test_m.py"] + self.ADDED})
        self.assertEqual((code, res["rejected_fixes"], (repo / "m.py").read_text()), (0, [], self.GUARDED))
        self.assertEqual(self.committed(repo)[0], ["m.py", "tests/test_m.py"])

    def test_an_additive_fix_is_still_discarded_when_it_has_no_test_that_fails_without_it_or_answers_no_confirmed_finding(self):
        passes = self.GUARD["tests/test_m.py"] + "\n    def test_again(self):\n        self.assertEqual(f(2), 2)\n"
        for writes, file in (({"m.py": self.GUARDED}, "m.py"), ({"m.py": self.GUARDED, "tests/test_m.py": passes}, "m.py"),
                             ({"m.py": self.GUARDED, "tests/test_m.py": self.GUARD["tests/test_m.py"] + self.ADDED}, "other.py")):
            code, repo, res = self.additive(writes, file=file)
            self.assertEqual((code, len(res["rejected_fixes"]), (repo / "m.py").read_text()), (1, 1, self.GUARD["m.py"]), writes)

    def test_a_fix_round_commits_only_to_the_layer_that_owns_the_changed_file(self):
        tasks = [{"id": "003", "files": ["a.go"], "protected": [], "test": "t3"}, {"id": "007", "files": ["b.go"], "protected": [], "test": "t7"}]
        code, repo, res = self.verify({"a.go": "fixed\n"}, tasks=tasks, top="007")
        nxt = "cut a new top task (id above 007) whose files: lists a.go, run it, then verify again"
        self.assertEqual((code, res["rejected_fixes"]), (1, [{"round": 1, "files": ["a.go"], "why": "changed a file of layer 003, not the top layer 007: a.go", "next": nxt}]))
        self.assertIn("fix    next  " + nxt, self.said)
        self.assertEqual(((repo / "a.go").read_text(), self.committed(repo)[0]), ("b\n", ["a.go"]))  # reverted, nothing committed
        code, _, res = self.verify({"a.go": "fixed\n"}, tasks=tasks, top="003")
        self.assertEqual((code, res["rejected_fixes"]), (0, []))
        tasks[1]["files"].append("a.go")  # a file two tasks list belongs to the top layer too
        code, _, res = self.verify({"a.go": "fixed\n"}, tasks=tasks, top="007")
        self.assertEqual((code, res["rejected_fixes"]), (0, []))

    def test_a_fix_round_runs_only_the_tests_of_the_tasks_its_fix_changed(self):
        tasks = [{"id": "001", "files": ["a.go"], "protected": ["a_test.go"], "test": "go test ./a"},
                 {"id": "002", "files": ["b.go"], "protected": ["b_test.go"], "test": "go test ./b"},
                 {"id": "003", "kind": "design", "files": ["a.go"], "protected": [], "test": "compose figma judge"}]
        code, _, _ = self.verify({"a.go": "fixed\n"}, tasks=tasks)
        self.assertEqual((code, self.scopes), (0, [None, ["go test ./a"]]))  # the first round runs every check
        self.verify({"a.go": "fixed\n", "yarn.lock": "new\n"}, tasks=tasks + [{"id": "004", "files": ["yarn.lock"], "protected": [], "test": "go test ./a"}])
        self.assertEqual(self.scopes, [None, ["go test ./a"]])  # one command once, however many tasks it proves
        self.verify({"a.go": "fixed\n"}, tasks=[])
        self.assertEqual(self.scopes, [None, None])  # a file no task names: only the full checks cover it

    def test_a_committed_fix_records_fix_tasks_on_the_round_it_fixed(self):
        tasks = [{"id": "001", "files": ["a.go"], "protected": [], "test": "ta"},
                 {"id": "002", "files": ["b.go"], "protected": ["a.go"], "test": "tb"},
                 {"id": "003", "kind": "design", "files": ["a.go"], "protected": [], "test": "td"}]
        fix_tasks = lambda res: [r.get("fix_tasks", "absent") for r in res["rounds"]]
        code, _, res = self.verify({"a.go": "fixed\n"}, tasks=tasks)
        self.assertEqual((code, fix_tasks(res)), (0, [["001", "002"], "absent"]))  # task order, never the design task; the passing round has none
        _, _, res = self.verify({"a.go": "fixed\n"}, tasks=tasks[:1])
        self.assertEqual(fix_tasks(res), [["001"], "absent"])
        _, _, res = self.verify({"a.go": "fixed\n"}, tasks=[])
        self.assertEqual(fix_tasks(res), [[], "absent"])  # a file no task names
        code, _, res = self.verify({"b.go": "new\n"}, tasks=tasks[:1])
        self.assertEqual((code, res["rejected_fixes"], fix_tasks(res)), (1, [{"round": 1, "files": ["b.go"]}], ["absent"]))
        code, _, res = self.verify({}, tasks=tasks)
        self.assertEqual((code, fix_tasks(res)), (1, ["absent"]))  # the fixer changed nothing
        self.assertEqual(sorted(res), ["cost", "passed", "rejected_fixes", "rounds", "seconds"])  # nothing else in verify.json changed

    def test_fix_tests_cover_every_changed_file_or_fall_back_to_the_full_checks(self):
        tasks = [{"id": "001", "files": ["a.go"], "protected": ["a_test.go"], "test": "ta"},
                 {"id": "002", "files": ["b.go", "c.go"], "protected": ["b_test.go"], "test": "tb"}]
        self.assertEqual(fc.fc_verify.fix_tests(tasks, ["c.go", "a.go"]), ["ta", "tb"])  # task order
        self.assertEqual(fc.fc_verify.fix_tests(tasks, ["b_test.go"]), ["tb"])
        self.assertIsNone(fc.fc_verify.fix_tests(tasks, ["a.go", "setup.js"]))
        self.assertIsNone(fc.fc_verify.fix_tests(tasks, []))

    def test_fix_commit_uses_the_fixers_one_line_description(self):
        _, repo, _ = self.verify({"a.go": "fixed\n"}, reply='"Match the autofill panel file name"\nI also changed...')
        self.assertEqual(subprocess.run(["git", "log", "-1", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout.strip(),
                         "Match the autofill panel file name")
        _, repo, _ = self.verify({"a.go": "fixed\n"}, reply="Fix verification findings")  # a process message is not used
        self.assertEqual(subprocess.run(["git", "log", "-1", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout.strip(), "Fix a.go")

    def test_review_confirms_only_defects_against_the_requirements(self):
        systems = []
        def fj(env, system, user, **kw):
            systems.append((system, user))
            return {"issues": [{"file": "a.mjs", "severity": "medium", "note": "handle Windows paths"}]} if "strict code reviewer" in system \
                else {"confirmed": True, "evidence": "e"}
        with mock.patch.object(fc.fc_models, "fast_json", fj), mock.patch.object(fc.fc_core, "load_env", return_value={}):
            fc.fc_verify.fc_review.llm_review({"perspectives": ["correctness"]}, "diff", "THE PLAN", "", True)
        review, confirm = systems
        self.assertIn("hardening", review[0])
        self.assertIn("hardening", confirm[0])
        self.assertIn("THE PLAN", confirm[1])

    def test_review_drops_a_claim_a_passing_test_in_the_code_already_proves_wrong(self):
        code = "def test_a_floor_turns_check_only_into_deep(self):\n    assert floor(x) == 'deep'\nreturn floor(x)"
        def review(proof, tests_pass=True):
            def fj(env, system, user, **kw):
                if "strict code reviewer" in system:
                    return {"issues": [{"file": "a.py", "severity": "high", "note": "floor does not refuse check-only"}]}
                return {"wrong_line": "return floor(x)", "fixed_line": "return x", "proof": proof, "confirmed": True}
            with mock.patch.object(fc.fc_models, "fast_json", fj), mock.patch.object(fc.fc_core, "load_env", return_value={}):
                return fc.fc_verify.fc_review.llm_review({"perspectives": ["spec"]}, code, "plan", "", tests_pass)
        rv = review("test_a_floor_turns_check_only_into_deep")
        self.assertEqual(([i["evidence"] for i in rv["issues"]], rv["confirmed"], rv["pass"]),
                         (["a passing test already proves it: test_a_floor_turns_check_only_into_deep"], [], True))
        for proof, tests_pass in (("", True), ("test_not_in_the_code", True), ("test_a_floor_turns_check_only_into_deep", False)):
            self.assertEqual(len(review(proof, tests_pass)["confirmed"]), 1, (proof, tests_pass))  # no passing proof: the claim stands

    def test_a_review_where_no_perspective_answered_is_an_error_never_ok(self):
        def review(answers):
            def fj(env, system, user, **kw):
                if any(PERSPECTIVES[name] in system for name in answers):
                    return {"issues": []}
                raise RuntimeError("openrouter (fast model): HTTP 429 Too Many Requests")
            with mock.patch.object(fc.fc_models, "fast_json", fj), mock.patch.object(fc.fc_core, "load_env", return_value={}):
                return fc.fc_verify.fc_review.llm_review({"perspectives": ["correctness", "security"]}, "diff", "plan", "", True)
        PERSPECTIVES = fc.fc_verify.fc_review.PERSPECTIVES
        rv = review(())
        self.assertFalse(rv["pass"])
        self.assertTrue(rv["error"].startswith("no review perspective returned a result (2 of 2 failed): RuntimeError: openrouter (fast model): HTTP 429"), rv["error"])
        rv = review(("security",))  # one answered: the other's gap is a low note, as before
        self.assertEqual((rv["pass"], "error" in rv), (True, False))

    def test_a_claude_review_model_reviews_and_confirms_through_claude_code_never_openrouter(self):
        calls = []
        def run(cmd, cwd=None, env=None, timeout=None, input=None):
            calls.append((cmd, cwd, env, input))
            reply = {"requirement": "add adds", "wrong_line": "return a - b", "fixed_line": "return a + b", "proof": "", "confirmed": True} \
                if "verifying one code-review claim" in cmd[cmd.index("--system-prompt") + 1] \
                else {"issues": [{"file": "a.py", "line": 1, "severity": "high", "note": "adds with minus"}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps(reply), "usage": {"input_tokens": 9, "output_tokens": 3}}), "")
        with mock.patch.object(fc.fc_config, "REVIEW_MODEL", "claude:claude-sonnet-x"), mock.patch.object(fc.fc_proc, "run", run), \
                mock.patch.object(fc.fc_models, "http_json", side_effect=AssertionError("reached OpenRouter")), \
                mock.patch.object(fc.fc_core, "load_env", return_value={"TEXT_MODEL": "inception/mercury-2.5", "TEXT_MODEL_API_KEY": "k"}):
            rv = fc.fc_verify.fc_review.llm_review({"perspectives": ["correctness"]}, "def add(a, b): return a - b", "add adds", "", True)
        self.assertEqual(len(calls), 2)  # the review, then the confirm of its high finding
        self.assertEqual((rv["pass"], [i["note"] for i in rv["confirmed"]]), (False, ["adds with minus"]))
        for cmd, cwd, env, user in calls:
            self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-x")
            self.assertEqual((cmd[cmd.index("--tools") + 1], "--safe-mode" in cmd, "-p" in cmd), ("", True, True))
            self.assertFalse(any(k.startswith("ANTHROPIC_") for k in env))  # it bills to the subscription
        self.assertIn("return a - b", calls[0][3])  # the diff goes in on stdin, never on the command line
        self.assertFalse(any("return a - b" in a for a in calls[0][0]))

    def test_a_review_cut_off_at_its_token_budget_is_asked_again_with_a_bigger_one(self):
        full = json.dumps({"issues": [{"file": "a.go", "severity": "low", "note": "n"}]})
        for need, found in ((3000, "n"), (10 ** 6, "did not return usable findings")):  # tokens the whole reply needs
            budgets = []
            def http(req, provider, timeout=90, need=need):
                budgets.append(json.loads(req.data)["max_tokens"])
                short = budgets[-1] < need
                return {"choices": [{"message": {"content": full[:20] if short else full}, "finish_reason": "length" if short else "stop"}]}
            with mock.patch.object(fc.fc_models, "http_json", http), mock.patch.object(fc.fc_core, "load_env", return_value={"TEXT_MODEL_API_KEY": "k"}):
                rv = fc.fc_verify.fc_review.llm_review({"perspectives": ["spec"]}, "diff", "plan", "", True)
            self.assertEqual(budgets, [1500, 1500 * fc.fc_models.CUT_RETRY])
            self.assertIn(found, rv["issues"][0]["note"])

    def test_a_review_reply_that_is_not_json_is_asked_again(self):
        good = json.dumps({"issues": [{"file": "a.go", "severity": "low", "note": "n"}]})
        for replies, found in ((['{\n  issues": []\n}', good], "n"), (["{issues: []}", "", "never asked"], "did not return usable findings")):
            asked = []
            def http(req, provider, timeout=90, replies=replies):
                asked.append(json.loads(req.data)["max_tokens"])
                return {"choices": [{"message": {"content": replies[len(asked) - 1]}, "finish_reason": "stop"}]}
            with mock.patch.object(fc.fc_models, "http_json", http), mock.patch.object(fc.fc_core, "load_env", return_value={"TEXT_MODEL_API_KEY": "k"}):
                rv = fc.fc_verify.fc_review.llm_review({"perspectives": ["docs"]}, "diff", "plan", "docs", True)
            self.assertEqual(asked, [1500, 1500])
            self.assertIn(found, rv["issues"][0]["note"])

    def test_a_confirm_reply_with_prose_or_a_fence_around_the_object_is_read(self):
        confirm = json.dumps({"requirement": "r", "wrong_line": "return a - b", "fixed_line": "return a + b", "confirmed": True})
        found = json.dumps({"issues": [{"file": "a.py", "line": 2, "severity": "high", "note": "n"}]})
        for reply in (f"The claim says `x = {{}}`, so:\n```json\n{confirm}\n```\nThat is my verdict.", f"Verdict: {confirm} (truncated notes", confirm):
            replies = [found, reply]
            def http(req, provider, timeout=90, replies=replies):
                return {"choices": [{"message": {"content": replies.pop(0)}, "finish_reason": "stop"}]}
            with mock.patch.object(fc.fc_models, "http_json", http), mock.patch.object(fc.fc_core, "load_env", return_value={"TEXT_MODEL_API_KEY": "k"}):
                rv = fc.fc_verify.fc_review.llm_review({"perspectives": ["correctness"]}, "return a - b", "plan", "", True)
            self.assertEqual(([i["evidence"] for i in rv["confirmed"]], rv["pass"]), (["return a - b"], False), reply)

    def test_a_confirm_reply_that_stays_unparseable_is_not_judged_and_does_not_fail_verify(self):
        found = json.dumps({"issues": [{"file": "a.py", "line": 2, "severity": "high", "note": "n"}]})
        for bad in ('{"requirement": "r", "wrong_line": "return a - b", "fixed_', '{"requirement": "r"}', "no verdict here"):
            replies = [found, bad, bad]  # the first ask and the strict re-ask
            def http(req, provider, timeout=90, replies=replies):
                return {"choices": [{"message": {"content": replies.pop(0)}, "finish_reason": "stop"}]}
            with mock.patch.object(fc.fc_models, "http_json", http), mock.patch.object(fc.fc_core, "load_env", return_value={"TEXT_MODEL_API_KEY": "k"}):
                rv = fc.fc_verify.fc_review.llm_review({"perspectives": ["correctness"]}, "return a - b", "plan", "", True)
            i = rv["issues"][0]
            self.assertEqual((replies, i["confirmed"], i["evidence"], i.get("judge_error"), rv["pass"], "error" in rv),
                             ([], False, "confirm unparseable, not judged", None, True, False), bad)

    def test_review_confirms_only_a_quoted_wrong_line_the_fix_changes(self):
        def review(reply, finding=None, code="build: {\n  fileName: 'x.js',\n}", perspective="spec", cfg=None, users=None):
            def fj(env, system, user, **kw):
                if "strict code reviewer" in system:
                    return {"issues": [finding or {"file": "a.ts", "severity": "high", "note": "n"}]}
                (users if users is not None else []).append(user)
                return reply
            with mock.patch.object(fc.fc_models, "fast_json", fj), mock.patch.object(fc.fc_core, "load_env", return_value={}):
                return fc.fc_verify.fc_review.llm_review({"perspectives": [perspective], **(cfg or {})}, code, "plan", "==== README.md ====\nREAD ME", True)
        rv = review({"wrong_line": "fileName:   'x.js',", "fixed_line": "fileName: 'x.mjs',", "confirmed": True})
        self.assertEqual([(i["evidence"], i["fix"]) for i in rv["confirmed"]], [("fileName:   'x.js',", "fileName: 'x.mjs',")])
        for reply in ({"wrong_line": "lib output defaults to main.js", "fixed_line": "x", "confirmed": True},  # not a quote of the code
                      {"wrong_line": "", "fixed_line": "x", "confirmed": True},  # no line named
                      {"confirmed": True, "evidence": "fileName: 'x.js',"}):  # a verdict and a quote, but no wrong line
            rv = review(reply)
            self.assertEqual((rv["confirmed"], rv["pass"]), ([], True), reply)
        # A finding whose own text says the code meets the rule in the design context: the line as it must read is the line as it is.
        line = 'state = "ended" if last.get("state") not in (None, "done") and tab_closed(c, agents) else last.get("state", "-")'
        context = "crew list shows `ended` for a closed teammate whose last report is any state but done"
        finding = {"file": "bin/fc_crew.py", "line": 177, "severity": "high", "note": "crew list shows \"ended\" for any closed teammate whose last "
                   "report is not \"done\", but the spec says only for teammates whose last report is any state but done. The diff excludes None "
                   "and \"done\" but still includes other states correctly."}
        users = []
        rv = review({"requirement": context, "wrong_line": line, "fixed_line": " " + line, "confirmed": True}, finding, "+            " + line, "docs",
                    {"review_context": context}, users)
        self.assertEqual((rv["confirmed"], rv["pass"]), ([], True))
        self.assertIn("is the line as it is", rv["issues"][0]["evidence"])
        for seen in (context, "bin/fc_crew.py:177:", "READ ME"):
            self.assertIn(seen, users[0])

    def test_review_counts_only_well_formed_findings_and_prints_each_on_one_line(self):
        # A reviewer that spent its JSON reply on its reasoning: the schema echoed back, then objects of loose thoughts.
        reasoning = [{"file": "str", "line": "int|null", "severity": "high|medium|low", "note": "str}]}`.\n  *   Avoid repeating words."},
                     {"severity": "high", "Analysis": "Seems to align with C1. C1:"}, {"issue": "then later", "line": "If the diff relies on it"},
                     {"file": "a.py", "line": 2.5, "severity": "low", "note": "n"}, {"file": "", "severity": "low", "note": "n"}]
        confirms = []
        def fj(env, system, user, **kw):
            if "strict code reviewer" in system:
                return {"issues": reasoning + [{"file": "a.py", "line": 2, "severity": "high", "note": "add\n  subtracts"}]}
            confirms.append(user)
            return {"wrong_line": "return a - b", "fixed_line": "return a + b", "confirmed": True}
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        out = io.StringIO()
        with mock.patch.object(fc.fc_models, "fast_json", fj), mock.patch.object(fc.fc_core, "load_env", return_value={}), \
                mock.patch.object(fc.fc_verify, "review_inputs", return_value=("return a - b", "plan", "", [])), redirect_stdout(out):
            ok, report, findings = fc.fc_verify.run_stages(proj, proj, {"perspectives": ["correctness"], "jev_acceptance": False},
                                                           type("A", (), {"skip_e2e": True, "skip_review": False}))
        self.assertEqual((ok, len(confirms), [i["file"] for i in report["review"]["issues"]]), (False, 1, ["a.py"]))
        self.assertIn("1 confirmed of 1 raised, 5 malformed dropped", out.getvalue())
        self.assertIn("confirmed [high/correctness] a.py:2: add subtracts\n", out.getvalue())
        self.assertIn("Evidence: return a - b\nMust read: return a + b", findings)
        self.assertNotIn("Avoid", out.getvalue() + findings)
        self.assertNotIn("Analysis", out.getvalue() + findings)

    def test_review_whose_only_failure_is_a_confirm_call_error_reports_a_tool_error_and_hands_no_fixer_a_finding(self):
        def fj(env, system, user, **kw):
            if "strict code reviewer" in system:
                return {"issues": [{"file": "a.py", "line": 2, "severity": "high", "note": "n"}]}
            raise RuntimeError("HTTP 500 from the judge")
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        out = io.StringIO()
        with mock.patch.object(fc.fc_models, "fast_json", fj), mock.patch.object(fc.fc_core, "load_env", return_value={}), \
                mock.patch.object(fc.fc_verify, "review_inputs", return_value=("return a - b", "plan", "", [])), redirect_stdout(out):
            ok, report, findings = fc.fc_verify.run_stages(proj, proj, {"perspectives": ["correctness"], "jev_acceptance": False},
                                                           type("A", (), {"skip_e2e": True, "skip_review": False}))
        self.assertEqual((ok, findings), (False, ""))
        self.assertRegex(out.getvalue(), r"review TOOL ERROR .*0 confirmed of 1 raised\n")
        self.assertIn("1 confirm call(s) failed: HTTP 500 from the judge", out.getvalue())
        self.assertIn("judge err [high/correctness] a.py:2: n\n", out.getvalue())

    def test_lockfile_or_new_file_rejects_the_round_and_fails(self):
        code, repo, res = self.verify({"a.go": "fixed\n", "yarn.lock": "rewritten\n", "package-lock.json": "{}\n"})
        self.assertEqual((code, res["passed"]), (1, False))
        self.assertEqual(sorted(res["rejected_fixes"][0]["files"]), ["package-lock.json", "yarn.lock"])
        self.assertEqual((repo / "a.go").read_text(), "b\n")  # the whole round is reverted
        self.assertEqual((repo / "yarn.lock").read_text(), "lock\n")
        self.assertFalse((repo / "package-lock.json").exists())

    def test_no_stack_diff_still_refuses_lockfiles(self):
        self.assertEqual(fc.fc_verify.fix_strays(["src/x.go", "client/yarn.lock"], set()), ["client/yarn.lock"])
        self.assertEqual(fc.fc_verify.fix_strays(["src/x.go", "client/yarn.lock"], set(), {"client/yarn.lock"}), [])  # a task names it in files:
        self.assertEqual(fc.fc_verify.fix_strays(["a.go", "yarn.lock"], {"a.go"}, {"yarn.lock"}), [])


def ws(name=None, target=None, stdin="", linear=False):
    out = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(out):
        fc.fc_config.cmd_workspace(type("A", (), {"name": name, "target": target, "linear": linear}))
    return out.getvalue()


class Workspaces(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.path, self.auth = root / "config.json", root / "auth.json"
        self.path.write_text('{"workspace": "work", "models": {"implementer": "pi:a/b", "fallback": "pi:g/h", "fallback_after": 3,'
                             ' "test_reader": "claude:claude-haiku-x", "review": "claude:claude-sonnet-x"},'
                             ' "workspaces": {"work": {}, "personal": {"models": {"fallback": null}}}}')
        self.auth.write_text(json.dumps({"workspaces": {"default": {"openrouter": "sk-or-test-d0d0", "linear": "lin-test-def"},
                                                        "work": {"openrouter": "sk-or-test-w1x2"}}, "typesafe": "ts-test"}))
        for p in (mock.patch.object(fc.fc_config, "CONFIG_PATH", self.path), mock.patch.object(fc.fc_config, "AUTH_PATH", self.auth),
                  mock.patch.object(fc.fc_config, "ENV_FILE", root / ".env"),
                  mock.patch.dict(os.environ, {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}, clear=True),  # the real environment may hold real keys
                  mock.patch.object(fc.fc_config, "DEFAULT_MODEL", fc.fc_config.DEFAULT_MODEL), mock.patch.object(fc.fc_config, "FALLBACK_MODEL", fc.fc_config.FALLBACK_MODEL),
                  mock.patch.object(fc.fc_config, "OPENROUTER_KEY", None), mock.patch.object(fc.fc_config, "LINEAR_KEY", None), mock.patch.object(fc.fc_config, "PARALLEL", None), mock.patch.object(fc.fc_config, "CLAUDE_PARALLEL", 3),
                  mock.patch.object(fc.fc_config, "FALLBACK_AFTER", fc.fc_config.FALLBACK_AFTER), mock.patch.object(fc.fc_config, "TEST_READER", fc.fc_config.TEST_READER), mock.patch.object(fc.fc_config, "REVIEW_MODEL", None),
                  mock.patch.object(fc.fc_config, "WORKSPACE", fc.fc_config.WORKSPACE), mock.patch.object(fc.fc_config, "CREW_MODELS", fc.fc_config.CREW_MODELS),
                  mock.patch.object(fc.fc_config, "CREW_EFFORT", fc.fc_config.CREW_EFFORT), mock.patch.object(fc.fc_config, "CREW_FALLBACK", fc.fc_config.CREW_FALLBACK)):
            p.start(); self.addCleanup(p.stop)

    def keys(self):
        return fc.fc_config.OPENROUTER_KEY, fc.fc_config.LINEAR_KEY, os.environ.get("OPENROUTER_API_KEY"), os.environ.get("TEXT_MODEL_API_KEY")

    def same(self, got, want):
        """Compares item by item without printing either side: a wrong value may be a real key."""
        self.assertEqual(len(got), len(want))
        for i, (g, w) in enumerate(zip(got, want)):
            self.assertTrue(g == w, f"item {i} differs")

    def test_keys_come_from_auth_json_with_the_default_fallback(self):
        self.same((fc.fc_config.apply_config(), *self.keys()), ("work", "sk-or-test-w1x2", "lin-test-def", "sk-or-test-w1x2", "sk-or-test-w1x2"))
        self.same((os.environ.get("TYPESAFE_API_KEY"), "OPENCODE_CONFIG_CONTENT" in os.environ), ("ts-test", False))  # OpenCode reads its key from a file
        self.assertEqual((fc.fc_config.DEFAULT_MODEL, fc.fc_models.fallback_for("pi:a/b")), ("pi:a/b", "pi:g/h"))
        self.assertEqual((fc.fc_config.FALLBACK_AFTER, fc.fc_config.TEST_READER, fc.fc_config.REVIEW_MODEL), (3, "claude:claude-haiku-x", "claude:claude-sonnet-x"))
        ws("personal")  # no auth.json entry of its own
        self.same((fc.fc_config.apply_config(), *self.keys()), ("personal", "sk-or-test-d0d0", "lin-test-def", "sk-or-test-d0d0", "sk-or-test-d0d0"))

    def test_old_keys_move_first_one_stderr_line_each_and_stdout_stays_clean(self):
        fc_auth = fc.fc_config.fc_auth  # the module apply_config calls, also after another test file reloaded bin/
        def move(*paths):  # the move lands the old config.json key in auth.json
            self.auth.write_text(json.dumps({"workspaces": {"work": {"openrouter": "sk-or-test-mv01"}}}))
            return ["moved workspaces.work.openrouter_api_key out of config.json", "moved TYPESAFE_API_KEY out of .env"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(fc_auth, "move_keys", side_effect=move) as moved, redirect_stdout(out), redirect_stderr(err):
            fc.fc_config.apply_config()
        moved.assert_called_once_with(self.path, fc.fc_config.ENV_FILE, self.auth)
        self.same((err.getvalue().splitlines(), out.getvalue(), fc.fc_config.OPENROUTER_KEY), (move(), "", "sk-or-test-mv01"))

    def test_parallel_comes_from_the_config_and_a_workspace_overrides_it(self):
        fc.fc_config.apply_config()
        self.assertIsNone(fc.fc_config.PARALLEL)  # unset: each command keeps its own default
        cfg = fc.fc_config.read_config()
        cfg["parallel"], cfg["workspaces"]["personal"]["parallel"] = 20, 5
        self.path.write_text(json.dumps(cfg))
        fc.fc_config.apply_config()
        self.assertEqual(fc.fc_config.PARALLEL, 20)
        ws("personal")
        fc.fc_config.apply_config()
        self.assertEqual(fc.fc_config.PARALLEL, 5)
        for bad in (0, "many", 2.5, True):
            self.path.write_text(json.dumps({**cfg, "workspace": "default", "parallel": bad}))
            with self.assertRaisesRegex(SystemExit, "parallel must be a whole number"):
                fc.fc_config.apply_config()

    def test_add_key_remove_and_listing_use_auth_json_and_never_print_a_key(self):
        listing = ws("add", "client-x", stdin="sk-or-test-c1c1\n") + ws("key", "client-x", stdin="lin-test-cx\n", linear=True)
        listing += ws("key", "default", stdin="sk-or-test-r0t8\n") + ws()
        self.same([json.loads(self.auth.read_text())["workspaces"]["client-x"]], [{"openrouter": "sk-or-test-c1c1", "linear": "lin-test-cx"}])
        for line in ("work             openrouter key …w1x2, linear key inherits default", "client-x         openrouter key …c1c1, linear key set",
                     "default          openrouter key …r0t8, linear key set"):
            self.assertIn(line, listing)
        self.assertFalse(any(w in listing + self.path.read_text() for w in ("-test-", "api_key")))  # no key printed, none in config.json
        ws("remove", "client-x")
        self.assertFalse({"client-x"} & {*json.loads(self.auth.read_text())["workspaces"], *fc.fc_config.read_config()["workspaces"]})

    def test_an_empty_key_exits_instead_of_billing_default_and_names_are_checked(self):
        ws("add", "later"); ws("later")
        with self.assertRaises(SystemExit):
            fc.fc_config.apply_config()
        for bad in (("nope",), ("add", "default"), ("add", "work"), ("remove", "default"), ("add", "a b"), ("add", None)):
            self.assertRaises(SystemExit, ws, *bad)

    def test_pi_turn_reports_the_providers_error_not_stderr(self):
        err = json.dumps({"message": 'Preset "glm-5-3-flash-fast" not found for this user.', "code": 404,
                          "metadata": {"presetErrorCode": "preset_not_found", "presetSlug": "glm-5-3-flash-fast"}})
        end = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": f"404: {err}"}})
        run = mock.Mock(return_value=mock.Mock(stdout=end + "\n", stderr="Warning: No project session found with id 'x'; creating a new session\n", returncode=0))
        with mock.patch.object(fc.fc_proc, "run", run), mock.patch.object(subprocess, "run", side_effect=AssertionError("went around fc_proc.run")), \
                mock.patch.object(fc.fc_config, "WORKSPACE", "work"), self.assertRaises(RuntimeError) as cm:
            fc.fc_models.pi_turn(tempfile.mkdtemp(), "x/y", "hi")
        self.assertIn("404", str(cm.exception))
        self.assertIn("preset 'glm-5-3-flash-fast' not found in workspace 'work'", str(cm.exception))
        self.assertNotIn("No project session", str(cm.exception))

    def test_a_429_before_any_token_is_a_rate_limit(self):
        err = json.dumps({"message": "Provider returned error", "code": 429, "metadata": {"raw": "z-ai/glm-5.3-flash is temporarily rate-limited upstream."}})
        end = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": f"429: {err}"}})
        run = mock.Mock(return_value=mock.Mock(stdout=end + "\n", stderr="", returncode=0))
        with mock.patch.object(fc.fc_proc, "run", run), self.assertRaises(fc.fc_models.RateLimited):
            fc.fc_models.pi_turn(tempfile.mkdtemp(), "x/y", "hi")

    def test_a_503_overloaded_before_any_token_waits_like_a_rate_limit(self):
        end = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": HerdrPiTurn.BUSY}})
        run = mock.Mock(return_value=mock.Mock(stdout=end + "\n", stderr="", returncode=0))
        with mock.patch.object(fc.fc_proc, "run", run), self.assertRaises(fc.fc_models.RateLimited) as cm:
            fc.fc_models.pi_turn(tempfile.mkdtemp(), "x/y", "hi")
        self.assertIn("overloaded_error", cm.exception.why)


class KeepOldKeys(unittest.TestCase):
    setUp, same = Workspaces.setUp, Workspaces.same

    def old(self):
        """Old keys in config.json and .env that apply_config() moves: personal has no auth.json entry, .env's equals default's."""
        cfg = json.loads(self.path.read_text())
        cfg["parallel"], cfg["env"] = 4, {"KEEP_X": "1"}
        cfg["workspaces"]["personal"]["openrouter_api_key"] = "sk-or-test-p3p3"
        self.path.write_text(json.dumps(cfg))
        self.env = fc.fc_config.ENV_FILE
        self.env.write_text("OTHER=1\nTEXT_MODEL_API_KEY=sk-or-test-d0d0\n")
        return [p.read_bytes() for p in (self.path, self.env, self.auth)]

    def apply(self, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            ws = fc.fc_config.apply_config(**kw)
        c = fc.fc_config
        got = (ws, c.OPENROUTER_KEY, c.LINEAR_KEY, os.environ.get("OPENROUTER_API_KEY"), os.environ.get("TYPESAFE_API_KEY"), os.environ.get("KEEP_X"),
               c.WORKSPACE, c.DEFAULT_MODEL, c.FALLBACK_MODEL, c.FALLBACK_AFTER, c.TEST_READER, c.PARALLEL)
        return got, out.getvalue() + err.getvalue()

    def test_move_false_keeps_every_file_and_prints_nothing_but_applies_the_same_settings(self):
        before = self.old()
        dry, printed = self.apply(move=False)
        self.same([p.read_bytes() for p in (self.path, self.env, self.auth)], before)
        self.assertEqual(printed, "")
        self.assertEqual(len(fc.fc_config.fc_auth.old_keys(self.path, self.env)), 2)
        self.same(dry[:6], ("work", "sk-or-test-w1x2", "lin-test-def", "sk-or-test-w1x2", "ts-test", "1"))
        self.assertEqual(dry[6:], ("work", "pi:a/b", "pi:g/h", 3, "claude:claude-haiku-x", 4))
        real, printed = self.apply()  # today's move still runs: both old keys leave, one stderr line each
        self.same(real, dry)
        self.assertEqual((fc.fc_config.fc_auth.old_keys(self.path, self.env), len(printed.splitlines())), ([], 2))
        self.assertEqual(self.env.read_text(), "OTHER=1\n")

    def main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(fc.fc_config, "apply_config", return_value="default") as applied, mock.patch.object(sys, "argv", ["compose", *argv]), \
             mock.patch.object(fc.fc_setup, "cmd_setup"), mock.patch.object(fc.fc_init, "cmd_doctor"), \
             mock.patch.dict(os.environ, {"COLUMNS": "500", "NO_COLOR": "1"}), redirect_stdout(out), redirect_stderr(out):
            try:
                fc.main()
            except SystemExit as e:
                self.assertEqual(e.code, 0, out.getvalue())
        moves = [c.kwargs.get("move", c.args[0] if c.args else True) for c in applied.call_args_list]
        return moves, " ".join(out.getvalue().split())

    def test_only_setup_dry_run_passes_move_false_and_help_names_it(self):
        for argv, want in ((["setup", "--dry-run"], [False]), (["setup", "--yes", "--dry-run"], [False]), (["setup"], [True]),
                           (["setup", "--yes"], [True]), (["doctor"], [True])):
            self.assertEqual(self.main(*argv)[0], want, argv)
        sub, top = self.main("setup", "--help")[1], self.main("--help")[1]
        self.assertIn("--dry-run walk the whole setup and change nothing: every change prints as a would line", sub)
        self.assertIn("setup install and configure everything compose needs, one question at a time on a terminal; --yes takes every default and never prompts", top)
        self.assertNotIn("guided", sub + top)


class PiKeyFromEnv(unittest.TestCase):
    """pi gets the OpenRouter key from its environment only: never in argv, never from .env."""
    KEY, MODELS = "sk-or-test", ("pi:openrouter-fast/z-ai/glm@preset/p", "pi:openrouter/x/y", "pi:x/y")

    def setUp(self):
        env = Path(tempfile.mkdtemp()) / ".env"
        env.write_text('TEXT_MODEL_API_KEY="sk-or-dotenv"\n')
        for p in (mock.patch.object(fc.fc_config, "ENV_FILE", env), mock.patch.object(fc.fc_config, "OPENROUTER_KEY", self.KEY)):
            p.start(); self.addCleanup(p.stop)

    def keyless(self, argv):
        self.assertFalse([x for x in argv if x == "--api-key" or "sk-or" in str(x)], argv)

    def pi_turn(self, environ):
        run = mock.Mock(return_value=mock.Mock(stdout='{"type":"tool_execution_start"}', stderr="", returncode=0))
        with mock.patch.object(fc.fc_proc, "run", run), mock.patch.object(subprocess, "run", side_effect=AssertionError("went around fc_proc.run")), mock.patch.dict(os.environ, environ, clear=True):
            fc.fc_models.pi_turn(tempfile.mkdtemp(), "openrouter/x/y", "hi")
        return run.call_args[0][0], run.call_args[1]["env"]

    def test_pi_turn_passes_the_process_key_unchanged_and_none_in_argv(self):
        for workspace_key in (self.KEY, None):
            with self.subTest(workspace_key=workspace_key), mock.patch.object(fc.fc_config, "OPENROUTER_KEY", workspace_key):
                argv, env = self.pi_turn({"PATH": os.environ["PATH"], "OPENROUTER_API_KEY": "sk-or-env"})
                self.assertEqual(env["OPENROUTER_API_KEY"], "sk-or-env")
                self.keyless(argv)

    def test_pi_turn_never_reads_dotenv_for_the_key(self):
        argv, env = self.pi_turn({"PATH": os.environ["PATH"]})
        self.assertFalse("OPENROUTER_API_KEY" in env)
        self.keyless(argv)

    def test_pi_teammate_start_carries_no_key(self):
        for model in self.MODELS:
            proj, calls = Path(tempfile.mkdtemp()) / ".compose" / "shop", []
            proj.mkdir(parents=True)
            replies = {("agent", "list"): {"agents": []}, ("tab", "create"): {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}},
                       ("agent", "get"): {"agent": {"agent_status": "working"}}}
            herdr = lambda *args, **kw: calls.append(args) or replies.get(args[:2], {})
            a = type("A", (), {"action": "start", "role": "architect", "title": "t", "brief": "b", "model": model})
            with self.subTest(model=model), mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                    mock.patch.object(time, "sleep"), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), \
                    mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                    mock.patch.object(fc.fc_mcp, "installed_servers", return_value=[]), mock.patch.object(fc.fc_models, "model_available", return_value=(True, "")), \
                    mock.patch("builtins.print"):
                fc.fc_crew.cmd_crew(a)
                start = next(c for c in calls if c[:2] == ("agent", "start"))
                self.assertEqual(start[start.index("--model") + 1], model[3:])
                self.keyless(start)


class PiCompaction(unittest.TestCase):
    """A task's pi runs with an agent directory of its own that pins pi's default compaction, whatever the user's pi settings say:
    a huge compaction.reserveTokens there made GLM compact every few tool calls until the 180 s turn limit."""
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.user = self.root / "user-agent"
        self.user.mkdir()
        (self.user / "models.json").write_text("{}")
        (self.user / "settings.json").write_text(json.dumps({"compaction": {"enabled": True, "reserveTokens": 116072, "keepRecentTokens": 4000}}))
        (self.root / "wt").mkdir()
        p = mock.patch.object(fc.fc_models, "PI_AGENT_DIR", self.user)
        p.start(); self.addCleanup(p.stop)

    def check(self, adir):
        self.assertEqual(json.loads((Path(adir) / "settings.json").read_text()), {"compaction": {"enabled": True, "reserveTokens": 16384, "keepRecentTokens": 20000}})
        self.assertEqual((Path(adir) / "models.json").resolve(), (self.user / "models.json").resolve())  # the user's providers stay
        self.assertFalse((Path(adir) / "auth.json").exists())  # the user has none, so no dangling link
        self.assertEqual(Path(adir).parent, self.root)  # beside the clone, so `git add -A` never commits it
        self.assertIn("reserveTokens\": 116072", (self.user / "settings.json").read_text())  # the user's own file is untouched

    def test_pi_turn_runs_in_the_pinned_agent_dir(self):
        run = mock.Mock(return_value=mock.Mock(stdout='{"type":"tool_execution_start"}', stderr="", returncode=0))
        with mock.patch.object(fc.fc_proc, "run", run):
            fc.fc_models.pi_turn(str(self.root / "wt"), "openrouter/x/y", "hi")
        self.check(run.call_args[1]["env"]["PI_CODING_AGENT_DIR"])

    def test_herdr_pi_pane_runs_in_the_pinned_agent_dir(self):
        panes = []
        task = {"meta": {"id": "028", "title": "t"}, "proj": self.root / "shop", "space": "w9"}
        with mock.patch.object(fc.fc_herdr, "herdr", return_value={}), mock.patch.object(fc.fc_herdr, "herdr_agent_start", side_effect=KeyboardInterrupt), \
                mock.patch.object(fc.fc_herdr, "herdr_implementer_pane", lambda space, cwd, env, label: panes.append(env) or "p1"), self.assertRaises(KeyboardInterrupt):
            fc.fc_implement.herdr_pi(str(self.root / "wt"), "openrouter/x/y", "hi", None, task)
        self.check(panes[0]["PI_CODING_AGENT_DIR"])


class ProbeKey(unittest.TestCase):
    """A pi probe gets the process's OpenRouter key in its env, the way pi_turn does; a claude probe keeps headless_env()."""
    KEY, PI, CLAUDE = "sk-or-test-probe", ("pi:openrouter-fast/z-ai/glm@preset/p", "pi:openrouter/x/y", "pi:x/y"), "claude:opus"
    OTHERS = {"TEXT_MODEL_API_KEY": "sk-tm-test", "TYPESAFE_API_KEY": "ts-test", "OPENCODE_CONFIG_CONTENT": "oc-test"}

    def setUp(self):
        env = Path(tempfile.mkdtemp()) / ".env"
        env.write_text('TEXT_MODEL_API_KEY="sk-or-dotenv"\n')
        for p in (mock.patch.object(fc.fc_config, "ENV_FILE", env), mock.patch.object(fc.fc_config, "OPENROUTER_KEY", "sk-or-workspace"),
                  mock.patch.object(fc.fc_core, "scratch_config", return_value="/scratch/config.json")):
            p.start(); self.addCleanup(p.stop)

    def probe(self, model, environ):
        """Runs probe_once under exactly `environ` (plus PATH and HOME). Returns (argv, env, expected headless_env, result)."""
        out = '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"ok"}]}}'
        run = mock.Mock(return_value=mock.Mock(stdout='{"is_error": false}' if model.startswith("claude:") else out, stderr="", returncode=0))
        environ = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **environ}
        with mock.patch.object(subprocess, "run", run), mock.patch.dict(os.environ, environ, clear=True):
            result, headless = fc.fc_models.probe_once(model, 5), fc.fc_claude.headless_env()
        return run.call_args[0][0], run.call_args[1]["env"], headless, result

    def keyless(self, argv):
        self.assertFalse([x for x in argv if x == "--api-key" or "sk-" in str(x)], argv)

    def test_pi_probe_gets_the_process_key_unchanged_and_no_other_secret(self):
        for model in self.PI:
            with self.subTest(model=model):
                argv, env, headless, result = self.probe(model, {"OPENROUTER_API_KEY": self.KEY, **self.OTHERS})
                self.assertEqual(env.get("OPENROUTER_API_KEY"), self.KEY)  # not the workspace key, not .env's
                self.assertEqual(env, {**headless, "OPENROUTER_API_KEY": self.KEY})  # nothing else added or dropped
                self.assertFalse(set(self.OTHERS) & set(env))
                self.keyless(argv)
                self.assertEqual(result, (True, "answered", False))

    def test_pi_probe_without_the_process_key_has_none(self):
        for environ in ({}, self.OTHERS):
            with self.subTest(environ=list(environ)):
                argv, env, headless, _ = self.probe(self.PI[0], environ)
                self.assertFalse("OPENROUTER_API_KEY" in env)  # never print an env: it may hold keys
                self.assertFalse("sk-" in json.dumps(env))
                self.assertEqual(env, headless)
                self.keyless(argv)

    def test_claude_probe_env_is_headless_env_exactly(self):
        for environ in ({"OPENROUTER_API_KEY": self.KEY, **self.OTHERS}, {}):
            with self.subTest(with_key=bool(environ)):
                argv, env, headless, _ = self.probe(self.CLAUDE, environ)
                self.assertEqual(env, headless)
                self.assertFalse("OPENROUTER_API_KEY" in env)
                self.keyless(argv)


class HerdrPiKey(unittest.TestCase):
    """The pi implementer started in a herdr pane gets the key from the process environment only: never in argv, never from .env."""
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / ".env").write_text('TEXT_MODEL_API_KEY="sk-env"\n')
        (self.root / "wt").mkdir()
        p = mock.patch.object(fc.fc_config, "ENV_FILE", self.root / ".env")
        p.start(); self.addCleanup(p.stop)

    def start(self, environ, workspace_key="sk-or-test"):
        """Runs herdr_pi up to the agent start. Returns (pane env, start argv)."""
        panes, starts = [], []
        def agent_start(*args):
            starts.append(args)
            self.process_key = "OPENROUTER_API_KEY" in os.environ  # read under the patched environment
            raise KeyboardInterrupt  # stop at the start; the turn is not under test
        task = {"meta": {"id": "028", "title": "t"}, "proj": self.root / "shop", "space": "w9"}
        environ = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **environ}  # nothing from the real environment, which may hold real keys
        with mock.patch.object(fc.fc_config, "OPENROUTER_KEY", workspace_key), mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(fc.fc_herdr, "herdr", return_value={}), mock.patch.object(fc.fc_herdr, "herdr_agent_start", agent_start), \
                mock.patch.object(fc.fc_herdr, "herdr_implementer_pane", lambda space, cwd, env, label: panes.append(env) or "p1"), \
                self.assertRaises(KeyboardInterrupt):
            fc.fc_implement.herdr_pi(str(self.root / "wt"), "openrouter/x/y", "hi", None, task)
        return panes[0], starts[0]

    def keyless(self, argv):
        self.assertEqual(argv[argv.index("--model") + 1], "openrouter/x/y")
        self.assertFalse([x for x in argv if x == "--api-key" or "sk-" in str(x)], argv)

    def test_argv_has_no_key_with_or_without_a_workspace_key(self):
        for workspace_key in ("sk-or-test", None):
            for environ in ({"OPENROUTER_API_KEY": "sk-or-proc"}, {}):
                with self.subTest(workspace_key=workspace_key, environ=environ):
                    self.keyless(self.start(environ, workspace_key)[1])

    def test_pane_gets_no_key_and_nothing_from_dotenv_without_the_variable(self):
        for environ in ({}, {"TEXT_MODEL_API_KEY": "sk-tm"}):
            with self.subTest(environ=environ):
                env, _ = self.start(environ)
                self.assertFalse("OPENROUTER_API_KEY" in env)  # never print an env: it may hold keys
                self.assertFalse("sk-" in json.dumps(env))
                self.assertFalse(self.process_key)  # the fallback did not leak into the process either


    def test_the_agent_is_recorded_as_started_when_the_clone_is_a_Path(self):
        task = {"meta": {"id": "028", "title": "t"}, "proj": self.root / "shop", "space": "w9"}
        with mock.patch.object(fc.fc_herdr, "herdr", return_value={}), mock.patch.object(fc.fc_herdr, "herdr_agent_start", return_value={}), \
                mock.patch.object(fc.fc_herdr, "herdr_implementer_pane", return_value="p1"), mock.patch.object(fc.fc_herdr, "agent_pids", return_value=[4242]), \
                mock.patch.object(fc.fc_herdr, "herdr_error", return_value=(None, "stopped")), self.assertRaises(RuntimeError):  # the turn is not under test
            fc.fc_implement.herdr_pi(self.root / "wt", "openrouter/x/y", "hi", None, task)
        self.assertEqual(fc.fc_implement.fc_started.entries()[0]["cwd"], str(self.root / "wt"))
        fc.fc_implement.fc_started.forget(4242)

class HerdrPiTurn(unittest.TestCase):
    """One turn of the herdr pi implementer once its agent is up, on a fake clock: herdr answers the prompt, pi writes the session file."""
    USER = {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}
    BUSY = ('503: {"error":{"message":"Provider returned error","code":503,"metadata":{"raw":"{\\"type\\":\\"overloaded_error\\",\\"code\\":'
            '\\"start_generation_failed\\",\\"message\\":\\"Failed to start generation: tcp connect error\\"}","provider_name":"Together"}}}')

    def reply(self, text="done", stop="stop", error=None):
        m = {"role": "assistant", "content": [{"type": "text", "text": text}] if text else [], "stopReason": stop,
             "usage": {"input": 10, "output": 2} if text else {}}
        return {"type": "message", "message": {**m, **({"errorMessage": error} if error else {})}}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(fc.fc_proc.HOOKS.pop, "shop-028", None)
        self.sfile, self.now, self.calls = self.root / "s.jsonl", 1000.0, []

    def write(self, *events):
        with self.sfile.open("a") as f:
            f.writelines(json.dumps(e) + "\n" for e in events)

    def turn(self, on_prompt, status=lambda: "idle", on_wait=lambda: None):
        """herdr_pi on a live session. `on_prompt(n)` is herdr's error for the n-th `agent prompt` (None: accepted) and may write the
        session file; `status()` is the agent's state at each `agent get`; `on_wait()` runs on `agent wait`. The herdr calls land in
        self.calls as (args, clock)."""
        prompts = []
        def herdr_error(*a, timeout=None):
            self.calls.append((a, self.now))
            if a[:2] == ("agent", "prompt"):
                prompts.append(a)
                err = on_prompt(len(prompts))
                return (None, err) if err else ({}, None)
            if a[:2] == ("agent", "wait"):
                on_wait()
            return {}, None
        def herdr(*a, **k):
            self.calls.append((a, self.now))
            return {"agent": {"agent_status": status()}} if a[:2] == ("agent", "get") else {}
        clock = types.SimpleNamespace(time=lambda: self.now, sleep=lambda s: setattr(self, "now", self.now + s))
        session = {"name": "shop-028", "pane": "p1", "sfile": str(self.sfile), "seen": 0}
        task = {"meta": {"id": "028", "title": "t"}, "proj": self.root / "shop", "space": "w9"}
        with mock.patch.object(fc.fc_herdr, "herdr", herdr), mock.patch.object(fc.fc_herdr, "herdr_error", herdr_error), \
                mock.patch.object(fc.fc_implement, "time", clock), mock.patch.object(fc.fc_herdr, "time", clock):
            return fc.fc_implement.herdr_pi(str(self.root / "wt"), "openrouter/x/y", "hi", session, task, timeout=180)

    def sent(self, *what):
        return [(a, t) for a, t in self.calls if a[:len(what)] == what]

    def test_a_turn_slow_to_start_is_waited_for_in_its_pane_not_timed_out(self):
        states = iter(["idle"] * 8)  # herdr gave up on seeing work after 5 s; pi starts the turn 8 s later
        _, text, _, _ = self.turn(lambda n: "agent_prompt_stalled", status=lambda: next(states, "working"),
                                  on_wait=lambda: self.write(self.USER, self.reply("done")))
        self.assertEqual(text, "done")
        self.assertEqual((len(self.sent("agent", "prompt")), self.sent("pane", "close")), (1, []))
        (wait, _), = self.sent("agent", "wait")
        self.assertTrue(170000 <= int(wait[wait.index("--timeout") + 1]) <= 180000, wait)  # what is left of the turn's time

    def test_a_turn_herdr_never_saw_working_is_read_from_the_session_file_and_not_sent_again(self):
        polls = []
        def status():
            polls.append(1)
            if len(polls) == 40:  # past the start wait: a recorded prompt is never sent again
                self.write(self.reply("done"))
            return "idle"
        _, text, tools, _ = self.turn(lambda n: self.write(self.USER, self.reply("", stop="toolUse")) or "agent_prompt_stalled", status=status)
        self.assertEqual(text, "done")
        self.assertEqual((len(self.sent("agent", "prompt")), self.sent("agent", "wait"), self.sent("pane", "close")), (1, [], []))

    def test_a_prompt_with_no_trace_is_sent_once_more_in_the_same_session(self):
        _, text, _, _ = self.turn(lambda n: "agent_prompt_stalled" if n == 1 else self.write(self.USER, self.reply("done")))
        (first, t1), (again, t2) = self.sent("agent", "prompt")
        self.assertEqual((text, first[2:4], again[2:4], self.sent("pane", "close")), ("done", ("shop-028", "hi"), ("shop-028", "hi"), []))
        self.assertGreaterEqual(t2 - t1, fc.fc_herdr.PROMPT_START_WAIT)

    def test_a_turn_cut_at_the_time_limit_carries_the_usage_it_had_written(self):
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            self.turn(lambda n: self.write(self.USER, self.reply("halfway", stop="toolUse")) or "timeout")
        self.assertEqual((cm.exception.usage["tokens_in"], cm.exception.usage["tokens_out"]), (10, 2))
        self.assertEqual(self.sent("pane", "close")[0][0], ("pane", "close", "p1"))

    def test_a_turn_that_never_starts_ends_as_a_timeout_after_one_resend(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.turn(lambda n: "agent_prompt_stalled")
        self.assertEqual(len(self.sent("agent", "prompt")), 2)
        self.assertEqual([a for a, _ in self.calls[-2:]], [("agent", "send-keys", "shop-028", "esc"), ("pane", "close", "p1")])
        self.assertLess(self.now - 1000, 180)

    def test_a_busy_provider_or_a_429_before_any_token_waits_like_a_rate_limit(self):
        for err, why in ((self.BUSY, "provider busy: 503"), ("429 Provider returned error: rate limit exceeded", "rate-limited by provider")):
            with self.subTest(why=why):
                self.sfile.write_text("")
                with self.assertRaises(fc.fc_models.RateLimited) as cm:
                    self.turn(lambda n: self.write(self.USER, self.reply(None, stop="error", error=err)))
                self.assertTrue(cm.exception.why.startswith(why), cm.exception.why)

    def test_a_definite_refusal_still_ends_the_turn_with_the_providers_message(self):
        err = '404: {"message":"Preset \\"glm\\" not found for this user.","code":404}'
        with self.assertRaises(RuntimeError) as cm:
            self.turn(lambda n: self.write(self.USER, self.reply(None, stop="error", error=err)))
        self.assertNotIsInstance(cm.exception, fc.fc_models.RateLimited)
        self.assertIn("provider refused the request: 404", str(cm.exception))


class TypeSafeKey(unittest.TestCase):
    """`compose workspace key <name> --typesafe` stores the one TypeSafe key at auth.json's top level; the other paths are unchanged."""
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.auth = self.root / "auth.json"
        (self.root / "config.json").write_text(json.dumps({"workspaces": {"work": {}}}))
        self.auth.write_text(json.dumps({"workspaces": {"default": {"openrouter": "sk-or-test-d0d0"}, "work": {"linear": "lin-test-w0"}},
                                         "typesafe": "ts-test-old"}))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "ANTHROPIC_"))} | {
            "HOME": str(self.root), "COMPOSE_CONFIG": str(self.root / "config.json"), "NO_COLOR": "1"}

    def key(self, *args, stdin):
        p = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "compose"), "workspace", "key", *args],
                           input=stdin, capture_output=True, text=True, env=self.env, timeout=60)
        self.assertNotIn("-test-", p.stdout + p.stderr)  # never a key value, not even the old one
        return p

    def stored(self):
        return json.loads(self.auth.read_text())

    def test_stores_the_top_level_key_keeping_every_other_entry(self):
        for name in ("work", "default"):  # a named workspace still writes the one shared key, never workspaces.<name>.typesafe
            with self.subTest(name=name):
                p = self.key(name, "--typesafe", stdin=f"  ts-test-{name}\n")
                self.assertEqual(p.returncode, 0)
                self.assertIn("TypeSafe key set (every workspace uses it)", p.stdout.splitlines())
                self.assertTrue(self.stored() == {"workspaces": {"default": {"openrouter": "sk-or-test-d0d0"}, "work": {"linear": "lin-test-w0"}},
                                                  "typesafe": f"ts-test-{name}"}, "auth.json differs")
        self.assertNotIn("test", (self.root / "config.json").read_text())

    def test_an_empty_read_exits_and_names_the_command(self):
        p = self.key("work", "--typesafe", stdin="\n")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no key read; copy it, then: pbpaste | compose workspace key work --typesafe", p.stderr)
        self.assertTrue(self.stored()["typesafe"] == "ts-test-old", "the old key changed")

    def test_typesafe_with_linear_is_not_allowed(self):
        p = self.key("work", "--typesafe", "--linear", stdin="ts-test-x\n")
        self.assertEqual(p.returncode, 2)
        self.assertRegex(p.stderr, r"argument --(typesafe|linear): not allowed with argument --(typesafe|linear)")
        self.assertTrue(self.stored()["typesafe"] == "ts-test-old", "the old key changed")

    def test_openrouter_and_linear_paths_leave_the_typesafe_key_alone(self):
        self.assertEqual(self.key("work", stdin="sk-or-test-w1\n").returncode, 0)
        self.assertEqual(self.key("work", "--linear", stdin="lin-test-w2\n").returncode, 0)
        self.assertTrue(self.stored() == {"workspaces": {"default": {"openrouter": "sk-or-test-d0d0"}, "work": {"openrouter": "sk-or-test-w1", "linear": "lin-test-w2"}},
                                          "typesafe": "ts-test-old"}, "auth.json differs")


class RetryMessage(unittest.TestCase):
    def run_three(self, summary="", backup=None, session="s", reader_cost=0.0):
        """Attempts write a, b, yes; the test prints 5,000 copies and fails until yes. Returns (result, prompts, logs seen, log path);
        the run's log lines land in self.said."""
        root = Path(tempfile.mkdtemp())
        repo, proj = root / "repo", root / "proj"
        repo.mkdir(); proj.mkdir()
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (repo / "a.txt").write_text("no\n")
        git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        test = "python3 -c \"s=open('a.txt').read().strip(); print(s*5000); raise SystemExit(s!='yes')\""
        task = {"id": "001", "title": "answer", "files": ["a.txt"], "protected": [], "test": test,
                "model": PRIMARY, "after": "", "linear": "ENG-1", "body": "## Goal\nWrite yes."}
        log = proj / "results" / "001.test.log"
        prompts, seen = [], []
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            prompts.append(prompt)
            seen.append(log.read_text() if log.exists() else None)
            (Path(cwd) / "a.txt").write_text(["a", "b", "yes"][len(prompts) - 1] + "\n")
            return session, "", 1, {"tokens_in": 1, "tokens_out": 1, "cost": 0.0}
        self.said = []
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_implement, "read_failure", return_value=(summary, {"cost": reader_cost, "reference_cost": 0.5} if summary else None)), \
                mock.patch.object(fc.fc_core, "say", self.said.append), \
                mock.patch.object(fc.fc_config, "DEFAULT_MODEL", PRIMARY), mock.patch.object(fc.fc_config, "FALLBACK_MODEL", backup), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            return fc.fc_implement.run_task(repo, proj, task, 3), prompts, seen, log

    def test_retry_sends_a_short_tail_and_the_full_log_path(self):
        r, prompts, seen, log = self.run_three(summary="case_x expected yes, got a")
        self.assertEqual(r["status"], "passed")
        self.assertEqual(seen[1:], ["a" * 5000 + "\n", "b" * 5000 + "\n"])  # whole output, overwritten by the next failed attempt
        for p, ch in ((prompts[1], "a"), (prompts[2], "b")):
            self.assertIn("What failed:\ncase_x expected yes, got a\n", p)
            tail = p.split("Output (tail):\n", 1)[1].split("Full output: ", 1)[0]
            self.assertTrue(900 <= tail.count(ch) <= 1000, tail.count(ch))
            path = p.split("Full output: ", 1)[1].split(";", 1)[0]
            self.assertTrue(Path(path).is_absolute(), path)
            self.assertEqual(Path(path).resolve(), log.resolve())
        self.assertEqual([len(e["test_output"]) for e in r["log"][:2]], [3000, 3000])

    def test_reader_cost_counts_and_shows(self):
        r, _, _, _ = self.run_three(summary="case_x expected yes, got a", reader_cost=0.01)
        self.assertAlmostEqual(r["cost"], 0.02)
        fails = [l for l in self.said if " attempt " in l and " fail " in l]
        self.assertEqual(len(fails), 2)
        self.assertTrue(all("reader $0.0100" in l for l in fails), fails)

    def test_fallback_starts_from_the_task_and_ends_with_the_same_problems(self):
        r, prompts, _, _ = self.run_three(backup=BACKUP)
        self.assertEqual(r["log"][1]["fallback"]["to"], BACKUP)  # after models.fallback_after (2) misses
        self.assertTrue(prompts[2].startswith(prompts[0]))
        problems = prompts[2][prompts[2].index("The test still fails."):]
        self.assertIn("Full output: ", problems)
        self.assertLess(len(problems), 2000)  # the short tail, not the 3,000-character one


class ProcGroupTurns(unittest.TestCase):
    """Task 002 (NEB-919): pi_turn and opencode start through fc_proc.run; timeout and stop."""
    def setUp(self):
        self.addCleanup(fc.fc_proc.STOP.clear); self.addCleanup(fc.fc_proc.HOOKS.clear); self.addCleanup(fc.fc_proc.RUNNING.clear)
        self.d = Path(tempfile.mkdtemp()); (self.d / "bin").mkdir(); (self.d / "wt").mkdir(); self.wt = str(self.d / "wt"); self.addCleanup(shutil.rmtree, self.d, True)

    def fake(self, body):
        for f in (self.d / "bin" / "pi", self.d / "bin" / "opencode"): f.write_text(f"#!/bin/sh\n{body}\n"); f.chmod(0o755)
        p = mock.patch.dict(os.environ, {"PATH": f"{self.d / 'bin'}{os.pathsep}{os.environ['PATH']}"}); p.start(); self.addCleanup(p.stop)

    def test_both_turns_start_through_fc_proc_run_with_todays_argv_env_and_timeout(self):
        for name, turn, timeout, want, out, extra in (("pi", fc.fc_models.pi_turn, None, fc.fc_models.TURN_TIMEOUT, '{"type":"tool_execution_start"}', {"PI_CODING_AGENT_DIR": str(self.d / "wt.pi-agent")}),
                                                      ("opencode", fc.fc_models.opencode, 7, 7, '{"type":"text","sessionID":"s","part":{"text":"ok"}}',
                                                       {"OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1"})):
            with self.subTest(name), mock.patch.object(fc.fc_proc, "run", return_value=subprocess.CompletedProcess([], 0, out, "")) as run, \
                    mock.patch.object(subprocess, "run", side_effect=AssertionError("went around fc_proc.run")):
                turn(self.wt, "openrouter/x/y", "hi", timeout=timeout)
                (argv,), kw = run.call_args
                self.assertEqual((argv[0], argv[-3:], kw["cwd"], kw["timeout"]), (name, ["--model", "openrouter/x/y", "hi"], self.wt, want))
                self.assertEqual({k: v for k, v in kw["env"].items() if k != "COMPOSE_CONFIG"}, {k: v for k, v in {**fc.fc_core.clean_env(), **extra}.items() if k != "COMPOSE_CONFIG"})

    def test_after_the_stop_no_turn_starts(self):
        self.fake(f"touch {self.d / 'started'}; echo '{{\"type\":\"tool_execution_start\"}}'; echo '{{\"type\":\"text\",\"sessionID\":\"s\",\"part\":{{\"text\":\"ok\"}}}}'")
        fc.fc_proc.STOP.set()
        self.assertRaises(fc.fc_proc.Stopped, fc.fc_models.pi_turn, self.wt, "openrouter/x/y", "hi")
        self.assertRaises(fc.fc_proc.Stopped, fc.fc_models.opencode, self.wt, "openrouter/x/y", "hi")
        self.assertFalse((self.d / "started").exists(), "a turn started after the stop")


class RunnerReadGuard(unittest.TestCase):
    """Task 007 (NEB-955): pi_turn and opencode argv, env and config with and without a guard file."""

    def turn(self, runner, guard, key=None):
        wt = Path(tempfile.mkdtemp()) / "001"; wt.mkdir(); self.addCleanup(shutil.rmtree, wt.parent, True); g = wt.parent / "001.guard.json"
        if guard:
            g.write_text(json.dumps({"root": str(wt), "allow": [], "log": str(wt.parent / "001.refused")}))
        out = '{"type":"tool_execution_start"}' if runner is fc.fc_models.pi_turn else '{"type":"text","sessionID":"s","part":{"text":"ok"}}'
        with mock.patch.object(fc.fc_proc, "run", return_value=subprocess.CompletedProcess([], 0, out, "")) as run, \
                mock.patch.object(subprocess, "run", run), mock.patch.object(fc.fc_readguard, "command", lambda f: f"guard {f}"), \
                mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": key} if key else {}):
            runner(str(wt), "openrouter/x/y", "hi")
        argv, env = run.call_args.args[0], run.call_args.kwargs["env"]
        self.assertEqual(env.get("COMPOSE_READGUARD"), f"guard {g}" if guard else None)
        return argv, env

    def test_pi_turn_loads_the_extension_only_with_a_guard_file(self):
        argv, _ = self.turn(fc.fc_models.pi_turn, True)
        self.assertIn(["-e", str(fc.fc_readguard.PI_EXT)], [argv[i:i + 2] for i in range(len(argv))])
        self.assertNotIn("-e", self.turn(fc.fc_models.pi_turn, False)[0])

    def test_opencode_config_adds_the_plugin_and_deny_rules_to_the_key_block_only_with_a_guard_file(self):
        rules = {"plugin": [f"file://{fc.fc_readguard.OC_PLUGIN}"], "permission": {"external_directory": "deny",
                 "bash": {"*": "deny", "trail ask *": "allow", "trail callers *": "allow"}}}
        block = lambda: {"provider": {"openrouter": {"options": {"apiKey": f"{{file:{fc.fc_config.key_file('sk-x')}}}"}}}}
        for guard, key, want in [(True, "sk-x", {**block(), **rules}), (True, None, rules), (False, "sk-x", block()), (False, None, None)]:
            env = self.turn(fc.fc_models.opencode, guard, key)[1]
            got = env.get("OPENCODE_CONFIG_CONTENT")
            self.assertEqual(got and json.loads(got), want, (guard, key))


class ReadGuardWiring(unittest.TestCase):
    """Task 009 (NEB-946): run_task writes the guard, herdr pi loads it, refusals are counted per attempt."""
    USAGE = {"tokens_in": 1, "tokens_out": 1, "cost": 0.0}

    def run_one(self, impl, task_extra=None, attempts=3, files={"a.txt": "no\n"}, pack="", stale=0, figma=None):
        root = Path(tempfile.mkdtemp()); repo, proj = root / "repo", root / "proj"; repo.mkdir(); proj.mkdir(); self.addCleanup(shutil.rmtree, root, True)
        if figma:
            (proj / "linear.json").write_text(json.dumps({"figma": figma}))
        (proj / "worktrees").mkdir(); (proj / "worktrees" / "001.refused").write_text("{}\n" * stale)  # a previous run's refusals
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        for f, t in files.items(): (repo / f).parent.mkdir(parents=True, exist_ok=True); (repo / f).write_text(t)
        git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        task = {"id": "001", "title": "answer", "files": ["a.txt"], "protected": [], "test": "grep -q yes a.txt",
                "model": "pi:p/primary", "after": "", "linear": "ENG-1", "body": "## Goal\nWrite yes.", **(task_extra or {})}
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=pack), \
                mock.patch.object(fc.fc_config, "TEST_READER", None), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            return fc.fc_implement.run_task(repo, proj, task, attempts), repo, proj

    def test_the_guard_beside_the_clone_allows_files_protected_tests_and_the_pack_before_the_turn(self):
        files = {"a.txt": "no\n", "t.sh": "x\n", "lib/x.py": "y\n"}
        for pack, allowed in [("1. f · function\n   lib/x.py:L1-L2\n   t.sh:L3-L4", {"a.txt", "t.sh", "lib/x.py"}), ("", {"a.txt", "t.sh"})]:
            seen = []
            def impl(cwd, *a, **k):
                g = Path(cwd).parent / "001.guard.json"
                seen.append(g.exists() and json.loads(g.read_text()))
                (Path(cwd) / "a.txt").write_text("yes\n")
                return ("s", "", 1, self.USAGE)
            _, _, proj = self.run_one(impl, {"protected": ["t.sh"]}, files=files, pack=pack)
            wt = proj / "worktrees" / "001"
            self.assertTrue(seen[0], "no guard file beside the clone when the turn started")
            self.assertEqual(set(seen[0]["allow"]), {os.path.realpath(wt / f) for f in allowed}, pack)

    def test_a_code_task_reads_its_figma_nodes_from_the_clone_and_never_commits_them(self):
        doc = {"metadata": {"name": "f"}, "nodes": [{"id": "7:21", "name": "message", "type": "FRAME"}], "globalVars": {"styles": {}}}
        fetched, seen = [], []
        def fetch(key, node=None, depth=None):
            fetched.append((key, node))
            return json.dumps(doc)
        def impl(cwd, model, prompt, *a, **k):
            g = json.loads((Path(cwd).parent / "001.guard.json").read_text())
            seen.append((prompt, g["allow"], json.loads((Path(cwd) / ".figma" / "7-21.json").read_text())))
            (Path(cwd) / "a.txt").write_text("yes\n")
            return ("s", "", 1, self.USAGE)
        with mock.patch.object(fc.fc_figma, "framelink_fetch", fetch):
            result, _, proj = self.run_one(impl, {"figma_nodes": ["7-21"]}, figma="KEY")
        wt = proj / "worktrees" / "001"
        self.assertEqual(result["status"], "passed")
        self.assertEqual(fetched, [("KEY", "7:21")])
        prompt, allow, doc = seen[0]
        self.assertEqual(doc["nodes"][0]["name"], "message")
        self.assertIn(os.path.realpath(wt / ".figma" / "7-21.json"), allow)
        self.assertIn(str(wt.resolve() / ".figma" / "7-21.json"), prompt)
        tracked = subprocess.run(["git", "ls-tree", "-r", "--name-only", result["branch"]], cwd=wt, capture_output=True, text=True).stdout
        self.assertNotIn(".figma", tracked)

    def test_each_attempt_counts_only_its_own_refusals_in_the_result_and_the_run_log(self):
        def impl(cwd, *a, **k):
            first = not (Path(cwd).parent / "turned").exists(); (Path(cwd).parent / "turned").touch()
            with open(Path(cwd).parent / "001.refused", "a") as f:
                f.write('{"tool": "read", "args": {}}\n' * (2 if first else 1))
            (Path(cwd) / "a.txt").write_text("no\n" if first else "yes\n")
            return ("s", "", 1, self.USAGE)
        out = io.StringIO()
        with redirect_stdout(out):
            result, _, proj = self.run_one(impl, stale=5)
        self.assertEqual(result["status"], "passed")
        self.assertEqual([e.get("refused") for e in json.loads((proj / "results" / "001.json").read_text())["log"]], [2, 1])
        self.assertRegex(out.getvalue(), r"001 attempt 1 fail .*lines=\d+\s+refused=2\b")
        self.assertRegex(out.getvalue(), r"001 attempt 2 pass .*lines=\d+\s+refused=1\b")

    def test_herdr_pi_loads_the_extension_and_the_guard_command_only_with_a_guard_file(self):
        for guard in (True, False):
            root = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, True); (root / "wt").mkdir(); g = root / "wt.guard.json"
            if guard:
                g.write_text(json.dumps({"root": str(root / "wt"), "allow": [], "log": str(root / "wt.refused")}))
            panes, starts = [], []
            task = {"meta": {"id": "028", "title": "t"}, "proj": root / "shop", "space": "w9"}
            with mock.patch.object(fc.fc_herdr, "herdr", return_value={}), \
                    mock.patch.object(fc.fc_herdr, "herdr_agent_start", lambda *a: starts.append(list(a)) or (_ for _ in ()).throw(KeyboardInterrupt)), \
                    mock.patch.object(fc.fc_herdr, "herdr_implementer_pane", lambda space, cwd, env, label: panes.append(env) or "p1"), \
                    mock.patch.object(fc.fc_readguard, "command", lambda f: f"guard {f}"), self.assertRaises(KeyboardInterrupt):
                fc.fc_implement.herdr_pi(str(root / "wt"), "openrouter/x/y", "hi", None, task)
            pairs = [starts[0][i:i + 2] for i in range(len(starts[0]))]
            self.assertEqual(["-e", str(fc.fc_readguard.PI_EXT)] in pairs, guard)
            self.assertEqual(panes[0].get("COMPOSE_READGUARD"), f"guard {g}" if guard else None)


class LibrarySection(unittest.TestCase):
    """Task 012 (NEB-950): the LIBRARY API section in the implementer prompt and the shared budget."""
    def run_one(self, section, pack=True):
        """run_task with fc_libapi.section answering `section` and a fake trail_pack that fills its cap (or gives nothing).
        Returns (first prompt, the caps the pack got, the section calls, the clone)."""
        root = Path(tempfile.mkdtemp()); repo, proj = root / "repo", root / "proj"; repo.mkdir(); proj.mkdir(); self.addCleanup(shutil.rmtree, root, True)
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        for f, t in {"a.txt": "no\n", "t.sh": "grep -q yes a.txt  # TEST TEXT\n"}.items(): (repo / f).write_text(t)
        git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        task = {"id": "001", "title": "answer", "files": ["a.txt"], "protected": ["t.sh", "gone.sh"], "test": "sh t.sh",
                "model": "pi:p/primary", "after": "", "linear": "ENG-1", "body": "## Goal\nWrite yes.\n## Notes\nKeep it short."}
        prompts, caps = [], []
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            prompts.append(prompt); (Path(cwd) / "a.txt").write_text("yes\n")
            return "s", "", 1, {"tokens_in": 1, "tokens_out": 1, "cost": 0.0}
        def trail_pack(wt, query, repo=None, limit=6, cap=12000):
            caps.append(cap); return "P" * cap if pack else ""
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", trail_pack), \
                mock.patch.object(fc.fc_libapi, "section", return_value=section) as sec, mock.patch.object(fc.fc_config, "TEST_READER", None), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            self.assertEqual(fc.fc_implement.run_task(repo, proj, task, 3)["status"], "passed")
        return prompts[0], caps, sec.call_args_list, proj / "worktrees" / "001"

    def test_the_section_sits_between_the_pack_and_the_body_and_shares_the_12000_budget(self):
        section = "LIBRARY API for the packages this task imports\nlib 1.0\nlib.f(x, y=2)"
        prompt, caps, calls, wt = self.run_one(section)
        self.assertEqual(len(calls), 1)
        (cwt, files, words), kw = calls[0]
        self.assertEqual((Path(cwt).resolve(), files, kw), (wt.resolve(), ["a.txt"], {}))
        self.assertIn("Write yes.", words); self.assertIn("TEST TEXT", words); self.assertNotIn("Keep it short.", words)
        self.assertEqual(caps, [12000 - len(section)])
        pack = "P" * caps[0]
        self.assertLess(prompt.index(pack + "\n"), prompt.index(section))
        self.assertLess(prompt.index(section) + len(section), prompt.index("## Goal\nWrite yes."))
        self.assertNotIn("P" * (caps[0] + 1), prompt)


class DeleteTasks(unittest.TestCase):
    """Task 015 (NEB-952): delete: applied in the clone, and delete-only tasks with no model turn."""
    def run_one(self, impl, task_extra=None, attempts=3, files={"a.txt": "no\n"}):
        root = Path(tempfile.mkdtemp()); repo, proj = root / "repo", root / "proj"; repo.mkdir(); proj.mkdir(); self.addCleanup(shutil.rmtree, root, True)
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        for f, t in files.items(): (repo / f).parent.mkdir(parents=True, exist_ok=True); (repo / f).write_text(t)
        git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        task = {"id": "001", "title": "answer", "files": ["a.txt"], "protected": [], "test": "grep -q yes a.txt",
                "model": "pi:p/primary", "after": "", "linear": "ENG-1", "body": "## Goal\nWrite yes.", **(task_extra or {})}
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_config, "TEST_READER", None), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            return fc.fc_implement.run_task(repo, proj, task, attempts), repo, proj

    def tree(self, repo):
        branch = fc.fc_worktree.task_branch(repo, repo.parent / "proj", "001")  # run_one puts proj beside repo
        out = subprocess.run(["git", "ls-tree", "-r", "--name-only", branch], cwd=repo, capture_output=True, text=True)
        return out.stdout.split() if out.returncode == 0 else None

    def edit(self, seen, extra=lambda cwd: None):
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            seen.append(sorted(str(p.relative_to(cwd)) for p in Path(cwd).rglob("*.txt"))); (Path(cwd) / "a.txt").write_text("yes\n"); extra(Path(cwd))
            return "s", "", 1, {"tokens_in": 1, "tokens_out": 1, "cost": 0.0}
        return impl

    def test_a_delete_only_task_removes_a_400_line_directory_and_commits_with_no_turn(self):
        impl = mock.Mock(side_effect=AssertionError("no turn"))
        files = {"a.txt": "no\n", "old/a.py": "x\n" * 300, "old/sub/b.py": "y\n" * 100}
        result, repo, _ = self.run_one(impl, {"files": [], "delete": ["old/"], "test": "test ! -e old"}, files=files)
        impl.assert_not_called()
        self.assertEqual((result["status"], len(result["log"])), ("passed", 1))
        self.assertEqual(self.tree(repo), ["a.txt"])

    def test_a_delete_only_task_whose_test_fails_or_touches_other_files_fails_after_one_attempt(self):
        for test in ("grep -q yes a.txt", "test ! -e gone.txt && touch b.py"):
            impl = mock.Mock(side_effect=AssertionError("no turn"))
            result, repo, _ = self.run_one(impl, {"files": [], "delete": ["gone.txt"], "test": test}, files={"a.txt": "no\n", "gone.txt": "z\n"})
            impl.assert_not_called()
            self.assertEqual((result["status"], len(result["log"]), self.tree(repo)), ("failed", 1, None), test)

    def test_delete_paths_are_gone_before_the_turn_and_from_the_branch_beside_an_edit(self):
        seen = []
        result, repo, _ = self.run_one(self.edit(seen), {"delete": ["gone.txt"]}, files={"a.txt": "no\n", "gone.txt": "z\n", "keep.txt": "k\n"})
        self.assertEqual((result["status"], seen), ("passed", [["a.txt", "keep.txt"]]))
        self.assertEqual(self.tree(repo), ["a.txt", "keep.txt"])

    def test_a_turn_deleting_a_file_outside_files_and_delete_is_rejected_for_scope(self):
        impl = self.edit([], lambda cwd: (cwd / "keep.txt").unlink())
        result, _, _ = self.run_one(impl, {"delete": ["gone.txt"]}, attempts=1, files={"a.txt": "no\n", "gone.txt": "z\n", "keep.txt": "k\n"})
        self.assertEqual((result["status"], result["log"][0]["stray"]), ("rejected_scope", ["keep.txt"]))

    def test_a_task_without_delete_and_without_files_still_gets_a_turn_and_no_scope_check(self):
        seen = []
        result, repo, _ = self.run_one(self.edit(seen, lambda cwd: (cwd / "b.py").write_text("b\n")), {"files": []})
        self.assertEqual((result["status"], len(seen), self.tree(repo)), ("passed", 1, ["a.txt", "b.py"]))


class SiblingStubs(unittest.TestCase):
    """A task that passes its own test but removes what another task's stub in the same file owns must not pass."""
    STUBS = "def one():\n    raise NotImplementedError\n\n\ndef two():\n    raise NotImplementedError\n"
    TESTS = {"tests/test_one.py": "import unittest, m\n\n\nclass One(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(m.one(), 1)\n",
             "tests/test_names.py": "import unittest, m\n\n\nclass Names(unittest.TestCase):\n    def test_two_is_defined(self):\n        self.assertTrue(hasattr(m, 'two'))\n"}

    NAMES = "python3 -m unittest discover -s tests -p test_names.py"

    def run_one(self, answers, checks=(), tests=None, others=None, own=None, task_checks=None):
        root = Path(tempfile.mkdtemp()); repo, proj = root / "repo", root / "proj"; repo.mkdir(); proj.mkdir(); self.addCleanup(shutil.rmtree, root, True)
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        for f, t in {"m.py": self.STUBS, ".gitignore": "__pycache__/\n", **(tests or self.TESTS)}.items(): (repo / f).parent.mkdir(parents=True, exist_ok=True); (repo / f).write_text(t)
        git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        (proj / "verify.json").write_text(json.dumps({"checks": list(checks), **({} if task_checks is None else {"task_checks": list(task_checks)})}))
        task = {"id": "001", "title": "one", "files": ["m.py"], "protected": ["tests/test_one.py"], "model": "pi:p/primary", "after": "", "linear": "ENG-1",
                "test": own or "python3 -m unittest discover -s tests -p test_one.py", "body": "## Goal\nMake one() return 1."}
        if others:  # the other tasks as synced: id -> (the file it owns, its test)
            (proj / "results").mkdir(); (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [task] + [
                {"id": i, "title": i, "files": [f], "protected": [], "after": "", "linear": f"ENG-{i}", "test": t, "body": "## Goal\nx"} for i, (f, t) in others.items()]}))
        prompts, turn = [], iter(answers)
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            prompts.append(prompt); (Path(cwd) / "m.py").write_text(next(turn))
            return "s", "", 1, {"tokens_in": 1, "tokens_out": 1, "cost": 0.0}
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_config, "TEST_READER", None), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}), \
                redirect_stdout(io.StringIO()) as said:
            result = fc.fc_implement.run_task(repo, proj, task, 2)
        self.said = said.getvalue()
        return result, prompts

    def test_a_passing_task_that_deleted_a_sibling_stub_is_rejected_and_told_which_test_broke(self):
        result, prompts = self.run_one(["def one():\n    return 1\n", "def one():\n    return 1\n\n\ndef two():\n    raise NotImplementedError\n"], others={"002": ("m.py", self.NAMES)})
        self.assertEqual((result["status"], result["attempts"]), ("passed", 2))
        self.assertEqual(result["log"][0]["test_exit"], 0)
        self.assertIn("test_two_is_defined", " ".join(result["log"][0]["broke"]))
        self.assertIn("test_two_is_defined", prompts[1])

    def test_a_task_that_ends_with_a_sibling_stub_gone_does_not_pass(self):
        result, _ = self.run_one(["def one():\n    return 1\n"] * 2, others={"002": ("m.py", self.NAMES)})
        self.assertEqual(result["status"], "rejected_regression")

    def counted(self, cmd="true"):
        counter = Path(tempfile.mkdtemp()) / "runs"; self.addCleanup(shutil.rmtree, counter.parent, True)
        return counter, f"echo run >> {counter}; {cmd}"

    def test_a_whole_suite_check_runs_neither_after_the_task_nor_on_its_base(self):
        counter, suite = self.counted("python3 -m unittest discover -s tests && false")
        result, _ = self.run_one(["def one():\n    return 1\n\n\ndef two():\n    raise NotImplementedError\n"], checks=(suite,))
        self.assertEqual(result["status"], "passed")
        self.assertFalse(counter.exists())

    def test_the_test_of_a_task_owning_a_changed_file_runs_once_and_no_other_test_runs(self):
        (sibling, covering), (far, elsewhere), (mine, own) = self.counted(self.NAMES), self.counted(), self.counted("python3 -m unittest discover -s tests -p test_one.py")
        result, _ = self.run_one(["def one():\n    return 1\n\n\ndef two():\n    raise NotImplementedError\n"], own=own, others={"002": ("m.py", covering), "003": ("other.py", elsewhere)})
        self.assertEqual(result["status"], "passed")
        self.assertEqual([c.read_text().split() if c.exists() else [] for c in (sibling, far, mine)], [["run"], [], ["run"] * 2])  # the task's own test: red before the attempt, green after it, never again as a check

    def test_a_covering_test_that_fails_on_the_base_too_is_not_blamed_on_the_task(self):
        result, _ = self.run_one([self.FIX], tests=self.UNTOUCHED, others={"002": ("m.py", self.NAMES)})
        self.assertEqual((result["status"], result["log"][0].get("broke")), ("passed", None))

    def test_task_checks_replace_checks_after_a_task_and_leave_the_suite_to_verify(self):
        counter = Path(tempfile.mkdtemp()) / "runs"; self.addCleanup(shutil.rmtree, counter.parent, True)
        suite = f"echo run >> {counter}; python3 -m unittest discover -s tests"
        result, _ = self.run_one(["def one():\n    return 1\n\n\ndef two():\n    raise NotImplementedError\n"], checks=(suite,), task_checks=("true",))
        self.assertEqual((result["status"], counter.exists()), ("passed", False))
        self.assertIn("001 checks  `true` after the task", self.said)

    def test_a_check_that_was_red_before_the_task_is_not_blamed_on_it(self):
        result, _ = self.run_one(["def one():\n    return 1\n\n\ndef two():\n    raise NotImplementedError\n"], checks=("python3 -m unittest discover -s tests -p test_one.py && false",))
        self.assertEqual(result["status"], "passed")

    # test_three reads m.py as text, imports nothing the task changes, and fails before and after it.
    UNTOUCHED = {"tests/test_one.py": TESTS["tests/test_one.py"],
                 "tests/test_names.py": "import unittest\n\n\nclass Names(unittest.TestCase):\n    def test_three(self):\n        self.assertIn('def three', open('m.py').read())\n"}
    ID = "test_three (test_names.Names.test_three)"
    FIX = "def one():\n    return 1\n"

    def test_an_id_failing_after_a_task_whose_base_run_is_unknown_is_not_taken_for_base_red(self):
        unknown = "grep -q 'return 1' m.py || exit 2; python3 -m unittest discover -s tests -p test_names.py"
        result, _ = self.run_one([self.FIX], checks=(unknown,), tests=self.UNTOUCHED)
        self.assertEqual((result["status"], result["attempts"]), ("rejected_regression", 1))
        self.assertIn(self.ID, " ".join(result["log"][0]["base unknown"]))
        self.assertIn("exited 2 and printed no failing test", " ".join(result["log"][0]["base unknown"]))

    def test_an_id_that_fails_on_the_base_tree_too_is_accepted_when_the_run_on_the_base_was_unknown(self):
        counter = Path(tempfile.mkdtemp()) / "runs"; self.addCleanup(shutil.rmtree, counter.parent, True)
        flaky = f"n=$(cat {counter} 2>/dev/null || echo 0); echo $((n+1)) > {counter}; [ $n = 1 ] && exit 2; python3 -m unittest discover -s tests -p test_names.py"
        result, _ = self.run_one([self.FIX], checks=(flaky,), tests=self.UNTOUCHED)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["log"][0]["red on base (baseline unknown)"], [self.ID])


class TestTierEscalation(unittest.TestCase):
    SONNET = "claude:claude-sonnet-5-5"

    def test_a_cheap_model_escalates_to_standard_whatever_the_fallback_is(self):
        for fallback in (None, BACKUP):
            with mock.patch.object(fc.fc_config, "FALLBACK_MODEL", fallback):
                self.assertEqual(fc.fc_models.fallback_for(fc.fc_config.DEFAULT_MODEL), self.SONNET)
                self.assertEqual(fc.fc_models.fallback_for("claude:claude-haiku-5-5"), self.SONNET)

    def test_standard_and_frontier_models_have_nowhere_to_escalate(self):
        with mock.patch.object(fc.fc_config, "FALLBACK_MODEL", BACKUP):
            self.assertIsNone(fc.fc_models.fallback_for(self.SONNET))
            self.assertIsNone(fc.fc_models.fallback_for("claude:claude-opus-5-5"))

    def test_a_model_in_no_tier_keeps_the_old_rule(self):
        with mock.patch.object(fc.fc_config, "FALLBACK_MODEL", BACKUP):
            self.assertEqual(fc.fc_models.fallback_for(PRIMARY), BACKUP)
            self.assertIsNone(fc.fc_models.fallback_for(BACKUP))
        with mock.patch.object(fc.fc_config, "FALLBACK_MODEL", None):
            self.assertIsNone(fc.fc_models.fallback_for(PRIMARY))

    def test_escalation_follows_the_configured_tiers_to_the_first_standard_model(self):
        tiers = {"cheap": [("pi:a/b", None)], "standard": [("pi:s/one", None), ("pi:s/two", None)], "frontier": [("claude:claude-opus-5-5", "high")]}
        with mock.patch.object(fc.fc_config, "TIERS", tiers), mock.patch.object(fc.fc_config, "FALLBACK_MODEL", BACKUP):
            self.assertEqual(fc.fc_models.fallback_for("pi:a/b"), "pi:s/one")
            self.assertIsNone(fc.fc_models.fallback_for("pi:s/one"))
            self.assertEqual(fc.fc_models.fallback_for(PRIMARY), BACKUP)


class TestReferenceCost(unittest.TestCase):
    def result(self, turns, rate_limited=None):
        """Each entry of `turns` is the usage of one implementer turn; the first turn misses, the last one writes the answer."""
        root = Path(tempfile.mkdtemp())
        repo, proj = root / "repo", root / "proj"
        repo.mkdir(); proj.mkdir()
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (repo / "a.txt").write_text("no\n")
        git("add", "-A"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        task = {"id": "001", "title": "answer", "files": ["a.txt"], "protected": [], "test": "grep -q yes a.txt",
                "model": PRIMARY, "after": "", "linear": "ENG-1", "body": "## Goal\nWrite yes."}
        usages = list(turns)
        def run_impl(cwd, model, prompt, session=None, task=None, timeout=None):
            if rate_limited and model == PRIMARY:
                raise fc.fc_models.RateLimited(rate_limited)
            usage = {"tokens_in": 1, "tokens_out": 1, **usages.pop(0)}
            (Path(cwd) / "a.txt").write_text("yes\n" if not usages else "nope\n")
            return "s", "", 1, usage
        with mock.patch.object(fc.fc_implement, "implementer", run_impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_config, "DEFAULT_MODEL", PRIMARY), mock.patch.object(fc.fc_config, "FALLBACK_MODEL", BACKUP), \
                mock.patch.object(fc.fc_config, "FALLBACK_AFTER", 2), mock.patch.object(fc.fc_config, "TEST_READER", None), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            fc.fc_implement.run_task(repo, proj, task, 3)
        return json.loads((proj / "results" / "001.json").read_text())

    def test_a_real_cost_stays_apart_from_the_reference_cost(self):
        r = self.result([{"cost": 0.125, "reference_cost": 0.25}] * 2)
        self.assertEqual((r["reference_cost"], r["cost"]), (0.5, 0.25))

    def test_a_missing_or_none_reference_cost_counts_as_zero(self):
        for usage in ({"cost": 0.0}, {"cost": 0.0, "reference_cost": None}):
            self.assertEqual(self.result([usage] * 2)["reference_cost"], 0.0)
        self.assertEqual(self.result([{"cost": 0.0, "reference_cost": None}, {"cost": 0.0, "reference_cost": 0.25}])["reference_cost"], 0.25)

    def test_a_rate_limited_attempt_adds_its_reference_cost(self):
        r = self.result([{"cost": 0.0, "reference_cost": 0.25}] * 2, rate_limited={"cost": 0.0, "reference_cost": 0.125})
        self.assertEqual(r["reference_cost"], 0.625)
