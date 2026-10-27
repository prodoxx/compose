import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_cost, fc_history, fc_ledger  # noqa: E402

ENV = {"HERDR_ENV": "", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class Repo(unittest.TestCase):
    """A git repository with one project, and a history file of its own."""
    def setUp(self):
        env = mock.patch.dict(os.environ, ENV); env.start(); self.addCleanup(env.stop)
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.repo, True)
        git = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main"); (self.repo / "a.txt").write_text("base\n"); git("add", "-A"); git("commit", "-qm", "init")
        (self.repo / ".git" / "info" / "exclude").write_text("/.compose/\n")
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        self.cfg = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.cfg, True)
        cp = mock.patch.object(fc_history.fc_config, "CONFIG_PATH", self.cfg / "config.json"); cp.start(); self.addCleanup(cp.stop)

    def lines(self):
        f = self.cfg / "history.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


class Record(Repo):
    def test_a_line_beside_the_config_keeps_only_the_known_fields_and_no_secret(self):
        fc_history.record(self.proj, self.repo, "task", task="001", model="m", outcome="passed", cost=0.5, prompt="write the thing", log="x" * 99, note="ignored")
        [e] = self.lines()
        self.assertEqual(sorted(e), ["at", "cost", "kind", "model", "outcome", "project", "repo", "task"])
        self.assertEqual((e["project"], e["repo"], e["kind"], e["task"]), ("p", str(self.repo), "task", "001"))
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-secret-value-123"}):
            fc_history.record(self.proj, self.repo, "task", task="x", model="sk-secret-value-123")
        self.assertNotIn("sk-secret-value-123", (self.cfg / "history.jsonl").read_text())

    def test_it_outlives_the_project_directory_and_a_linked_worktree_reads_the_same_history(self):
        fc_history.record(self.proj, self.repo, "run-start", tasks=2)
        shutil.rmtree(self.repo / ".compose")  # what returning a worktree does to results/
        wt = self.repo.parent / (self.repo.name + "-wt")
        subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", "other"], cwd=self.repo, check=True)
        self.addCleanup(shutil.rmtree, wt, True)
        self.assertEqual([e["kind"] for e in fc_history.read(wt)], ["run-start"])
        self.assertEqual(fc_history.read(Path(tempfile.mkdtemp()).resolve()), [])  # another repository sees none of it

    def test_an_unwritable_history_never_fails_the_run(self):
        (self.cfg / "history.jsonl").mkdir()
        fc_history.record(self.proj, self.repo, "run-start", tasks=1)

    def test_read_filters_by_project_and_keeps_the_newest_in_order(self):
        for i in range(5):
            fc_history.record(self.proj, self.repo, "task", task=f"00{i}")
        fc_history.record(self.repo / ".compose" / "q", self.repo, "task", task="009")
        with open(self.cfg / "history.jsonl", "a") as f: f.write("not json\n")
        self.assertEqual([e["task"] for e in fc_history.read(self.repo, "p", 2)], ["003", "004"])
        self.assertEqual([e["task"] for e in fc_history.read(self.repo, None, 0)], ["000", "001", "002", "003", "004", "009"])

    def test_render_lists_each_line_and_totals_the_cost_of_task_and_verify_end_lines(self):
        fc_history.record(self.proj, self.repo, "task", task="001", model="m", attempts=2, seconds=65, cost=0.25, outcome="passed")
        fc_history.record(self.proj, self.repo, "run-end", cost=0.25, outcome="passed")  # the run's total is not counted twice
        fc_history.record(self.proj, self.repo, "verify-end", rounds=1, cost=0.5, outcome="pass")
        out = fc_history.render(fc_history.read(self.repo)).splitlines()
        self.assertEqual(len(out), 4)
        self.assertTrue("task" in out[0] and "001" in out[0] and "x2" in out[0] and "$0.2500" in out[0] and "passed" in out[0], out[0])
        self.assertEqual(out[-1], "total cost $0.7500 (task and verify-end lines)")
        self.assertEqual(fc_history.render([]), "history  none")


