import hermetic  # first: no test sees the real home
import io, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from cleanup_kit import CLI, ENV, fc, repo
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""


class TestCrewWorktrees(unittest.TestCase):
    """Test-writing teammates write on their own branches; collect brings their commits back in task order."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        self.repo, self.run = repo()
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        tasks = [{"id": "001", "title": "A", "files": ["a.go"], "protected": ["a_test.go"], "test": "true", "body": "a"},
                 {"id": "002", "title": "B", "files": ["b.go"], "protected": ["b_test.go"], "test": "true", "body": "b", "test_lines": "25"}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        for p in (mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=self.repo)):
            p.start(); self.addCleanup(p.stop)

    def crew_commits(self, tid, name, text, state="working"):
        wt = fc.fc_briefs.test_worktree(self.proj, tid)
        (wt / name).write_text(text)
        self.run("add", name, cwd=wt); self.run("commit", "-qm", f"Test {name}", cwd=wt)
        if state:
            fc.fc_crew.write_report(self.proj, f"p-tests-{tid}", state, "test ready: x")
        return wt

    def writer_branches(self):
        return self.run("branch", "--list", "--format=%(refname:short)", "*tests-*").stdout.split()

    def test_testcrews_puts_writers_on_the_project_branch_name(self):
        (self.proj / "verify.json").write_text(json.dumps({"branch": "team/p"}))
        self.run("branch", "fast/pq/001")  # another project's legacy branch leaves this one on new names
        self.start_writers([])
        self.assertEqual(self.writer_branches(), ["team/p-tests-001", "team/p-tests-002"])

    def test_a_project_with_legacy_branches_keeps_its_writers_on_them(self):
        self.run("branch", "fast/p/001")
        self.crew_commits("001", "a_test.go", "a\n")
        self.assertEqual(self.writer_branches(), ["fast/p/tests-001"])
        fc.fc_briefs.collect_tests(self.proj)
        self.assertTrue((self.repo / "a_test.go").exists())
        self.assertEqual(self.writer_branches(), [])

    def add_task(self, tid, protected, after="", test="true"):
        f = self.proj / "results" / "tasks.json"
        task = {"id": tid, "title": tid, "files": [], "protected": protected.split(), "test": test, "body": tid, "after": after}
        f.write_text(json.dumps({"feature": {}, "tasks": json.loads(f.read_text())["tasks"] + [task]}))

    def start_writers(self, started):
        (self.proj / "results" / "contract.md").write_text("c")
        out = io.StringIO()
        with mock.patch.object(fc.fc_herdr, "herdr_space"), mock.patch.object(fc.fc_crew, "cmd_crew", lambda ns: started.append(ns.role)), \
             mock.patch("sys.stdout", out):
            fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only=None, print_=False, collect=False, budget=None))
        return out.getvalue()

    def test_writers_of_one_test_file_start_one_after_another(self):
        self.add_task("003", "a_test.go", after="001")
        started = []
        out = self.start_writers(started)
        self.assertEqual(sorted(started), ["tests-001", "tests-002"])  # 003 shares a_test.go with 001: both creating it conflicted
        self.assertIn("tests-003 waits for tests-001", out)
        self.crew_commits("001", "a_test.go", "a\n", state="done")
        self.crew_commits("002", "b_test.go", "b\n", state="done")
        with mock.patch.object(fc.fc_herdr, "herdr_space"), mock.patch.object(fc.fc_crew, "cmd_crew", lambda ns: started.append(ns.role)), \
             mock.patch.object(fc.fc_run, "run_tasks") as rt, self.assertRaises(SystemExit) as e:
            fc.fc_run.cmd_run(SimpleNamespace(repo=str(self.repo), project="p", only=None, redo=False, no_stack=False))
        self.assertIn("tests-003", str(e.exception)); rt.assert_not_called()  # its test is still to be written
        self.assertEqual(started[-1], "tests-003")  # started by the collect, off the branch that now holds 001's test
        wt = self.crew_commits("003", "a_test.go", "a\nc\n")
        self.assertEqual(self.run("log", "--format=%s", "-3", cwd=wt).stdout.split("\n")[:3], ["Test a_test.go", "Test b_test.go", "Test a_test.go"])
        fc.fc_briefs.collect_tests(self.proj)
        self.assertEqual((self.repo / "a_test.go").read_text(), "a\nc\n")
        self.assertEqual(self.writer_branches(), [])

    def test_a_test_that_cannot_load_stays_on_its_writer_branch_until_the_writer_reports_again(self):
        f = self.proj / "results" / "tasks.json"
        tasks = json.loads(f.read_text())["tasks"]
        tasks[0]["test"] = f"{sys.executable} -c 'import no_such_module_xyz'"
        f.write_text(json.dumps({"feature": {}, "tasks": tasks}))
        self.start_writers([])
        wt = self.crew_commits("001", "a_test.go", "a\n")
        sent = []
        with mock.patch.object(fc.fc_herdr, "live_agents", lambda: [{"name": "p-tests-001"}]), mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: sent.append(a) or {}):
            fc.fc_briefs.collect_tests(self.proj, "001")
            self.assertTrue(self.run("branch", "--list", "*tests-001", cwd=wt).stdout.strip())  # not collected: its branch is kept
            self.assertFalse((self.repo / "a_test.go").exists())
            self.assertEqual(len(sent), 1)
            fc.fc_crew.write_report(self.proj, "p-tests-001", "working", "test ready: y")
            fc.fc_briefs.collect_tests(self.proj, "001")  # sent back once already: collected
        self.assertTrue((self.repo / "a_test.go").exists())
        self.assertEqual(len(sent), 1)

    def test_a_writer_on_a_turn_after_its_test_ready_report_is_not_collected(self):
        """A correction sent to the writer with `herdr agent prompt` leaves no report: its turn is the only trace."""
        self.start_writers([])
        wt = self.crew_commits("001", "a_test.go", "a\n")
        status = ["working"]
        with mock.patch.object(fc.fc_herdr, "live_agents", lambda: [{"name": "p-tests-001", "agent_status": status[0]}]):
            fc.fc_briefs.collect_tests(self.proj, "001")
            self.assertTrue(wt.exists())  # kept: its writer is fixing the test
            self.assertTrue(self.run("branch", "--list", "*tests-001").stdout.strip())
            self.assertFalse((self.repo / "a_test.go").exists())
            status[0] = "idle"
            fc.fc_briefs.collect_tests(self.proj, "001")
        self.assertTrue((self.repo / "a_test.go").exists())
        self.assertEqual(self.writer_branches(), ["compose/p-tests-002"])

    def test_a_collected_writer_is_not_started_again(self):
        self.start_writers([])
        self.crew_commits("001", "a_test.go", "a\n")
        fc.fc_briefs.collect_tests(self.proj, "001")
        started = []
        self.start_writers(started)
        self.assertEqual(started, ["tests-002"])  # 001's test is on the project branch; 002 was never collected

    def test_a_redone_writer_that_waits_for_another_writer_is_started_by_the_collect(self):
        """A re-plan changed passed tasks 001 and 003, which share a_test.go: --redo starts 001's writer, and the collect starts 003's."""
        self.add_task("003", "a_test.go")
        for tid in ("001", "003"):
            (self.proj / "results" / f"{tid}.json").write_text('{"status": "passed"}')
        (self.proj / "results" / "contract.md").write_text("c")
        started, out = [], io.StringIO()
        crews = (mock.patch.object(fc.fc_herdr, "herdr_space"), mock.patch.object(fc.fc_crew, "cmd_crew", lambda ns: started.append(ns.role)))
        with crews[0], crews[1], mock.patch("sys.stdout", out):
            fc.fc_briefs.cmd_testcrews(SimpleNamespace(repo=str(self.repo), project="p", contract=None, only="001,003", print_=False, collect=False,
                                                       budget=None, redo=True))
        self.assertEqual(started, ["tests-001"])
        self.assertIn("tests-003 waits for tests-001", out.getvalue())
        self.crew_commits("001", "a_test.go", "a\n", state="done")
        with crews[0], crews[1], mock.patch("sys.stdout", io.StringIO()):
            fc.fc_briefs.collect_tests(self.proj)
        self.assertEqual(started, ["tests-001", "tests-003"])

    def capped_writers(self, started, cap=1):
        (self.proj / "results" / "contract.md").write_text("c")
        crews = (mock.patch.object(fc.fc_herdr, "herdr_space"), mock.patch.object(fc.fc_crew, "cmd_crew", lambda ns: started.append(ns.role)),
                 mock.patch.object(fc.fc_config, "PARALLEL", cap), mock.patch("sys.stdout", io.StringIO()))
        for c in crews: c.start(); self.addCleanup(c.stop)
        ns = SimpleNamespace(repo=str(self.repo), project="p", contract=None, only=None, print_=False, collect=False, budget=None)
        fc.fc_briefs.cmd_testcrews(ns)

    def test_a_writer_queued_past_the_cap_still_starts_before_a_later_writer_of_its_file(self):
        self.add_task("003", "b_test.go")  # shares b_test.go with 002, which the cap of 1 queues behind 001
        started = []
        self.capped_writers(started)
        self.assertEqual(started, ["tests-001"])
        self.crew_commits("001", "a_test.go", "a\n", state="done")
        fc.fc_briefs.collect_tests(self.proj)
        self.assertEqual(started, ["tests-001", "tests-002"])  # 003 waits for 002, not the other way round

    def test_collect_never_starts_writers_past_the_cap(self):
        self.add_task("003", "c_test.go")
        started = []
        self.capped_writers(started)
        fc.fc_briefs.collect_tests(self.proj)  # 001 is still writing: its branch is open, so the cap of 1 is full
        self.assertEqual(started, ["tests-001"])
        self.crew_commits("001", "a_test.go", "a\n", state="done")
        fc.fc_briefs.collect_tests(self.proj)
        self.assertEqual(started, ["tests-001", "tests-002"])

    def test_writers_of_a_protected_file_that_already_exists_start_together(self):
        (self.repo / "a_test.go").write_text("pinned\n"); self.run("add", "a_test.go"); self.run("commit", "-qm", "stubs")  # groundwork wrote it, no writer edits it
        self.add_task("003", "c_test.go a_test.go", after="001", test="go test ./c_test.go")  # its test runs c_test.go: a_test.go only pins names
        started = []
        out = self.start_writers(started)
        self.assertEqual(sorted(started), ["tests-001", "tests-002", "tests-003"])
        self.assertNotIn("waits for", out)

    def test_writers_whose_tests_run_one_existing_file_start_one_after_another(self):
        self.main_commits("a_test.go", "package a\n", "stubs")  # each writer appends its test to it: started together, the second collect conflicted
        self.add_task("003", "a_test.go", test="go test ./a_test.go -run C")
        started = []
        out = self.start_writers(started)
        self.assertEqual(sorted(started), ["tests-001", "tests-002"])
        self.assertIn("tests-003 waits for tests-001", out)

    def test_collect_skips_a_writer_that_has_not_reported_test_ready(self):
        fc.fc_crew.write_report(self.proj, "p-tests-001", "working", "started")
        wt = fc.fc_briefs.test_worktree(self.proj, "001")  # started, nothing committed yet
        self.crew_commits("002", "b_test.go", "b\n", state=None)
        fc.fc_crew.write_report(self.proj, "p-tests-002", "done", "d")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            fc.fc_briefs.collect_tests(self.proj)
        self.assertIn("tests-001 skipped", out.getvalue())
        self.assertTrue(wt.exists())  # its worktree and branch stay for the writer still in it
        self.assertEqual(self.writer_branches(), ["compose/p-tests-001"])
        self.assertTrue((self.repo / "b_test.go").exists())

    def test_a_collected_writer_counts_as_done_so_its_ended_session_is_no_stall(self):
        self.crew_commits("001", "a_test.go", "a\n")  # test ready, still working: collect ends its session
        self.crew_commits("002", "b_test.go", "b\n", state="done")
        for n in ("p-tests-001", "p-tests-002"):
            (self.proj / "results" / "crew" / f"{n}.json").write_text(json.dumps({"name": n, "space": "w9"}))
        with mock.patch("sys.stdout", io.StringIO()):
            fc.fc_briefs.collect_tests(self.proj)
        self.assertEqual([r["state"] for r in fc.fc_crew.crew_reports(self.proj, "p-tests-001")], ["working", "done"])
        self.assertEqual(len(fc.fc_crew.crew_reports(self.proj, "p-tests-002")), 1)  # already done: nothing added
        with mock.patch.object(fc.fc_herdr, "herdr", return_value={"agents": []}):
            snap = fc.fc_watch.watch_snapshot(self.repo, self.proj)
        self.assertEqual(snap["closed"], ["p-tests-001", "p-tests-002"])
        self.assertEqual([e for e in fc.fc_watch.watch_stalls(self.proj, snap, 240) if "stall=ended" in e], [])

    def test_a_collected_writer_has_its_tab_closed_once_and_a_writer_not_collected_keeps_it(self):
        self.crew_commits("001", "a_test.go", "a\n")
        for n, tab in (("p-tests-001", "w9:t5"), ("p-tests-002", "w9:t6")):
            (self.proj / "results" / "crew" / f"{n}.json").write_text(json.dumps({"name": n, "space": "w9", "tab": tab}))
        with mock.patch("sys.stdout", io.StringIO()), mock.patch.object(fc.fc_herdr, "herdr") as herdr:
            fc.fc_briefs.collect_tests(self.proj)
            fc.fc_briefs.collect_tests(self.proj)
        self.assertEqual([c.args for c in herdr.call_args_list if c.args[:2] == ("tab", "close")], [("tab", "close", "w9:t5")])
        self.assertIn("collected", fc.fc_crew.crew_record(self.proj, "p-tests-001")["closed"]["why"])
        self.assertNotIn("closed", fc.fc_crew.crew_record(self.proj, "p-tests-002"))

    def test_each_crew_has_its_own_branch_and_collect_lands_them_in_order(self):
        self.crew_commits("002", "b_test.go", "b\n")
        self.crew_commits("001", "a_test.go", "a\n")
        self.assertEqual(self.run("branch", "--show-current").stdout.strip(), "main")  # the project checkout never moved
        fc.fc_briefs.collect_tests(self.proj)
        log = self.run("log", "--format=%s", "-3").stdout.split("\n")
        self.assertEqual(log[:2], ["Test b_test.go", "Test a_test.go"])  # 001 first, then 002
        self.assertTrue((self.repo / "a_test.go").exists() and (self.repo / "b_test.go").exists())
        self.assertEqual(self.writer_branches(), [])
        self.assertFalse((self.proj / "worktrees" / "tests-001").exists())

    def test_run_refuses_a_task_whose_protected_test_is_not_on_the_project_branch(self):
        self.main_commits("a_test.go", "a\n", "Test a")
        with mock.patch.object(fc.fc_run, "run_tasks") as rt, self.assertRaises(SystemExit) as e:
            fc.fc_run.cmd_run(SimpleNamespace(repo=str(self.repo), project="p", only=None, redo=False, no_stack=False))
        rt.assert_not_called()
        self.assertIn("002 b_test.go", str(e.exception))
        self.assertNotIn("a_test.go", str(e.exception))
        with mock.patch.object(fc.fc_run, "run_tasks") as rt:
            fc.fc_run.cmd_run(SimpleNamespace(repo=str(self.repo), project="p", only="001", redo=False, no_stack=False))
        rt.assert_called_once()

    def test_a_conflict_names_the_task_and_keeps_its_branch(self):
        self.crew_commits("001", "a.txt", "teammate\n")
        self.crew_commits("002", "b_test.go", "b\n")
        (self.repo / "a.txt").write_text("main\n"); self.run("commit", "-qam", "main moved")
        with self.assertRaises(SystemExit) as e:
            fc.fc_briefs.collect_tests(self.proj)
        self.assertIn("tests-001", str(e.exception))
        self.assertEqual(self.writer_branches(), ["compose/p-tests-001"])
        self.assertTrue((self.repo / "b_test.go").exists())  # the writers after it are still collected, their worktrees freed
        self.assertEqual(self.run("status", "--porcelain").stdout.strip(), "")  # the cherry-pick was aborted cleanly

    def main_commits(self, name, text, msg):
        (self.repo / name).write_text(text); self.run("add", name); self.run("commit", "-qm", msg)

    def test_commits_already_picked_by_hand_or_by_patch_are_skipped_and_the_next_task_lands(self):
        f = self.proj / "results" / "tasks.json"
        f.write_text(json.dumps({"feature": {}, "tasks": json.loads(f.read_text())["tasks"] + [{"id": "003", "title": "C", "files": [], "protected": ["c_test.go"], "test": "true", "body": "c"}]}))
        self.main_commits("t.py", "import a, b\n\ndef one():\n    pass\n", "base")
        self.crew_commits("001", "t.py", "import a, b, c\n\ndef one():\n    pass\n\ndef two():\n    pass\n")
        self.crew_commits("002", "b_test.go", "b\n")
        self.main_commits("t.py", "import a, b, d\n\ndef one():\n    pass\n\ndef three():\n    pass\n", "another task's test")
        with self.assertRaises(subprocess.CalledProcessError):
            self.run("cherry-pick", "compose/p-tests-001")  # the team lead picks it by hand and resolves the conflict
        (self.repo / "t.py").write_text("import a, b, c, d\n\ndef one():\n    pass\n\ndef three():\n    pass\n\ndef two():\n    pass\n")
        self.run("add", "t.py"); self.run("-c", "core.editor=true", "cherry-pick", "--continue")
        trimmed = "import a, b, c, d\n\ndef one():\n    pass\n\ndef three():\n    pass\n"
        self.main_commits("t.py", trimmed, "Drop test two")  # then edits it on the branch: a replay would bring it back
        self.run("cherry-pick", "compose/p-tests-002")  # a clean hand pick: same patch
        self.crew_commits("003", "c_test.go", "c\n")
        fc.fc_briefs.collect_tests(self.proj)
        self.assertEqual((self.repo / "t.py").read_text(), trimmed)
        self.assertEqual(self.run("log", "--format=%s", "-2").stdout.split("\n")[:2], ["Test c_test.go", "Test b_test.go"])
        self.assertEqual(self.run("status", "--porcelain").stdout.strip(), "")
        self.assertEqual(self.writer_branches(), [])



def server(cwd, port):
    """A process standing in for a server with working directory `cwd`: the fake lsof reports it listening on `port`, and the test's end kills it."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hermetic.listen(p.pid, port, Path(cwd).resolve())
    return p


