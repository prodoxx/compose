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

    def run_round(self, ok=True, cfg=None, skip_review=False, events=EVENTS, report=None):
        cfg, rounds = {"review_base": "main", **(cfg or {})}, []
        parent = mock.Mock()
        with mock.patch.object(fc.fc_verify, "run_stages", return_value=(ok, report or {"checks": []}, "" if ok else "finding")), \
                mock.patch.object(fc_ledger, "verify_record", parent.verify_record), \
                mock.patch.object(fc.fc_accept, "jev_sufficiency", parent.jev_sufficiency), \
                mock.patch.object(fc.fc_verify, "verify_failed"), mock.patch.object(fc.fc_linear, "linear_reconcile"), \
                mock.patch.object(fc.fc_core, "say"), mock.patch.object(fc.fc_worktree, "review_base", return_value="main"), \
                mock.patch.object(fc.fc_worktree, "dirty_paths", return_value=set()), \
                mock.patch.object(fc.fc_core, "sh", return_value=subprocess.CompletedProcess([], 0, "", "")), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            parent.jev_sufficiency.return_value = events
            fc.fc_verify.verify_rounds(self.repo, self.proj, cfg, type("A", (), {"fix": 0, "skip_review": skip_review})(), rounds, time.time())
        return cm.exception.code, rounds, json.loads((self.proj / "results" / "verify.json").read_text()), parent

    def test_the_round_keeps_jev_s_events_after_recording_its_own(self):
        _, rounds, _, parent = self.run_round(cfg={"jev_acceptance": True})
        self.assertEqual([c[0] for c in parent.mock_calls], ["verify_record", "jev_sufficiency"])
        self.assertEqual(parent.jev_sufficiency.call_args.args, ({"review_base": "main", "jev_acceptance": True}, self.proj, self.repo, None))
        self.assertEqual(rounds[0]["sufficiency"], EVENTS)

    def test_sufficiency_gets_the_rounds_acceptance_so_a_reused_verdict_is_not_judged_again(self):
        acc = {"criteria": [{"task": "031", "criterion": "a", "choice": "MET", "reused": True}]}
        _, _, _, parent = self.run_round(cfg={"jev_acceptance": True}, report={"checks": [], "acceptance": acc})
        self.assertIs(parent.jev_sufficiency.call_args.args[3], acc)

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


class FixRoundRules(unittest.TestCase):
    """The decisions a verify round makes about the checkout and about the files of its fix, from plain facts."""
    def test_the_checkout_moved_when_the_branch_is_not_the_one_the_stages_started_on(self):
        moved = fc.fc_fixround.checkout_moved
        self.assertFalse(moved("compose/p", "compose/p"))  # a fix commit stays on its branch
        self.assertTrue(moved("compose/p", "main"))
        self.assertTrue(moved("compose/p", ""))  # detached now
        self.assertTrue(moved("", "compose/p"))  # detached when it started
        self.assertFalse(moved("", ""))

    def test_the_void_line_names_both_branches_and_a_detached_head(self):
        line = fc.fc_fixround.void_message(Path("/wt"), "compose/p", "")
        self.assertTrue(line.startswith("verify VOID  nothing verified or recorded: /wt stood on compose/p when"))
        self.assertIn("stands on a detached HEAD now", line)
        self.assertIn("run verify again", line)

    def test_verify_ends_void_with_exit_1_and_records_nothing_when_the_checkout_moved(self):
        proj, said, branches = Path(tempfile.mkdtemp()) / "feat", [], iter(["compose/p", "main", "main"])
        proj.mkdir()
        sh = lambda cmd, cwd, **kw: subprocess.CompletedProcess([], 0, next(branches) + "\n" if cmd[:3] == ["git", "branch", "--show-current"] else "", "")
        with mock.patch.object(fc.fc_verify, "run_stages", return_value=(True, {}, "")), mock.patch.object(fc_ledger, "verify_record") as record, \
                mock.patch.object(fc.fc_core, "say", said.append), mock.patch.object(fc.fc_worktree, "review_base", return_value="main"), \
                mock.patch.object(fc.fc_worktree, "dirty_paths", return_value=set()), mock.patch.object(fc.fc_core, "sh", sh), self.assertRaises(SystemExit) as cm:
            fc.fc_verify.verify_rounds(Path(tempfile.mkdtemp()), proj, {}, type("A", (), {"fix": 1, "skip_review": True})(), [], time.time())
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual([l.split("  ")[0] for l in said], ["verify VOID"])
        record.assert_not_called()
        self.assertFalse((proj / "results" / "verify.json").exists())

    def test_the_agent_wiring_and_blank_entries_are_never_part_of_a_fix(self):
        wiring = fc.fc_worktree.WIRING
        self.assertEqual(fc.fc_fixround.unwired(["b.py", "", "a.py", *wiring, "b.py"]), ["a.py", "b.py"])
        self.assertEqual(fc.fc_fixround.unwired(list(wiring)), [])
        self.assertEqual(fc.fc_fixround.unwired(["sub/.claude/settings.json"]), ["sub/.claude/settings.json"])  # only the repo's own wiring files

    def test_a_rejected_round_restores_what_head_has_and_deletes_what_the_fix_added(self):
        plan = fc.fc_fixround.revert_plan
        self.assertEqual(plan(["b.py", "new.py", "a.py"], {"new.py"}), (["a.py", "b.py"], ["new.py"]))
        self.assertEqual(plan(["a.py"], set()), (["a.py"], []))
        self.assertEqual(plan(["new.py"], {"new.py"}), ([], ["new.py"]))
        self.assertEqual(plan(["a.py"], {"other-untracked.py"}), (["a.py"], []))  # an untracked file the fix did not touch stays
        self.assertEqual(plan([], {"new.py"}), ([], []))


