import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
import sys, time, types
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""
os.environ["COMPOSE_E2E_HEADLESS"] = "1"  # testing the tool itself never opens a browser window; real projects keep theirs

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


PRIMARY, BACKUP = "pi:p/primary", "pi:b/backup"


class Fallback(unittest.TestCase):
    def test_fallback_has_no_fallback(self):
        with mock.patch.object(fc.fc_config, "FALLBACK_MODEL", BACKUP):
            self.assertIsNone(fc.fc_models.fallback_for(BACKUP))
            self.assertIsNone(fc.fc_models.fallback_for("claude"))


class AttemptLoop(unittest.TestCase):
    """The decisions of fc_attempt that run_task acts on: facts in, decision out."""
    def test_the_fallback_takes_over_after_the_configured_misses_and_only_when_there_is_one(self):
        self.assertEqual([fc.fc_implement.fc_attempt.fallback_due(n, BACKUP, 2) for n in (1, 2, 3)], [False, True, True])
        self.assertTrue(fc.fc_implement.fc_attempt.fallback_due(1, BACKUP, 1))
        self.assertFalse(fc.fc_implement.fc_attempt.fallback_due(5, None, 2))  # a bench run, or a model with nowhere to fall back to

    def test_a_rate_limit_switches_at_once_when_there_is_a_fallback_and_costs_no_wait(self):
        self.assertEqual(fc.fc_implement.fc_attempt.rate_step(BACKUP, 0), ("switch", 0, 0))
        self.assertEqual(fc.fc_implement.fc_attempt.rate_step(BACKUP, 3), ("switch", 3, 0))

    def test_a_busy_provider_without_a_fallback_waits_longer_each_time_and_gives_up_after_four(self):
        steps, waits = [], 0
        for _ in range(5):
            step, waits, seconds = fc.fc_implement.fc_attempt.rate_step(None, waits)
            steps.append((step, waits, seconds))
        self.assertEqual(steps, [("wait", 1, 20), ("wait", 2, 40), ("wait", 3, 60), ("wait", 4, 80), ("give_up", 5, 100)])

    def test_a_turn_that_timed_out_across_a_sleep_costs_no_attempt_and_no_step_to_the_fallback(self):
        self.assertEqual(fc.fc_implement.fc_attempt.timeout_step(False, 5220), "refund")
        self.assertEqual(fc.fc_implement.fc_attempt.timeout_step(False, 0), "miss")

    def test_after_the_stop_an_implementer_error_or_timeout_is_no_attempt_and_starts_no_fallback(self):
        for nap in (0, 5220):
            self.assertEqual(fc.fc_implement.fc_attempt.timeout_step(True, nap), "stop")
        for fallback in (BACKUP, None):
            self.assertEqual(fc.fc_implement.fc_attempt.error_step(True, fallback), "stop")
        self.assertEqual((fc.fc_implement.fc_attempt.error_step(False, BACKUP), fc.fc_implement.fc_attempt.error_step(False, None)), ("retry", "end"))

    def test_the_turn_limit_leaves_no_room_for_a_test_run_whatever_the_task_allows_its_test(self):
        for meta in ({}, {"test_timeout": 300}, {"test_timeout": 5}):
            self.assertEqual(fc.fc_implement.fc_attempt.turn_limit(meta), fc.fc_models.TURN_TIMEOUT)

    def test_a_refused_turn_a_payment_stop_or_a_turn_slept_in_is_no_attempt_and_a_turn_and_its_test_are_one(self):
        log = [{"attempt": 1, "rate_limited": True}, {"attempt": 1}, {"attempt": 1, "cut_short": True}, {"attempt": 2, "slept": 5220},
               {"attempt": 2, "payment_required": {}}, {"attempt": 3}, {"leak_reverted": ["a.txt"]}]
        self.assertEqual(fc.fc_implement.fc_attempt.attempts_spent(log), 2)
        self.assertEqual(fc.fc_implement.fc_attempt.attempts_spent([]), 0)

    def test_a_cut_turn_carries_its_tools_and_usage_and_a_bare_one_costs_nothing(self):
        spent = {"cost": 0.5, "tokens_in": 100, "tokens_out": 20}
        e = subprocess.TimeoutExpired("herdr agent prompt", 180)
        e.tools, e.usage = 4, spent
        self.assertEqual(fc.fc_implement.fc_attempt.cut_of(e), (4, spent))
        self.assertEqual(fc.fc_implement.fc_attempt.cut_of(subprocess.TimeoutExpired("pi", 1)), (0, {"cost": 0.0, "tokens_in": 0, "tokens_out": 0}))


