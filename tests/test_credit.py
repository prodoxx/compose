import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile, threading, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_credit

OR = "pi:openrouter/z-ai/glm-5.3-flash"
CLAUDE = "claude:claude-sonnet-5-5"


class Answer:
    def __init__(self, body): self.body = json.dumps(body).encode()
    def read(self, *a): return self.body
    def __enter__(self): return self
    def __exit__(self, *a): return False


class Credit(unittest.TestCase):
    """A run whose OpenRouter key cannot pay for the tasks that start at once stops before it clones anything."""
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        mock.patch.object(fc.fc_core, "worktree_branches", return_value=[(self.repo, "main")]).start(); self.addCleanup(mock.patch.stopall)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "verify.json").write_text("{}")
        self.requests = []
        for p in (mock.patch.object(fc.fc_config, "OPENROUTER_KEY", "or-secret"), mock.patch.dict(os.environ, {"COMPOSE_NO_PREFLIGHT": ""})):
            p.start(); self.addCleanup(p.stop)

    def finished(self, tid, model, cost, age=0):
        f = self.proj / "results" / f"{tid}.json"
        f.write_text(json.dumps({"id": tid, "model": model, "cost": cost, "status": "passed"}))
        os.utime(f, (time.time() - age,) * 2)

    def left(self, limit_remaining):
        def urlopen(req, timeout=None):
            self.requests.append(req)
            return Answer({"data": {"limit_remaining": limit_remaining}})
        return mock.patch("urllib.request.urlopen", urlopen)

    def tasks(self, *models): return [{"id": f"{i:03}", "model": m} for i, m in enumerate(models, 1)]

    def test_a_task_is_estimated_at_the_mean_of_the_models_latest_finished_tasks_else_a_default(self):
        self.finished("001", OR, 0.10); self.finished("002", OR, 0.30); self.finished("003", "pi:openrouter/other", 5.0)
        self.assertEqual(fc_credit.task_cost(self.repo, OR), (0.20, True))
        self.assertEqual(fc_credit.task_cost(self.repo, "pi:openrouter/unseen"), (fc_credit.DEFAULT_TASK_COST, False))

    def test_only_the_latest_finished_tasks_count(self):
        for i in range(fc_credit.RECENT):
            self.finished(f"{i + 1:03}", OR, 1.0, age=i)
        self.finished("099", OR, 100.0, age=3600)  # older than the window
        self.assertEqual(fc_credit.task_cost(self.repo, OR), (1.0, True))

    def test_too_little_credit_stops_with_the_numbers_the_top_up_link_and_the_way_past_it_but_never_the_key(self):
        self.finished("001", OR, 0.50)
        with self.left(1.20):
            why = fc_credit.check(self.repo, self.tasks(OR, OR, OR), 3)
        self.assertIn("$1.20 of credit left", why)
        self.assertIn("3 task(s) at once need about $1.50", why)
        self.assertIn(fc.fc_models.TOP_UP["openrouter"], why)
        self.assertIn("--force", why)
        self.assertNotIn("or-secret", why)
        self.assertEqual(self.requests[0].get_header("Authorization"), "Bearer or-secret")

    def test_the_need_is_the_dearest_parallel_tasks_not_every_task(self):
        self.finished("001", OR, 0.50)
        with self.left(1.20):
            self.assertIsNone(fc_credit.check(self.repo, self.tasks(*[OR] * 10), 2))
            self.assertIsNotNone(fc_credit.check(self.repo, self.tasks(*[OR] * 10), 3))

    def test_enough_credit_gives_no_verdict_and_an_unseen_model_is_priced_at_the_default(self):
        with self.left(fc_credit.DEFAULT_TASK_COST * 2):
            self.assertIsNone(fc_credit.check(self.repo, self.tasks(OR, OR), 2))
            self.assertIn("3 task(s)", fc_credit.check(self.repo, self.tasks(OR, OR, OR), 3))

    def test_force_a_claude_only_run_no_limit_a_failed_check_and_the_opt_out_make_no_verdict(self):
        tasks = self.tasks(OR, OR, OR)
        with self.left(0):
            self.assertIsNone(fc_credit.check(self.repo, tasks, 3, force=True))
            self.assertIsNone(fc_credit.check(self.repo, self.tasks(CLAUDE), 3))
            with mock.patch.dict(os.environ, {"COMPOSE_NO_PREFLIGHT": "1"}):
                self.assertIsNone(fc_credit.check(self.repo, tasks, 3))
            with mock.patch.object(fc.fc_config, "OPENROUTER_KEY", None):
                self.assertIsNone(fc_credit.check(self.repo, tasks, 3))
        self.assertEqual(self.requests, [])
        with self.left(None):
            self.assertIsNone(fc_credit.check(self.repo, tasks, 3))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(fc_credit.check(self.repo, tasks, 3))

    def run_tasks(self, *tasks, **flags):
        with mock.patch.object(fc.fc_run, "preflight", side_effect=SystemExit("preflight reached")), mock.patch.object(fc.fc_proc, "keep_awake"), \
             mock.patch.object(fc.fc_core, "say"), self.assertRaises(SystemExit) as e:
            fc.fc_run.run_tasks(self.repo, self.proj, list(tasks), SimpleNamespace(trunk=None, no_stack=True, parallel=2, **flags))
        return str(e.exception)

    def test_run_stops_before_preflight_unless_forced(self):
        tasks = [{"id": "001", "model": OR}, {"id": "002", "model": OR}]
        with self.left(0.10):
            stopped = self.run_tasks(*tasks, force=False)
            self.assertIn("nothing was cloned or run", stopped)
            self.assertIn("$0.10 of credit left", stopped)
            self.assertEqual(self.run_tasks(*tasks, force=True), "preflight reached")

