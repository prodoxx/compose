import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, re, string, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path; test_treehouse covers the pool
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

    def test_brief_covers_the_detected_toolchains_and_appends_the_notes(self):
        out = io.StringIO()
        with self.patched()[0], self.patched()[1], mock.patch("sys.stdout", out):
            fc.fc_briefs.cmd_groundwork(SimpleNamespace(repo=str(self.repo), project="p", extra="Stub Open(dsn string) (*sql.DB, error)."))
        brief = out.getvalue()
        self.assertIn("Go module in server", brief)
        self.assertIn("Node package in client with yarn", brief)
        self.assertIn(str(self.proj / "results" / "contract.md"), brief)
        self.assertIn("$E2E_CLIENT_PORT", brief)
        self.assertIn("copied from the briefs, never added", brief)  # implementers never see the contract
        self.assertIn("`compose verify --project p --prove`: each must fail only because the feature is missing, and `run` refuses", brief)
        self.assertTrue(brief.rstrip().endswith("Stub Open(dsn string) (*sql.DB, error)."))

    def test_wayfinder_brief_asks_by_text_and_opens_lavish_only_for_visual_cards(self):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-9"}))
        out = io.StringIO()
        with self.patched()[0], self.patched()[1], mock.patch("sys.stdout", out):
            fc.fc_briefs.cmd_wayfinder(SimpleNamespace(repo=str(self.repo), project="p", extra="Spec: README.md"))
        brief = out.getvalue()
        page = str(self.proj / "lavish" / "wayfinder.html")
        self.assertIn("NEB-9", brief)
        self.assertIn("Words are text (the default)", brief)
        self.assertIn("A visual card is never asked as text, and a text card is never put on the page", brief)
        self.assertIn("needs-user", brief)
        self.assertIn("before any poll", brief)
        self.assertIn('data-lavish-annotate="off"', brief)
        self.assertIn("data-lavish-question", brief)
        self.assertIn("Answers arrive in the poll's `text`", brief)
        self.assertIn('answer: [{question, answer}, ...]', brief)
        self.assertIn("run it again at once", brief)
        self.assertIn(f"npx -y lavish-axi poll {page}", brief)
        self.assertIn("Decisions so far", brief)
        self.assertTrue(brief.rstrip().endswith("Spec: README.md"))

    def test_a_test_crew_commits_its_files_as_separate_paths(self):
        brief = fc.fc_briefs.test_brief({"id": "005", "protected": ["tests/test_a.py", "tests/test_b.py"], "test": "true"}, "")
        self.assertIn("Test file: tests/test_a.py, tests/test_b.py.", brief)  # prose keeps the comma list
        self.assertIn("`git add tests/test_a.py tests/test_b.py && git commit tests/test_a.py tests/test_b.py -m", brief)

    def planner(self, issue=None, extra="Spec: README.md", stdin=""):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-9"}))
        out = io.StringIO()
        with self.patched()[0], self.patched()[1], mock.patch("sys.stdout", out), mock.patch("sys.stdin", io.StringIO(stdin)):
            fc.fc_briefs.cmd_planner(SimpleNamespace(repo=str(self.repo), project="p", issue=issue, extra=extra))
        return out.getvalue()

    def test_planner_brief_names_the_feature_and_routes_every_gap(self):
        brief = self.planner()
        for s in ("NEB-9", str(self.repo), "p-planner", "### Codebase brief", "## Plan", "needs-user", "blocked",
                  "compose sync", "spec-check --project p", "compose tasks", "change start --why", "change done",
                  "crew start --role", "explore", "groundwork", "wayfinder", "Design", "designer"):
            self.assertIn(s, brief)
        runs = [m.start() for m in re.finditer(r"compose run\b", brief)]
        self.assertTrue(runs, "the brief must say the planner never runs `compose run`")
        for i in runs:  # every mention forbids it; none tells the planner to dispatch
            self.assertRegex(brief[max(0, i - 60):i], r"[Nn]ever|[Nn]ot\b|\b[Nn]o\b")
        self.assertTrue(brief.rstrip().endswith("Spec: README.md"))

    MCP_TOOLS = ("save_issue", "get_issue", "list_issues", "save_comment", "get_project", "list_issue_labels", "create_issue_label",
                 "save_document", "mcp__claude_ai_Linear")

    def printed(self, cmd, **kw):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-9"}))
        out = io.StringIO()
        with self.patched()[0], self.patched()[1], mock.patch("sys.stdout", out):
            cmd(SimpleNamespace(repo=str(self.repo), project="p", extra=None, **kw))
        return " ".join(out.getvalue().split())

    def test_briefs_name_compose_linear_verbs_and_no_linear_mcp_tool(self):
        b = fc.fc_briefs
        printed = [self.printed(c, **kw) for c, kw in ((b.cmd_groundwork, {}), (b.cmd_wayfinder, {}), (b.cmd_planner, {"issue": None}),
                                                         (b.cmd_explore, {}))]
        consts = [getattr(b, n) for n in ("GROUNDWORK_BRIEF", "EXPLORE_BRIEF", "WAYFINDER_BRIEF", "PLANNER_BRIEF", "TOOLSMITH_BRIEF", "CHANGE_BRIEF")]
        for brief in printed + consts:
            self.assertEqual([t for t in self.MCP_TOOLS if t in brief], [], brief[:60])
        _, wayfinder, planner, explore = printed
        self.assertIn('compose linear list --search', wayfinder)
        self.assertRegex(wayfinder, r'compose linear update[^`]*--section "## Decisions so far"')
        self.assertRegex(planner, r'compose linear update[^`]*--section "## Plan"')
        self.assertIn("compose linear create", planner)
        self.assertIn("`compose sync` (it reads the feature and every issue under it from Linear) as sections/2.md says", planner)
        self.assertRegex(explore, r'compose linear update[^`]*--section "## Notes"')
        for s in ("### Codebase brief", "NEB-9"):
            self.assertIn(s, explore)
        self.assertRegex(" ".join(b.CHANGE_BRIEF.split()), r"compose linear update[^`]*--section")

    def test_planner_issue_overrides_the_feature_and_dash_reads_notes_from_stdin(self):
        brief = self.planner(issue="NEB-12", extra="-", stdin="Story NEB-12 only.\n")
        self.assertIn("NEB-12", brief)
        self.assertTrue(brief.rstrip().endswith("Story NEB-12 only."))
        self.assertNotIn("Project notes", self.planner(extra=None))

    def test_planner_is_a_command_with_issue_and_extra(self):
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")
        top = subprocess.run([sys.executable, cli, "--help"], capture_output=True, text=True).stdout
        self.assertRegex(top, r"(?m)^    planner ")
        sub = subprocess.run([sys.executable, cli, "planner", "--help"], capture_output=True, text=True)
        self.assertEqual(sub.returncode, 0, sub.stderr)
        self.assertIn("--issue", sub.stdout); self.assertIn("--extra", sub.stdout)

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

    def test_testcrews_takes_parallel_on_the_command_line(self):
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")
        sub = subprocess.run([sys.executable, cli, "testcrews", "--help"], capture_output=True, text=True)
        self.assertIn("--parallel", sub.stdout)

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
        self.assertIn("Its brief changed after it was built: rewrite the test server/store_test.go holds to the TASK BRIEF.", redo)
        self.assertIn("003 skipped: its state is Done\n", redo)  # Done on Linear stays skipped, as in `run --redo`
        self.assertNotIn("rewrite the test", printed(only="002"))  # a task never built gets the plain brief

    def test_testcrews_takes_redo_on_the_command_line(self):
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")
        sub = subprocess.run([sys.executable, cli, "testcrews", "--help"], capture_output=True, text=True)
        self.assertEqual(sub.returncode, 0, sub.stderr)
        self.assertIn("--redo", sub.stdout)

    def test_no_contract_means_no_test_crews(self):
        with self.patched()[0], self.assertRaises(SystemExit):
            fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only=None, print_=True))


