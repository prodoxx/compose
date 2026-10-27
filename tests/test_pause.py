import hermetic  # first: no test sees the real home
import io, json, os, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_reflect import fc, project, task, with_tasks, run_all, synced, issue, BLOCK, MAP  # the loaded compose and the run-loop helpers
os.environ["HERDR_ENV"] = ""


class HerdrPiPause(unittest.TestCase):
    def turn(self, answer):
        proj = Path(tempfile.mkdtemp()) / "hw"
        task = {"proj": proj, "meta": {"id": "001", "title": "t"}, "space": "s"}
        session = {"name": "hw-001", "pane": "p1", "sfile": None, "seen": 0}
        self.addCleanup(fc.fc_proc.resume_task, "001")
        fc.fc_proc.pause_task("001")
        with mock.patch.object(fc.fc_herdr, "herdr_error", return_value=answer), mock.patch.object(fc.fc_herdr, "herdr", return_value={}):
            fc.fc_implement.herdr_pi(tempfile.mkdtemp(), "x/y", "hi", session, task)

    def test_a_paused_task_raises_paused_whether_or_not_herdr_reported_an_error(self):
        for answer in (({"ok": 1}, None), (None, "agent_error")):
            with self.subTest(answer=answer), self.assertRaises(fc.fc_proc.Paused):
                self.turn(answer)


class RunLoopPauses(unittest.TestCase):
    def setUp(self):
        self.proj = project()
        with_tasks(self.proj, [task("001"), task("002")])
        self.addCleanup(fc.fc_proc.resume_task, "002")
        p = mock.patch.object(fc.fc_core, "checkout", return_value=None); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(fc.fc_green, "tree_of", return_value=None); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(fc.fc_accept.fc_ledger, "run_events", return_value=[]); p.start(); self.addCleanup(p.stop)

    def hold_002_and_wait(self, tid):
        if tid != "002":
            return
        (self.proj / "results" / "hold.json").write_text(json.dumps({"002": {"reason": "D2 changed", "at": "2026-10-26 10:00:00"}}))
        end = time.time() + 6
        while time.time() < end and not fc.fc_proc.task_paused("002"):
            time.sleep(0.05)

    def test_the_run_pauses_a_held_running_task_within_a_pass_and_keeps_it_pending(self):
        calls, out, _ = run_all(self.proj, status={"002": "paused"}, during=self.hold_002_and_wait, parallel=2)
        self.assertIn("002 paused  D2 changed", out)
        self.assertEqual(calls.count("002"), 1)  # not restarted while held
        self.assertIn("002 held    D2 changed", out)
        self.assertFalse(fc.fc_proc.task_paused("002"))  # cleared when its result came back

    def test_a_hold_that_vanishes_between_reads_does_not_stop_the_loop(self):
        gone = []
        real = fc.fc_tasks.held
        def held(proj):  # the hold shows for two reads once 002 runs, then a release lands
            if len(gone) >= 2 or not seen:
                return {}
            gone.append(1)
            return {"002": {"reason": "D2 changed", "at": "x"}}
        seen = []
        def during(tid):
            if tid == "002":
                seen.append(1)
                end = time.time() + 3
                while time.time() < end and len(gone) < 2:
                    time.sleep(0.05)
        with mock.patch.object(fc.fc_tasks, "held", held):
            _, out, _ = run_all(self.proj, during=during, parallel=2)
        self.assertIn("run done", out)


    def test_a_regression_writer_and_a_finished_task_are_not_paused(self):
        paused = []
        real_pause = fc.fc_proc.pause_task
        def pause(tid, hook=None):
            paused.append(tid)
            return real_pause(tid, hook)
        def hold(tid):
            f = self.proj / "results" / "hold.json"
            f.write_text(json.dumps({**(json.loads(f.read_text()) if f.exists() else {}), tid: {"reason": "D2 changed", "at": "x"}}))
        def regress(repo, proj, meta, prior, attempts):
            end = time.time() + 3
            while time.time() < end and not dropped:  # one writer of hold.json at a time
                time.sleep(0.02)
            hold("002")
            end = time.time() + 1.5  # long enough for the loop to go round with the hold in place
            while time.time() < end and not fc.fc_proc.task_paused("002"):
                time.sleep(0.05)
            return {"id": "002", "title": "task 002", "status": "passed", "attempts": 1, "seconds": 0.0, "cost": 0.0, "branch": "-", "log": [], "proof": {}}
        import concurrent.futures as cf
        real_wait, dropped = cf.wait, []
        def wait(fs, timeout=None, return_when=None):  # 001 ends done but is left in `running` for one pass, as a pass that missed it would
            done, rest = real_wait(fs, timeout=timeout, return_when=return_when)
            late = {f for f in done if not dropped and f.result()["id"] == "001"}
            if late:
                hold("001")
                dropped.append(1)
            return done - late, rest | late
        self.addCleanup(fc.fc_proc.resume_task, "001")
        prior = lambda proj, tid: {"branch": "task-002"} if tid == "002" else None
        with mock.patch.object(fc.fc_proc, "pause_task", pause), mock.patch.object(fc.fc_run.fc_unproven, "restartable", prior), \
                mock.patch.object(fc.fc_run.fc_regress, "regress", regress), mock.patch.object(cf, "wait", wait):
            calls, out, _ = run_all(self.proj, parallel=2)
        self.assertEqual(dropped, [1])
        self.assertEqual(paused, [])
        self.assertNotIn("002 paused", out)
        self.assertNotIn("001 paused", out)


