import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_taskreview  # noqa: E402

BODY = "## Goal\nx\n\n## Acceptance criteria\n* greets\n\n## Test plan\ny"
HIGH = {"severity": "high", "perspective": "correctness", "file": "b.py", "line": 1, "note": "wrong greeting", "confirmed": True,
        "evidence": "print('hi')", "fix": "print('hello')"}


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class ReviewCommand(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.proj = self.repo / ".compose" / "feat"
        (self.proj / "results").mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.py").write_text("a = 1\n")
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "base")
        for tid, base, line in (("001", "main", "print('hi')"), ("002", "t/001", "print('two')")):
            git(self.repo, "checkout", "-q", "-b", f"t/{tid}", base)
            (self.repo / f"b{tid}.py").write_text(line + "\n")
            git(self.repo, "add", "."); git(self.repo, "commit", "-qm", f"task {tid}")
            (self.proj / "results" / f"{tid}.json").write_text(json.dumps({"base": base if base != "main" else "HEAD"}))
        git(self.repo, "checkout", "-q", "main")
        (self.proj / "verify.json").write_text(json.dumps({"task_prefix": "t/", "review_base": "main"}))
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-9"}))
        tasks = [{"id": t, "title": f"task {t}", "body": BODY, "files": [], "protected": [], "linear": f"NEB-{t}"} for t in ("001", "002")]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {"description": "The feature plan"}, "tasks": tasks}))

    def run_review(self, only=None, range=None, rv=None):
        a = SimpleNamespace(repo=str(self.repo), project="feat", only=only, range=range)
        rv = rv or {"pass": True, "issues": [], "confirmed": [], "malformed": 0}
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_core, "say"), \
                mock.patch.object(fc_taskreview.fc_review, "llm_review", return_value=rv) as llm, \
                mock.patch.object(fc_taskreview.fc_ledger, "record_task", return_value=True) as task, \
                mock.patch.object(fc_taskreview.fc_ledger, "record", return_value=True) as feature, \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ex:
            fc_taskreview.cmd_review(a)
        return ex.exception.code, out.getvalue(), llm, task, feature

    def test_one_task_is_reviewed_on_its_own_diff_brief_and_recorded_on_its_issue(self):
        bad = {"pass": False, "issues": [HIGH], "confirmed": [HIGH], "malformed": 0}
        code, out, llm, task, feature = self.run_review(only="001", rv=bad)
        diff, plan = llm.call_args.args[1:3]
        self.assertIn("+print('hi')", diff)
        self.assertNotIn("a.py", diff)
        self.assertIn("The feature plan", plan)
        self.assertIn("# Task 001: task 001", plan)
        self.assertNotIn("Task 002", plan)
        self.assertEqual(code, 1)
        self.assertIn("b.py:1", out)
        self.assertIn("print('hello')", out)
        (_, _, meta, events), _ = task.call_args
        e = events[0]
        self.assertEqual((meta["id"], e["phase"], e["source"], e["result"], e["task_ids"], e["commit_sha"]), ("001", "review", "review", "fail", ["001"], git(self.repo, "rev-parse", "t/001")))
        self.assertTrue(e["criterion_id"])
        feature.assert_not_called()
        self.assertEqual(json.loads((self.proj / "results" / "review-001.json").read_text())["review"]["confirmed"], [HIGH])

    def test_a_stacked_task_is_reviewed_against_the_layer_below_it(self):
        code, _, llm, task, _ = self.run_review(only="001,002")
        first, second = (c.args[1] for c in llm.call_args_list)
        self.assertIn("b001.py", first)
        self.assertIn("b002.py", second)
        self.assertNotIn("b001.py", second)
        self.assertEqual((code, [c.args[2]["id"] for c in task.call_args_list], task.call_args.args[3][0]["result"]), (0, ["001", "002"], "pass"))

    def test_a_range_is_one_review_recorded_on_the_feature_without_tasks(self):
        code, _, llm, task, feature = self.run_review(range="main..t/001")
        self.assertEqual(code, 0)
        self.assertIn("b001.py", llm.call_args.args[1])
        task.assert_not_called()
        issue, events = feature.call_args.args
        self.assertEqual((issue, events[0]["task_ids"], events[0]["source"], events[0]["commit_sha"]), ("NEB-9", [], "review", git(self.repo, "rev-parse", "t/001")))
        self.assertEqual(len(list((self.proj / "results").glob("review-range-*.json"))), 1)

    def test_a_range_with_only_is_briefed_by_and_recorded_on_those_tasks(self):
        _, _, llm, task, feature = self.run_review(only="001", range="main..t/001")
        self.assertIn("# Task 001", llm.call_args.args[2])
        self.assertEqual([c.args[2]["id"] for c in task.call_args_list], ["001"])
        feature.assert_not_called()

    def test_a_confirm_call_that_failed_is_an_error_not_a_fail(self):
        broken = {"pass": False, "issues": [], "confirmed": [], "error": "1 confirm call(s) failed: boom"}
        code, out, _, task, _ = self.run_review(only="001", rv=broken)
        self.assertEqual((code, task.call_args.args[3][0]["result"]), (1, "error"))
        self.assertIn("boom", out)

    def test_a_merged_task_with_no_branch_points_to_range(self):
        git(self.repo, "branch", "-D", "t/002")
        a = SimpleNamespace(repo=str(self.repo), project="feat", only="002", range=None)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), self.assertRaises(SystemExit) as ex:
            fc_taskreview.cmd_review(a)
        self.assertIn("--range <base>..<tip>", str(ex.exception))

if __name__ == "__main__":
    unittest.main()