class Explore(unittest.TestCase):
    def test_brief_is_trail_only_time_boxed_and_names_the_feature(self):
        proj = Path(tempfile.mkdtemp()) / ".compose" / "p"
        proj.mkdir(parents=True)
        (proj / "linear.json").write_text(json.dumps({"feature": "ENG-7"}))
        out = []
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=proj.parent.parent), \
                mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            fc.fc_briefs.cmd_explore(SimpleNamespace(extra="the client lives in web/"))
        brief = out[0]
        for want in ("ENG-7", "through trail only", "trail ask", "trail callers", "trail map", "trail skeleton", "under 3 minutes",
                     "at most 40 lines", "the client lives in web/"):
            self.assertIn(want, brief)
        self.assertNotIn("graft", brief)
        self.assertNotIn("graft", fc.fc_briefs.cmd_explore.__doc__)

    def test_no_brief_names_a_graft_command(self):
        b = fc.fc_briefs
        self.assertIn('trail ask "<question>" --source', b.GROUNDWORK_BRIEF)
        self.assertIn('trail ask "<question>" --source', b.WAYFINDER_BRIEF)
        self.assertIn('trail ask "<question>" --source', b.CHANGE_BRIEF)
        for name in ("GROUNDWORK_BRIEF", "EXPLORE_BRIEF", "WAYFINDER_BRIEF", "PLANNER_BRIEF", "CHANGE_BRIEF"):
            self.assertNotRegex(getattr(b, name), r"\bgraft (ask|callers|map|skeleton|grep)\b", name)
            self.assertNotRegex(getattr(b, name), r"`graft\b", name)