class Leftovers(unittest.TestCase):
    """Servers a teammate or run leaves in the project end with teardown; anything else is left alone."""
    def test_teardown_stops_servers_in_the_project_only(self):
        proj_wt = Path(tempfile.mkdtemp()).resolve()
        proj = proj_wt / ".compose" / "p"
        scratch = fc.fc_crew.scratch_dir(proj, "p-groundwork"); scratch.mkdir(parents=True)
        mine, other = server(scratch, 8001), server(tempfile.mkdtemp(), 8002)
        self.addCleanup(lambda: [(p.kill(), p.wait()) for p in (mine, other)])
        with mock.patch.object(fc.fc_herdr, "project_worktree", return_value=proj_wt), mock.patch.dict(os.environ, {"HERDR_ENV": ""}), \
                mock.patch("sys.stdout", io.StringIO()):
            fc.fc_crew.teardown(proj)
        self.assertIsNotNone(mine.wait(timeout=5))
        self.assertIsNone(other.poll())
        self.assertTrue(fc.fc_crew.scratch_dir(proj).exists())

    def test_a_stage_ends_only_its_own_new_daemons(self):
        def spawn(name):
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "browser_harness.daemon"], env={**os.environ, "BU_NAME": name})
            self.addCleanup(lambda: (p.kill(), p.wait()))
            return p
        mine_name, other_name = f"fc-test-{os.getpid()}-a", f"fc-test-{os.getpid()}-b"  # unique: suites may run at once
        shared = spawn(mine_name)  # running before the stage under its name (shared through e2e_env): kept
        time.sleep(0.2)
        before = fc.fc_jev.harness_daemons(mine_name)
        mine, other = spawn(mine_name), spawn(other_name)  # another project's stage starts one meanwhile: kept
        time.sleep(0.2)
        self.assertEqual(fc.fc_jev.end_new_daemons(mine_name, before), {str(mine.pid)})
        self.assertIsNotNone(mine.wait(timeout=5))
        self.assertIsNone(shared.poll())
        self.assertIsNone(other.poll())


