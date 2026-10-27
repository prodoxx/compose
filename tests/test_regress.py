import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, unittest
from pathlib import Path
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_fast, fc_ledger, fc_regproof, fc_regress, fc_regwriter, fc_unproven  # noqa: E402
import json, tempfile
from unittest import mock

ENV = {"HERDR_ENV": "", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
FIX = "fix-then-regression-test"
PROOF = {"commit": "abc1234", "command": "true", "paths": ["tests/test_x.py"], "proven": True}


BLOCK = "id: 003\nfiles: bin/a.py\nprotected: tests/test_a.py\ntest: python3 -m unittest tests.test_a\n"
DESC = "## Goal\nFix it.\n\nprotected: stays\n\n```compose\n" + BLOCK + "```\n\n## Notes\ntest: stays\n"
B = ("tests/test_b.py", "python3 -m unittest tests.test_b")
PINNED = DESC.replace(BLOCK, "id: 003\nfiles: bin/a.py\nprotected: tests/test_a.py, tests/test_b.py\ntest: python3 -m unittest tests.test_a && python3 -m unittest tests.test_b\n")


class Pin(unittest.TestCase):
    """A proven regression test is pinned into the task's protected files and test command."""
    def test_pinned_edits_only_the_compose_block_and_lists_each_path_once(self):
        self.assertEqual(fc_regress.pinned(DESC, [B[0]], B[1]), PINNED)
        self.assertEqual(fc_regress.pinned(DESC, ["tests/test_a.py", B[0]], B[1]), PINNED)
        bare = DESC.replace(BLOCK, "id: 003\nfiles: bin/a.py\ntest: true\n")
        self.assertEqual(fc_regress.pinned(bare, [B[0], "tests/test_c.py"], B[1]),
                         bare.replace("test: true\n", "protected: tests/test_b.py, tests/test_c.py\ntest: true && " + B[1] + "\n"))

    def pin(self, fail=None):
        env = mock.patch.dict(os.environ, ENV); env.start(); self.addCleanup(env.stop)
        proj = Path(tempfile.mkdtemp()) / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        meta = {"id": "003", "title": "t", "files": ["bin/a.py"], "protected": ["tests/test_a.py"], "test": "python3 -m unittest tests.test_a", "linear": "ENG-3"}
        other = dict(meta, id="004", linear="ENG-4")
        (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [other, meta], "issues": {"ENG-3": {"description": DESC}, "ENG-4": {"description": DESC}}}))
        calls = []
        def gql(query, variables):
            calls.append((query.split("(")[0], variables))
            if fail and query.startswith(fail): raise RuntimeError("boom")
            return {"issue": {"id": "ENG-3", "description": DESC}}
        with mock.patch.object(fc.fc_linear, "linear_gql", gql), mock.patch.object(fc.fc_core, "say") as say:
            ok = fc_regress.pin_test(proj, meta, [B[0]], B[1])
        return ok, calls, say, json.loads((proj / "results" / "tasks.json").read_text())

    def check_cache(self, cache):
        got = {t["id"]: t for t in cache["tasks"]}
        self.assertEqual((got["003"]["protected"], got["003"]["test"]), (["tests/test_a.py", B[0]], "python3 -m unittest tests.test_a && " + B[1]))
        self.assertEqual((got["004"]["protected"], cache["issues"]["ENG-3"]["description"], cache["issues"]["ENG-4"]["description"]), (["tests/test_a.py"], PINNED, DESC))

    def test_pin_test_writes_the_pinned_description_once_and_the_cache(self):
        ok, calls, say, cache = self.pin()
        self.assertTrue(ok); say.assert_not_called(); self.check_cache(cache)
        self.assertEqual([c[1] for c in calls if c[0].startswith("mutation")], [{"id": "ENG-3", "in": {"description": PINNED}}])

    def test_a_failed_linear_write_still_pins_the_cache_says_so_and_returns_false(self):
        for fail in ("query", "mutation"):
            with self.subTest(fail):
                ok, calls, say, cache = self.pin(fail)
                self.assertFalse(ok); say.assert_called_once_with("003 pin     Linear update failed: boom"); self.check_cache(cache)