class SectionRewrite(unittest.TestCase):
    """Briefs that record a line in one section read it first, then write the whole section back."""
    GET, WHOLE, NEVER = "compose linear get <id> --body", "write the whole section back", "never a whole-description write"

    def setUp(self):
        env = mock.patch.dict(os.environ, ENV); env.start(); self.addCleanup(env.stop)
        self.repo, _ = repo_with({"README.md": "x\n"})
        self.proj = self.repo / ".compose" / "p"
        self.proj.mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-9"}))
        for p in (mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj),
                  mock.patch.object(fc.fc_herdr, "project_worktree", return_value=self.repo)):
            p.start(); self.addCleanup(p.stop)

    def printed(self, cmd, **kw):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            cmd(SimpleNamespace(repo=str(self.repo), project="p", extra=None, **kw))
        return " ".join(out.getvalue().split())

    def test_wayfinder_and_change_briefs_read_then_rewrite_decisions_so_far_in_step_5(self):
        update = 'compose linear update <id> --section "## Decisions so far" --file <path>'
        for brief in (self.printed(fc.fc_briefs.cmd_wayfinder), " ".join(fc.fc_briefs.CHANGE_BRIEF.split())):
            step = brief[brief.index(" 5. "):brief.index(" 6. ")]
            for s in (self.GET, self.WHOLE, update):
                self.assertIn(s, step)
            self.assertLess(step.index(self.GET), step.index(update))  # read, then write back
            self.assertIn(self.NEVER, brief)

    def test_change_brief_keeps_its_strike_through_format(self):
        change = " ".join(fc.fc_briefs.CHANGE_BRIEF.split())
        for s in ("- ~~<old line>~~ ({cid}) - <new line> ({cid}: <why, naming the evidence's source>)", '"keep it as is" changes nothing in the map.'):
            self.assertIn(s, change)

    def test_explore_brief_reads_then_rewrites_notes_with_the_codebase_brief(self):
        explore, update = self.printed(fc.fc_briefs.cmd_explore), 'compose linear update <id> --section "## Notes" --file <path>'
        for s in (self.GET, self.WHOLE, update, "### Codebase brief", self.NEVER, "NEB-9"):
            self.assertIn(s, explore)
        self.assertLess(explore.index(self.GET), explore.index(update))

    def test_no_brief_says_patch_append(self):
        b = fc.fc_briefs
        printed = [self.printed(c, **kw) for c, kw in ((b.cmd_groundwork, {}), (b.cmd_wayfinder, {}), (b.cmd_planner, {"issue": None}), (b.cmd_explore, {}))]
        consts = [" ".join(getattr(b, n).split()) for n in dir(b) if n.endswith("_BRIEF")] + [" ".join(fc.fc_figma.DESIGNER_BRIEF.split())]
        self.assertEqual(len(consts), 7)
        self.assertEqual([brief[:60] for brief in printed + consts if "patch append" in brief], [])


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


