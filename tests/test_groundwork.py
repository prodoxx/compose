import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, re, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


_mcp = mock.patch.object(fc.fc_mcp, "installed_servers", side_effect=lambda cwd: [])  # no test runs the real `claude mcp list`


def setUpModule():
    _mcp.start()


def tearDownModule():
    _mcp.stop()

ENV = {"HERDR_ENV": "", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def repo_with(files):
    r = Path(tempfile.mkdtemp())
    run = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    for name, text in files.items():
        (r / name).parent.mkdir(parents=True, exist_ok=True)
        (r / name).write_text(text)
    run("add", "-A"); run("commit", "-qm", "init")
    return r, run


class Groundwork(unittest.TestCase):
    """The groundwork brief is a template; test writers get the task plus the shared contract, never a guess."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        self.repo, _ = repo_with({"server/go.mod": "module x\n", "client/package.json": "{}", "client/yarn.lock": ""})
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        tasks = [{"id": "001", "title": "Store", "files": ["server/store.go"], "protected": ["server/store_test.go"],
                  "test": "cd server && go test ./... -run TestStore", "body": "## Goal\nOpen the store."},
                 {"id": "002", "title": "Form", "files": ["client/src/Form.js"], "protected": ["client/src/Form.test.js"],
                  "test": "cd client && yarn test Form", "body": "## Goal\nShow the form."}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))

    def patched(self):
        return (mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=self.repo))

    def planner(self, issue=None, extra="Spec: README.md", stdin=""):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-9"}))
        out = io.StringIO()
        with self.patched()[0], self.patched()[1], mock.patch("sys.stdout", out), mock.patch("sys.stdin", io.StringIO(stdin)):
            fc.fc_briefs.cmd_planner(SimpleNamespace(repo=str(self.repo), project="p", issue=issue, extra=extra))
        return out.getvalue()

    def test_planner_brief_includes_the_prd_when_it_exists(self):
        prd = {"id": "d", "title": "PRD", "url": "u/prd", "content": "## Problem\n\nRunners lack shoes.\n"}
        with mock.patch.object(fc.fc_briefs.fc_prd, "read_prd", return_value=prd) as read, mock.patch.object(fc.fc_briefs.fc_config, "LINEAR_KEY", "k"):
            brief = self.planner()
        self.assertIn("The PRD (u/prd)", brief); self.assertIn("Runners lack shoes.", brief)
        self.assertEqual(read.call_args.args[0], self.repo)
        for found in (None, RuntimeError("linear down"), SystemExit("no .compose/linear.json")):
            kw = {"side_effect": found} if isinstance(found, BaseException) else {"return_value": found}
            with mock.patch.object(fc.fc_briefs.fc_prd, "read_prd", **kw), mock.patch.object(fc.fc_briefs.fc_config, "LINEAR_KEY", "k"):
                self.assertNotIn("The PRD (", self.planner())

    MCP_TOOLS = ("save_issue", "get_issue", "list_issues", "save_comment", "get_project", "list_issue_labels", "create_issue_label",
                 "save_document", "mcp__claude_ai_Linear")

    def printed(self, cmd, **kw):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-9"}))
        out = io.StringIO()
        with self.patched()[0], self.patched()[1], mock.patch("sys.stdout", out):
            cmd(SimpleNamespace(repo=str(self.repo), project="p", extra=None, **kw))
        return " ".join(out.getvalue().split())

    def test_planner_issue_overrides_the_feature_and_dash_reads_notes_from_stdin(self):
        brief = self.planner(issue="NEB-12", extra="-", stdin="Story NEB-12 only.\n")
        self.assertIn("NEB-12", brief)
        self.assertTrue(brief.rstrip().endswith("Story NEB-12 only."))
        self.assertNotIn("Project notes", self.planner(extra=None))

    def test_planner_is_a_command_with_issue_and_extra(self):
        def help(*argv):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["compose", *argv, "--help"]), mock.patch("sys.stdout", out), self.assertRaises(SystemExit) as cm:
                fc.main()
            self.assertEqual(cm.exception.code, 0)
            return out.getvalue()
        self.assertRegex(help(), r"(?m)^    planner ")
        sub = help("planner")
        self.assertIn("--issue", sub); self.assertIn("--extra", sub)

    def test_each_test_crew_gets_its_task_and_the_contract(self):
        (self.proj / "results" / "contract.md").write_text("Store is a value type: store.Store{}")
        out = io.StringIO()
        with self.patched()[0], mock.patch("sys.stdout", out):
            fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only="002", print_=True))
        text = out.getvalue()
        self.assertIn("===== tests-002", text); self.assertNotIn("tests-001", text)
        self.assertIn("client/src/Form.test.js", text)
        self.assertIn("Store is a value type", text)
        self.assertIn("Assert only what the TASK BRIEF states", text)
        self.assertIn("## Goal\nShow the form.", text)

    def test_test_crews_commit_only_their_file_and_never_rewrite_the_shared_branch(self):
        (self.proj / "results" / "contract.md").write_text("c")
        out = io.StringIO()
        with self.patched()[0], mock.patch("sys.stdout", out):
            fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only="002", print_=True))
        text = out.getvalue()
        self.assertIn("git add client/src/Form.test.js && git commit client/src/Form.test.js -m", text)
        self.assertIn("Never run `git commit --amend`, `git reset`, `git rebase`", text)

    def test_one_test_crew_failing_to_start_does_not_stop_the_rest(self):
        (self.proj / "results" / "contract.md").write_text("c")
        started, out = [], io.StringIO()
        def crew(ns):
            if ns.role.endswith("001"):
                raise RuntimeError("agent_prompt_stalled")
            started.append((ns.role, ns.cwd, ns.leased))
        with self.patched()[0], mock.patch.object(fc.fc_crew, "cmd_crew", crew), mock.patch.object(fc.fc_herdr, "herdr_space"), \
             mock.patch.object(fc.fc_briefs, "test_worktree", lambda proj, tid: self.repo), mock.patch("sys.stdout", out):
            fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only=None, print_=False))
        self.assertEqual(started, [("tests-002", str(self.repo), True)])  # each writer runs in the worktree leased for it
        self.assertIn("tests-001 not started: agent_prompt_stalled", out.getvalue())

    def test_the_configs_parallel_caps_test_writers_and_prints_the_rest(self):
        (self.proj / "results" / "contract.md").write_text("c")
        started, out = [], io.StringIO()
        with self.patched()[0], mock.patch.object(fc.fc_crew, "cmd_crew", lambda ns: started.append(ns.role)), mock.patch.object(fc.fc_herdr, "herdr_space"), \
             mock.patch.object(fc.fc_briefs, "test_worktree", lambda proj, tid: self.repo), mock.patch("sys.stdout", out), mock.patch.object(fc.fc_config, "PARALLEL", 1):
            fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only=None, print_=False))
        self.assertEqual(started, ["tests-001"])
        self.assertIn("1 test writer(s) queued past the cap of 1", out.getvalue())
        self.assertIn("compose testcrews --project p --only 002", out.getvalue())

    def test_a_call_whose_writers_are_all_queued_behind_a_full_cap_still_says_so(self):
        (self.proj / "results" / "contract.md").write_text("c")
        started, out = [], io.StringIO()
        with self.patched()[0], mock.patch.object(fc.fc_crew, "cmd_crew", lambda ns: started.append(ns.role)), mock.patch.object(fc.fc_herdr, "herdr_space"), \
             mock.patch.object(fc.fc_briefs, "test_open", lambda proj, tid: tid == "001"), mock.patch("sys.stdout", out), mock.patch.object(fc.fc_config, "PARALLEL", 1):
            fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only="002", print_=False))
        self.assertEqual(started, [])
        self.assertIn("1 test writer(s) queued past the cap of 1", out.getvalue())
        self.assertIn("compose testcrews --project p --only 002", out.getvalue())

    def test_claude_test_writers_stay_within_claude_parallel_but_pi_writers_do_not(self):
        (self.proj / "results" / "contract.md").write_text("c")
        for models, cap, expect in (({}, 1, ["tests-001"]), ({"tests": "claude:opus"}, 1, ["tests-001"]), ({"tests": "pi:zai/glm-5"}, 1, ["tests-001", "tests-002"])):
            started, out = [], io.StringIO()
            with self.patched()[0], mock.patch.object(fc.fc_crew, "cmd_crew", lambda ns: started.append(ns.role)), mock.patch.object(fc.fc_herdr, "herdr_space"), \
                 mock.patch.object(fc.fc_briefs, "test_worktree", lambda proj, tid: self.repo), mock.patch("sys.stdout", out), \
                 mock.patch.object(fc.fc_config, "PARALLEL", None), mock.patch.object(fc.fc_config, "CLAUDE_PARALLEL", cap), mock.patch.object(fc.fc_config, "CREW_MODELS", models):
                fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only=None, print_=False))
            self.assertEqual(sorted(started), expect, models)  # the writers start on threads, so their order is not fixed
            self.assertEqual("queued past the cap of 1" in out.getvalue(), len(expect) == 1, models)

    def test_parallel_flag_starts_that_many_writers_over_both_config_caps(self):
        (self.proj / "results" / "contract.md").write_text("c")
        for models in ({}, {"tests": "claude:opus"}):
            for flag, expect in ((2, ["tests-001", "tests-002"]), (None, ["tests-001"])):
                started, out = [], io.StringIO()
                with self.patched()[0], mock.patch.object(fc.fc_crew, "cmd_crew", lambda ns: started.append(ns.role)), mock.patch.object(fc.fc_herdr, "herdr_space"), \
                     mock.patch.object(fc.fc_briefs, "test_worktree", lambda proj, tid: self.repo), mock.patch("sys.stdout", out), \
                     mock.patch.object(fc.fc_config, "PARALLEL", 1), mock.patch.object(fc.fc_config, "CLAUDE_PARALLEL", 1), mock.patch.object(fc.fc_config, "CREW_MODELS", models):
                    fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only=None, print_=False, parallel=flag))
                self.assertEqual(sorted(started), expect, (models, flag))  # the writers start on threads, so their order is not fixed

    def test_testcrews_takes_parallel_force_and_redo_on_the_command_line(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["compose", "testcrews", "--help"]), mock.patch("sys.stdout", out), self.assertRaises(SystemExit) as cm:
            fc.main()
        self.assertEqual(cm.exception.code, 0)
        for flag in ("--parallel", "--force", "--redo"):
            self.assertIn(flag, out.getvalue())

    def test_designcrews_takes_force_on_the_command_line(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["compose", "designcrews", "--help"]), mock.patch("sys.stdout", out), self.assertRaises(SystemExit) as cm:
            fc.main()
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--force", out.getvalue())

    def test_force_goes_on_to_every_writer_and_is_off_without_it(self):
        (self.proj / "results" / "contract.md").write_text("c")
        for flag, expect in ((True, [True, True]), (None, [False, False])):
            forced = []
            with self.patched()[0], mock.patch.object(fc.fc_crew, "cmd_crew", lambda ns: forced.append(ns.force)), mock.patch.object(fc.fc_herdr, "herdr_space"), \
                 mock.patch.object(fc.fc_briefs, "test_worktree", lambda proj, tid: self.repo):
                ns = SimpleNamespace(repo=str(self.repo), project="p", contract=None, only=None, print_=False)
                if flag:
                    ns.force = True
                fc.fc_briefs.cmd_testcrews(ns)
            self.assertEqual(forced, expect, flag)

    def test_a_named_task_that_needs_no_test_writer_says_why(self):
        (self.proj / "results" / "contract.md").write_text("c")
        (self.proj / "results" / "001.json").write_text(json.dumps({"id": "001", "status": "hand_done"}))
        out = io.StringIO()
        with self.patched()[0], mock.patch("sys.stdout", out):
            fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only="001,009", print_=True))
        self.assertIn("001 skipped: its result is hand_done", out.getvalue())
        self.assertIn("009 skipped: no such task", out.getvalue())
        self.assertNotIn("===== tests-001", out.getvalue())

    def test_redo_writes_a_new_test_for_a_passed_task_a_re_plan_changed(self):
        (self.proj / "results" / "contract.md").write_text("c")
        (self.proj / "results" / "001.json").write_text(json.dumps({"id": "001", "status": "passed"}))
        f = self.proj / "results" / "tasks.json"
        data = json.loads(f.read_text())
        data["tasks"].append({**data["tasks"][1], "id": "003", "linear_state": "Done"})
        f.write_text(json.dumps(data))
        def printed(only="001,003", **kw):
            out = io.StringIO()
            with self.patched()[0], mock.patch("sys.stdout", out):
                fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only=only, print_=True, **kw))
            return out.getvalue()
        kept = printed()
        self.assertIn("001 skipped: its result is passed; pass --redo to rewrite its test\n", kept)
        self.assertNotIn("===== tests-001", kept)
        redo = printed(redo=True)
        self.assertIn("===== tests-001", redo)
        self.assertIn("The test server/store_test.go holds is being replaced (the brief changed after it was written, or the team lead sent it back): rewrite it to the TASK BRIEF.", redo)
        self.assertIn("003 skipped: its state is Done\n", redo)  # Done on Linear stays skipped, as in `run --redo`
        self.assertNotIn("rewrite the test", printed(only="002"))  # a task never built gets the plain brief

    def test_no_contract_means_no_test_crews(self):
        with self.patched()[0], self.assertRaises(SystemExit):
            fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only=None, print_=True))


class Triage(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {**ENV, "TEXT_MODEL_API_KEY": "k"}); self.env.start(); self.addCleanup(self.env.stop)
        self.repo, run = repo_with({"a.go": "a\n", "b.go": "b\n"})
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        tasks = []
        for tid, f in (("001", "a.go"), ("002", "b.go")):
            run("checkout", "-q", "-b", f"fast/p/{tid}", "main")
            (self.repo / f).write_text(f"{tid} change\n"); run("commit", "-qam", tid); run("checkout", "-q", "main")
            (self.proj / "results" / f"{tid}.json").write_text(json.dumps({"id": tid, "status": "passed"}))
            tasks.append({"id": tid, "files": [f], "body": "brief"})
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))

    def test_tasks_are_triaged_at_once_and_the_verdict_is_kept(self):
        live, peak, lock = [0], [0], threading.Lock()
        def fake(env, system, user, **kw):
            with lock:
                live[0] += 1; peak[0] = max(peak[0], live[0])
            time.sleep(0.2)
            with lock:
                live[0] -= 1
            return {"verdict": "flag", "reason": "adds a retry flag: `retries := 3`"} if "001 change" in user else {"verdict": "clean"}
        with mock.patch.object(fc.fc_models, "fast_json", fake), mock.patch.object(fc.fc_core, "say"):
            out = fc.fc_run.triage(self.repo, self.proj, ["001", "002"])
        self.assertEqual(peak[0], 2)
        self.assertEqual(out["001"][0], "flag"); self.assertEqual(out["002"][0], "clean")
        self.assertEqual(json.loads((self.proj / "results" / "001.json").read_text())["triage"]["verdict"], "flag")

    def test_a_stacked_task_is_shown_only_its_own_layer(self):
        run = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        run("checkout", "-q", "-b", "fast/p/003", "fast/p/001")
        (self.repo / "a.go").write_text("001 change\n003 change\n"); run("commit", "-qam", "003"); run("checkout", "-q", "main")
        (self.proj / "results" / "003.json").write_text(json.dumps({"id": "003", "status": "passed", "base": "fast/p/001"}))
        t = json.loads((self.proj / "results" / "tasks.json").read_text())
        t["tasks"].append({"id": "003", "after": "001", "files": ["a.go"], "body": "brief"})
        (self.proj / "results" / "tasks.json").write_text(json.dumps(t))
        users = []
        with mock.patch.object(fc.fc_models, "fast_json", lambda env, system, user, **kw: users.append(user) or {"verdict": "clean"}), \
             mock.patch.object(fc.fc_core, "say"):
            fc.fc_run.triage(self.repo, self.proj, ["003"])
        diff = users[0].split("DIFF:\n", 1)[1]
        self.assertIn("+003 change", diff)
        self.assertNotIn("+001 change", diff)

    def test_a_failed_call_is_unknown_never_a_crash(self):
        with mock.patch.object(fc.fc_models, "fast_json", side_effect=RuntimeError("down")), mock.patch.object(fc.fc_core, "say"):
            out = fc.fc_run.triage(self.repo, self.proj, ["001"])
        self.assertEqual(out["001"][0], "unknown")

    def test_a_reply_that_is_not_a_verdict_object_is_asked_again_then_unknown_with_why(self):
        def replies(*contents):
            calls = []
            def http(req, provider, timeout=90):
                calls.append(json.loads(req.data)["messages"][0]["content"])
                return {"choices": [{"message": {"content": contents[len(calls) - 1]}}]}
            return http, calls
        cut = '{"verdict": "clean", "reason": "the diff only chang'
        for contents, want in ((('["clean"]', '{"verdict": "flag", "reason": "`x := 1`"}'), ("flag", "`x := 1`")),
                               (('[{"verdict": "clean"}]',), ("clean", "")),
                               (('["clean"]', '{"issues": []}'), ("unknown", "triage failed: reply was not a")),
                               ((cut, cut), ("unknown", "triage failed: reply was not valid JSON"))):
            http, calls = replies(*contents)
            with mock.patch.object(fc.fc_models, "http_json", http), mock.patch.object(fc.fc_core, "say"):
                verdict, reason = fc.fc_run.triage(self.repo, self.proj, ["001"])["001"]
            self.assertEqual((verdict, reason[:len(want[1])]), want, contents)
            self.assertEqual(len(calls), len(contents))
            self.assertTrue(len(calls) == 1 or "exactly one JSON object" in calls[1])

    def test_a_reply_cut_off_at_its_token_budget_is_asked_again_with_a_bigger_one(self):
        full = '{"verdict": "flag", "reason": "`retries := 3`"}'
        for cut, need in ((full[:20], 1000), ("", 1000), (full[:20], 10 ** 6)):  # cut mid-object, or before any text
            budgets = []
            def http(req, provider, timeout=90, cut=cut, need=need):  # the whole reply needs `need` tokens
                budgets.append(json.loads(req.data)["max_tokens"])
                short = budgets[-1] < need
                return {"choices": [{"message": {"content": cut if short else full}, "finish_reason": "length" if short else "stop"}]}
            with mock.patch.object(fc.fc_models, "http_json", http), mock.patch.object(fc.fc_core, "say"):
                verdict, reason = fc.fc_run.triage(self.repo, self.proj, ["001"])["001"]
            self.assertEqual(len(budgets), 2)
            self.assertGreaterEqual(budgets[1], 4 * budgets[0])
            if need < 10 ** 6:
                self.assertEqual((verdict, reason), ("flag", "`retries := 3`"), cut)
            else:
                self.assertEqual((verdict, reason), ("unknown", f"triage failed: reply cut off at max_tokens={budgets[1]}"))

    def test_the_diff_is_read_from_the_helper_named_branch(self):
        git = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        git("branch", "-m", "fast/p/001", "team/p-001"); git("branch", "-D", "fast/p/002")
        (self.proj / "verify.json").write_text(json.dumps({"branch": "team/p"}))
        seen = []
        with mock.patch.object(fc.fc_models, "fast_json", lambda env, system, user, **kw: seen.append(user) or {"verdict": "clean"}), \
                mock.patch.object(fc.fc_core, "say"):
            out = fc.fc_run.triage(self.repo, self.proj, ["001"])
        self.assertEqual(out["001"][0], "clean")
        self.assertIn("+001 change", seen[0])


class ReadFailure(unittest.TestCase):
    def test_haiku_writes_the_brief_read_only_and_failures_add_nothing(self):
        meta, calls = {"test": "pytest t.py", "files": ["a.py"]}, []
        def once(cwd, prompt, timeout=900, model=None, tools=None):
            calls.append((model, tools, prompt))
            return None, "\nt.py::test_x expected 2, got 1; look at a.py:3\n", 2, {"cost": 0.0, "reference_cost": 0.002}
        with mock.patch.object(fc.fc_claude, "claude_once", once):
            brief, usage = fc.fc_implement.read_failure(Path("/w"), meta, "FAIL test_x", Path("/w/001.test.log"))
        self.assertEqual((brief, usage["reference_cost"]), ("t.py::test_x expected 2, got 1; look at a.py:3", 0.002))
        model, tools, prompt = calls[0]
        self.assertEqual((model, tools), ("claude-haiku-5-5", "Read,Grep,Glob"))
        for part in ("FAIL test_x", "pytest t.py", "/w/001.test.log", "a.py", "file:line"):
            self.assertIn(part, prompt)
        with mock.patch.object(fc.fc_claude, "claude_once", side_effect=subprocess.TimeoutExpired("claude", 120)):
            self.assertEqual(fc.fc_implement.read_failure(Path("/w"), meta, "FAIL", Path("/w/l")), ("", None))
        with mock.patch.object(fc.fc_config, "TEST_READER", None), mock.patch.object(fc.fc_claude, "claude_once", side_effect=AssertionError("no reader set")):
            self.assertEqual(fc.fc_implement.read_failure(Path("/w"), meta, "FAIL", Path("/w/l")), ("", None))


class CrewWorkdir(unittest.TestCase):
    """A teammate pointed at another repo works in its own worktree; the repo's checkout never switches branches."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        self.project, _ = repo_with({"README.md": "project\n"})
        self.tool, self.tool_git = repo_with({"bin/tool": "v1\n"})
        self.home = Path(tempfile.mkdtemp())
        trusted = {"projects": {str(self.tool.resolve()): {"hasTrustDialogAccepted": True}}}
        (self.home / ".claude.json").write_text(json.dumps(trusted))

    def test_another_repo_gets_a_worktree_on_a_new_branch(self):
        with mock.patch.object(Path, "home", return_value=self.home):
            path, branch = fc.fc_crew.crew_workdir(str(self.tool), self.project, "shop-toolsmith")
        self.assertTrue(branch.startswith("shop-toolsmith-"))
        self.assertEqual(path.parent, self.tool.resolve() / ".claude" / "worktrees")
        self.assertTrue((path / "bin" / "tool").exists())
        self.assertEqual(self.tool_git("branch", "--show-current").stdout.strip(), "main")  # the checkout stayed put
        self.assertEqual(self.tool_git("status", "--porcelain").stdout.strip(), "")  # the worktree is excluded, not untracked noise

    def test_a_leased_worktree_and_plain_folders_run_where_pointed(self):
        self.assertEqual(fc.fc_crew.crew_workdir(str(self.project), self.project, "x", leased=True), (self.project, None))
        plain = tempfile.mkdtemp()
        self.assertEqual(fc.fc_crew.crew_workdir(plain, self.project, "x"), (Path(plain), None))
        self.assertEqual(fc.fc_crew.crew_workdir(None, self.project, "x"), (self.project, None))

    def test_an_untrusted_repo_is_named_up_front(self):
        (self.home / ".claude.json").write_text(json.dumps({"projects": {}}))
        with mock.patch.object(Path, "home", return_value=self.home), self.assertRaises(SystemExit) as e:
            fc.fc_crew.crew_workdir(str(self.tool), self.project, "shop-toolsmith")
        self.assertIn("trust", str(e.exception))
        self.assertEqual(self.tool_git("worktree", "list").stdout.count("\n"), 1)  # nothing created


class ClaudeTrust(unittest.TestCase):
    """Claude Code's own rule: a folder in a repo is trusted through the repo's main checkout or a folder between it and the
    repo's top, never through a folder above the repo; outside git any parent counts."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        self.home = Path(tempfile.mkdtemp())

    def trusted(self, path, *accepted):
        (self.home / ".claude.json").write_text(json.dumps({"projects": {str(p): {"hasTrustDialogAccepted": True} for p in accepted}}))
        with mock.patch.object(Path, "home", return_value=self.home):
            return fc.fc_claude.claude_trusted(path)

    def test_a_repo_nested_in_a_trusted_folder_still_asks(self):
        outer = Path(tempfile.mkdtemp()).resolve()
        (outer / "repo" / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(outer / "repo")], check=True)
        (outer / "plain" / "sub").mkdir(parents=True)
        self.assertIs(self.trusted(outer / "repo", outer), False)
        self.assertIs(self.trusted(outer / "repo" / "src", outer), False)
        self.assertIs(self.trusted(outer / "repo" / "src", outer / "repo"), True)
        self.assertIs(self.trusted(outer / "plain" / "sub", outer), True)

    def test_a_worktree_shares_its_main_checkouts_trust(self):
        main, git = repo_with({"src/a.py": ""})
        main, wt = main.resolve(), Path(tempfile.mkdtemp()).resolve() / "wt"
        git("worktree", "add", "-q", str(wt))
        self.assertIs(self.trusted(wt / "src", main), True)
        self.assertIs(self.trusted(wt, main / "src"), False)
        self.assertIs(self.trusted(wt / "src", wt / "src"), True)


class DesignerBrief(unittest.TestCase):
    """The designer's brief names the project, its Figma file, and the tools and gates the designer must use."""
    def setUp(self):
        self.wt = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.wt / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        self.write({"feature": "ENG-1"})
        home, bin_ = Path(os.environ["HOME"]), self.wt / "bin"  # the designer's rows ok: claude, Framelink at user scope, its token
        bin_.mkdir(); (bin_ / "claude").write_text("#!/bin/sh\n"); (bin_ / "claude").chmod(0o755)
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"framelink-figma": {}}}))
        token = home / ".config" / "figma-context-mcp" / ".env"
        token.parent.mkdir(parents=True); token.write_text("FIGMA_API_KEY=figd-test\n")
        for p in (mock.patch.dict(os.environ, {"PATH": f"{bin_}:{os.environ['PATH']}"}), mock.patch.object(fc.fc_init.fc_steps, "FRAMELINK_ENV", token)):
            p.start(); self.addCleanup(p.stop)

    def write(self, data):
        (self.proj / "linear.json").write_text(json.dumps(data))

    def brief(self, extra=None, figma=None, stdin=""):
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=self.wt), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)), mock.patch.object(fc.fc_core, "say"), mock.patch("sys.stdout", out):
            fc.fc_figma.cmd_designer(SimpleNamespace(repo=str(self.wt), project="p", extra=extra, figma=figma))
        return out.getvalue()

    def test_figma_url_stores_the_key_before_printing(self):
        self.write({"feature": "ENG-1", "figma": "OldKey000000"})
        brief = self.brief(figma="https://www.figma.com/design/AbCdEf123456/Shop?node-id=1-2")
        self.assertEqual(json.loads((self.proj / "linear.json").read_text()), {"feature": "ENG-1", "figma": "AbCdEf123456"})
        self.assertIn("AbCdEf123456", brief)
        self.assertNotIn("OldKey000000", brief)

    def test_an_unreadable_figma_link_exits_without_storing_or_printing(self):
        with mock.patch.object(fc.fc_core, "figma_key", return_value=None), mock.patch.object(fc.fc_figma, "store_figma") as store, \
                self.assertRaises(SystemExit) as e:
            self.brief(figma="https://example.com/nope")
        self.assertNotIn(e.exception.code, (0, None))
        store.assert_not_called()
        self.assertEqual(json.loads((self.proj / "linear.json").read_text()), {"feature": "ENG-1"})

    def test_extra_from_stdin_ends_the_brief_under_the_notes_header(self):
        brief = self.brief(extra="-", stdin="Checkout only.\n")
        self.assertIn("Project notes from the team lead:\nCheckout only.", brief)
        self.assertTrue(brief.rstrip().endswith("Checkout only."))