class PaymentHalt(unittest.TestCase):
    """A payment halt leaves the top-up link and the exact resume command where the watch can say them."""
    def test_the_saved_halt_names_the_top_up_page_and_the_resume_command_and_the_banner_prints_both(self):
        repo = Path(tempfile.mkdtemp()); proj = repo / ".compose" / "p"; proj.mkdir(parents=True)
        err = fc.fc_models.PaymentRequired("openrouter", "no credit")
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as e:
            fc.fc_models.payment_halt(repo, proj, "run", ["002"], {"parallel": 3}, err)
        self.assertEqual(e.exception.code, 3)
        saved = json.loads((proj / "results" / "resume.json").read_text())
        resume = f"compose resume --repo {repo} --project p"
        self.assertEqual((saved["top_up"], saved["resume"]), (fc.fc_models.TOP_UP["openrouter"], resume))
        self.assertIn(resume, out.getvalue())

    def test_a_payment_error_with_tasks_running_starts_no_new_task_and_lets_the_running_one_finish(self):
        repo = Path(tempfile.mkdtemp()); proj = repo / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        tasks = [{"id": t, "title": t, "after": "", "files": [], "protected": [], "test": "true", "model": "m", "body": ""} for t in ("001", "002", "003")]
        paid, started = threading.Event(), []
        def run_task(r, p, meta, n):
            started.append(meta["id"])
            if meta["id"] == "001":
                status, log = "payment_required", [{"payment_required": {"provider": "openrouter", "message": "402"}}]
                paid.set()
            else:  # 002 is still running when 001 reports the payment error
                paid.wait(5); time.sleep(0.3); status, log = "passed", []
            return {"id": meta["id"], "title": meta["id"], "status": status, "attempts": 1, "seconds": 1.0, "cost": 0.0, "branch": "b/" + meta["id"], "log": log}
        halted = []
        with mock.patch.object(fc.fc_proc, "keep_awake"), mock.patch.object(fc.fc_run, "preflight"), mock.patch.object(fc.fc_herdr, "herdr_space"), mock.patch.object(fc.fc_core, "say"), mock.patch.object(fc.fc_core, "sh", return_value=subprocess.CompletedProcess([], 0, "a" * 40, "")), \
             mock.patch.object(fc.fc_implement, "run_task", run_task), mock.patch.object(fc.fc_models, "payment_halt", lambda *a: halted.append(a[3]) or sys.exit(3)), \
             mock.patch("builtins.print"), self.assertRaises(SystemExit):
            fc.fc_run.run_tasks(repo, proj, tasks, mock.Mock(trunk="main", attempts=1, parallel=2, no_stack=True))
        self.assertEqual(sorted(started), ["001", "002"])  # 003 was never started
        self.assertEqual(halted, [["001", "003"]])  # what resume picks up: the task that hit the 402 and the one that never started; 002 finished

if __name__ == "__main__":
    unittest.main()