RESTS_MAP = MAP.replace("1. Show a toast on save\n", "1. Show a toast on save\n- D3: one plan\n- D9: another plan\n")


class HoldCase(unittest.TestCase):
    """A project for the hold command with its say, project_dir and Linear note faked."""
    def hold(self, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            fc.fc_change.cmd_hold(SimpleNamespace(**{"only": None, "rests_on": None, "reason": None, "release": None, **kw}))
        return out.getvalue()

    def fake(self):
        fc.fc_tasks._hold_warned.clear()
        for mod, name, value in ((fc.fc_core, "say", lambda m, *a, **k: None), (fc.fc_core, "project_dir", lambda a: self.proj),
                                 (fc.fc_linear, "linear_note", lambda meta, body: None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)


class RestsOn(HoldCase):
    def setUp(self):
        self.proj = project(RESTS_MAP)
        def tissue(tid, goal, parent, status="Todo", blocked=()):
            return {**issue(tid, goal, status, blocked), "parentId": parent}
        synced(self.proj, RESTS_MAP, {"ENG-2": {"title": "ENG-2 story", "description": "", "status": "Todo", "parentId": "ENG-1"},
                                      "ENG-3": {"title": "ENG-3 story", "description": "", "status": "Todo", "parentId": "ENG-1"},
                                      "ENG-11": tissue("001", "Write it.", "ENG-2"),
                                      "ENG-12": tissue("002", "Read it.", "ENG-2", blocked=["ENG-11"]),
                                      "ENG-13": tissue("003", "Follow D3.", "ENG-3"),
                                      "ENG-14": tissue("004", "Done already.", "ENG-3")})
        (self.proj / "results" / "004.json").write_text(json.dumps({"id": "004", "status": "passed"}))
        self.fake()

    def write_reflect(self, flagged):
        (self.proj / "results" / "reflect-20260101-000000-01.json").write_text(json.dumps({"flagged": [{"line": l} for l in flagged]}))

    def test_an_issue_a_tech_doc_or_the_feature_rests_its_tasks_and_what_they_block(self):
        self.assertEqual(fc.fc_change.rests_on(self.proj, ["ENG-2"]), {"001": "under ENG-2", "002": "under ENG-2"})
        self.assertEqual(fc.fc_change.rests_on(self.proj, ["ENG-11"]), {"001": "is ENG-11", "002": "after 001"})
        self.assertEqual(set(fc.fc_change.rests_on(self.proj, ["ENG-2: tech doc"])), {"001", "002"})
        self.assertEqual(set(fc.fc_change.rests_on(self.proj, ["ENG-1"])), {"001", "002", "003"})

    def test_a_decision_or_the_newest_reflect_rests_the_briefs_that_cite_it_and_an_uncited_one_rests_everything(self):
        self.assertEqual(fc.fc_change.rests_on(self.proj, ["D3"]), {"003": "cites D3"})
        self.assertEqual(fc.fc_change.rests_on(self.proj, ["D9"]), {t: "D9 cited by no brief" for t in ("001", "002", "003")})
        self.assertEqual(fc.fc_change.rests_on(self.proj, ["D42"]), {})
        self.write_reflect(flagged=["D3: one plan"])
        self.assertEqual(fc.fc_change.rests_on(self.proj, ["reflect"]), {"003": "cites D3"})

    def test_a_torn_newest_reflect_falls_back_to_the_next_one_or_none(self):
        self.write_reflect(flagged=["D3: one plan"])
        (self.proj / "results" / "reflect-20270101-000000-01.json").write_text('{"flagged": [')
        self.assertEqual(fc.fc_change.rests_on(self.proj, ["reflect"]), {"003": "cites D3"})
        (self.proj / "results" / "reflect-20260101-000000-01.json").unlink()
        self.assertEqual(fc.fc_change.rests_on(self.proj, ["reflect"]), {})

    def test_a_reflect_file_appears_whole_and_a_parent_cycle_ends(self):
        with mock.patch.object(fc.fc_models, "jev_ask", return_value={"model": "m", "answers": {}}), \
                mock.patch.object(fc.fc_change.os, "replace", wraps=os.replace) as rep, mock.patch.object(fc.fc_change.os, "link", wraps=os.link) as link:
            path, rec = fc.fc_change.reflect(self.proj, "manual", "x")
        self.assertEqual(json.loads(path.read_text())["trigger"], "manual")
        self.assertTrue(link.called)
        cache = json.loads((self.proj / "results" / "tasks.json").read_text())
        cache["issues"]["ENG-4"] = {"title": "x", "description": "", "status": "Todo"}; cache["issues"]["ENG-2"]["parentId"] = "ENG-3"; cache["issues"]["ENG-3"]["parentId"] = "ENG-2"
        (self.proj / "results" / "tasks.json").write_text(json.dumps(cache))
        self.assertEqual(fc.fc_change.rests_on(self.proj, ["ENG-4"]), {})

    def test_hold_rests_on_holds_them_and_a_ref_that_matches_nothing_is_said(self):
        out = self.hold(rests_on="ENG-2,NOPE-9", reason="story ENG-2 changed")
        self.assertIn("rests on ENG-2: 001 (under ENG-2), 002 (under ENG-2)", out)
        self.assertIn("rests on nothing: NOPE-9", out)
        self.assertEqual(set(fc.fc_tasks.held(self.proj)), {"001", "002"})

    def test_only_and_rests_on_are_unioned_and_a_reason_is_required(self):
        self.hold(only="003", rests_on="ENG-11", reason="x")
        self.assertEqual(set(fc.fc_tasks.held(self.proj)), {"001", "002", "003"})
        with self.assertRaises(SystemExit):
            self.hold(rests_on="ENG-2")


class TeammateSteer(HoldCase):
    def setUp(self):
        self.proj = project()
        with_tasks(self.proj, [task("001"), task("002", "001"), task("003", "002")])
        self.fake()
        self.calls, self.live = [], {}  # agent -> its herdr status
        def herdr(*args, **kw):
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": self.live[args[2]]}} if args[2] in self.live else None
            self.calls.append(args)
            if args[:2] == ("agent", "prompt") and args[3].startswith("Paused"):
                self.live[args[2]] = "done"  # the teammate answers the pause in one short turn and goes idle
            return {}
        p = mock.patch.object(fc.fc_herdr, "herdr", herdr); p.start(); self.addCleanup(p.stop)

    def test_a_hold_pauses_a_live_test_teammate_and_a_release_sends_it_back_to_its_brief(self):
        agent = fc.fc_herdr.agent_name(self.proj.name, "tests-002")
        self.live = {agent: "working"}
        out = self.hold(only="002,003", reason="D2 changed")
        self.assertEqual(self.calls[:2], [("agent", "send-keys", agent, "esc"),
                                          ("agent", "prompt", agent, "Paused: D2 changed. Stop now, keep what you wrote, and wait for the next prompt.")])
        self.assertIn("002 held  D2 changed; its test teammate is paused", out)
        self.assertIn("003 held  D2 changed\n", out)
        self.assertTrue(fc.fc_tasks.held(self.proj)["002"]["teammate"])
        self.assertEqual(self.live[agent], "done")
        self.calls.clear()
        self.hold(release="002")
        self.assertEqual([c[:3] for c in self.calls], [("agent", "prompt", agent)])  # idle and waiting: prompted, not interrupted
        self.assertIn("compose linear get ENG-2 --body", self.calls[-1][3])

    def test_holding_a_held_task_again_keeps_its_paused_teammate_for_the_release(self):
        agent = fc.fc_herdr.agent_name(self.proj.name, "tests-002")
        self.live = {agent: "working"}
        self.hold(only="002", reason="D2 changed")
        self.hold(only="002", reason="D3 changed too")  # the teammate is idle now: this hold does not pause it again
        self.assertTrue(fc.fc_tasks.held(self.proj)["002"]["teammate"])
        self.assertEqual(fc.fc_tasks.held(self.proj)["002"]["reason"], "D3 changed too")
        self.calls.clear()
        self.hold(release="002")
        self.assertEqual([c[:3] for c in self.calls], [("agent", "prompt", agent)])

    def test_a_release_that_lands_while_a_hold_steers_its_teammates_stays_released(self):
        agent = fc.fc_herdr.agent_name(self.proj.name, "tests-002")
        self.live = {agent: "working"}
        self.hold(only="003", reason="x")
        real = fc.fc_change.steer_teammate
        def slow(proj, tid, text, release=False):
            out = real(proj, tid, text, release)
            if tid == "002" and not release:
                self.hold(release="003")  # another `compose hold --release 003` lands mid-steer
            return out
        with mock.patch.object(fc.fc_change, "steer_teammate", slow):
            self.hold(only="002", reason="D2 changed")
        self.assertEqual(sorted(fc.fc_tasks.held(self.proj)), ["002"])
        self.assertTrue(fc.fc_tasks.held(self.proj)["002"]["teammate"])

    def test_a_release_leaves_a_teammate_the_hold_did_not_pause_alone(self):
        agent = fc.fc_herdr.agent_name(self.proj.name, "tests-003")
        self.hold(only="003", reason="D2 changed")  # no teammate yet
        self.live = {agent: "working"}  # one started after the hold, mid-turn now
        self.calls.clear()
        self.hold(release="003")
        self.assertEqual(self.calls, [])

    def test_a_hold_does_not_stop_a_regression_writer(self):
        agent = fc.fc_herdr.agent_name(self.proj.name, "tests-002")
        self.live = {agent: "working"}
        (self.proj / "results" / "002.json").write_text(json.dumps({"id": "002", "status": "unproven"}))
        out = self.hold(only="002", reason="D2 changed")
        self.assertEqual(self.calls, [])
        self.assertNotIn("teammate", out)

    def test_a_teammate_that_is_done_or_does_not_take_the_prompt_is_not_called_paused(self):
        agent = fc.fc_herdr.agent_name(self.proj.name, "tests-002")
        self.live = {agent: "working"}
        with mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: {"agent": {"agent_status": "done"}} if a[1] == "get" else {}):
            self.assertNotIn("teammate", self.hold(only="002", reason="x"))
        self.hold(release="002")
        with mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: {} if a[1] == "get" else None):
            self.assertNotIn("teammate", self.hold(only="002", reason="x"))


