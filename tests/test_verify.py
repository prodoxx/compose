import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_ledger  # noqa: E402

EVENTS = [{"event_id": "e1", "result": "contradicted", "cites": [], "needs": ["manual"]}]


class SufficiencyVerify(unittest.TestCase):
    def setUp(self):
        self.repo, self.proj = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "feat"
        self.proj.mkdir()
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main"); (self.repo / "a.py").write_text("1\n"); git("add", "-A"); git("commit", "-q", "-m", "a")

    def run_round(self, ok=True, cfg=None, skip_review=False, events=EVENTS):
        cfg, rounds = {"review_base": "main", **(cfg or {})}, []
        parent = mock.Mock()
        with mock.patch.object(fc.fc_verify, "run_stages", return_value=(ok, {"checks": []}, "" if ok else "finding")), \
                mock.patch.object(fc_ledger, "verify_record", parent.verify_record), \
                mock.patch.object(fc.fc_accept, "jev_sufficiency", parent.jev_sufficiency), \
                mock.patch.object(fc.fc_verify, "verify_failed"), mock.patch.object(fc.fc_linear, "linear_reconcile"), \
                mock.patch.object(fc.fc_core, "say"), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            parent.jev_sufficiency.return_value = events
            fc.fc_verify.verify_rounds(self.repo, self.proj, cfg, type("A", (), {"fix": 0, "skip_review": skip_review})(), rounds, time.time())
        return cm.exception.code, rounds, json.loads((self.proj / "results" / "verify.json").read_text()), parent

    def test_the_round_keeps_jev_s_events_after_recording_its_own(self):
        _, rounds, _, parent = self.run_round(cfg={"jev_acceptance": True})
        self.assertEqual([c[0] for c in parent.mock_calls], ["verify_record", "jev_sufficiency"])
        self.assertEqual(parent.jev_sufficiency.call_args.args, ({"review_base": "main", "jev_acceptance": True}, self.proj, self.repo))
        self.assertEqual(rounds[0]["sufficiency"], EVENTS)

    def test_skip_review_or_jev_acceptance_off_does_not_ask_jev(self):
        for kw in ({"skip_review": True}, {"cfg": {"jev_acceptance": False}}):
            _, rounds, _, parent = self.run_round(**kw)
            parent.jev_sufficiency.assert_not_called()
            self.assertIn("sufficiency", rounds[0])
            self.assertIsNone(rounds[0]["sufficiency"])

    def test_the_events_never_change_the_verdict_or_the_rounds(self):
        for ok in (True, False):
            for gate in (True, False):
                with self.subTest(ok=ok, acceptance_gate=gate):
                    got = [self.run_round(ok, {"acceptance_gate": gate}, events=ev)[:3] for ev in (EVENTS, [])]
                    (code, rounds, final), (code2, rounds2, final2) = got
                    self.assertEqual((rounds[0]["sufficiency"], rounds2[0]["sufficiency"]), (EVENTS, []))
                    self.assertEqual((code, final["passed"], len(rounds)), (0 if ok else 1, ok, 1))
                    self.assertEqual((code2, final2["passed"], len(rounds2)), (code, ok, 1))


import fc_load  # noqa: E402

SUITE, SCOPED, E2E = "python3 -m unittest discover -s tests", "python3 -m unittest tests/test_a.py", [{"name": "x"}]


class VerifyLoadWait(unittest.TestCase):
    def stages(self, cfg, force=None, **flags):
        log, runs, say = [], [], mock.Mock()

        @contextlib.contextmanager
        def slot(say, quick=False):
            log.append(("slot", quick)); yield "7"; log.append(("free",))

        def run(cmd, cwd=None, env=None, timeout=None):
            runs.append(cmd); log.append(("run", cmd[-1], env.get("COMPOSE_TEST_SLOT")))
            bad = cmd[-1] == "make bad" and len(runs) == 1  # the first run fails with a tool error: one retry
            return mock.Mock(returncode=int(bad), stdout="command not found" if bad else "", stderr="")
        a = type("A", (), {"skip_e2e": False, "skip_review": True, **flags, **({} if force is None else {"force": force})})()
        with mock.patch.object(fc_load, "wait", side_effect=lambda say, force=False: log.append(("wait", say, force))), \
                mock.patch.object(fc.fc_proc, "slot", slot), mock.patch.object(fc.fc_proc, "run_limited", run), \
                mock.patch.object(fc.fc_verify, "e2e_stage", side_effect=lambda *x, **k: log.append(("e2e",)) or True), \
                mock.patch.object(fc.fc_herdr, "herdr_verify_panes"), mock.patch.object(fc.fc_herdr, "herdr_stage"), \
                mock.patch.object(fc.fc_herdr, "herdr_space"), mock.patch.object(fc.fc_core, "say", say), \
                contextlib.redirect_stdout(io.StringIO()):
            fc.fc_verify.run_stages(Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()), cfg, a)
        return log, say

    def test_checks_wait_then_each_check_runs_in_its_own_slot_with_it_in_the_env(self):
        log, say = self.stages({"checks": [SUITE, SCOPED]})
        self.assertEqual(log, [("wait", say, False), ("slot", False), ("run", SUITE, "7"), ("free",), ("slot", True), ("run", SCOPED, "7"), ("free",)])

    def test_the_retry_runs_in_the_same_slot_with_it_in_the_env(self):
        log, say = self.stages({"checks": ["make bad"]})
        self.assertEqual(log, [("wait", say, False), ("slot", True), ("run", "make bad", "7"), ("run", "make bad", "7"), ("free",)])

    def test_e2e_waits_before_it_is_submitted_and_after_the_checks_stage(self):
        self.assertEqual([e[0] for e in self.stages({"checks": [SCOPED], "e2e": E2E})[0]], ["wait", "slot", "run", "free", "wait", "e2e"])
        self.assertEqual([e[0] for e in self.stages({"e2e": E2E})[0]], ["wait", "e2e"])