class DesignCrews(unittest.TestCase):
    """designcrews briefs and starts one design-NNN builder per open design task, with the stored Figma key."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "figma": "AbCdEf123456"}))
        def issue(n, status="Todo", **kv):
            block = "\n".join(f"{k}: {v}" for k, v in {"id": n, "kind": "design", "test": f"judge {n}", **kv}.items())
            return {"id": f"ENG-{n}", "title": f"Task {n}", "description": f"## Goal\nDraw {n}.\n\n```compose\n{block}\n```\n", "status": status, "blockedBy": []}
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            issue("001", nodes="Checkout/Cart, Checkout/Pay"), issue("002", kind="code", files="a.go"), issue("003", "Done", nodes="Home/Hero"),
            issue("004", "In Review", nodes="Home/Nav"), issue("005", nodes="Account/Profile")]})

    def run_crews(self, only=None, print_=False, busy=(), max=None, live=(), redo=False, force=False):
        started, out = [], io.StringIO()
        def crew(ns):
            started.append(ns)
            if ns.role in busy: raise SystemExit("busy")
        agents = [{"name": fc.fc_herdr.agent_name("p", role), "workspace_id": "w1"} for role in live]
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr_space") as space, \
                mock.patch.object(fc.fc_herdr, "live_agents", return_value=agents), mock.patch.object(fc.fc_herdr, "herdr_project_space", return_value="w1"), \
                mock.patch.object(fc.fc_crew, "cmd_crew", side_effect=crew), mock.patch.object(fc.fc_linear, "linear_event") as self.ev, mock.patch("sys.stdout", out):
            fc.fc_figma.cmd_designcrews(SimpleNamespace(repo=".", project="p", only=only, print_=print_, max=max, redo=redo, force=force))
        return started, out.getvalue(), space

    def test_print_shows_one_brief_per_open_design_task(self):
        started, out, space = self.run_crews(print_=True)
        for text in ("===== design-001", "===== design-005", "Figma file AbCdEf123456", "Checkout/Cart", "Checkout/Pay", str(self.proj / "results" / "001.tree.txt"), "judge 001", "figma:figma-use", "TASK BRIEF:", "## Goal\nDraw 001."):
            self.assertIn(text, out)
        self.assertEqual((out.count("====="), started, space.call_count), (2, [], 0))
        flat = " ".join(out.split())
        for text in ("WRITES ONLY", "mcp__framelink-figma__get_figma_data", "compose figma tree --project p --only 001", "never a read-only `use_figma` script", "Never use another `plugin:*figma*`",
                     "`use_figma` (and the `create_session` it needs)", "A `use_figma` script that sets nothing is a read, even when it only measures layout, sizes, or positions: measure from Framelink tree data"):
            self.assertIn(text, flat)
        for text in ("whoami", "get_libraries", "search_design_system"):  # named once each, in the ban on them
            self.assertEqual(flat.count(text), 2)  # one builder brief per open task, two tasks

    def test_default_pace_is_three_builders_at_once(self):
        def issue(n):
            return {"id": f"ENG-{n}", "title": n, "description": f"```compose\nid: {n}\nkind: design\ntest: t\nnodes: P{n}/A\n```\n", "status": "Todo", "blockedBy": []}
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [issue(f"10{i}") for i in range(5)]})
        with mock.patch.object(fc.fc_config, "PARALLEL", 20):  # the config's parallel never raises it: Figma's write rate limit sets it
            started, out, _ = self.run_crews()
        self.assertEqual((len(started), "queued past the cap of 3" in out), (3, True))

    def test_starts_one_crew_per_open_design_task_and_reports_one_that_fails(self):
        started, out, space = self.run_crews(busy={"design-001"})
        self.assertEqual(sorted(ns.role for ns in started), ["design-001", "design-005"])
        self.assertEqual(space.call_count, 1); self.assertIn("design-001 not started: busy", out)
        ns, meta = next(ns for ns in started if ns.role == "design-005"), fc.fc_tasks.load_tasks(self.proj)[-1]
        self.assertEqual((ns.project, ns.action, ns.title, ns.force, ns.cwd, ns.model), ("p", "start", "design 005", False, None, None))
        self.assertEqual(ns.brief, fc.fc_figma.design_brief(meta, "AbCdEf123456", self.proj / "results"))
        self.assertEqual([ns.role for ns in self.run_crews(only="005")[0]], ["design-005"])

    def test_force_is_passed_to_each_started_design_builder(self):
        started, _, _ = self.run_crews(force=True)
        self.assertEqual([ns.force for ns in started], [True, True])

    def test_starts_at_most_the_cap_and_prints_the_rest_with_the_command_to_start_them(self):
        started, out, _ = self.run_crews(max=1)
        self.assertEqual([ns.role for ns in started], ["design-001"])
        self.assertIn("queued past the cap of 1: 005", out)
        self.assertIn("compose designcrews --project p --only 005", out)
        (self.proj / "verify.json").write_text(json.dumps({"design_concurrency": 1}))
        self.assertEqual(len(self.run_crews()[0]), 1)
        self.assertEqual(len(self.run_crews(max=2)[0]), 2)
        (self.proj / "verify.json").unlink()
        started, out, _ = self.run_crews()  # the default of 3 holds both open tasks
        self.assertEqual((len(started), "queued" in out), (2, False))

    def test_a_named_id_that_is_skipped_prints_one_line_with_the_reason(self):
        started, out, _ = self.run_crews(only="003,004,002,099,005", print_=True)
        for line in ("003 skipped: its state is Done", "004 skipped: its state is In Review", "002 skipped: not a design task (kind code)", "099 skipped: no such task"):
            self.assertIn(line, out)
        self.assertEqual((out.count("skipped"), out.count("=====")), (4, 1))
        self.assertNotIn("skipped", self.run_crews(only="005", print_=True)[1])

    def test_a_task_whose_result_is_passed_or_hand_done_is_not_started_again_before_linear_catches_up(self):
        for tid, status in (("001", "hand_done"), ("005", "passed")):
            (self.proj / "results" / f"{tid}.json").write_text(json.dumps({"id": tid, "status": status}))
        started, out, _ = self.run_crews(only="001,005")
        self.assertEqual(started, [])
        for line in ("001 skipped: its result is hand_done", "005 skipped: its result is passed"):
            self.assertIn(line, out)
        (self.proj / "results" / "005.json").write_text(json.dumps({"id": "005", "status": "failed"}))
        self.assertEqual([ns.role for ns in self.run_crews()[0]], ["design-005"])

    def test_redo_reopens_a_hand_accepted_task_for_a_new_builder_and_sets_its_result_aside(self):
        hand = json.dumps({"id": "004", "status": "hand_done"})
        res = self.proj / "results" / "004.json"
        res.write_text(hand)
        self.assertEqual(self.run_crews(only="004")[0], [])  # without --redo it stays refused
        started, out, _ = self.run_crews(only="004", redo=True)
        self.assertEqual([ns.role for ns in started], ["design-004"])
        self.assertFalse(res.exists())
        self.assertEqual((self.proj / "results" / "004.json.before-redo").read_text(), hand)
        self.assertEqual([(c.args[1]["id"], c.args[2]) for c in self.ev.call_args_list], [("004", "dispatched")])
        self.assertIn("004 reopened", out)

    def test_a_reopened_task_is_no_longer_shown_as_passed_while_the_new_builder_works(self):
        def row(tid):
            meta = next(m for m in fc.fc_tasks.load_tasks(self.proj) if m["id"] == tid)
            return fc.fc_status.task_state(".", self.proj, meta, "", {"prefix": "t/", "have": set(), "merged": set()})
        (self.proj / "results" / "004.json").write_text(json.dumps({"id": "004", "status": "passed"}))
        self.assertEqual(row("004")["state"], "passed")
        self.run_crews(only="004", redo=True)
        self.assertEqual((row("004")["state"], row("004")["note"]), ("pending", ""))
        self.assertEqual(fc.fc_tasks.load_tasks(self.proj)[3]["linear_state"], "In Progress")
        self.assertEqual(row("003")["state"], "passed")  # the other tasks keep what the last sync said
        cache = json.loads((self.proj / "results" / "tasks.json").read_text())
        cache["tasks"][3]["linear_state"] = "In Review"
        (self.proj / "results" / "tasks.json").write_text(json.dumps(cache))
        self.run_crews(only="004", redo=True, busy={"design-004"})  # a builder that does not start leaves the task as it was
        self.assertEqual(row("004")["state"], "passed")

    def test_redo_needs_only_leaves_done_tasks_closed_and_touches_nothing_when_the_builder_does_not_start(self):
        with self.assertRaises(SystemExit) as e:
            self.run_crews(redo=True)
        self.assertIn("--redo needs --only", str(e.exception))
        self.assertIn("003 skipped: its state is Done", self.run_crews(only="003", redo=True)[1])
        res = self.proj / "results" / "004.json"
        res.write_text(json.dumps({"id": "004", "status": "hand_done"}))
        started, out, _ = self.run_crews(only="004", redo=True, busy={"design-004"})
        self.assertIn("design-004 not started: busy", out)
        self.assertEqual(json.loads(res.read_text())["status"], "hand_done")
        self.assertEqual((self.ev.called, (self.proj / "results" / "004.json.before-redo").exists()), (False, False))
        started, out, _ = self.run_crews(only="004", redo=True, live=("design-004",))
        self.assertEqual((started, res.exists(), self.ev.called), ([], True, False))

    def test_a_builder_already_running_is_skipped_with_its_reason_not_reported_as_a_failure(self):
        started, out, _ = self.run_crews(live=("design-001",))
        self.assertEqual([ns.role for ns in started], ["design-005"])
        self.assertIn("design-001 is already running", out)
        self.assertNotIn("not started", out)
        self.assertNotIn("start them again", out)

    def test_a_task_figma_line_names_its_own_file_else_the_stored_key(self):
        def issue(n, **kv):
            block = "\n".join(f"{k}: {v}" for k, v in {"id": n, "kind": "design", "test": "t", "nodes": "P/A", **kv}.items())
            return {"id": f"ENG-{n}", "title": n, "description": f"```compose\n{block}\n```\n", "status": "Todo", "blockedBy": []}
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            issue("123", figma="https://www.figma.com/design/LibKey123456789/Lib?node-id=1-2"), issue("124")]})
        out = self.run_crews(print_=True)[1]
        self.assertEqual((out.count("Figma file LibKey123456789"), out.count("Figma file AbCdEf123456")), (1, 1))
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
        self.assertIn("design-123", self.run_crews(only="123", print_=True)[1])  # every task names its file: no stored key needed
        with self.assertRaises(SystemExit):
            self.run_crews(only="124")
        with self.assertRaises(SystemExit) as e:
            fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [issue("125", figma="junk")]})
        self.assertIn("figma is not a Figma file url or key: junk", str(e.exception))

    def test_no_stored_figma_key_refuses(self):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
        with self.assertRaises(SystemExit) as e:
            self.run_crews()
        self.assertEqual(str(e.exception), "no Figma file: compose figma check --figma <url or key>")


class DesignCrewsWait(unittest.TestCase):
    """designcrews holds back a design task until every blocker is In Review or Done: not started, not briefed, no slot under the cap."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "figma": "AbCdEf123456"}))
        def issue(n, status="Todo", kind="design", after=()):
            block = f"id: {n}\nkind: {kind}\ntest: t\n" + (f"nodes: P{n}/A\n" if kind == "design" else "files: a.go\n")
            return {"id": f"ENG-{n}", "title": n, "description": f"```compose\n{block}```\n", "status": status, "blockedBy": [f"ENG-{b}" for b in after]}
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            issue("001", "Done", "code"), issue("002", "In Progress", "code"), issue("003", kind="code"), issue("004", after=("001", "002", "003")),
            issue("005", after=("001", "006")), issue("006", "In Review"), issue("007"), issue("008", after=("006",)), issue("009")]})

    def run_crews(self, only=None, print_=False, max=None):
        started, out = [], io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr_space"), \
                mock.patch.object(fc.fc_crew, "cmd_crew", side_effect=started.append), mock.patch("sys.stdout", out):
            fc.fc_figma.cmd_designcrews(SimpleNamespace(repo=".", project="p", only=only, print_=print_, max=max, redo=False))
        return sorted(ns.role for ns in started), out.getvalue()

    def assert_waits_once(self, out):
        self.assertIn("004 waits on 002,003", out.splitlines())
        self.assertEqual(len(re.findall(r"\b004\b", out)), 1)  # no brief, failure, queue or skipped line names it

    def test_tasks_whose_blockers_are_in_review_or_done_start_and_one_with_open_blockers_waits(self):
        started, out = self.run_crews(max=10)
        self.assertEqual(started, ["design-005", "design-007", "design-008", "design-009"])
        self.assert_waits_once(out)

    def test_a_held_back_task_takes_no_slot_under_the_cap(self):
        started, out = self.run_crews(max=3)
        self.assertEqual((len(started), "design-004" in started), (3, False))
        left = ({"005", "007", "008", "009"} - {r.removeprefix("design-") for r in started}).pop()
        self.assertIn(f"queued past the cap of 3: {left}", out)
        self.assert_waits_once(out)

    def test_print_briefs_only_the_tasks_that_can_start(self):
        started, out = self.run_crews(print_=True)
        self.assertEqual((started, out.count("====="), "===== design-004" in out), ([], 4, False))
        self.assert_waits_once(out)

    def test_a_held_back_task_named_with_only_prints_its_waits_line_not_skipped(self):
        started, out = self.run_crews(only="004")
        self.assertEqual((started, out.strip()), ([], "004 waits on 002,003"))
        started, out = self.run_crews(only="004,005,003")
        self.assertEqual(started, ["design-005"])
        self.assertIn("003 skipped: not a design task (kind code)", out)
        self.assert_waits_once(out)