class PlannerRules(unittest.TestCase):
    """The planner brief carries the seven-item spec check, its routing, the cutting rules, and the task issue format."""
    def setUp(self):
        env = mock.patch.dict(os.environ, ENV); env.start(); self.addCleanup(env.stop)
        repo, _ = repo_with({"README.md": "x"})
        proj = repo / ".compose" / "p"; proj.mkdir(parents=True)
        (proj / "linear.json").write_text(json.dumps({"feature": "NEB-9"}))
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=repo), mock.patch("sys.stdout", out):
            fc.fc_briefs.cmd_planner(SimpleNamespace(repo=str(repo), project="p", issue=None, extra=""))
        self.brief = out.getvalue()

    def test_the_seven_spec_checks_come_in_numbered_order(self):
        at = -1
        for n, kw in enumerate(("Not yet specified", "### Codebase brief", "traceable", "trail ask", "protected:", "open how", "contract"), 1):
            i = self.brief.find(kw, at + 1)
            self.assertGreater(i, at, f"spec check {n} ({kw}) missing or out of order")
            at = i
        self.assertIn("trail callers", self.brief)
        self.assertNotRegex(self.brief, r"\bgraft (ask|callers|map|skeleton|grep)\b")
        self.assertIn("as new", self.brief)
        self.assertIn("verify.json", self.brief)

    def test_briefs_state_what_the_test_asserts_instead_of_deferring_to_the_contract(self):
        self.assertNotIn("come from the contract", self.brief)  # implementers never see contract.md
        self.assertIn("implementers never see it", self.brief)
        self.assertIn("exact line its test asserts", self.brief)

    def test_gaps_are_routed_to_the_chain_or_to_teammates(self):
        for s in ("decision gap", "fact gap", "1, 3, 6", "2, 4, 5"):
            self.assertIn(s, self.brief)

    def test_every_cutting_rule_is_stated(self):
        for s in ("one or two files", "2500", "five", "350", "no documents", "files:", "after:", "blocked-by", "review order", "split"):
            self.assertIn(s, self.brief)

    def test_task_issue_format_lists_sections_and_block_keys(self):
        at = -1
        for sec in ("## Goal", "## Acceptance criteria", "## Test plan", "## Constraints", "```compose"):
            i = self.brief.find(sec, at + 1)
            self.assertGreater(i, at, f"{sec} missing or out of order")
            at = i
        self.assertIn("id, files, protected, test", self.brief)
        self.assertRegex(self.brief, r"(?s)`after`.{0,80}`title`.{0,80}not .{0,20}key|`title`.{0,80}`after`.{0,80}not .{0,20}key")


