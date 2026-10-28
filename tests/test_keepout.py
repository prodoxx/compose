import hermetic  # first: no test sees the real home
import os, sys, tempfile, unittest
os.environ["HERDR_ENV"] = ""
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import fc_keepout  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