class ToolsmithBrief(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        self.log = self.home / ".compose" / "tool-bugs.jsonl"
        for p in (mock.patch.object(fc.fc_crew, "TOOL_BUGS", self.log),
                  mock.patch.object(fc.fc_config, "CONFIG_PATH", self.home / ".compose" / "config.json"),
                  mock.patch.object(fc.fc_core, "project_dir", return_value=self.home / "shop")):
            p.start(); self.addCleanup(p.stop)

    def entry(self, project, message, **kw):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "a") as f:
            f.write(json.dumps({"at": "2026-10-17 15:30:00", "project": project, "reporter": f"{project}-tests", "message": message,
                                "cli_commit": "abc1234", "cli_repo": "/Users/alice9/code/compose", **kw}) + "\n")

    def brief(self, state):
        with mock.patch.object(fc.fc_toolbug, "gh_status", return_value=(state, None if state == "no-gh" else "alice9")) as gh, \
                mock.patch("sys.stdout", io.StringIO()) as out:
            fc.fc_briefs.cmd_toolsmith(SimpleNamespace(repo=".", project="shop"))
        self.assertEqual(gh.call_count, 1)
        return out.getvalue()

    def test_the_filing_command_names_only_this_projects_newest_entry(self):
        self.entry("shopping", "a project whose name starts with shop", at="2026-10-16 08:00:00")
        self.entry("shop", "older", at="2026-10-16 09:00:00")
        self.entry("shop", "newest", at="2026-10-16 21:45:07")
        self.entry("shopping", "a later entry of that project", at="2026-10-16 23:00:00")
        for state in ("maintainer", "reporter", "no-gh"):
            out = self.brief(state)
            self.assertEqual(out.count('compose tool-bug file --project shop --entry "2026-10-16 21:45:07"'), 1, state)
            self.assertEqual(out.count('--entry "2026-10-16 21:45:07"'), 2 if state == "maintainer" else 1, state)
            self.assertNotIn("--entry 2026", out, state)
            for s in ("08:00:00", "09:00:00", "23:00:00"):
                self.assertNotIn(s, out, state)
        self.assertIn("run the same filing command with `--approved`", self.brief("reporter"))

    def test_no_entry_for_the_project_exits_naming_it(self):
        for setup in (lambda: None, lambda: self.entry("other", "not ours")):
            setup()
            with self.assertRaises(SystemExit) as cm, mock.patch.object(fc.fc_toolbug, "gh_status", return_value=("maintainer", "alice9")):
                fc.fc_briefs.cmd_toolsmith(SimpleNamespace(repo=".", project="shop"))
            self.assertNotIn(cm.exception.code, (0, None))
            self.assertIn("shop", str(cm.exception.code))