class LeanPlannerBrief(unittest.TestCase):
    """The planner's "Stories missing" step proposes the thinnest stories, with split cues and a probe when none fits."""
    setUp = PlannerRules.setUp

    def step(self):
        flat = " ".join(self.brief.split())
        start, end = flat.find("Stories missing"), flat.find("Then check and cut each story")
        self.assertGreater(start, -1)
        self.assertGreater(end, start)
        return flat[start:end]

    def test_the_stories_missing_step_states_the_lean_story_rule(self):
        step = self.step()
        for phrase in ("A story is the thinnest slice that works end to end and unblocks the next story, with at most 5 criteria.",
                       'Split cues: "all", "every", several When/Then pairs, several outside systems.',
                       "When no split fits, a time-boxed probe answers the unknown first, then the split.",
                       "in ship order"):
            self.assertIn(phrase, step)
        self.assertNotIn("one per user-visible slice", self.brief)

    def test_the_step_keeps_its_anchors_in_order(self):
        flat = " ".join(self.brief.split())
        at = -1
        for kw in ("Stories missing", "draft the proposal on the map", "in ship order", "with a card id per story",
                   '`needs-user "<issue>: <n> cards <ids>; recommendation <one line>"`', "Then check and cut each story"):
            i = flat.find(kw, at + 1)
            self.assertGreater(i, at, f"{kw} missing or out of order")
            at = i

    def test_the_step_keeps_the_wrapped_line_style(self):
        lines = self.brief.splitlines()
        first = next(n for n, l in enumerate(lines) if "Stories missing" in l)
        last = next(n for n, l in enumerate(lines) if "Then check and cut each story" in l)
        for line in lines[first:last + 1]:
            self.assertRegex(line, r"^ {5}\S")
            self.assertLessEqual(len(line), 130)
        for line in lines[first:last]:  # no orphan wrap: only the step's last line may be short
            self.assertGreaterEqual(len(line), 100, line)

    def test_the_template_keeps_its_placeholders_and_house_text(self):
        import string
        fields = {f for _, f, _, _ in string.Formatter().parse(fc.fc_briefs.PLANNER_BRIEF) if f}
        self.assertEqual(fields, {"issue", "name", "slug", "wt"})
        for banned in ("—", "Product-Manager-Skills", "deanpeters"):
            self.assertNotIn(banned, fc.fc_briefs.PLANNER_BRIEF)
            self.assertNotIn(banned, self.brief)