class StartOver(unittest.TestCase):
    def test_delete_unmerged_removes_the_project_folder_with_its_ignored_files(self):
        r, run = repo()
        env = {**os.environ, **ENV}
        subprocess.run([CLI, "init", "--project", "p", "--repo", str(r)], check=True, capture_output=True, env=env)
        wt = r.parent / "compose-p"
        (wt / ".compose" / "p" / "results" / "x.json").parent.mkdir(parents=True, exist_ok=True)
        (wt / ".compose" / "p" / "results" / "x.json").write_text("{}")
        (wt / "feature.txt").write_text("f\n")
        subprocess.run(["git", "add", "feature.txt"], cwd=wt, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "feature"], cwd=wt, check=True, env=env)
        subprocess.run([CLI, "clean", "--project", "p", "--delete-unmerged", "--repo", str(r)], check=True, capture_output=True, env=env)
        self.assertFalse(wt.exists())
        self.assertEqual(run("branch", "--list", "compose/p").stdout.strip(), "")


class CleanSafety(unittest.TestCase):
    """A repo-wide clean only removes projects whose work is provably done, and never one with live crew."""
    def setUp(self):
        self.r, self.run = repo()
        self.env = {**os.environ, **ENV}
        for n in ("empty", "work"):
            subprocess.run([CLI, "init", "--project", n, "--repo", str(self.r)], check=True, capture_output=True, env=self.env)
        self.wt = {n: self.r.parent / f"compose-{n}" for n in ("empty", "work")}
        (self.wt["work"] / "f.txt").write_text("f\n")
        subprocess.run(["git", "add", "-A"], cwd=self.wt["work"], check=True, env=self.env)
        subprocess.run(["git", "commit", "-qm", "work"], cwd=self.wt["work"], check=True, env=self.env)
        self.run("merge", "-q", "--ff-only", "compose/work")

    def clean(self, *args):
        return subprocess.run([CLI, "clean", "--repo", str(self.r), *args], capture_output=True, text=True, env=self.env)

    def crew(self, n, state):
        d = self.wt[n] / ".compose" / n / "results" / "crew"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{n}-b1.json").write_text(json.dumps({"name": f"{n}-b1", "title": "b1"}))
        (d / f"{n}-b1.status").write_text(json.dumps({"state": state, "message": "m"}) + "\n")

    def test_repo_wide_clean_removes_the_merged_project_unless_crew_is_live_and_keeps_the_empty_one(self):
        self.crew("work", "working")
        self.assertIn("live crew", self.clean().stdout)
        self.assertTrue(self.wt["work"].exists())
        self.clean("--force")  # --force alone never overrides a repo-wide clean
        self.assertTrue(self.wt["work"].exists())
        self.crew("work", "done")  # a done teammate is not live crew
        out = self.clean().stdout
        self.assertFalse(self.wt["work"].exists())
        self.assertTrue(self.wt["empty"].exists())
        self.assertIn("no commits of its own", out)
        self.clean("--project", "empty")
        self.assertFalse(self.wt["empty"].exists())


