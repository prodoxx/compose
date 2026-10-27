import hermetic  # first: no test sees the real home
import importlib, importlib.machinery, importlib.util, os, unittest
from pathlib import Path
import subprocess, sys, tempfile
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_regproof, fc_regwriter  # noqa: E402


def proc(code, out="", err=""):
    return subprocess.CompletedProcess(["t"], code, out, err)


class Classify(unittest.TestCase):
    """A regression test's run on the base: a pass, a harness error, or a failure that proves it."""

    def test_pattern(self):
        self.assertEqual(fc_regproof.HARNESS_RE.pattern, r"ImportError|ModuleNotFoundError|Failed to import test module|ERROR collecting|errors? during collection")

    def test_exit_zero_passes_even_naming_an_import_error(self):
        for out in ("", "ImportError: x\nOK", "command not found"):
            self.assertEqual(fc_regproof.base_result(proc(0, out)), ("pass", False), out)

    def test_exit_124_is_an_error(self):
        self.assertEqual(fc_regproof.base_result(proc(124, "plain output")), ("error", None))

    def test_harness_output_is_an_error_in_stdout_or_stderr(self):
        for text in ("bash: pytest: command not found", "ImportError: no x", "ModuleNotFoundError: y", "Failed to import test module: t",
                     "ERROR collecting tests/t.py", "1 error during collection", "2 errors during collection"):
            for out, err in ((text, ""), ("", text)):
                self.assertEqual(fc_regproof.base_result(proc(1, out, err)), ("error", None), text)

    def test_other_failing_exit_fails_expected(self):
        for code in (1, 2, 5, -9):
            self.assertEqual(fc_regproof.base_result(proc(code, "AssertionError: 1 != 2")), ("fail_expected", True), code)

class Prove(unittest.TestCase):
    """The regression test runs on the base and on the fix, and the proof says whether it proves the fix."""
    ENV = {"HERDR_ENV": "", "COMPOSE_NO_TREEHOUSE": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    KEYS = {"commit", "command", "paths", "base_sha", "base", "base_output", "tip_sha", "tip_exit", "tip_output", "proven"}

    def git(self, cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, cwd, files):
        for n, t in files.items(): (Path(cwd) / n).write_text(t)
        self.git(cwd, "add", "-A"); self.git(cwd, "commit", "-qm", "m")
        return self.git(cwd, "rev-parse", "HEAD")

    def setUp(self):
        for patch in (mock.patch.dict(os.environ, self.ENV), mock.patch.object(fc_regwriter, "writer_branch", return_value="w/001")):
            patch.start(); self.addCleanup(patch.stop)
        root = Path(tempfile.mkdtemp()); self.repo, self.wt, self.proj = root / "repo", root / "wt", root / "repo" / ".compose" / "p"
        self.repo.mkdir(); self.git(self.repo, "init", "-q", "-b", "main"); self.base = self.commit(self.repo, {"val.txt": "bad"})
        self.git(root, "clone", "-q", str(self.repo), str(self.wt)); self.git(self.wt, "checkout", "-q", "-b", "task/001")
        self.fix = self.commit(self.wt, {"val.txt": "good"})

    def prove(self, steps, command):
        self.git(self.repo, "checkout", "-q", "-b", "w/001", self.base)
        for files in steps: commit = self.commit(self.repo, files)
        self.git(self.repo, "checkout", "-q", "main"); self.runs = []
        real = fc.fc_implement.run_test
        def spy(meta, path):
            self.runs.append((Path(path).name, self.git(path, "rev-parse", "HEAD"), self.git(path, "diff", "--cached", "--name-only")))
            return real(meta, path)
        with mock.patch.object(fc.fc_implement, "run_test", spy):
            return fc_regproof.prove(self.repo, self.proj, {"id": "001", "test": "x"}, {"worktree": str(self.wt), "branch": "task/001"}, commit, command)

    def assertAtFix(self, p):
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), self.fix)
        self.assertFalse(self.git(self.wt, "status", "--porcelain", "--untracked-files=no"))
        self.assertEqual((p["proven"], self.git(self.repo, "branch", "--list", "task/001")), (False, ""))
        self.assertEqual(len(self.git(self.repo, "worktree", "list").splitlines()), 1)

    def test_failing_on_base_and_passing_on_fix_is_proven(self):
        p = self.prove([{"check.sh": 'test "$(cat val.txt)" = good\n'}], "sh check.sh")
        tip = self.git(self.wt, "rev-parse", "HEAD")
        self.assertEqual(set(p), self.KEYS)
        self.assertEqual((p["command"], p["paths"], p["base_sha"], p["base"], p["tip_sha"], p["tip_exit"], p["proven"]), ("sh check.sh", ["check.sh"], self.base, ("fail_expected", True), tip, 0, True))
        self.assertEqual(self.runs[0], ("proof-001", self.base, "check.sh"))
        self.assertEqual((self.git(self.repo, "rev-parse", "task/001"), self.git(self.repo, "rev-parse", "task/001~1")), (tip, self.fix))
        self.assertEqual(len(self.git(self.repo, "worktree", "list").splitlines()), 1)

    def test_not_proven_leaves_the_task_at_the_fix(self):
        # one repo, not one per verdict: Classify covers how each exit is classed
        p = self.prove([{"check.sh": ""}], "printf '%03000d' 0; echo ImportError; exit 1")
        self.assertEqual((p["base"], p["tip_exit"]), (("error", None), 1)); self.assertAtFix(p)
        self.assertEqual(self.git(self.wt, "rev-parse", p["tip_sha"] + "~1"), self.fix)
        self.assertIn("ImportError", p["base_output"]); self.assertLessEqual(len(p["base_output"]), 2000)

    def test_a_test_commit_that_does_not_apply_to_the_base_is_an_error(self):
        p = self.prove([{"val.txt": "good"}, {"val.txt": "better"}], "true")
        self.assertEqual((p["base"], p["tip_exit"]), (("error", None), 0)); self.assertAtFix(p)

    def test_a_test_commit_that_does_not_apply_to_the_fix_is_aborted(self):
        p = self.prove([{"val.txt": "worse"}], "true")
        self.assertEqual((p["tip_sha"], p["base"]), ("", ("pass", False))); self.assertNotEqual(p["tip_exit"], 0); self.assertAtFix(p)


