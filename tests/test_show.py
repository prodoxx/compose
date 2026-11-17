import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_show

FIRST_RUN = ("[10:00:00] run start project=recommender tasks=2 parallel=20 attempts=3\n"
             "[10:01:00] task 001 passed\n"
             "[10:05:00] run done   1/2 passed  wall 5m00.0s  sum of task time 6m00.0s\n")
LAST_RUN = ("[11:37:34] run start project=recommender tasks=1 parallel=20 attempts=3\n"
            "[11:37:40] task 001 passed\n"
            "[11:38:02] run done   7/7 passed  wall 4m12.0s  sum of task time 9m30.1s\n")


class Show(unittest.TestCase):
    """`compose show` prints the saved output of the last run or verify, or one line of it."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "recommender"
        self.results = self.proj / "results"
        self.results.mkdir(parents=True)

    def verify(self, **fields):
        (self.results / "verify.json").write_text(json.dumps(fields))

    def show(self, what, line=False):
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), contextlib.redirect_stdout(out):
            fc_show.cmd_show(type("A", (), {"what": what, "line": line}))
        return out.getvalue()

    def test_the_run_line(self):
        (self.results / "run.log").write_text(FIRST_RUN + LAST_RUN)
        (self.results / "run-cost.json").write_text(json.dumps({"total": 0.31, "openrouter": {}}))
        self.assertEqual(fc_show.summary(self.proj, "run"), "run 7/7 passed, 4m12.0s, $0.31")
        (self.results / "run-cost.json").unlink()
        self.assertEqual(fc_show.summary(self.proj, "run"), "run 7/7 passed, 4m12.0s, cost not recorded")
        (self.results / "run-cost.json").write_text("not json")
        self.assertEqual(fc_show.summary(self.proj, "run"), "run 7/7 passed, 4m12.0s, cost not recorded")
        (self.results / "run-cost.json").write_text(json.dumps({"openrouter": {}}))
        self.assertEqual(fc_show.summary(self.proj, "run"), "run 7/7 passed, 4m12.0s, cost not recorded")

    def test_the_verify_line(self):
        self.verify(passed=True, seconds=1600.3, cost={"total": 0.008173})
        self.assertEqual(fc_show.summary(self.proj, "verify"), f"verify passed, {fc.fc_core.fmt(1600.3)}, $0.01")
        self.verify(passed=False, seconds=42.0, cost={"total": 1.234})
        self.assertEqual(fc_show.summary(self.proj, "verify"), "verify failed, 42.0s, $1.23")
        self.verify(passed=False, seconds=42.0)
        self.assertEqual(fc_show.summary(self.proj, "verify"), "verify failed, 42.0s, cost not recorded")

    def test_nothing_on_record(self):
        self.assertEqual(fc_show.summary(self.proj, "run"), "run: none finished on record")
        self.assertEqual(fc_show.summary(self.proj, "verify"), "verify: none on record")
        (self.results / "run.log").write_text("[10:00:00] run start project=recommender tasks=1 parallel=20 attempts=3\n")
        self.assertEqual(fc_show.summary(self.proj, "run"), "run: none finished on record")

    def test_show_run_prints_from_the_last_run_start(self):
        (self.results / "run.log").write_text(FIRST_RUN + LAST_RUN)
        (self.results / "run-cost.json").write_text(json.dumps({"total": 0.31}))
        out = self.show("run")
        self.assertEqual(out, LAST_RUN + "run 7/7 passed, 4m12.0s, $0.31\n")
        self.assertNotIn("1/2 passed", out)
        self.assertEqual(self.show("run", line=True), "run 7/7 passed, 4m12.0s, $0.31\n")
        (self.results / "verify.log").write_text("stage one\nstage two\n")
        self.verify(passed=True, seconds=42.0, cost={"total": 0.5})
        self.assertEqual(self.show("verify"), "stage one\nstage two\nverify passed, 42.0s, $0.50\n")
        self.assertEqual(self.show("verify", line=True), "verify passed, 42.0s, $0.50\n")


if __name__ == "__main__":
    unittest.main()