class UsageTally(unittest.TestCase):
    def test_a_real_cost_stays_apart_from_the_reference_cost(self):
        cost, ref = 0.0, 0.0
        for usage in [{"cost": 0.125, "reference_cost": 0.25}] * 2:
            cost, ref = fc.fc_implement.fc_attempt.tally(cost, ref, usage)
        self.assertEqual((ref, cost), (0.5, 0.25))

    def test_a_missing_or_none_reference_cost_counts_as_zero(self):
        for usage in ({"cost": 0.0}, {"cost": 0.0, "reference_cost": None}):
            self.assertEqual(fc.fc_implement.fc_attempt.tally(0.0, 0.0, usage), (0.0, 0.0))
        self.assertEqual(fc.fc_implement.fc_attempt.tally(0.0, 0.0, {"cost": 0.0, "reference_cost": 0.25}), (0.0, 0.25))

    def test_a_rate_limited_turn_and_a_reader_add_their_reference_cost_too(self):
        cost, ref = fc.fc_implement.fc_attempt.tally(0.0, 0.0, {"cost": 0.0, "reference_cost": 0.125})  # the refused turn
        cost, ref = fc.fc_implement.fc_attempt.tally(cost, ref, {"cost": 0.0, "reference_cost": 0.25})  # the turn that answered
        cost, ref = fc.fc_implement.fc_attempt.tally(cost, ref, {"cost": 0.0625, "reference_cost": 0.25})  # the test reader
        self.assertEqual((cost, ref), (0.0625, 0.625))


class AttemptVerdict(unittest.TestCase):
    def verdict(self, code=0, over=False, new_docs=(), stray=(), broke=(), cannot=()):
        return fc.fc_implement.fc_attempt.verdict_of(code, over, list(new_docs), list(stray), list(broke), list(cannot))

    def test_a_passing_test_with_nothing_over_its_limits_passes(self):
        self.assertEqual(self.verdict(), "pass")
        self.assertEqual(self.verdict(code=1), "fail")

    def test_a_passing_task_that_removed_a_sibling_stub_is_rejected_for_the_regression(self):
        for kw in ({"broke": ["test_sibling (tests.test_b.B.test_sibling)"]}, {"cannot": ["`tests.run` base unknown"]}):
            self.assertEqual(self.verdict(**kw), "fail")
        self.assertEqual(fc.fc_implement.fc_attempt.miss_status(0, False, [], [], ["test_sibling"], "failed"), "rejected_regression")

    def test_the_status_of_a_missed_attempt_names_the_first_limit_the_passing_test_broke(self):
        miss = lambda **kw: fc.fc_implement.fc_attempt.miss_status(0, kw.get("over", False), kw.get("docs", []), kw.get("stray", []), kw.get("broke", []), "failed")
        self.assertEqual(miss(over=True, docs=["a.md"], stray=["b"], broke=["t"]), "rejected_size")
        self.assertEqual(miss(docs=["a.md"], stray=["b"], broke=["t"]), "rejected_docs")
        self.assertEqual(miss(stray=["b"], broke=["t"]), "rejected_scope")
        self.assertEqual(miss(broke=["t"]), "rejected_regression")
        self.assertEqual(miss(), "failed")
        self.assertEqual(fc.fc_implement.fc_attempt.miss_status(1, True, ["a.md"], ["b"], ["t"], "rejected_size"), "rejected_size")  # a failing test leaves the status as it was

    def test_a_tampered_file_or_an_unknown_base_ends_the_task_and_a_pass_ends_it_passed(self):
        settle = lambda verdict="fail", tampered=(), cannot=(), only=False, status="failed": fc.fc_implement.fc_attempt.settled(verdict, list(tampered), list(cannot), only, status)
        self.assertEqual(settle(tampered=["t.py"]), "rejected_tampered")
        self.assertEqual(settle(tampered=["t.py"], cannot=["x"], verdict="pass"), "rejected_tampered")
        self.assertEqual(settle(cannot=["x"]), "rejected_regression")
        self.assertEqual(settle(verdict="pass"), "passed")
        self.assertIsNone(settle())  # another attempt may follow

    def test_a_delete_only_task_ends_after_its_one_attempt_whatever_its_test_said(self):
        self.assertEqual(fc.fc_implement.fc_attempt.settled("fail", [], [], True, "failed"), "failed")
        self.assertEqual(fc.fc_implement.fc_attempt.settled("pass", [], [], True, "failed"), "passed")


