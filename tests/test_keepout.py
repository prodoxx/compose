import hermetic  # first: no test sees the real home
import os, re, sys, tempfile, unittest
os.environ["HERDR_ENV"] = ""
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import fc_core, fc_keepout  # noqa: E402


class Keep(unittest.TestCase):
    def test_the_tail_is_the_last_lines_and_the_artifacts_of_each_failure_are_kept_apart(self):
        self.assertEqual(fc_keepout.tail("a\n\nb\nc\n", 2), "\n           | b\n           | c")
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            (repo / "e2e/test-results").mkdir(parents=True)
            (repo / "e2e/test-results/trace.zip").write_text("one")
            logs = Path(d) / "results/checks"
            logs.mkdir(parents=True)
            first = fc_keepout.keep_artifacts(repo, ["e2e/test-results", "missing", "../outside"], logs / "r1.log")
            (repo / "e2e/test-results/trace.zip").write_text("two")  # the next round's run rewrites it
            second = fc_keepout.keep_artifacts(repo, ["e2e/test-results"], logs / "r2.log")
            self.assertEqual((Path(first, "test-results/trace.zip").read_text(), Path(second, "test-results/trace.zip").read_text()), ("one", "two"))
            self.assertIsNone(fc_keepout.keep_artifacts(repo, ["missing"], logs / "r3.log"))


class Excerpt(unittest.TestCase):
    WARNING = '(!) Module level directives cause errors when bundled, "use client" was ignored.\n'

    def test_short_output_is_kept_whole(self):
        self.assertEqual(fc_core.excerpt("a\nb\n", 3000), "a\nb\n")
        self.assertEqual(fc_core.excerpt("x" * 3000, 3000), "x" * 3000)

    def test_a_failure_before_a_screenful_of_warnings_is_kept_and_each_cut_says_how_much_it_dropped(self):
        # #452: the last 3000 characters were bundler warnings; the failing line came before them and was lost
        out = "build start\n" + "compiling module\n" * 200 + "Error: Cannot find module '.prisma/client'\n  at load (a.js:1)\n" + self.WARNING * 100
        got = fc_core.excerpt(out, 3000)
        self.assertTrue(got.startswith("build start\n"))
        self.assertIn("Error: Cannot find module '.prisma/client'\n  at load (a.js:1)", got)
        self.assertTrue(got.endswith(self.WARNING))
        marks = re.findall(r"\n\[\.\.\. (\d+) characters cut \.\.\.\]\n", got)
        self.assertEqual(len(marks), 2)
        self.assertEqual(len(re.sub(r"\n\[\.\.\. \d+ characters cut \.\.\.\]\n", "", got)) + sum(map(int, marks)), len(out))  # what is kept and what is said cut make the whole
        self.assertLess(len(got), 3200)

    def test_a_warning_that_says_errors_is_no_failure_line(self):
        out = "x" * 1000 + "\n" + self.WARNING * 60 + "FAIL: test_a (m.C.test_a)\n" + self.WARNING * 100
        self.assertIn("FAIL: test_a (m.C.test_a)", fc_core.excerpt(out, 3000))
        self.assertIsNone(fc_core.FAILURE_RE.search(self.WARNING))

    def test_a_failure_in_the_tail_or_none_at_all_leaves_head_and_tail(self):
        out = "head\n" + "line\n" * 2000 + "Error: last\n"
        got = fc_core.excerpt(out, 3000)
        self.assertEqual((got.startswith("head\n"), got.endswith("Error: last\n"), got.count("characters cut")), (True, True, 1))
        got = fc_core.excerpt("line\n" * 2000, 3000)
        self.assertEqual(got.count("characters cut"), 1)


if __name__ == "__main__":
    unittest.main()