class PauseKeeps(unittest.TestCase):
    """What a pause keeps and where, decided from plain facts; the git plumbing that writes the ref stays untested."""
    P = staticmethod(lambda: fc.fc_implement.fc_pause)

    def test_the_work_is_kept_on_a_ref_of_its_project_and_task(self):
        self.assertEqual(self.P().paused_ref(Path("/p/shop"), "007"), "refs/compose/shop/paused/007")

    def test_a_second_pause_keeps_the_first_pauses_commit_reachable_from_the_ref(self):
        self.assertEqual(self.P().parents("base1", ""), ["-p", "base1"])
        self.assertEqual(self.P().parents("base1", "first"), ["-p", "base1", "-p", "first"])

    def test_a_pause_that_kept_the_edits_is_a_paused_result_with_no_branch_and_the_ref_it_wrote(self):
        block = {"ref": "refs/compose/shop/paused/001", "base": "b1", "files": ["001.txt"], "at": "now", "reason": "D2 broken"}
        r = self.P().paused_record(task("001"), "main", [{"attempt": 1}], 1, 0.123456789, 4.5, 0.0, block)
        self.assertEqual((r["status"], r["branch"], r["paused"], r["attempts"], r["cost"], r["base"]), ("paused", "-", block, 1, 0.123457, "main"))
        self.assertNotIn("snapshot_failed", r)

    def test_a_snapshot_that_fails_reports_a_failed_result_and_names_the_clone_that_keeps_the_edits(self):
        r = self.P().failed_record(task("001"), Path("/p/shop/worktrees/001"), "main", [{"error": "pause snapshot failed: x"}], 2, 0.5, 9.0)
        self.assertEqual((r["status"], r["branch"], r["snapshot_failed"], r["worktree"], r["attempts"]), ("failed", "-", True, "/p/shop/worktrees/001", 2))
        self.assertNotIn("paused", r)

    def test_only_a_failed_snapshot_of_this_very_clone_keeps_it(self):
        wt = Path("/p/shop/worktrees/001")
        keeps = self.P().keeps_clone
        self.assertTrue(keeps({"snapshot_failed": True, "worktree": str(wt)}, wt))
        self.assertFalse(keeps({"snapshot_failed": True, "worktree": "/elsewhere"}, wt))
        self.assertFalse(keeps({"worktree": str(wt)}, wt))
        self.assertFalse(keeps({}, wt))

    def clone(self, last):
        proj = Path(tempfile.mkdtemp()) / "shop"
        wt = proj / "worktrees" / "001"
        wt.mkdir(parents=True)
        (wt / "edit.txt").write_text("unsaved\n")
        (proj / "results").mkdir()
        if last is not None:
            (proj / "results" / "001.json").write_text(json.dumps({**last, "worktree": str(wt)}))
        said = []
        with mock.patch.object(fc.fc_core, "say", said.append):
            self.P().clear_clone(proj, "001", wt)
        return proj, wt, said

    def test_the_next_run_moves_a_clone_a_failed_snapshot_kept_aside_and_says_so(self):
        proj, wt, said = self.clone({"snapshot_failed": True})
        self.assertFalse(wt.exists())
        (aside,) = [p for p in wt.parent.iterdir()]
        self.assertTrue(aside.name.startswith("001" + fc.fc_worktree.UNSAVED_CLONE))
        self.assertEqual((aside / "edit.txt").read_text(), "unsaved\n")
        self.assertIn(str(aside), said[0])

    def test_any_other_clone_is_removed_whole(self):
        for last in (None, {"status": "failed"}):
            proj, wt, said = self.clone(last)
            self.assertEqual((list(wt.parent.iterdir()), said), ([], []))