class AdditiveFixVerdict(unittest.TestCase):
    """A fix that only adds code answers a finding when its own tests fail without it and pass with it."""
    state = lambda self, failing=(), unknown=False: {"exit": 1 if failing else 0, "failing": list(failing), "unknown": unknown}

    def test_it_proves_when_a_test_that_failed_without_the_fix_passes_with_it(self):
        proves = fc.fc_gate.fails_without_passes_with
        self.assertTrue(proves({"c": ["t1"]}, {"c": self.state()}, ["c"]))
        self.assertTrue(proves({"c": ["t1", "t2"]}, {"c": self.state(["t2"])}, ["c"]))  # one of them is enough

    def test_it_does_not_prove_when_the_test_still_fails_or_never_failed(self):
        proves = fc.fc_gate.fails_without_passes_with
        self.assertFalse(proves({"c": ["t1"]}, {"c": self.state(["t1"])}, ["c"]))
        self.assertFalse(proves({"c": []}, {"c": self.state()}, ["c"]))  # it passed without the fix too: the test proves nothing

    def test_a_run_with_the_fix_whose_ids_are_unknown_proves_nothing(self):
        self.assertFalse(fc.fc_gate.fails_without_passes_with({"c": ["t1"]}, {"c": self.state(unknown=True)}, ["c"]))

    def test_any_one_check_is_enough(self):
        within = {"a": self.state(["t1"]), "b": self.state()}
        self.assertTrue(fc.fc_gate.fails_without_passes_with({"a": ["t1"], "b": ["t9"]}, within, ["a", "b"]))
        self.assertFalse(fc.fc_gate.fails_without_passes_with({"a": ["t1"], "b": []}, within, ["a", "b"]))

    def test_a_fix_with_no_test_file_or_only_test_files_is_never_proven(self):
        run = mock.Mock()
        for fixed in (["a.py"], ["tests/test_a.py"], []):
            self.assertFalse(fc.fc_gate.new_tests_prove(run, Path("/x"), fixed, ["cmd"]), fixed)
        self.assertFalse(fc.fc_gate.new_tests_prove(run, Path("/x"), ["a.py", "tests/test_a.py"], []))  # no check prints ids
        run.assert_not_called()


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
                mock.patch.object(fc.fc_herdr, "herdr_space"), mock.patch.object(fc.fc_core, "say", say), mock.patch.object(fc.fc_green, "tree_of", return_value=""), \
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