class AbandonUnmerged(unittest.TestCase):
    """`compose clean --abandon --project <slug>` drops an unmerged project on an explicit flag: it lists what goes and what stays, then removes."""
    def setUp(self):
        self.r, self.run = repo()
        self.env = {**os.environ, **ENV}
        subprocess.run([CLI, "init", "--project", "p", "--repo", str(self.r)], check=True, capture_output=True, env=self.env)
        self.wt = self.r.parent / "compose-p"; self.proj = self.wt / ".compose" / "p"
        self.run("checkout", "-q", "-b", "compose/p-001", cwd=self.wt)
        (self.wt / "f.txt").write_text("f\n"); self.run("add", "f.txt", cwd=self.wt); self.run("commit", "-qm", "Add f", cwd=self.wt)
        self.run("checkout", "-q", "compose/p", cwd=self.wt); self.run("merge", "-q", "--no-ff", "-m", "stack", "compose/p-001", cwd=self.wt)
        (self.wt / "wip.txt").write_text("not committed\n")
        self.remote = self.r.parent / "remote.git"  # the project branch was pushed once: that copy outlives the abandon
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.run("remote", "add", "origin", str(self.remote)); self.run("push", "-q", "origin", "compose/p")
        self.run("fetch", "-q", "origin")

    def cli(self, *args, cwd=None):
        return subprocess.run([CLI, "clean", "--repo", str(self.r), *args], cwd=cwd or self.r, capture_output=True, text=True, env=self.env)

    def branches(self):
        return self.run("branch", "--format=%(refname:short)").stdout.split()

    def test_abandon_is_explicit_refuses_when_unsafe_and_otherwise_removes_only_local_state(self):
        for args in (("--project", "p"), ("--project", "p", "--dry-run"), ()):  # without the flag an unmerged project is kept
            self.cli(*args)
            self.assertTrue(self.wt.exists(), args); self.assertIn("compose/p", self.branches())
        for args, why in (("--abandon",), "names one project"), (("--abandon", "--project", "nope"), "known"), \
                (("--abandon", "--project", "p", "--delete-unmerged"), "names one project"):
            p = self.cli(*args)
            self.assertEqual(p.returncode, 1, args); self.assertIn(why, p.stdout + p.stderr); self.assertTrue(self.wt.exists())
        p = self.cli("--abandon", "--project", "p", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        removes, keeps = p.stdout.split("abandon  keeps")
        for part in (f"worktree {self.wt}", "1 uncommitted path(s)", "local branches compose/p", "herdr tabs"):
            self.assertIn(part, removes)
        for part in ("Linear issues, untouched", "origin/compose/p", "GitHub PRs"):
            self.assertIn(part, keeps)
        self.assertTrue((self.wt / "wip.txt").exists()); self.assertEqual(sorted(self.branches()), ["compose/p", "main"])
        sleeper = subprocess.Popen(["sleep", "60"]); self.addCleanup(sleeper.wait); self.addCleanup(sleeper.kill)
        (self.proj / "results").mkdir(exist_ok=True)
        (self.proj / "results" / "run.json").write_text(json.dumps({"pid": sleeper.pid}))
        for force in ((), ("--force",)):  # --force never overrides a live run
            p = self.cli("--abandon", "--project", "p", *force)
            self.assertEqual(p.returncode, 1); self.assertIn(f"a run is live (pid {sleeper.pid})", p.stdout + p.stderr); self.assertTrue(self.wt.exists())
        (self.proj / "results" / "run.json").unlink()
        p = self.cli("--abandon", "--project", "p", cwd=self.wt)
        self.assertEqual(p.returncode, 1); self.assertIn("this command runs inside it", p.stdout + p.stderr); self.assertTrue(self.wt.exists())
        d = self.proj / "results" / "crew"; d.mkdir(parents=True, exist_ok=True)
        (d / "p-b1.json").write_text(json.dumps({"name": "p-b1", "title": "b1"}))
        (d / "p-b1.status").write_text(json.dumps({"state": "working", "message": "m"}) + "\n")
        p = self.cli("--abandon", "--project", "p")
        self.assertEqual(p.returncode, 1); self.assertIn("live crew", p.stdout + p.stderr); self.assertTrue(self.wt.exists())
        p = self.cli("--abandon", "--project", "p", "--force")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"abandon  p: removed worktree {self.wt} and 1 local branch(es)", p.stdout)
        self.assertFalse(self.wt.exists()); self.assertEqual(self.branches(), ["main"])
        self.assertIn("compose/p", subprocess.run(["git", "-C", str(self.remote), "branch", "--list"], capture_output=True, text=True).stdout)
        self.assertEqual(self.run("worktree", "list", "--porcelain").stdout.count("worktree "), 1)
        self.assertEqual(self.run("status", "--porcelain").stdout, "")
        self.assertEqual(self.run("log", "--format=%s").stdout.split("\n")[0], "init")