class Record(unittest.TestCase):
    """A proof's red and final rows go on the task's ledger."""
    META = {"id": "001", "linear": "ENG-3"}
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def setUp(self):
        self.ledger = importlib.import_module("fc_ledger")  # the foundation's module, not on every branch
        self.add = mock.Mock(return_value=True)
        for patch in (mock.patch.object(self.ledger, "add_row", self.add), mock.patch.object(self.ledger, "run_id", return_value="run-1"),
                      mock.patch.object(self.ledger, "criterion_ids", return_value=["001.1:ab", "001.2:cd"])):
            patch.start(); self.addCleanup(patch.stop)

    def proof(self, base=("fail_expected", True), tip_exit=0, **kw):
        return dict({"commit": self.SHA, "base_sha": "b" * 40, "base": base, "tip_sha": "t" * 40, "tip_exit": tip_exit}, **kw)

    def rows(self, red, final):
        e, c = self.ledger.event, ["001.1:ab", "001.2:cd"]
        return [e("run-1", "b" * 40, ["001"], c, "red", "run", red[0], red[1], "regression test 0123456789ab " + red[2] + " on the base"),
                e("run-1", "t" * 40, ["001"], c, "final", "run", final[0], None, "regression test 0123456789ab " + final[1] + " on the fix")]

    def test_rows_by_the_base_and_the_tip(self):
        for base, tip_exit, red, final in ((("fail_expected", True), 0, ("fail_expected", True, "fails"), ("pass", "passes")),
                                           (("pass", False), 0, ("pass", False, "passes"), ("pass", "passes")),
                                           (("error", None), 1, ("error", None, "errors"), ("fail", "fails")),
                                           (("fail_expected", True), 2, ("fail_expected", True, "fails"), ("fail", "fails"))):
            with self.subTest(base=base, tip_exit=tip_exit):
                self.add.reset_mock()
                fc_regproof.record_proof(Path("p"), self.META, self.proof(base, tip_exit))
                self.add.assert_called_once_with("ENG-3", self.rows(red, final))

    def test_a_failed_pick_has_an_empty_tip_sha(self):
        fc_regproof.record_proof(Path("p"), self.META, self.proof(("pass", False), 1, tip_sha=""))
        self.assertEqual(self.add.call_args[0][1][1]["commit_sha"], "")