class DeleteScope(unittest.TestCase):
    def test_the_deletion_is_the_whole_task_only_when_no_file_is_named(self):
        only = fc.fc_implement.fc_attempt.only_delete
        self.assertEqual([only(["old/"], []), only(["old/"], ["a.txt"]), only([], []), only([], ["a.txt"])], [True, False, False, False])

    def test_the_scope_check_allows_the_files_and_covers_a_task_that_deletes_but_names_none(self):
        self.assertEqual(fc.fc_implement.fc_attempt.scope_of(["a.txt"], []), ["a.txt"])
        self.assertEqual(fc.fc_implement.fc_attempt.scope_of([], ["gone.txt"]), [])  # a turn deleting another file is rejected for scope
        self.assertIsNone(fc.fc_implement.fc_attempt.scope_of([], []))  # no files and no delete: no scope check


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
    """Git is faked at the helpers that ask it: the ignored dependency dirs and env files, the exclude file, the stamp path and the lockfiles."""
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.root, True)
        self.repo, self.wt = self.root / "repo", self.root / "wt"
        self.repo.mkdir(); self.wt.mkdir()
        self.excluded, self.installs, self.locks = [], [], []
        def sh(cmd, cwd=None, *a, **k):
            if isinstance(cmd, str):
                self.installs.append((cmd, Path(cwd)))
                return mock.Mock(stdout="", returncode=0)
            out = str(self.root / "stamp") if "--git-path" in cmd else "".join(f"100644 blob abc\t{f}\n" for f in self.locks) if "ls-tree" in cmd else ""
            return mock.Mock(stdout=out, returncode=0)
        self.sh = sh

    def ready(self, deps=(), envs=(), **kw):
        with mock.patch.object(fc.fc_worktree, "dep_dirs", return_value=list(deps)), mock.patch.object(fc.fc_worktree, "env_files", return_value=list(envs)), \
                mock.patch.object(fc.fc_worktree, "exclude_paths", lambda wt, rules: self.excluded.append(rules)), mock.patch.object(fc.fc_worktree, "stamp_trail_wiring"), \
                mock.patch.object(fc.fc_core, "sh", self.sh):
            fc.fc_worktree.ready_checkout(self.repo, self.wt, **kw)

    def test_clone_borrows_installs_and_stays_clean(self):
        (self.repo / "client" / "node_modules" / "jest").mkdir(parents=True)
        (self.wt / "client").mkdir()
        self.ready(deps=["client/node_modules"])
        self.ready(deps=["client/node_modules"])  # idempotent: a re-run neither fails nor replaces the link
        self.assertTrue((self.wt / "client" / "node_modules").is_symlink())
        self.assertTrue((self.wt / "client" / "node_modules" / "jest").is_dir())
        self.assertTrue(all("/client/node_modules" in rules and "/graft/" in rules and "/.trail/" in rules for rules in self.excluded))  # the link is a file to git: /dir, not dir/

    def test_clone_borrows_ignored_env_files_only(self):
        (self.repo / "app").mkdir(); (self.wt / "app").mkdir()
        (self.repo / "app" / ".env").write_text("A=1\n"); (self.repo / "app" / ".env.local").write_text("B=2\n")
        (self.wt / "app" / ".env.example").write_text("A=\n")  # tracked: the clone has its own
        self.ready(envs=["app/.env", "app/.env.local"])
        self.ready(envs=["app/.env", "app/.env.local"])
        for name in (".env", ".env.local"):
            self.assertTrue((self.wt / "app" / name).is_symlink())
            self.assertEqual((self.wt / "app" / name).read_text(), (self.repo / "app" / name).read_text())
        self.assertFalse((self.wt / "app" / ".env.example").is_symlink())

    def test_install_command_replaces_borrowing(self):
        (self.repo / "node_modules" / "old").mkdir(parents=True)
        (self.wt / "node_modules").symlink_to(self.repo / "node_modules")  # a link left by an earlier borrow
        self.ready(deps=["node_modules"], install="bun install")
        self.assertFalse((self.wt / "node_modules").is_symlink())  # the install lives in the checkout, so build output cannot depend on the project worktree's path
        self.assertEqual(self.installs, [("bun install", self.wt)])

    def test_install_reruns_only_when_the_lockfile_changes(self):
        self.locks = ["bun.lock"]
        for _ in range(2): self.ready(install="bun install")
        self.assertEqual(len(self.installs), 1)  # the same lockfiles: no second install
        (self.root / "stamp").write_text("a different lockfile set\n")  # what the last install recorded
        self.ready(install="bun install")
        self.assertEqual(len(self.installs), 2)  # changed lockfiles install again

    def test_lockfile_task_installs_on_its_own_and_hands_it_over(self):
        (self.repo / "node_modules" / "old").mkdir(parents=True)
        for f in (["bun.lock"], ["a.js", "package-lock.json"]):
            self.assertTrue(fc.fc_worktree.names_lockfile({"files": f}))
        self.assertFalse(fc.fc_worktree.names_lockfile({"files": ["a.js"]}))
        self.ready(deps=["node_modules"], borrow=False)
        self.assertFalse((self.wt / "node_modules").exists())
        (self.wt / "node_modules" / "new").mkdir(parents=True)  # the task's own install
        real, seen = shutil.rmtree, []
        def rmtree(path, *a, **k):
            real(path, *a, **k)
            seen.append((self.repo / "node_modules").exists())
        with mock.patch.object(fc.fc_worktree, "dep_dirs", return_value=["node_modules"]), mock.patch.object(shutil, "rmtree", rmtree):
            fc.fc_worktree.adopt_installs(self.repo, self.wt)
        self.assertTrue(seen and all(seen), "the install dir was missing during the swap")
        self.assertTrue((self.repo / "node_modules" / "new").is_dir())
        self.assertFalse((self.repo / "node_modules" / "old").exists())
        self.assertFalse((self.repo / "node_modules").is_symlink())


