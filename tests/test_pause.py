import hermetic  # first: no test sees the real home
import io, json, os, subprocess, sys, tempfile, threading, time, unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_reflect import fc, project, task, with_tasks, run_all, synced, issue, BLOCK, MAP  # the loaded compose and the run-loop helpers
os.environ["HERDR_ENV"] = ""

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True, env={**os.environ, **ENV}).stdout


class PauseRunningTask(unittest.TestCase):
    def go(self, impl, reason=""):
        """run_task over a real repo with `impl` as the implementer and a hold on 001 written once its first turn starts."""
        root = Path(tempfile.mkdtemp())
        repo, proj = root / "repo", root / "proj"
        repo.mkdir(); (proj / "results").mkdir(parents=True)
        git(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("no\n")
        git(repo, "add", "-A"); git(repo, "commit", "-qm", "init")
        meta = {"id": "001", "title": "answer", "files": ["a.txt", "b.txt"], "protected": [], "test": "grep -q yes a.txt",
                "model": "pi:p/primary", "after": "", "linear": "ENG-1", "body": "## Goal\nWrite yes."}
        self.linear_events = []
        def wrapped(*a, **kw):
            (proj / "results" / "hold.json").write_text(json.dumps({"001": {"reason": reason, "at": "2026-10-26 10:00:00"}}))
            return impl(*a, **kw)
        self.addCleanup(fc.fc_proc.resume_task, "001")
        with mock.patch.object(fc.fc_implement, "implementer", wrapped), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_config, "TEST_READER", None), mock.patch.dict(os.environ, ENV), \
                mock.patch.object(fc.fc_linear, "linear_event", lambda p, m, e, *r: self.linear_events.append((m["id"], e))):
            return fc.fc_implement.run_task(repo, proj, meta, 3), repo, proj

    def test_a_hold_during_a_turn_pauses_the_task_and_keeps_its_edits_on_a_ref(self):
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            (Path(cwd) / "a.txt").write_text("half\n"); (Path(cwd) / "b.txt").write_text("new\n")
            threading.Timer(0.3, fc.fc_proc.pause_task, ("001",)).start()
            fc.fc_proc.run(["sleep", "30"])  # the turn, still going when the hold lands
        r, repo, proj = self.go(impl, reason="D2 changed")
        self.assertEqual(r["status"], "paused")
        self.assertEqual(r["paused"]["ref"], f"refs/compose/{proj.name}/paused/001")
        self.assertEqual(sorted(r["paused"]["files"]), ["a.txt", "b.txt"])
        self.assertEqual(git(repo, "show", f"{r['paused']['ref']}:a.txt"), "half\n")
        self.assertEqual(json.loads((proj / "results" / "001.json").read_text())["status"], "paused")
        self.assertEqual(list((proj / "worktrees").glob("001*")), [])  # the clone is gone, the ref holds the work
        self.assertEqual([e for _, e in self.linear_events], ["dispatched"])  # no failed or passed event

    def test_a_pause_between_turns_stops_before_the_next_turn(self):
        turns = []
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            turns.append(1); fc.fc_proc.pause_task("001")  # lands while the test runs after this turn
            (Path(cwd) / "a.txt").write_text("nope\n")
            return "s", "", 1, {"cost": 0.0}
        r, _, _ = self.go(impl)
        self.assertEqual((r["status"], len(turns)), ("paused", 1))


    def test_a_turn_that_returns_after_the_pause_never_reaches_its_test(self):
        tests, turned = [], []  # the base is tested once before the turn; only a test after it counts
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            turned.append(1)
            fc.fc_proc.pause_task("001")  # the cut did not end the turn with an error: it came back with the answer written
            (Path(cwd) / "a.txt").write_text("yes\n")
            return "s", "", 1, {"cost": 0.0}
        with mock.patch.object(fc.fc_implement, "run_test", lambda *a: (tests.append(1) if turned else None) or mock.Mock(returncode=1, stdout="", stderr="")):
            r, repo, _ = self.go(impl)
        self.assertEqual((r["status"], tests), ("paused", []))
        self.assertEqual([e for _, e in self.linear_events], ["dispatched"])
        self.assertEqual(git(repo, "branch", "--list", "task-*"), "")  # nothing committed to a task branch

    def test_a_snapshot_that_fails_reports_an_error_and_keeps_the_clone(self):
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            (Path(cwd) / "b.txt").write_text("new\n")
            fc.fc_proc.pause_task("001")
            return "s", "", 1, {"cost": 0.0}
        with mock.patch.object(fc.fc_implement.fc_pause, "snapshot", side_effect=RuntimeError("git broke")):
            r, _, proj = self.go(impl, reason="D2 changed")
        self.assertEqual(r["status"], "failed")
        self.assertIn("pause snapshot failed", json.dumps(r["log"]))
        self.assertTrue((proj / "worktrees" / "001" / "b.txt").exists())
        self.assertEqual(json.loads((proj / "results" / "001.json").read_text())["status"], "failed")

    def test_a_hold_during_the_last_attempts_test_that_fails_ends_paused_with_its_work(self):
        turns = []
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            turns.append(1)
            (Path(cwd) / "a.txt").write_text("half\n")
            return "s", "", 1, {"cost": 0.0}
        real = fc.fc_implement.run_test
        def run_test(meta, wt):
            if len(turns) == 3:  # the hold lands while the last attempt's test runs, and that test fails
                fc.fc_proc.pause_task("001")
            return real(meta, wt)
        with mock.patch.object(fc.fc_implement, "run_test", run_test):
            r, repo, proj = self.go(impl, reason="D2 changed")
        self.assertEqual((r["status"], len(turns)), ("paused", 3))
        self.assertEqual(git(repo, "show", f"{r['paused']['ref']}:a.txt"), "half\n")
        self.assertEqual(json.loads((proj / "results" / "001.json").read_text())["status"], "paused")
        self.assertEqual([e for _, e in self.linear_events], ["dispatched"])  # no failed event

    def test_a_pass_before_the_hold_landed_stays_passed(self):
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            (Path(cwd) / "a.txt").write_text("yes\n")
            return "s", "", 1, {"cost": 0.0}
        real = fc.fc_implement.run_test
        def run_test(meta, wt):
            out = real(meta, wt)
            if not out.returncode:
                fc.fc_proc.pause_task("001")
            return out
        with mock.patch.object(fc.fc_implement, "run_test", run_test):
            r, _, _ = self.go(impl)
        self.assertEqual(r["status"], "passed")

    def test_a_payment_failure_or_a_contradiction_keeps_its_status_under_a_hold(self):
        self.addCleanup(fc.fc_proc.resume_task, "009")
        fc.fc_proc.pause_task("009")
        ends = fc.fc_implement.fc_pause.ends_paused
        self.assertEqual([ends(s, "009") for s in ("payment_required", "contradiction", "passed")], [False, False, False])
        self.assertEqual([ends(s, "009") for s in ("failed", "rejected_tampered", "rejected_regression", "paused")], [True] * 4)
        fc.fc_proc.resume_task("009")
        self.assertFalse(ends("failed", "009"))

    def test_the_paused_result_is_written_before_the_clone_is_removed(self):
        seen = []
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            (Path(cwd) / "a.txt").write_text("half\n")
            fc.fc_proc.pause_task("001")
            return "s", "", 1, {"cost": 0.0}
        real = fc.fc_implement.fc_pause.shutil.rmtree
        def rmtree(path, *a, **kw):
            res = Path(path).parent.parent / "results" / "001.json"
            seen.append(json.loads(res.read_text()).get("status") if res.exists() else None)
            return real(path, *a, **kw)
        with mock.patch.object(fc.fc_implement.fc_pause.shutil, "rmtree", rmtree):
            self.go(impl)
        self.assertEqual(seen[-1], "paused")

    def test_a_failed_snapshot_says_to_rescue_the_clone_and_the_next_run_moves_it_aside(self):
        said = []
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            (Path(cwd) / "b.txt").write_text("new\n")
            fc.fc_proc.pause_task("001")
            return "s", "", 1, {"cost": 0.0}
        with mock.patch.object(fc.fc_implement.fc_pause, "snapshot", side_effect=RuntimeError("git broke")), \
                mock.patch.object(fc.fc_core, "say", lambda m, *a, **k: said.append(m)):
            r, repo, proj = self.go(impl, reason="D2 changed")
        self.assertIn("rescue it before the task runs again", " ".join(said))
        fc.fc_proc.resume_task("001")
        (proj / "results" / "hold.json").unlink()
        said.clear()
        meta = {"id": "001", "title": "answer", "files": ["a.txt", "b.txt"], "protected": [], "test": "grep -q yes a.txt",
                "model": "pi:p/primary", "after": "", "linear": "ENG-1", "body": "## Goal\nWrite yes."}
        def fix(cwd, model, prompt, session=None, task=None, timeout=None):
            (Path(cwd) / "a.txt").write_text("yes\n")
            return "s", "", 1, {"cost": 0.0}
        with mock.patch.object(fc.fc_implement, "implementer", fix), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_config, "TEST_READER", None), mock.patch.dict(os.environ, ENV), \
                mock.patch.object(fc.fc_linear, "linear_event", lambda *a: None), mock.patch.object(fc.fc_core, "say", lambda m, *a, **k: said.append(m)):
            fc.fc_implement.run_task(repo, proj, meta, 3)
        aside = [p for p in (proj / "worktrees").iterdir() if p.name.startswith("001-unsaved-")]
        self.assertEqual(len(aside), 1)
        self.assertEqual((aside[0] / "b.txt").read_text(), "new\n")
        self.assertIn(f"moved to {aside[0]}", " ".join(said))

    def test_a_second_pause_keeps_the_first_pauses_commit_reachable_from_the_ref(self):
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            (Path(cwd) / "a.txt").write_text("half\n"); (Path(cwd) / "b.txt").write_text("new\n")
            fc.fc_proc.pause_task("001")
            return "s", "", 1, {"cost": 0.0}
        r, repo, proj = self.go(impl)
        ref, base = r["paused"]["ref"], r["paused"]["base"]
        first = git(repo, "rev-parse", ref).strip()
        wt = proj / "worktrees" / "again"
        git(repo, "clone", "-q", "--shared", str(repo), str(wt))
        git(wt, "checkout", "-q", base)
        (wt / "a.txt").write_text("again\n")
        got, files = fc.fc_implement.fc_pause.snapshot(repo, proj, "001", wt, base)
        self.assertEqual((got, files), (ref, ["a.txt"]))
        subprocess.run(["git", "merge-base", "--is-ancestor", first, ref], cwd=repo, check=True)
        self.assertEqual(git(repo, "show", f"{ref}:a.txt"), "again\n")
        self.assertEqual(git(repo, "show", f"{ref}^2:b.txt"), "new\n")  # the file the second state left out stays on the ref


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


