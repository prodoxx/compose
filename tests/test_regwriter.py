import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_regwriter  # noqa: E402


BODY = "## Goal\nFix it.\n\n## Acceptance criteria\n\n* a\n* b\n\n## Test plan\nRun it.\n"
REPRO = "## Reproduction\n\nrun x\nsee y\n\n## Constraints\nnone"
FENCE = "`" * 3


class Brief(unittest.TestCase):
    """The regression test writer's branch and brief."""

    def brief(self, body, diff="diff --git a/x b/x\n+fix"):
        return fc_regwriter.writer_brief({"id": "007", "title": "Fix the thing", "body": body}, diff)

    def test_branch_is_task_prefix_plus_regress_id(self):
        proj = Path(tempfile.mkdtemp()) / ".compose" / "p"
        proj.mkdir(parents=True)
        (proj / "verify.json").write_text(json.dumps({"task_prefix": "fast/p/"}))
        self.assertEqual(fc_regwriter.writer_branch(proj, "007"), "fast/p/regress-007")

    def test_starts_with_four_lines_then_criteria_when_no_reproduction(self):
        self.assertTrue(self.brief(BODY).startswith(
            "Write one regression test for task 007: Fix the thing.\n"
            "The fix below passed. Your test must fail on the base and pass on the fix.\n"
            "Change only test files, in one commit; never edit the fix.\n"
            'Then report working "test ready: <commit> <command>", where <command> runs only your test.\n'
            "\n## Expected behavior\n* a\n* b\n\n## The fix\n"))

    def test_reproduction_wins_over_criteria(self):
        text = self.brief(BODY + "\n" + REPRO)
        self.assertIn("## Expected behavior\nrun x\nsee y\n\n## The fix\n", text)
        self.assertNotIn("* a", text)

    def test_section_runs_to_end_and_ends_at_next_level_two_heading_only(self):
        text = self.brief("## Acceptance criteria\n### sub\n* a\n\n## Z\nz")
        self.assertIn("## Expected behavior\n### sub\n* a\n\n## The fix", text)
        self.assertIn("## Expected behavior\nlast\n\n## The fix", self.brief("## Reproduction\n\nlast\n\n"))

    def test_ends_with_diff_in_fence(self):
        self.assertTrue(self.brief(BODY, "-a\n+b").endswith(f"## The fix\n{FENCE}diff\n-a\n+b\n{FENCE}"))


class ScenarioBrief(unittest.TestCase):
    """The regression writer's brief carries the task's scenarios block between the expected behavior and the fix."""
    BLOCK = "id: 1\ngiven: g\n\nid: 2\ngiven: h\nnegative_cases: n"
    PLAN = f"## Test plan\nRun it.\n\n{FENCE}scenarios\n{BLOCK}\n{FENCE}\n"

    def brief(self, body, diff="D"):
        return fc_regwriter.writer_brief({"id": "007", "title": "T", "body": body}, diff)

    def test_the_section_sits_between_expected_behavior_and_the_fix(self):
        body = BODY.split("## Test plan")[0] + self.PLAN
        self.assertTrue(self.brief(body, "-a\n+b").endswith(
            f"\n## Expected behavior\n* a\n* b\n\n## Scenarios\n{FENCE}scenarios\n{self.BLOCK}\n{FENCE}\n\n## The fix\n{FENCE}diff\n-a\n+b\n{FENCE}"))

    def test_the_block_does_not_leak_into_the_reproduction_text(self):
        text = self.brief(REPRO.split("## Constraints")[0] + self.PLAN)
        self.assertIn("## Expected behavior\nrun x\nsee y\n\n## Scenarios\n", text)
        self.assertEqual(text.count("id: 1"), 1)

    def test_a_body_without_a_block_keeps_today_brief(self):
        for body in (BODY, BODY + f"\n{FENCE}compose\nfiles: [a]\n{FENCE}\n"):
            text = self.brief(body)
            self.assertNotIn("## Scenarios", text)
            self.assertTrue(text.endswith(f"## Expected behavior\n* a\n* b\n\n## The fix\n{FENCE}diff\nD\n{FENCE}"))


