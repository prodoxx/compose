import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

ENV = {"HERDR_ENV": "", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
CASES = "def test_a\ndef test_b\ndef test_c\n"


def score(base, head, test="true", loose=None, **kv):
    r = Path(tempfile.mkdtemp())
    run = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True, text=True)
    put = lambda files: [(r / n).write_text(t) for n, t in files.items()]
    run("init", "-q", "-b", "main"); put(base); run("add", "-A"); run("commit", "-qm", "base")
    b = run("rev-parse", "HEAD").stdout.strip()
    put(head); run("add", "-A"); run("commit", "-qm", "head"); put(loose or {})
    meta = {"id": "001", "files": ["impl.txt"], "protected": ["test_impl.py"], "test": test, "body": "## Acceptance criteria\n\n* a\n* b\n", **kv}
    return r, fc.fc_bench.score_test(r, meta, b)


def want(**kv):
    return {"lines": 3, "budget": 60, "over_budget": False, "cases": 3, "holdouts": 1, "green": True, "red": True, **kv}


class Score(unittest.TestCase):
    def setUp(self):
        mock.patch.dict(os.environ, ENV).start(); self.addCleanup(mock.patch.stopall)
    def test_a_test_that_fails_at_base_is_green_and_red_and_leaves_files_at_base(self):
        r, got = score({"impl.txt": "0\n"}, {"impl.txt": "1\n", "test_impl.py": CASES}, "grep -qx 1 impl.txt")
        self.assertEqual(got, want())
        self.assertEqual((r / "impl.txt").read_text(), "0\n")

    def test_a_test_that_passes_at_base_is_not_red_and_test_lines_sets_the_budget(self):
        _, got = score({"impl.txt": "0\n"}, {"impl.txt": "1\n", "test_impl.py": CASES}, test_lines=2)
        self.assertEqual(got, want(budget=2, over_budget=True, red=False))

    def test_a_test_that_fails_at_head_is_not_green(self):
        _, got = score({"impl.txt": "0\n"}, {"impl.txt": "1\n", "test_impl.py": CASES}, "grep -qx 2 impl.txt")
        self.assertEqual(got, want(green=False))

    def test_a_files_path_absent_at_base_is_deleted_before_the_red_run(self):
        r, got = score({"impl.txt": "0\n"}, {"new.txt": "1\n", "test_impl.py": CASES}, "grep -qx 1 new.txt", files=["new.txt"])
        self.assertEqual(got, want())
        self.assertFalse((r / "new.txt").exists())

    def test_lines_count_added_plus_deleted_including_changed_and_untracked_tests(self):
        _, got = score({"impl.txt": "0\n", "test_impl.py": "a\nb\n"}, {"impl.txt": "1\n"},
                       loose={"test_impl.py": "a\nc\nd\n", "test_new.py": "x\ny\n"}, protected=["test_impl.py", "test_new.py"])
        self.assertEqual(got, want(lines=5, cases=0, holdouts=0, red=False))

    def test_cases_count_each_opener_and_holdouts_are_cases_over_criteria(self):
        text = "async def test_a():\nit('x')\ntest('y')\nfunc TestZ(t)\n# def test_no\nx = 1\n"
        _, got = score({"impl.txt": "0\n"}, {"impl.txt": "1\n"}, loose={"test_impl.py": text})
        self.assertEqual(got, want(lines=6, cases=4, holdouts=2, red=False))
