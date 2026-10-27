import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, re, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

ENV = {"HERDR_ENV": "", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
CASES = "def test_a\ndef test_b\ndef test_c\n"


def sh_fake(diff="", numstat="", at_base=("impl.txt",)):
    """The few git answers score_test asks for: which files exist at base, and the protected files' diff."""
    def sh(argv, cwd, check=True):
        out = (numstat if argv[2] == "--numstat" else diff) if argv[1] == "diff" else ""
        return subprocess.CompletedProcess(argv, 0 if argv[1] != "cat-file" or argv[3].split(":")[1] in at_base else 1, out, "")
    return sh


def score(test_rc, diff="", numstat="", **kv):
    meta = {"id": "001", "files": ["impl.txt"], "protected": ["test_impl.py"], "test": "t", "body": "## Acceptance criteria\n\n* a\n* b\n", **kv}
    rcs = iter(test_rc)
    with mock.patch.object(fc.fc_core, "sh", sh_fake(diff, numstat)), mock.patch.object(fc.fc_implement, "run_test", lambda *a: SimpleNamespace(returncode=next(rcs))):
        return fc.fc_bench.score_test(Path(tempfile.mkdtemp()), meta, "base")


def want(**kv):
    return {"lines": 3, "budget": 60, "over_budget": False, "cases": 3, "holdouts": 1, "green": True, "red": True, **kv}


NUMSTAT, DIFF = "3\t0\ttest_impl.py\n", "+def test_a\n+def test_b\n+def test_c\n"


class Score(unittest.TestCase):
    def test_a_test_that_passes_at_head_and_fails_at_base_is_green_and_red(self):
        self.assertEqual(score([0, 1], DIFF, NUMSTAT), want())

    def test_a_test_that_passes_at_base_is_not_red_and_test_lines_sets_the_budget(self):
        self.assertEqual(score([0, 0], DIFF, NUMSTAT, test_lines=2), want(budget=2, over_budget=True, red=False))

    def test_a_test_that_fails_at_head_is_not_green(self):
        self.assertEqual(score([1, 1], DIFF, NUMSTAT), want(green=False, red=True))

    def test_a_files_path_absent_at_base_is_deleted_before_the_red_run(self):
        wt = Path(tempfile.mkdtemp()); (wt / "new.txt").write_text("1\n")
        meta = {"id": "001", "files": ["new.txt"], "protected": ["test_impl.py"], "test": "t", "body": "## Acceptance criteria\n\n* a\n* b\n"}
        seen = []
        with mock.patch.object(fc.fc_core, "sh", sh_fake()), mock.patch.object(fc.fc_implement, "run_test", lambda *a: seen.append((wt / "new.txt").exists()) or SimpleNamespace(returncode=0)):
            fc.fc_bench.score_test(wt, meta, "base")
        self.assertEqual(seen, [True, False])

    def test_lines_sum_added_and_deleted_and_skip_binary_files(self):
        self.assertEqual(score([0, 0], "", "2\t3\ttest_impl.py\n-\t-\timg.png\n")["lines"], 5)

    def test_cases_count_each_opener_and_holdouts_are_cases_over_criteria(self):
        diff = "+async def test_a():\n+it('x')\n+test('y')\n+func TestZ(t)\n+# def test_no\n+x = 1\n def test_ctx\n"
        got = score([0, 0], diff, "6\t0\ttest_impl.py\n")
        self.assertEqual((got["cases"], got["holdouts"]), (4, 2))


SCORES = {"001": dict(lines=10, budget=60, over_budget=True, cases=4, holdouts=2, green=True, red=True),
          "002": dict(lines=20, budget=60, over_budget=False, cases=2, holdouts=0, green=True, red=False)}


class BenchTests(unittest.TestCase):
    def setUp(self):
        mock.patch.dict(os.environ, ENV).start(); self.addCleanup(mock.patch.stopall); mock.patch("sys.stdout", io.StringIO()).start()
        self.repo = Path(tempfile.mkdtemp()); self.proj = self.repo / ".compose" / "p"; (self.proj / "results").mkdir(parents=True)
        self.base = "b" * 40
        def sh(argv, cwd, check=True):
            """The clone is an empty folder; test_new.py is absent at base, every other protected file is "base\\n" there."""
            if argv[1] == "clone": Path(argv[-1]).mkdir(parents=True)
            if argv[1] == "checkout": (Path(cwd) / argv[-1]).write_text("base\n")
            return subprocess.CompletedProcess(argv, int(argv[1] == "cat-file" and argv[3].endswith("test_new.py")), "", "")
        mock.patch.object(fc.fc_core, "sh", sh).start(); mock.patch.object(fc.fc_worktree, "ready_checkout").start()
        self.tasks = [{"id": i, "title": "t" + i, "protected": p, "files": ["impl.py"], "test": "true", "body": "## Acceptance criteria\n\n* a\n* b\n"}
                      for i, p in (("001", ["test_impl.py"]), ("002", ["test_new.py"]), ("003", []), ("004", ["test_impl.py"]))]
        self.calls, self.usage = [], {"cost": 0.5, "reference_cost": 0.25}
        def implementer(wt, model, prompt, *rest, **kw):
            meta = next(t for t in self.tasks if t["id"] == re.search(r"for task (\d+)", prompt).group(1)); wt = Path(wt)
            self.calls.append((wt, prompt, meta, [(wt / f).read_text() if (wt / f).exists() else None for f in meta["protected"]]))
            if model == "m2" and meta["id"] == "002": raise RuntimeError("boom")
            return None, "", [], self.usage
        for mod, name, new in ((fc.fc_core, "project_dir", lambda *a: self.proj), (fc.fc_tasks, "load_tasks", lambda *a, **k: self.tasks),
                               (fc.fc_implement, "implementer", implementer), (fc.fc_bench, "score_test", lambda wt, meta, base: dict(SCORES[meta["id"]]))):
            mock.patch.object(mod, name, new).start()

    def go(self, **kv):
        a = dict(repo=str(self.repo), project="p", tests=True, base=self.base, only="001,002,003", models="m1,m2", runs=2, parallel=1, attempts=1)
        fc.fc_bench.bench_tests(SimpleNamespace(**{**a, **kv}))
        return json.loads(sorted((self.proj / "results").glob("bench-tests-*.json"))[-1].read_text())

    def test_without_base_it_exits_naming_the_flag_and_with_tests_cmd_bench_hands_over(self):
        self.assertRaisesRegex(SystemExit, "--base", self.go, base=None)
        with mock.patch.object(fc.fc_bench, "bench_tests", return_value="r"):
            self.assertEqual(fc.fc_bench.cmd_bench(SimpleNamespace(tests=True, repo=str(self.repo), project="p", only=None)), "r")

    def test_each_model_writes_each_protected_task_from_the_brief_with_the_test_reset_to_base_then_clones_go(self):
        self.go()
        self.assertEqual(len(self.calls), 8)  # 2 models x (001, 002) x 2 runs; 003 has no protected, 004 is not in --only
        for wt, prompt, meta, seen in self.calls:
            self.assertEqual(prompt, fc.fc_briefs.test_brief(meta, "(none)")
                             + "\nThis is a bench run: there is no contract and no team lead; write and commit the test, report nothing, then stop.")
            self.assertEqual((wt.parent, seen), (self.proj / "worktrees", ["base\n"] if meta["id"] == "001" else [None]))
        self.assertEqual(list((self.proj / "worktrees").glob("*")), [])

    def test_the_summary_has_a_row_per_model_with_rates_means_and_counts_over_scored_runs_and_every_run_is_saved(self):
        out = self.go(); m1, m2 = out["summary"]
        self.assertEqual(set(m1), set(m2)); self.assertEqual(set(m1), set("model runs errors red_rate green_rate both_rate lines_mean over_budget holdouts_mean cost_per_test".split()))
        self.assertEqual((m1["model"], m1["runs"], m1["errors"], m1["over_budget"], m2["model"], m2["errors"], m2["over_budget"]), ("m1", 4, 0, 2, "m2", 2, 2))
        for row, want in ((m1, dict(red_rate=.5, green_rate=1, both_rate=.5, lines_mean=15, holdouts_mean=1, cost_per_test=.75)),
                          (m2, dict(red_rate=1, green_rate=1, both_rate=1, lines_mean=10, holdouts_mean=2))):
            [self.assertAlmostEqual(row[k], v) for k, v in want.items()]
        errs = [r for r in out["runs"] if "error" in r]
        self.assertEqual((len(out["runs"]), len(errs), set(errs[0]), errs[0]["model"], "boom" in errs[0]["error"]), (8, 2, {"task", "run", "model", "error"}, "m2", True))