class CarryOver(unittest.TestCase):
    """What a released task takes from the edits its pause kept."""
    P = staticmethod(lambda: fc.fc_implement.fc_pause)
    PAUSED = {"ref": "refs/compose/shop/paused/001", "base": "b1", "files": ["a.txt", "b.txt", "c.txt"]}

    def last(self, **kw):
        return {"status": "paused", "paused": {**self.PAUSED, **kw}}

    def test_a_pause_with_no_choice_keeps_every_file_it_touched(self):
        self.assertEqual(self.P().carry_plan(self.last()), ("refs/compose/shop/paused/001", "b1", ["a.txt", "b.txt", "c.txt"]))
        self.assertEqual(self.P().carry_plan(self.last(choice="keep"))[2], ["a.txt", "b.txt", "c.txt"])

    def test_part_keeps_only_the_chosen_files_and_discard_keeps_none(self):
        self.assertEqual(self.P().carry_plan(self.last(choice="part", keep=["b.txt"]))[2], ["b.txt"])
        self.assertIsNone(self.P().carry_plan(self.last(choice="part", keep=[])))
        self.assertIsNone(self.P().carry_plan(self.last(choice="part")))
        self.assertIsNone(self.P().carry_plan(self.last(choice="discard")))

    def test_a_task_that_never_paused_or_has_nothing_kept_carries_nothing(self):
        for last in ({}, {"status": "failed"}, {"status": "paused"}, self.last(files=[])):
            self.assertIsNone(self.P().carry_plan(last), last)

    def test_a_released_run_that_is_stopped_keeps_the_kept_from_block_for_the_next_start(self):
        released = {"status": "running", "kept_from": {**self.PAUSED, "choice": "part", "keep": ["a.txt"]}}
        self.assertEqual(self.P().carry_plan(released)[2], ["a.txt"])
        self.assertEqual(self.P().carried(json.dumps(released)), {"kept_from": released["kept_from"]})
        self.assertEqual(self.P().carried(json.dumps(self.last())), {"kept_from": self.PAUSED})
        self.assertEqual((self.P().carried(None), self.P().carried(json.dumps({"status": "failed"}))), ({}, {}))

    def test_a_file_the_new_brief_dropped_or_that_conflicts_is_left_out(self):
        kept, dropped = self.P().split_kept(["a.txt", "b.txt", "c.txt"], ["a.txt", "c.txt"], lambda f: f != "c.txt")
        self.assertEqual((kept, dropped), (["a.txt"], ["b.txt", "c.txt"]))  # b.txt is no longer the brief's file, c.txt does not apply

    def test_a_file_the_brief_dropped_is_never_applied(self):
        tried = []
        self.P().split_kept(["a.txt", "b.txt"], ["a.txt"], lambda f: tried.append(f) or True)
        self.assertEqual(tried, ["a.txt"])

    def carry(self, last, fetched=True, applies=lambda f: True, files=("a.txt", "b.txt")):
        said = []
        with mock.patch.object(self.P(), "_fetch_ref", return_value=fetched), mock.patch.object(self.P(), "_apply_one", lambda wt, base, f: applies(f)), \
                mock.patch.object(fc.fc_core, "say", said.append):
            return self.P().carry_over(Path("/repo"), Path("/wt"), {"id": "001", "files": list(files)}, last), said

    def test_a_released_task_keeps_what_applies_and_says_what_stayed_on_the_ref(self):
        kept, said = self.carry(self.last(), applies=lambda f: f != "b.txt")
        self.assertEqual(kept, ["a.txt"])
        self.assertEqual(said, ["001 kept    a.txt; dropped b.txt, c.txt: they stay on refs/compose/shop/paused/001"])

    def test_a_ref_that_cannot_be_fetched_is_said_and_nothing_is_kept(self):
        kept, said = self.carry(self.last(), fetched=False)
        self.assertEqual((kept, said), ([], ["001 kept    nothing; the ref refs/compose/shop/paused/001 could not be fetched"]))

    def test_discard_and_a_task_that_never_paused_fetch_nothing(self):
        with mock.patch.object(self.P(), "_fetch_ref", side_effect=AssertionError("no fetch")):
            for last in (self.last(choice="discard"), {}):
                self.assertEqual(self.P().carry_over(Path("/repo"), Path("/wt"), {"id": "001", "files": ["a.txt"]}, last), [])

    def test_the_prompt_names_the_files_kept_and_says_nothing_when_none_are(self):
        self.assertIn("a.txt, b.txt already hold edits made under the old brief", self.P().carry_note(["a.txt", "b.txt"]))
        self.assertEqual(self.P().carry_note([]), "")


if __name__ == "__main__":
    unittest.main()