class SharedSpaceClean(unittest.TestCase):
    """In the space compose was started from, clean closes the project's tabs, never the space, and other agents there are not its crew."""
    def herdr(self, *args, check=False, timeout=120):
        self.calls.append(args)
        if args[:2] == ("workspace", "list"):
            return {"workspaces": getattr(self, "spaces", [{"workspace_id": "w7", "label": "shop"}])}
        if args[:2] == ("tab", "list"):
            tabs = getattr(self, "tabs", (("t1", "1"), ("t2", "shop shell"), ("t3", "shop status"), ("t4", "cart status")))
            return {"tabs": [{"tab_id": t, "label": l} for t, l in tabs]}
        if args[:2] == ("agent", "list"):
            return {"agents": [{"name": "manager", "tab_id": "t1", "workspace_id": "w7"}, {"name": "cart-orchestrator", "tab_id": "t4", "workspace_id": "w7"}]}
        return {}

    def test_a_teammate_whose_tab_closed_while_working_is_not_live_crew(self):
        wt = Path(tempfile.mkdtemp()); proj = wt / ".compose" / "shop"
        crew = proj / "results" / "crew"; crew.mkdir(parents=True)
        fc.fc_herdr.space_record(proj).write_text(json.dumps({"space": "w7"}))
        for n in ("shop-tests-002", "manager"):  # herdr lists no shop-tests-002: its session ended after it reported
            (crew / f"{n}.json").write_text(json.dumps({"name": n, "agent": n, "title": n, "space": "w7", "tab": "t9"}))
            (crew / f"{n}.status").write_text(json.dumps({"state": "working", "message": "test ready"}) + "\n")
        self.calls = []
        with mock.patch.object(fc.fc_herdr, "herdr", self.herdr), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True):
            self.assertEqual(fc.fc_clean.live_crew(proj), ["manager last reported working"])

    def test_clean_closes_the_projects_tabs_and_counts_only_its_agents(self):
        wt = Path(tempfile.mkdtemp()); proj = wt / ".compose" / "shop"
        (proj / "results").mkdir(parents=True)
        fc.fc_herdr.space_record(proj).write_text(json.dumps({"space": "w7"}))
        self.calls = []
        with mock.patch.object(fc.fc_herdr, "herdr", self.herdr), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True):
            self.assertEqual(fc.fc_clean.live_crew(proj), [])
            fc.fc_clean.close_space(proj, "shop")
        self.assertEqual([c for c in self.calls if c[0] in ("tab", "workspace") and c[1] == "close"], [("tab", "close", "t2"), ("tab", "close", "t3")])

    def clean(self, proj, crew):
        """crew maps a teammate to (its tab, its reports oldest first); returns the close calls and the `clean left` lines."""
        d = fc.fc_crew.crew_dir(proj)
        for n, (tab, states) in crew.items():
            (d / f"{n}.json").write_text(json.dumps({"name": n, "title": "t", "space": "w7", "tab": tab}))
            for st in states: fc.fc_crew.write_report(proj, n, st, "m")
        self.calls, out = [], io.StringIO()
        with mock.patch.object(fc.fc_herdr, "herdr", self.herdr), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch("sys.stdout", out):
            fc.fc_clean.close_space(proj, "shop")
        return [c for c in self.calls if c[0] in ("tab", "workspace") and c[1] == "close"], sorted(l for l in out.getvalue().splitlines() if "left" in l)

    def test_clean_keeps_the_tabs_of_teammates_whose_last_report_is_not_done(self):
        proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        (proj / "results").mkdir(parents=True)
        fc.fc_herdr.space_record(proj).write_text(json.dumps({"space": "w7"}))
        lead = fc.fc_herdr.agent_name("shop", "orchestrator")
        self.tabs = (("t1", "1"), ("t2", "shop shell"), ("t3", "shop status"), ("t4", "cart status"), ("t5", "shop tests-001"),
                     ("t6", "shop tests-002"), ("t7", "shop tests-003"), ("t8", "shop orchestrator"), ("t9", "shop tests-004"))
        closes, lines = self.clean(proj, {"shop-tests-001": ("t5", ["needs-user"]), "shop-tests-002": ("t6", ["done", "working"]),
                                          "shop-tests-003": ("t7", ["working", "done"]), lead: ("t8", ["blocked"]),
                                          "shop-tests-004": ("t9", []), "shop-tests-005": ("t99", ["working"])})
        self.assertEqual(closes, [("tab", "close", "t2"), ("tab", "close", "t3"), ("tab", "close", "t7")])
        self.assertEqual(lines, sorted([f"clean left {lead} open (last report: blocked)", "clean left shop-tests-001 open (last report: needs-user)",
                                        "clean left shop-tests-002 open (last report: working)", "clean left shop-tests-004 open (last report: nothing)"]))

    def test_a_space_of_its_own_stays_open_while_a_tab_is_kept(self):
        wt = Path(tempfile.mkdtemp()).resolve(); proj = wt / ".compose" / "shop"
        (proj / "results").mkdir(parents=True)
        self.spaces = [{"workspace_id": "w8", "label": "shop", "worktree": {"checkout_path": str(wt)}}]
        self.tabs = (("t1", "shell"), ("t2", "status"), ("t3", "tests-001"))
        closes, lines = self.clean(proj, {"shop-tests-001": ("t3", ["done"])})
        self.assertEqual((closes, lines), ([("workspace", "close", "w8")], []))
        closes, lines = self.clean(proj, {"shop-tests-001": ("t3", ["done", "working"])})
        self.assertEqual(closes, [("tab", "close", "t1"), ("tab", "close", "t2")])
        self.assertEqual(lines, ["clean left shop-tests-001 open (last report: working)"])


if __name__ == "__main__":
    unittest.main()