class Regress(unittest.TestCase):
    """A FAST fix passes only after its regression test is proven and saved."""
    def setUp(self):
        env = mock.patch.dict(os.environ, ENV); env.start(); self.addCleanup(env.stop)
        self.repo = Path(tempfile.mkdtemp()).resolve(); self.proj = self.repo / ".compose" / "p"; (self.proj / "results").mkdir(parents=True)
        self.meta = {"id": "001", "title": "t", "after": "", "files": [], "protected": [], "test": "true", "model": "m"}
        self.res = {"id": "001", "title": "t", "status": "unproven", "attempts": 1, "seconds": 0.0, "cost": 0.0, "branch": "fast/p/001", "log": []}
        self.flow = mock.Mock(return_value=FIX)
        for p in (mock.patch.object(fc_fast, "workflow", self.flow), mock.patch.object(fc.fc_tasks, "WORKFLOWS", ("test-first", "implement-then-test", FIX), create=True)):
            p.start(); self.addCleanup(p.stop)

    def test_fast_fix_is_an_unproven_fix_then_regression_test_result_without_a_proof(self):
        self.assertTrue(fc_regress.fast_fix(self.proj, self.meta, self.res))
        self.assertFalse(any(fc_regress.fast_fix(self.proj, self.meta, dict(self.res, **kw)) for kw in ({"proof": None}, {"status": "passed"})))

    def round(self, ready=("abc1234", "true"), proof=PROOF, saved=True, start=None):
        with mock.patch.object(fc_regwriter, "start_writer", side_effect=start, return_value=self.repo), mock.patch.object(fc_regwriter, "wait_ready", return_value=ready), \
             mock.patch.object(fc_regproof, "prove", return_value=proof), mock.patch.object(fc_regproof, "record_proof", return_value=saved), \
             mock.patch.object(fc_regress, "pin_test") as pin, mock.patch.object(fc_regress, "end_writer") as end, mock.patch.object(fc.fc_linear, "linear_event") as ev, \
             mock.patch.object(fc_unproven, "reason", return_value="why"):
            out = fc_regress.regress(self.repo, self.proj, self.meta, self.res)
        self.assertEqual(json.loads((self.proj / "results" / "001.json").read_text())["status"], out["status"])
        ev.assert_called_once_with(self.proj, self.meta, out["status"], out)
        return out, pin, end

    def test_a_proven_saved_proof_passes_the_task_pins_the_test_and_ends_the_writer(self):
        out, pin, end = self.round()
        self.assertEqual((out["status"], out["proof"]), ("passed", PROOF)); pin.assert_called_once(); end.assert_called_once_with(self.repo, self.proj, "001")

    def test_anything_short_of_a_saved_proof_keeps_the_task_unproven_and_unpinned(self):
        bad = dict(PROOF, proven=False)
        for why, kw, proof in (("unsaved", {"saved": False}, PROOF), ("unproven", {"proof": bad}, bad), ("no ready line", {"ready": None}, None),
                               ("start failed", {"start": RuntimeError("x")}, None), ("start exited", {"start": SystemExit("x")}, None)):
            with self.subTest(why):
                out, pin, end = self.round(**kw)
                self.assertEqual((out["status"], out["proof"]), ("unproven", proof)); pin.assert_not_called(); end.assert_not_called()

    def drive(self, flow, status):
        ran, a, self.flow.return_value = [], mock.Mock(trunk="trunk", attempts=3, parallel=1, no_stack=True, redo=False), flow
        def run_task(repo, proj, m, attempts):
            ran.append(m["id"]); return dict(self.res, id=m["id"], status="unproven" if m["id"] == "001" else "passed")
        with mock.patch.object(fc.fc_core, "say"), mock.patch.object(fc.fc_run, "preflight", lambda m: None), mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None), \
             mock.patch.object(fc.fc_run, "triage", lambda *x: None), mock.patch.object(fc_fast, "run_events", return_value=[]), mock.patch.object(fc_ledger, "record_task"), mock.patch.object(fc.fc_implement, "run_task", run_task), mock.patch("builtins.print") as out, self.assertRaises(SystemExit), \
             mock.patch.object(fc_regress, "regress", side_effect=lambda *x: {**x[3], "status": status, "proof": None}) as rg:
            fc.fc_run.run_tasks(self.repo, self.proj, [dict(self.meta), dict(self.meta, id="002", after="001")], a)
        return ran, rg, [str(c.args[0]) for c in out.call_args_list if c.args and str(c.args[0]).startswith("001")]

    def test_run_tasks_proves_an_unproven_fix_once_and_its_passed_row_releases_the_dependent(self):
        ran, rg, rows = self.drive(FIX, "passed")
        self.assertEqual((ran, len(rows)), (["001", "002"], 1)); self.assertIn("passed", rows[0])
        self.assertEqual([(c.args[0], c.args[1], c.args[2]["id"], c.args[3]["status"], c.args[4]) for c in rg.call_args_list], [(self.repo, self.proj, "001", "unproven", 3)])

    def test_run_tasks_keeps_an_unproven_fix_and_blocks_its_dependent_and_skips_other_workflows(self):
        ran, rg, rows = self.drive(FIX, "unproven")
        self.assertEqual((ran, len(rows)), (["001"], 1)); self.assertIn("unproven", rows[0])
        ran, rg, rows = self.drive("test-first", "passed")
        rg.assert_not_called(); self.assertEqual(ran, ["001"]); self.assertIn("unproven", rows[0])