class PlanJudgmentBrief(unittest.TestCase):
    """Step 5 of the planner brief tells the planner to write the delivery plan's judgment sections at each cut."""
    PHRASES = (
        'find the map\'s "<map id>: delivery plan" in `compose linear doc list`',
        "write its judgment sections (Outside waits, How this maps to compose, Priority when something has to give) at each cut"
        ' with `compose linear doc update <id> --section "<heading>" --file <path>`',
        "priority is the ranked list under Priority when something has to give; Linear's priority field stays unused",
        "the CLI rewrites the data sections (Start today, Lanes, Waves, Critical path); never edit them",
    )
    setUp = PlannerRules.setUp

    def step5(self):
        start = self.brief.index("5. After cutting")
        return self.brief[start:self.brief.index("6. Design stories", start)]

    def test_step_5_holds_each_phrase_and_keeps_its_old_steps(self):
        flat = " ".join(self.step5().split())
        for p in self.PHRASES + ("compose spec-check --project p", "compose change done"):
            self.assertIn(p, flat)

    def test_every_line_wraps_at_125_with_a_three_space_indent(self):
        lines = self.step5().rstrip("\n").split("\n")
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(len(line), 125, line)
        for line in lines[1:]:
            self.assertRegex(line, r"^ {3}\S")

    def test_brief_keeps_its_placeholders_and_has_no_em_dash(self):
        fields = {f for _, f, _, _ in string.Formatter().parse(fc.fc_briefs.PLANNER_BRIEF) if f}
        self.assertEqual(fields, {"issue", "name", "slug", "wt"})
        self.assertNotIn("\u2014", self.brief)


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

    def test_brief_names_the_project_and_the_tools_and_gates(self):
        self.write({"feature": "ENG-1", "figma": "AbCdEf123456"})
        brief = self.brief()
        for text in ("ENG-1", str(self.wt), "AbCdEf123456", "figma:figma-use", "0d", "needs-user", "design-review", "kind: design",
                     "compose frame0 up --project p", "compose figma judge --project p --only NNN", "compose designcrews --project p"):
            self.assertIn(text, brief)
        self.assertNotIn("not set", brief)
        self.assertNotIn("Project notes from the team lead:", brief)

    def test_without_a_stored_key_the_brief_says_to_ask_for_the_file(self):
        self.assertIn("not set: ask the product owner for the Figma file", self.brief())

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

    def test_brief_splits_figma_writes_from_framelink_reads(self):
        brief = " ".join(self.brief().split())
        for text in ("WRITES ONLY", "never a read-only script", "returns the facts it set", "mcp__framelink-figma__get_figma_data", "download_figma_images",
                     "never `get_screenshot`, `get_metadata`, or `get_design_context`", "Never use another `plugin:*figma*`", "compose figma tree --project p --only NNN"):
            self.assertIn(text, brief)

    def test_design_task_nodes_are_page_and_name_pairs_one_page_per_task(self):
        brief = " ".join(self.brief().split())
        step5 = brief[brief.index(" 5. "):brief.index(" 6. ")]
        self.assertIn("A design task carries `kind: design`, `nodes:` (the page and the top-level frames or component sets it may create or touch, "
                      "as <page>/<name>, one page per task), and `test: compose figma judge --project p --only NNN`.", step5)
        self.assertNotIn("node id", brief)
        self.assertEqual(brief.count("<page>/<name>"), 1)


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

    def run_crews(self, only=None, print_=False, busy=(), max=None):
        started, out = [], io.StringIO()
        def crew(ns):
            started.append(ns)
            if ns.role in busy: raise SystemExit("busy")
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr_space") as space, \
                mock.patch.object(fc.fc_crew, "cmd_crew", side_effect=crew), mock.patch("sys.stdout", out):
            fc.fc_figma.cmd_designcrews(SimpleNamespace(repo=".", project="p", only=only, print_=print_, max=max))
        return started, out.getvalue(), space

    def test_print_shows_one_brief_per_open_design_task(self):
        started, out, space = self.run_crews(print_=True)
        for text in ("===== design-001", "===== design-005", "Figma file AbCdEf123456", "Checkout/Cart", "Checkout/Pay", str(self.proj / "results" / "001.tree.txt"), "judge 001", "figma:figma-use", "TASK BRIEF:", "## Goal\nDraw 001."):
            self.assertIn(text, out)
        self.assertEqual((out.count("====="), started, space.call_count), (2, [], 0))
        flat = " ".join(out.split())
        for text in ("WRITES ONLY", "mcp__framelink-figma__get_figma_data", "compose figma tree --project p --only 001", "never a read-only `use_figma` script", "Never use another `plugin:*figma*`"):
            self.assertIn(text, flat)

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
            fc.fc_figma.cmd_designcrews(SimpleNamespace(repo=".", project="p", only=only, print_=print_, max=max))
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

    def test_every_variant_carries_the_newest_entry_of_its_project_and_the_filing_steps(self):
        self.entry("shop", "crew start hung at the trust prompt", at="2026-10-17 09:00:00", cli_commit="1111111")
        self.entry("shop", "report crashed with KeyError owner", at="2026-10-17 15:30:00", reporter="shop-impl-002")
        self.entry("other", "other project's newer bug", at="2026-10-18 08:00:00", cli_commit="9999999")
        for state in ("maintainer", "reporter", "no-gh"):
            out = self.brief(state)
            for s in ("report crashed with KeyError owner", "shop-impl-002", "2026-10-17 15:30:00", "abc1234",
                      "SKILL.md", "# <command>: <symptom>", "--comment <n>", "--new"):
                self.assertIn(s, out, state)
            self.assertIn('compose tool-bug file --project shop --entry "2026-10-17 15:30:00" --body <file>', out, state)
            for s in ("sections/", "trust prompt", "1111111", "other project's", "9999999",
                      "2026-10-17 09:00:00", "2026-10-18 08:00:00"):
                self.assertNotIn(s, out, state)

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

    def test_the_maintainers_toolsmith_fixes_on_a_branch_and_never_merges(self):
        self.entry("shop", "report crashed")
        out = self.brief("maintainer")
        for s in ("your own worktree", "compose tool-bug pr --project shop --entry", "You never merge", "Fixes #"):
            self.assertIn(s, out)
        self.assertNotIn("never edit or commit", out)

    def test_anyone_elses_toolsmith_only_diagnoses_and_asks_before_filing(self):
        self.entry("shop", "report crashed")
        for state in ("reporter", "no-gh"):
            out = self.brief(state)
            for s in ("never edit or commit", "needs-user", "--approved"):
                self.assertIn(s, out, state)
            self.assertNotIn("You never merge", out, state)

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