class CarryOver(unittest.TestCase):
    def pause_with(self, edits, choice=None, keep=None):
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            for name, text in edits.items():
                (Path(cwd) / name).write_text(text)
            fc.fc_proc.pause_task("001")
            return "s", "", 1, {"cost": 0.0}
        r, repo, proj = PauseRunningTask.go(self, impl)
        self.assertEqual(r["status"], "paused")
        path = proj / "results" / "001.json"
        data = json.loads(path.read_text())
        if choice:
            data["paused"]["choice"] = choice
        if keep is not None:
            data["paused"]["keep"] = keep
        path.write_text(json.dumps(data))
        (proj / "results" / "hold.json").unlink()
        fc.fc_proc.resume_task("001")
        return r, repo, proj

    def commit_on_main(self, repo, name, text):
        (repo / name).write_text(text)
        git(repo, "add", "-A"); git(repo, "commit", "-qm", f"move {name}")

    def resume(self, repo, proj, files=None, stop=False, bench=""):
        """A fresh run_task over the same repo and project; returns (a.txt, b.txt, prompt) of the first turn."""
        meta = {"id": "001", "title": "answer", "files": files or ["a.txt", "b.txt"], "protected": [], "test": "grep -q yes a.txt",
                "model": "pi:p/primary", "after": "", "linear": "ENG-1", "body": "## Goal\nWrite yes."}
        first, self.said, self.events = [], [], []
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            if not first:
                first.append(tuple((Path(cwd) / n).read_text() if (Path(cwd) / n).exists() else None for n in ("a.txt", "b.txt")) + (prompt,))
            if stop:
                raise KeyboardInterrupt
            (Path(cwd) / "a.txt").write_text("yes\n")
            return "s", "", 1, {"cost": 0.0}
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_config, "TEST_READER", None), mock.patch.dict(os.environ, ENV), \
                mock.patch.object(fc.fc_linear, "linear_event", lambda *a: self.events.append(a[2:])), \
                mock.patch.object(fc.fc_core, "say", lambda m, *a, **k: self.said.append(m)):
            try:
                fc.fc_implement.run_task(repo, proj, meta, 3, **({"reset_files": True, "tag": bench} if bench else {}))
            except KeyboardInterrupt:
                pass
        self.resume_prompt = first[0][2]
        return first[0]

    def test_a_released_task_starts_with_its_kept_edits_and_says_so_in_the_prompt(self):
        r, repo, proj = self.pause_with({"a.txt": "half\n", "b.txt": "new\n"})
        seen = self.resume(repo, proj)
        self.assertEqual(seen[:2], ("half\n", "new\n"))
        self.assertTrue(seen[2].startswith("WORK KEPT FROM BEFORE A PLAN CHANGE: a.txt, b.txt already hold edits"))

    def test_a_file_the_new_brief_dropped_or_that_conflicts_is_left_out_and_said(self):
        r, repo, proj = self.pause_with({"a.txt": "half\n", "b.txt": "new\n"})
        self.commit_on_main(repo, "a.txt", "moved\n")
        seen = self.resume(repo, proj, files=["a.txt"])
        self.assertEqual(seen[0], "moved\n")
        self.assertIn("001 kept    nothing; dropped a.txt, b.txt: they stay on refs/compose/", "\n".join(self.said))

    def test_part_keeps_only_the_chosen_files_and_discard_keeps_none(self):
        r, repo, proj = self.pause_with({"a.txt": "half\n", "b.txt": "new\n"}, choice="part", keep=["b.txt"])
        seen = self.resume(repo, proj)
        self.assertEqual(seen[:2], ("no\n", "new\n"))
        r, repo, proj = self.pause_with({"a.txt": "half\n", "b.txt": "new\n"}, choice="discard")
        seen = self.resume(repo, proj)
        self.assertEqual(seen[0], "no\n")
        self.assertFalse(seen[2].startswith("WORK KEPT"))

    def test_a_file_whose_diff_fails_is_dropped_and_never_kept(self):
        r, repo, proj = self.pause_with({"a.txt": "half\n", "b.txt": "new\n"})
        real = subprocess.run
        def flaky(cmd, *a, **k):
            if cmd[:3] == ["git", "diff", "--binary"] and cmd[-1] == "b.txt":
                return subprocess.CompletedProcess(cmd, 128, b"", b"bad object")
            return real(cmd, *a, **k)
        with mock.patch.object(fc.fc_implement.fc_pause.subprocess, "run", flaky):
            seen = self.resume(repo, proj)
        self.assertEqual(seen[:2], ("half\n", None))
        self.assertTrue(seen[2].startswith("WORK KEPT FROM BEFORE A PLAN CHANGE: a.txt already hold"))
        self.assertIn("001 kept    a.txt; dropped b.txt: they stay on", "\n".join(self.said))

    def test_a_ref_that_cannot_be_fetched_is_said(self):
        r, repo, proj = self.pause_with({"a.txt": "half\n"})
        path = proj / "results" / "001.json"
        data = json.loads(path.read_text())
        data["paused"]["ref"] = "refs/compose/gone"
        path.write_text(json.dumps(data))
        seen = self.resume(repo, proj)
        self.assertEqual(seen[0], "no\n")
        self.assertIn("001 kept    nothing; the ref refs/compose/gone could not be fetched", "\n".join(self.said))

    def test_a_bench_run_starts_from_scratch_and_leaves_the_paused_record_alone(self):
        r, repo, proj = self.pause_with({"a.txt": "half\n"})
        before = (proj / "results" / "001.json").read_text()
        seen = self.resume(repo, proj, files=["a.txt"], bench="-bench-m-r0")
        self.assertIsNone(seen[0])  # the bench reset removed the implementation, and nothing was carried back
        self.assertNotIn("WORK KEPT", seen[2])
        self.assertEqual((proj / "results" / "001.json").read_text(), before)
        self.assertTrue((proj / "results" / "001-bench-m-r0.json").exists())
        seen = self.resume(repo, proj)  # the real task still starts from its kept edits
        self.assertEqual(seen[0], "half\n")

    def test_triage_reads_only_bare_task_ids_not_bench_results(self):
        proj = Path(tempfile.mkdtemp())
        (proj / "results").mkdir()
        for name in ("001.json", "001-bench-m-r0.json"):
            (proj / "results" / name).write_text(json.dumps({"status": "passed"}))
        got = []
        with mock.patch.object(fc.fc_core, "project_dir", lambda a: proj), mock.patch.object(fc.fc_run, "triage", lambda repo, p, ids: got.append(ids) or {"x": 1}):
            fc.fc_run.cmd_triage(SimpleNamespace(repo=".", only=None))
        self.assertEqual(got, [["001"]])

    def test_kept_edits_survive_a_released_run_that_is_stopped(self):
        r, repo, proj = self.pause_with({"a.txt": "half\n", "b.txt": "new\n"})
        self.resume(repo, proj, stop=True)
        self.assertEqual(json.loads((proj / "results" / "001.json").read_text())["status"], "running")
        seen = self.resume(repo, proj)
        self.assertEqual(seen[:2], ("half\n", "new\n"))
        self.assertTrue(seen[2].startswith("WORK KEPT"))
        self.assertNotIn("kept_from", json.loads((proj / "results" / "001.json").read_text()))


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


if __name__ == "__main__":
    unittest.main()