class FixScope(unittest.TestCase):
    TRACKED = {"a.go", "yarn.lock"}

    def verify(self, writes, reply="", cfg=None, pack="", tasks=(), dirty=(), report=None, rerun=0, top=None):
        """A stack with one layer changing a.go; the first stage run fails, the fixer writes `writes`, later runs pass.
        `self.scopes` holds the tests each stage run was given (None: the full checks). `dirty` lists the paths the user left
        uncommitted before verify starts. The first stage run's `report` (by default one failing check, `c1`) is what the fix
        answers; the check exits `rerun` when verify runs it again. `top` is the task id of the stack's top layer. Git is
        faked: the stack diff names a.go, a new file is any written path outside TRACKED, and the fix diff removes the line `b`."""
        repo, proj = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        state = {}
        def sh(cmd, cwd=None, *a, **k):
            out = "a.go" if "--name-only" in cmd and cmd[-1].endswith("...HEAD") else "layer" if "branch" in cmd else \
                "\0".join(f for f in state if f not in self.TRACKED) if "ls-files" in cmd else "-b\n+fixed\n" if "-U0" in cmd else ""
            return mock.Mock(stdout=out, stderr="", returncode=0)
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            self.prompt = prompt
            for f, text in writes.items():
                (Path(cwd) / f).write_text(text); state[f] = text
            return "s", reply, 1, {"cost": 0.0}
        stages, self.scopes, self.said = iter([(False, report or {"checks": [{"cmd": "c1", "exit": 1}]}, "finding"), (True, {}, "")]), [], []
        def run_stages(repo, proj, cfg, a, tests=None):
            self.scopes.append(tests)
            return next(stages)
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=pack), \
                mock.patch.object(fc.fc_verify, "run_stages", run_stages), mock.patch.object(fc.fc_verify, "review_inputs", return_value=(0, 0, 0, [])), \
                mock.patch.object(fc.fc_tasks, "load_tasks", return_value=list(tasks)), mock.patch.object(fc.fc_verify, "changed_files", lambda repo: dict(state)), \
                mock.patch.object(fc.fc_change, "reflect", return_value=(Path("/p/reflect.json"), {})), \
                mock.patch.object(fc.fc_proc, "run", return_value=mock.Mock(returncode=rerun)), mock.patch.object(fc.fc_core, "sh", sh), \
                mock.patch.object(fc.fc_worktree, "stack_order", return_value=[f"compose/p-{top}"] if top else []), \
                mock.patch.object(fc.fc_worktree, "task_prefix", return_value="compose/p-"), mock.patch.object(fc.fc_worktree, "review_base", return_value="main"), \
                mock.patch.object(fc.fc_worktree, "dirty_paths", return_value=list(dirty)), \
                mock.patch.object(fc.fc_core, "say", self.said.append):  # a failed verify screens: never call Jev
            with self.assertRaises(SystemExit) as cm:
                fc.fc_verify.verify_rounds(repo, proj, {"review_base": "main", **(cfg or {})}, type("A", (), {"fix": 2, "skip_review": True})(), [], time.time())
        return cm.exception.code, repo, json.loads((proj / "results" / "verify.json").read_text())

    def test_uncommitted_changes_of_the_user_still_skip_the_fix(self):
        code, _, res = self.verify({"a.go": "fixed\n"}, dirty=["a.go"])
        self.assertEqual((code, len(res["rounds"])), (1, 1))
        self.assertIn("fix    skip  repo has uncommitted changes (a.go); commit first so a bad fix can be reverted", self.said)

    def test_a_fix_that_changes_no_failing_check_and_no_cited_line_is_discarded(self):
        code, _, res = self.verify({"a.go": "fixed\n"}, rerun=1)  # c1 still fails after the fix
        self.assertEqual((code, len(res["rounds"]), res["rejected_fixes"]), (1, 1, [{"round": 1, "files": ["a.go"],
                         "why": "changed no failing check and removed no line a confirmed finding cites"}]))
        rv = lambda quote: {"review": {"pass": False, "issues": [], "confirmed": [{"evidence": quote}]}}
        code, _, res = self.verify({"a.go": "fixed\n"}, report=rv("b"))  # it removes the line the finding quotes
        self.assertEqual((code, res["rejected_fixes"]), (0, []))
        code, _, res = self.verify({"a.go": "fixed\n"}, report=rv("zzz"))
        self.assertEqual((code, len(res["rejected_fixes"])), (1, 1))  # it leaves that line alone
        code, _, res = self.verify({"a.go": "fixed\n"}, report={"e2e": [{"name": "n", "exit": 1}]})  # a failing e2e case is its own reproduction
        self.assertEqual((code, res["rejected_fixes"]), (0, []))

    def test_a_fix_round_commits_only_to_the_layer_that_owns_the_changed_file(self):
        tasks = [{"id": "003", "files": ["a.go"], "protected": [], "test": "t3"}, {"id": "007", "files": ["b.go"], "protected": [], "test": "t7"}]
        code, _, res = self.verify({"a.go": "fixed\n"}, tasks=tasks, top="007")
        nxt = "cut a new top task (id above 007) whose files: lists a.go, run it, then verify again"
        self.assertEqual((code, res["rejected_fixes"]), (1, [{"round": 1, "files": ["a.go"], "why": "changed a file of layer 003, not the top layer 007: a.go", "next": nxt}]))
        self.assertIn("fix    next  " + nxt, self.said)
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
                 {"id": "003", "kind": "design", "files": ["a.go"], "protected": [], "test": "td"},
                 {"id": "004", "kind": "hand", "files": ["a.go"], "protected": [], "test": "th"}]
        fix_tasks = lambda res: [r.get("fix_tasks", "absent") for r in res["rounds"]]
        code, _, res = self.verify({"a.go": "fixed\n"}, tasks=tasks)
        self.assertEqual((code, fix_tasks(res)), (0, [["001", "002"], "absent"]))  # task order, never the design task or a hand task (nothing dispatches it); the passing round has none
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
        message = fc.fc_verify.fix_message
        self.assertEqual(message('"Match the autofill panel file name"\nI also changed...', ["a.go"]), "Match the autofill panel file name")
        self.assertEqual(message("Fix verification findings", ["a.go"]), "Fix a.go")  # a process message is not used

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
        code, _, res = self.verify({"a.go": "fixed\n", "yarn.lock": "rewritten\n", "package-lock.json": "{}\n"})
        self.assertEqual((code, res["passed"]), (1, False))
        self.assertEqual(sorted(res["rejected_fixes"][0]["files"]), ["package-lock.json", "yarn.lock"])

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
                    mock.patch.object(fc.fc_pin, "cli_commit", return_value="abc1234"), mock.patch("builtins.print"):
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
    """Task 009 (NEB-946): herdr pi loads the read guard."""
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