class Reprove(unittest.TestCase):
    """A regression test that is not proof re-prompts its writer up to the attempts, then leaves the task unproven."""
    def setUp(self):
        env = mock.patch.dict(os.environ, ENV); env.start(); self.addCleanup(env.stop)
        self.repo = Path(tempfile.mkdtemp()).resolve(); self.proj = self.repo / ".compose" / "p"; (self.proj / "results").mkdir(parents=True)
        self.meta = {"id": "001", "title": "t", "after": "", "files": [], "protected": [], "test": "true", "model": "m"}

    def go(self, proofs, attempts=3, saved=True, ready=True, restart=None, live=True):
        """Run regress with `proofs` as prove's answers; ready lines end after them unless `ready`."""
        res = {"id": "001", "title": "t", "status": "unproven", "attempts": 1, "branch": "fast/p/001", "log": []}
        if restart: res["proof"] = restart
        lines = [("c%d" % i, "cmd") for i in range(len(proofs))] + ([] if ready else [None])
        P = lambda **kw: mock.patch.object(**kw)
        with P(target=fc_regwriter, attribute="start_writer", return_value=self.repo) as start, P(target=fc_regwriter, attribute="wait_ready", side_effect=lines) as wait, \
             P(target=fc_regproof, attribute="prove", side_effect=proofs) as prove, P(target=fc_regproof, attribute="record_proof", return_value=saved) as rec, \
             P(target=fc_regress, attribute="pin_test"), P(target=fc_regress, attribute="end_writer"), P(target=fc.fc_crew, attribute="crew_reports", return_value=[0] * 3), \
             P(target=fc_unproven, attribute="prompt_text", side_effect=lambda p: "again " + p["commit"]), P(target=fc_unproven, attribute="writer_live", return_value=live), \
             P(target=fc_unproven, attribute="reason", side_effect=lambda p, saved=True: "why %s %s" % (p["commit"], saved)), P(target=fc_unproven, attribute="reprompt") as rep, \
             P(target=fc.fc_linear, attribute="linear_event") as ev:
            out = fc_regress.regress(self.repo, self.proj, self.meta, res, attempts)
        self.assertEqual(json.loads((self.proj / "results" / "001.json").read_text())["status"], out["status"])
        return out, start, wait, prove, rep, rec, ev

    def test_a_base_pass_a_base_error_and_a_tip_failure_reprompt_and_the_next_ready_commit_passes(self):
        bad = [dict(PROOF, commit=c, base=b, tip_exit=t, proven=False) for c, b, t in (("c0", ("pass", False), 0), ("c1", ("error", None), 0), ("c2", ("fail_expected", True), 1))]
        out, start, wait, prove, rep, rec, ev = self.go(bad + [dict(PROOF, commit="c3")], attempts=4)
        self.assertEqual((out["status"], out["proof"]["commit"], rec.call_count), ("passed", "c3", 4)); start.assert_called_once()
        self.assertEqual([c.args for c in rep.call_args_list], [(self.proj, "001", "again c%d" % i) for i in range(3)])
        self.assertEqual([c.args[:3] for c in wait.call_args_list], [(self.proj, "001", 3)] * 4)
        self.assertEqual([c.args[4:] for c in prove.call_args_list], [("c%d" % i, "cmd") for i in range(4)])

    def test_out_of_attempts_leaves_the_task_unproven_with_the_last_proof_and_its_reason_logged(self):
        out, start, wait, prove, rep, rec, ev = self.go([dict(PROOF, commit="c0", proven=False), dict(PROOF, commit="c1", proven=False)], attempts=2)
        self.assertEqual((out["status"], out["proof"]["commit"], out["log"][-1], prove.call_count, rep.call_count), ("unproven", "c1", {"error": "why c1 True"}, 2, 1))
        ev.assert_called_once_with(self.proj, self.meta, "unproven", out); self.assertEqual(ev.call_args.args[3]["log"][-1], {"error": "why c1 True"})

    def test_a_proof_that_was_not_saved_ends_unproven_at_once_without_a_reprompt(self):
        for proven in (True, False):
            with self.subTest(proven=proven):
                out, start, wait, prove, rep, rec, ev = self.go([dict(PROOF, proven=proven), dict(PROOF, commit="c1")], saved=False)
                self.assertEqual((out["status"], out["log"][-1], prove.call_count), ("unproven", {"error": "why abc1234 False"}, 1)); rep.assert_not_called()

    def test_a_restart_reprompts_a_live_writer_else_starts_one_and_keeps_its_proof_when_no_ready_line_comes(self):
        old = dict(PROOF, commit="old", proven=False)
        out, start, wait, prove, rep, rec, ev = self.go([], restart=old, ready=False)
        start.assert_not_called(); rep.assert_called_once_with(self.proj, "001", "again old"); self.assertEqual((out["status"], out["proof"]), ("unproven", old))
        out, start, wait, prove, rep, rec, ev = self.go([], restart=old, ready=False, live=False)
        start.assert_called_once(); rep.assert_not_called(); self.assertEqual(out["proof"], old); self.assertTrue(out["log"][-1]["error"].startswith("why old"))
