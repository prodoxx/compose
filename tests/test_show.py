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
            fc_show.cmd_show(type("A", (), {"what": what, "line": line, "repo": str(self.proj.parent)}))
        return out.getvalue()

    def run_end(self, project="recommender", **fields):
        """A run-end line in the run history, as a run (or a resume) writes it when it ends."""
        fc.fc_history.record(self.proj.parent / project, self.proj.parent, "run-end", **fields)

    def test_the_run_line_reads_the_newest_run_end_of_the_history(self):
        (self.results / "run.log").write_text(FIRST_RUN + LAST_RUN)  # the halted run's log, and a resume's cost beside it
        (self.results / "run-cost.json").write_text(json.dumps({"total": 0.05}))
        self.run_end(tasks=7, passed=3, seconds=300.0, cost=0.31, outcome="halted")
        self.run_end(tasks=4, passed=4, seconds=600.0, cost=0.05, outcome="passed")
        self.run_end(project="other", tasks=9, passed=1, seconds=1.0, cost=9.0, outcome="failed")
        self.assertEqual(fc_show.summary(self.proj, "run", self.proj.parent), f"run 4/4 passed, {fc.fc_core.fmt(600.0)}, $0.05")
        self.assertEqual(self.show("run", line=True), f"run 4/4 passed, {fc.fc_core.fmt(600.0)}, $0.05\n")

    def run_start(self, **fields):
        fc.fc_history.record(self.proj, self.proj.parent, "run-start", **fields)

    def test_a_run_that_started_after_the_last_run_end_is_running_or_did_not_finish(self):
        self.run_end(tasks=7, passed=7, seconds=252.0, cost=0.31, outcome="passed")
        self.run_start(tasks=2)
        (self.results / "run.json").write_text(json.dumps({"pid": os.getpid(), "started_at": "x"}))
        self.assertEqual(fc_show.summary(self.proj, "run", self.proj.parent), "run: still running")
        (self.results / "run.json").write_text(json.dumps({"pid": 999999, "started_at": "x"}))
        with mock.patch.object(fc.fc_tasks, "pid_alive", return_value=False):
            self.assertEqual(fc_show.summary(self.proj, "run", self.proj.parent), "run: did not finish")
        (self.results / "run.json").unlink()
        self.assertEqual(fc_show.summary(self.proj, "run", self.proj.parent), "run: did not finish")
        self.run_end(tasks=2, passed=1, seconds=60.0, cost=0.02, outcome="failed")
        self.assertEqual(fc_show.summary(self.proj, "run", self.proj.parent), f"run 1/2 passed, {fc.fc_core.fmt(60.0)}, $0.02")

    def test_with_no_history_a_log_whose_last_run_has_no_end_did_not_finish(self):
        (self.results / "run.log").write_text(LAST_RUN + FIRST_RUN.replace("[10:05:00] run done   1/2 passed  wall 5m00.0s  sum of task time 6m00.0s\n", ""))
        self.assertEqual(fc_show.summary(self.proj, "run"), "run: did not finish")
        out = self.show("run")
        self.assertTrue(out.startswith("[10:00:00] run start project=recommender") and out.endswith("run: did not finish\n"), out)

    def test_a_run_end_that_misses_a_number_says_it_is_not_recorded(self):
        self.run_end(tasks=7, seconds=12.0, outcome="interrupted")
        self.assertEqual(fc_show.summary(self.proj, "run", self.proj.parent), f"run count not recorded, {fc.fc_core.fmt(12.0)}, cost not recorded")
        self.run_end(tasks=7, passed=7, cost=0.5, outcome="passed")
        self.assertEqual(fc_show.summary(self.proj, "run", self.proj.parent), "run 7/7 passed, time not recorded, $0.50")

    def test_show_run_prints_the_newer_of_the_run_and_the_resume_log(self):
        (self.results / "run.log").write_text(FIRST_RUN)
        resume = "[12:00:00] resume run  halted x at openrouter\n" + LAST_RUN
        (self.results / "resume.log").write_text(resume)
        os.utime(self.results / "run.log", (1700000000, 1700000000))
        out = self.show("run")
        self.assertTrue(out.startswith(LAST_RUN), out)
        self.assertNotIn("1/2 passed", out)

    def test_a_run_starts_at_composes_own_line_and_a_log_with_bad_bytes_still_reads(self):
        teammate = "[11:37:50] task 001 note: the run start project=x line is ours\n"
        (self.results / "run.log").write_bytes((FIRST_RUN + LAST_RUN + teammate).encode() + b"\xff\xfe broken\n")
        out = self.show("run")
        self.assertTrue(out.startswith(LAST_RUN), out)
        self.assertIn("broken", out)
        self.assertTrue(out.endswith("run 7/7 passed, 4m12.0s, cost not recorded\n"), out)

    def test_a_verify_without_an_outcome_says_so(self):
        self.verify(seconds=42.0, cost={"total": 0.5})
        self.assertEqual(fc_show.summary(self.proj, "verify"), "verify: outcome not recorded, 42.0s, $0.50")
        self.verify(passed="yes", seconds=42.0)
        self.assertEqual(fc_show.summary(self.proj, "verify"), "verify: outcome not recorded, 42.0s, cost not recorded")

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
        (self.results / "run.log").write_text("[10:00:00] task 001 passed\n")
        self.assertEqual(fc_show.summary(self.proj, "run"), "run: none finished on record")
        (self.results / "run.log").write_text("[10:00:00] run start project=recommender tasks=1 parallel=20 attempts=3\n")
        self.assertEqual(fc_show.summary(self.proj, "run"), "run: did not finish")  # a run started and wrote no end

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