class NoTestToWrite(unittest.TestCase):
    """A task with no protected test has no writer, no brief, and says so."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        self.repo, _ = repo_with({"README.md": "x\n"})
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        tasks = [{"id": "001", "title": "Store", "files": ["server/store.go"], "protected": ["server/store_test.go"], "test": "go test ./...", "body": "## Goal\nOpen."},
                 {"id": "002", "title": "Copy", "files": ["README.md"], "protected": [], "test": "grep -q x README.md", "body": "## Goal\nFix the copy."},
                 {"id": "003", "title": "Form", "files": ["Form.js"], "protected": ["Form.test.js"], "test": "yarn test Form", "body": "## Goal\nShow."}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        (self.proj / "results" / "contract.md").write_text("c")

    def testcrews(self, only=None, print_=True):
        started, out = [], io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=self.repo), \
             mock.patch.object(fc.fc_crew, "cmd_crew", lambda ns: started.append(ns.role)), mock.patch.object(fc.fc_herdr, "herdr_space"), \
             mock.patch.object(fc.fc_briefs, "test_worktree", lambda proj, tid: self.repo), mock.patch("sys.stdout", out):
            fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only=only, print_=print_))
        return out.getvalue(), sorted(started)

    def test_print_briefs_only_the_tasks_with_a_protected_test(self):
        text, _ = self.testcrews()
        self.assertIn("===== tests-001", text); self.assertIn("===== tests-003", text)
        self.assertNotIn("===== tests-002", text)
        self.assertIn("002 skipped: no protected test to write", text)

    def test_only_names_a_skipped_task_and_still_briefs_the_others(self):
        text, _ = self.testcrews(only="002")
        self.assertIn("002 skipped: no protected test to write", text); self.assertNotIn("===== tests-", text)
        text, _ = self.testcrews(only="001,002")
        self.assertIn("002 skipped: no protected test to write", text); self.assertIn("===== tests-001", text)
        self.assertNotIn("skipped", self.testcrews(only="001")[0])  # only the named ids

    def test_no_writer_starts_for_it_and_the_others_still_start(self):
        text, started = self.testcrews(print_=False)
        self.assertEqual(started, ["tests-001", "tests-003"])
        self.assertEqual(self.testcrews(only="002", print_=False)[1], [])
        self.assertEqual(self.testcrews(only="003", print_=False)[1], ["tests-003"])


class ContractSlice(unittest.TestCase):
    CONTRACT = "pre\n## types\nT\n### sub\ns\n## task 001\nA\n### a-sub\nx\n## task 002\nB\n### b-sub\ny\n## helpers\nH\n### task 009\nnot a section\n## task 10\nC\n"

    def test_keeps_shared_parts_and_its_own_section_in_contract_order(self):
        self.assertEqual(fc.fc_briefs.contract_slice(self.CONTRACT, "001"),
                         "pre\n## types\nT\n### sub\ns\n## task 001\nA\n### a-sub\nx\n## helpers\nH\n### task 009\nnot a section\n")
        self.assertEqual(fc.fc_briefs.contract_slice(self.CONTRACT, "002"),
                         "pre\n## types\nT\n### sub\ns\n## task 002\nB\n### b-sub\ny\n## helpers\nH\n### task 009\nnot a section\n")

    def test_the_id_must_equal_the_heading_id(self):
        sliced = fc.fc_briefs.contract_slice(self.CONTRACT, "10")
        self.assertIn("## task 10\nC\n", sliced)
        self.assertNotIn("## task 001", sliced); self.assertNotIn("## task 002", sliced)

    def test_a_contract_without_task_sections_comes_back_unchanged(self):
        for text in ("", "plain text", "pre\n## types\nT\n### sub\ns\n", "## a\nb\n## c\nd"):
            self.assertEqual(fc.fc_briefs.contract_slice(text, "001"), text)

if __name__ == "__main__":
    unittest.main()