class RunWrites(Repo):
    """run_tasks appends a start, one line per task result and an end that carries the providers' calls and tokens."""
    def run_tasks(self, ids, only=None):
        tasks = [{"id": t, "title": t, "files": [], "protected": [], "test": "true", "body": "", "model": "m"} for t in ids]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        def run_task(repo, proj, meta, attempts):
            fc.fc_core.LEDGER.add("prov", 1000, 200, cost=0.1)
            return {"id": meta["id"], "status": meta["status"] if "status" in meta else "passed", "attempts": 2, "seconds": 3.0, "cost": 0.1, "branch": "-", "log": []}
        real = fc.fc_run
        fc.fc_core.LEDGER.rows.clear()
        with mock.patch.object(real, "preflight"), mock.patch.object(real.fc_proc, "keep_awake"), mock.patch.object(real.fc_herdr, "herdr_space"), \
                mock.patch.object(real.fc_blockers, "released", return_value=({}, {})), mock.patch.object(real.fc_implement, "run_task", run_task), \
                mock.patch.object(real, "triage"), mock.patch.object(real.fc_stack, "register_stack"), mock.patch.object(real.fc_core, "say"):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                real.run_tasks(self.repo, self.proj, [m for m in fc.fc_tasks.load_tasks(self.proj, required=True) if only in (None, m["id"])],
                               SimpleNamespace(attempts=3, parallel=1, trunk="main", no_stack=True))
        self.assertEqual(e.exception.code, 0)

    def test_a_run_writes_start_task_and_end_lines_that_survive_the_next_run(self):
        self.run_tasks(["001", "002"])
        first = self.lines()
        self.assertEqual([e["kind"] for e in first], ["run-start", "task", "task", "run-end"])
        self.assertEqual((first[1]["task"], first[1]["model"], first[1]["attempts"], first[1]["outcome"]), ("001", "m", 2, "passed"))
        self.assertEqual((first[-1]["tasks"], first[-1]["passed"], first[-1]["outcome"], first[-1]["providers"]["prov"]), (2, 2, "passed", {"calls": 2, "tokens_in": 2000, "tokens_out": 400}))
        shutil.rmtree(self.proj / "results" / "crew", ignore_errors=True)
        (self.proj / "results" / "run.log").unlink(missing_ok=True)
        self.run_tasks(["001", "002"], only="002")  # the rerun overwrites run-cost.json and the log, not the history
        self.assertEqual(self.lines()[:len(first)], first)
        self.assertEqual([e["kind"] for e in self.lines()[len(first):]], ["run-start", "task", "run-end"])

    def test_cost_totals_every_run_of_the_project(self):
        self.run_tasks(["001", "002"])
        self.run_tasks(["001", "002"], only="002")
        self.assertEqual(json.loads((self.proj / "results" / "run-cost.json").read_text())["prov"]["calls"], 1)  # the file holds only the latest run
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), contextlib.redirect_stdout(io.StringIO()) as out:
            fc.fc_status.cmd_cost(SimpleNamespace(repo=str(self.repo), project="p", json=True))
        [row] = json.loads(out.getvalue())["rows"]
        self.assertEqual((row["name"], row["turns"], row["input"], row["output"], row["note"]), ("prov", 3, 3000, 600, "2 runs"))

    def test_cost_without_a_history_reads_the_latest_run_cost_file(self):
        (self.proj / "results" / "run-cost.json").write_text(json.dumps({"prov": {"calls": 4, "tokens_in": 40, "tokens_out": 8}, "total": 1}))
        [row] = fc_cost.rows(self.proj)
        self.assertEqual((row["turns"], row["input"], row["note"]), (4, 40, ""))
        [row] = fc_cost.rows(self.proj, [{"providers": {"prov": {"calls": 1, "tokens_in": 10, "tokens_out": 2}}}, {"providers": {"prov": {"calls": 2, "tokens_in": 20, "tokens_out": 4}}}])
        self.assertEqual((row["turns"], row["input"], row["output"], row["note"]), (3, 30, 6, "2 runs"))


class StatusReads(Repo):
    def test_status_history_prints_the_lines_of_this_repository_and_project(self):
        fc_history.record(self.proj, self.repo, "task", task="001", outcome="passed")
        fc_history.record(self.repo / ".compose" / "q", self.repo, "task", task="002", outcome="failed")
        for project, want in (("p", ["001"]), (None, ["001", "002"])):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                fc.fc_status.cmd_status(SimpleNamespace(repo=str(self.repo), project=project, history=40))
            self.assertEqual([t for t in ("001", "002") if t in out.getvalue()], want, project)

class VerifyWrites(Repo):
    def verify(self, passed):
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        (self.proj / "results" / "verify.json").write_text('{"rounds": []}')
        with mock.patch.object(fc.fc_verify, "run_stages", return_value=(passed, {"checks": []}, "" if passed else "finding")), mock.patch.object(fc_ledger, "verify_record"), \
                mock.patch.object(fc.fc_verify.fc_costevent, "verify_events", return_value=[]), mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[{"id": "001"}]), \
                mock.patch.object(fc.fc_core.LEDGER, "snapshot", return_value={"total": 0.37}), mock.patch.object(fc.fc_verify, "verify_failed"), \
                mock.patch.object(fc.fc_linear, "linear_reconcile"), mock.patch.object(fc.fc_core, "say"), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            fc.fc_verify.verify_rounds(self.repo, self.proj, {"review_base": "main"}, SimpleNamespace(fix=0, skip_review=True, review_only=False), [], 0)

    def test_each_round_and_the_end_are_one_line(self):
        for passed in (True, False):
            self.verify(passed)
        got = [(e["kind"], e.get("round"), e.get("rounds"), e["outcome"]) for e in self.lines()]
        self.assertEqual(got, [("verify-round", 1, None, "pass"), ("verify-end", None, 1, "pass"), ("verify-round", 1, None, "fail"), ("verify-end", None, 1, "fail")])
        self.assertEqual(self.lines()[1]["cost"], 0.37)

    def test_a_passing_verify_closes_its_tab_and_a_failing_one_keeps_it(self):
        for passed, closed in ((True, [("w9", "verify")]), (False, [])):
            with mock.patch.object(fc.fc_herdr, "herdr_project_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_close_tabs") as close:
                self.verify(passed)
            self.assertEqual([c.args for c in close.call_args_list], closed, passed)


if __name__ == "__main__":
    unittest.main()