class ScenarioPlannerLine(unittest.TestCase):
    NEW = "   How to run (the `test:` command), what is covered in words, unlisted holdouts, then a `scenarios` block (SKILL.md)."
    OLD = "   How to run (the `test:` command), what is covered in words, and that the test has holdout cases not listed."
    BRIEF = fc.fc_briefs.PLANNER_BRIEF

    def test_the_new_line_follows_the_test_plan_heading(self):
        lines = self.BRIEF.splitlines()
        self.assertEqual(lines[lines.index("   ## Test plan") + 1], self.NEW)
        self.assertEqual(lines[lines.index("   ## Test plan") + 2], "   ## Constraints")

    def test_the_new_line_appears_once_and_the_old_line_is_gone(self):
        self.assertEqual(self.BRIEF.splitlines().count(self.NEW), 1)
        self.assertNotIn(self.OLD, self.BRIEF)
        self.assertNotIn("holdout cases not listed", self.BRIEF)

    def test_the_brief_still_formats_with_its_four_fields_and_the_line(self):
        fields = {f for _, f, _, _ in string.Formatter().parse(self.BRIEF) if f}
        self.assertEqual(fields, {"issue", "name", "slug", "wt"})
        printed = self.BRIEF.format(issue="NEB-9", name="n", slug="s", wt="/w")
        self.assertIn(self.NEW + "\n", printed)


class ScenarioTestBrief(unittest.TestCase):
    LINE = ("The TASK BRIEF's `scenarios` block gives one scenario per criterion: write a case for each scenario and for each of its negative_cases, "
            "then add holdout cases none of them lists, within the budget.\n")
    F = "`" * 3
    CRIT = "## Acceptance criteria\n\n* a\n* b\n\n"
    COMPOSE = f"{F}compose\nfiles: bin/a.py\n{F}\n"
    PLAIN = CRIT + COMPOSE
    BLOCK = f"## Test plan\n\n{F}scenarios\nid: 1\ngiven: g\nnegative_cases: n\n{F}\n\n"
    WITH = CRIT + BLOCK + COMPOSE
    META = {"id": "005", "protected": ["tests/test_a.py"], "test": "true", "files": ["bin/a.py"]}

    def brief(self, body="", **kw):
        return fc.fc_briefs.test_brief({**self.META, "body": body}, "C", **kw)

    def test_the_line_sits_between_the_cover_line_and_the_budget_line_once(self):
        brief = self.brief(self.WITH)
        self.assertIn("test behavior only.\n" + self.LINE + "Budget: at most 60 lines", brief)
        self.assertEqual(brief.count(self.LINE), 1)

    def test_with_the_line_removed_the_brief_is_the_plain_brief_for_the_same_body(self):
        self.assertEqual(self.brief(self.WITH).replace(self.LINE, "", 1), self.brief(self.PLAIN).replace(self.PLAIN, self.WITH))

    def test_the_line_holds_for_a_rebuilt_brief_and_a_task_budget(self):
        brief = self.brief(self.WITH, budget=30, rebuilt=True)
        self.assertIn("test behavior only.\n" + self.LINE + "Budget: at most 30 lines", brief)

    def test_no_line_without_a_block(self):
        plain = self.brief(self.PLAIN)
        for brief in (fc.fc_briefs.test_brief({k: v for k, v in self.META.items()}, "C"), self.brief(""), plain,
                      self.brief(self.CRIT + f"{self.F}python\nid: 1\n{self.F}\n" + self.COMPOSE)):
            self.assertNotIn("`scenarios` block", brief)
        self.assertIn("test behavior only.\nBudget: at most 60 lines", plain)

    def test_the_task_brief_part_carries_the_whole_body_block_included(self):
        self.assertIn(f"TASK BRIEF:\n{self.WITH}\n\nCONTRACT:\nC", self.brief(self.WITH))


if __name__ == "__main__":
    unittest.main()