class Writer(unittest.TestCase):
    """The regression test writer starts on its own branch and checkout off the task's tip."""

    def setUp(self):
        env = {"HERDR_ENV": "", "COMPOSE_NO_TREEHOUSE": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        root = Path(tempfile.mkdtemp())
        self.repo, self.proj, self.wt = root / "repo", root / "repo" / ".compose" / "p", root / "clone"
        self.proj.mkdir(parents=True)
        (self.proj / "verify.json").write_text(json.dumps({"task_prefix": "fast/p/", "install": "make deps"}))
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
        self.git(self.repo, "checkout", "-q", "-b", "fast/p/001")
        (self.repo / "x.py").write_text("fixed\n")
        self.git(self.repo, "add", "x.py")
        self.git(self.repo, "commit", "-q", "-m", "fix")
        self.git(self.repo, "checkout", "-q", "main")
        self.git(root, "clone", "-q", "-b", "fast/p/001", str(self.repo), str(self.wt))
        self.tip = self.git(self.repo, "rev-parse", "fast/p/001")
        self.result = {"branch": "fast/p/001", "worktree": str(self.wt)}

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def start(self):
        with mock.patch.object(fc.fc_crew, "cmd_crew") as crew, mock.patch.object(fc.fc_worktree, "ready_checkout") as ready:
            path = fc_regwriter.start_writer(self.repo, self.proj, {"id": "001", "title": "T", "body": BODY}, self.result)
        return path, ready, vars(crew.call_args[0][0])

    def test_plain_worktree_on_branch_at_tip_ready_with_install_and_started(self):
        self.git(self.repo, "branch", "fast/p/regress-001", "main")
        path, ready, crew = self.start()
        self.assertEqual(path, self.proj / "worktrees" / "regress-001")
        self.assertEqual(self.git(path, "branch", "--show-current"), "fast/p/regress-001")
        self.assertEqual(self.git(self.repo, "rev-parse", "fast/p/regress-001"), self.tip)
        ready.assert_called_once_with(self.repo, path, install="make deps")
        want = dict(repo=str(self.repo), project="p", action="start", role="tests-001", title="tests 001", force=False, cwd=str(path), leased=True)
        self.assertEqual({k: crew[k] for k in want}, want)
        self.assertIn("+fixed", crew["brief"])

    def test_treehouse_slot_is_leased_on_the_branch_and_recorded(self):
        slot = Path(tempfile.mkdtemp())
        with mock.patch.object(fc.fc_worktree, "use_treehouse", return_value=True), mock.patch.object(fc.fc_worktree, "slot_on", return_value=slot) as on, \
             mock.patch.object(fc.fc_worktree, "slots") as slots:
            path, ready, crew = self.start()
        on.assert_called_once_with(self.repo, "fast/p/regress-001", "compose:p:regress-001")
        slots.assert_called_once_with(self.proj, {"regress-001": str(slot)})
        self.assertEqual((path, crew["cwd"]), (slot, str(slot)))
        self.assertFalse((self.proj / "worktrees").exists())

    def test_model_is_tests_else_groundwork_else_none(self):
        for models, want in [({"tests": "a", "groundwork": "b"}, "tests"), ({"groundwork": "b"}, "groundwork"), ({"small": "c"}, None)]:
            with self.subTest(models=models), mock.patch.object(fc.fc_config, "CREW_MODELS", models), mock.patch.object(fc.fc_worktree, "use_treehouse", return_value=True), \
                 mock.patch.object(fc.fc_worktree, "slot_on", return_value=self.wt), mock.patch.object(fc.fc_worktree, "slots"):
                self.assertEqual(self.start()[2]["model"], want)


class Ready(unittest.TestCase):
    """run waits for the writer's ready line, and stops waiting when the writer ends without one."""

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "p"
        self.proj.mkdir(parents=True)
        self.name = fc.fc_herdr.agent_name("p", "tests-007")
        self.rec = fc.fc_crew.crew_dir(self.proj) / f"{self.name}.json"
        self.rec.write_text(json.dumps({"name": self.name, "space": "w1"}))
        self.agents, self.sleeps = {"agents": [{"name": self.name, "workspace_id": "w1"}]}, []

    def wait(self, *later, **kw):
        queue = list(later)
        def sleep(seconds):
            self.sleeps.append(seconds)
            self.assertTrue(queue, "still waiting")
            for state, message in queue.pop(0):
                fc.fc_crew.write_report(self.proj, self.name, state, message)
        with mock.patch.object(fc.fc_herdr, "herdr", return_value=self.agents), mock.patch("time.sleep", sleep):
            return fc_regwriter.wait_ready(self.proj, "007", **kw)

    def report(self, state, message):
        fc.fc_crew.write_report(self.proj, self.name, state, message)

    def test_parse_ready(self):
        self.assertEqual(fc_regwriter.READY_RE.pattern, r"test ready: ([0-9a-f]{7,40}) (.+)")
        self.assertEqual(fc_regwriter.parse_ready("x\ntest ready: abc1234 python3 -m unittest tests.test_x "), ("abc1234", "python3 -m unittest tests.test_x"))
        for message in (None, "", "test ready: abc12 cmd", "test ready: ABC1234 cmd"):
            self.assertIsNone(fc_regwriter.parse_ready(message))

    def test_first_ready_line_in_working_or_done_wins_even_after_a_blocked_report(self):
        self.report("blocked", "stuck")
        self.report("done", "no line")
        self.report("done", "test ready: abc1234 cmd one")
        self.report("working", "test ready: def5678 cmd two")
        self.assertEqual(self.wait(), ("abc1234", "cmd one"))

    def test_ready_line_before_since_is_ignored_and_a_closed_record_ends_the_wait(self):
        self.report("working", "test ready: abc1234 cmd")
        self.rec.write_text(json.dumps({"name": self.name, "space": "w1", "closed": True}))
        self.assertIsNone(self.wait(since=1))
        self.assertEqual(self.wait(since=0), ("abc1234", "cmd"))

    def test_report_written_after_the_first_pass_is_read_after_sleeping_poll_seconds(self):
        self.report("working", "still going")
        self.assertEqual(self.wait([("working", "x")], [("working", "test ready: abc1234 cmd")], poll=3), ("abc1234", "cmd"))
        self.assertEqual(self.sleeps, [3, 3])

    def test_blocked_tool_bug_contradiction_or_done_without_a_ready_line_ends_the_wait(self):
        for state in ("blocked", "tool-bug", "contradiction", "done"):
            with self.subTest(state=state):
                self.assertIsNone(self.wait([(state, "no line")]))

    def test_writer_gone_ends_the_wait_but_herdr_unable_to_say_keeps_waiting(self):
        self.agents = {"agents": []}
        self.assertIsNone(self.wait())
        self.rec.unlink()
        self.assertIsNone(self.wait())
        self.rec.write_text(json.dumps({"name": self.name, "space": "w1"}))
        self.agents = None
        self.assertEqual(self.wait([("working", "hi")], [("working", "test ready: abc1234 cmd")]), ("abc1234", "cmd"))
