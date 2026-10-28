import hermetic  # first: no test sees the real home
import io, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
from cleanup_kit import fc
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""


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


class CleanDecisions(unittest.TestCase):
    """The rules clean and abandon decide on, as facts in and a decision out."""
    def test_live_crew_holds_a_clean_unless_the_project_is_named_and_forced(self):
        c = fc.fc_clean.crew_holds_clean
        self.assertEqual([c(["a last reported working"], f, e) for f, e in ((0, 0), (1, 0), (0, 1), (1, 1))], [True, True, True, False])
        self.assertFalse(c([], False, False))

    def test_a_branch_with_no_proof_stays_unless_unmerged_work_is_to_be_deleted(self):
        f = fc.fc_clean.branch_fate
        self.assertEqual(f(None, [], {}, False), (None, False))
        self.assertEqual(f(None, [], {}, True), (None, True))
        self.assertEqual(f("contained in main", [], {}, False), ("contained in main", True))

    def test_a_task_branch_goes_with_the_layers_that_held_it_only_when_all_are_merged(self):
        f = fc.fc_clean.branch_fate
        self.assertEqual(f(None, ["l1", "l2"], {"l1": "PR #3 merged 2026-01-01", "l2": "x"}, False), ("held by l1, l2, merged: PR #3 merged 2026-01-01", True))
        self.assertEqual(f(None, ["l1", "l2"], {"l1": "x"}, False), (None, False))

    def test_an_empty_project_is_kept_unless_named_or_deleted_as_unmerged(self):
        f = fc.fc_clean.project_proof
        self.assertEqual(f("contained in main", False, False, False, {}, {}), (None, False, True))
        self.assertEqual(f("contained in main", False, True, False, {}, {}), ("contained in main", True, False))
        self.assertEqual(f("contained in main", False, False, True, {}, {}), ("contained in main", True, False))

    def test_a_project_without_proof_goes_only_on_delete_unmerged_or_when_every_layer_is_merged(self):
        f = fc.fc_clean.project_proof
        self.assertEqual(f(None, True, True, False, {}, {}), (None, False, False))
        self.assertEqual(f(None, True, True, True, {}, {}), ("UNMERGED, removed on --delete-unmerged", False, False))
        self.assertEqual(f(None, True, False, False, {"l1": ["001"]}, {"l1": "x"}), ("every layer built from it is merged (l1)", True, False))
        self.assertEqual(f(None, True, False, False, {"l1": ["001"]}, {}), (None, False, False))

    def test_a_run_inside_the_worktree_stands_in_it(self):
        root = Path("/w/p").resolve()
        self.assertTrue(fc.fc_clean.runs_inside(root, root))
        self.assertTrue(fc.fc_clean.runs_inside(root / "src" / "x", root))
        self.assertFalse(fc.fc_clean.runs_inside(root.parent, root))
        self.assertFalse(fc.fc_clean.runs_inside(None, root))

    def test_a_team_lead_still_working_keeps_the_worktree_until_it_reports_done(self):
        f = fc.fc_clean.lead_working
        rec = {"name": "lead", "tab": "t1"}
        self.assertTrue(f(rec, {"state": "working"}, False))
        self.assertTrue(f(rec, {}, False))
        self.assertFalse(f(rec, {"state": "done"}, False))
        self.assertFalse(f({**rec, "closed": True}, {"state": "working"}, False))
        self.assertFalse(f(None, {}, False))
        self.assertFalse(f(rec, {"state": "working"}, True))

    def test_abandon_refuses_a_live_run_and_live_crew_unless_forced(self):
        f = fc.fc_clean.live_run_or_crew
        self.assertEqual(f(None, False, [], False), [])
        self.assertEqual(f(42, True, [], False), ["a run is live (pid 42): stop it first"])
        self.assertEqual(f(None, False, ["a last reported working", "b is alive in its herdr space"], False),
                         ["live crew (a last reported working; b is alive in its herdr space); --force overrides"])
        self.assertEqual(f(None, False, ["a last reported working"], True), [])
        self.assertEqual(f(42, True, ["a last reported working"], True), ["a run is live (pid 42): stop it first"])

    def test_abandon_lists_what_goes_and_what_stays_then_stops_when_unsafe(self):
        proj = Path(tempfile.mkdtemp()) / "p"; wt = proj.parent / "wt"
        out = io.StringIO()
        sh = mock.Mock(return_value=mock.Mock(stdout="", returncode=0))
        with mock.patch.object(fc.fc_clean.fc_core, "sh", sh), mock.patch.object(fc.fc_clean.fc_core, "project_branch", return_value="compose/p"), \
             mock.patch.object(fc.fc_clean.fc_worktree, "own_branches", return_value=[]), mock.patch.object(fc.fc_clean.fc_worktree, "slots", return_value={}), \
             mock.patch.object(fc.fc_clean.fc_worktree, "review_base", return_value=None), mock.patch.object(fc.fc_clean.fc_worktree, "is_slot", return_value=False), \
             mock.patch.object(fc.fc_clean.fc_layers, "layer_prefix", return_value=""), mock.patch.object(fc.fc_clean, "live_crew", return_value=["a last reported working"]), \
             mock.patch.object(fc.fc_clean.fc_tasks, "run_alive", return_value=False), mock.patch.object(fc.fc_clean, "standing_in", return_value=["this command runs inside it"]), \
             mock.patch("sys.stdout", out), self.assertRaises(SystemExit) as stop:
            fc.fc_clean.abandon_project(wt.parent, "p", wt, proj)
        said = out.getvalue()
        self.assertIn("abandon  p: removes", said); self.assertIn("abandon  keeps", said); self.assertIn("Linear issues, untouched", said)
        self.assertIn("live crew (a last reported working); --force overrides; this command runs inside it", str(stop.exception))
        self.assertIn("nothing removed", str(stop.exception))
        self.assertEqual([c for c in sh.call_args_list if c.args[0][:2] in (["git", "branch"], ["git", "worktree"])], [])


class CleanEnds(unittest.TestCase):
    """The end of a clean: Linear first, the worktree after."""
    def test_linear_rolls_up_before_the_worktree_goes(self):
        order, m = [], mock.Mock()
        with mock.patch.object(fc.fc_clean.fc_linear, "linear_merged", lambda *a, **k: order.append("linear")), \
             mock.patch.object(fc.fc_clean.fc_linear, "refresh_ready", lambda *a, **k: order.append("ready")), \
             mock.patch.object(fc.fc_clean, "remove_project", lambda *a, **k: order.append("remove")), mock.patch("sys.stdout", io.StringIO()):
            fc.fc_clean.settle_clean("/m", "p", "/w", "/proj", "compose/p", {"feature": "ENG-1"}, {"001": {"id": "001"}}, ["001"], True, True, "line")
        self.assertEqual(order, ["linear", "ready", "remove"])

    def test_a_kept_worktree_is_not_removed_but_linear_still_rolls_up(self):
        order = []
        with mock.patch.object(fc.fc_clean.fc_linear, "linear_merged", lambda *a, **k: order.append("linear")), \
             mock.patch.object(fc.fc_clean.fc_linear, "refresh_ready", lambda *a, **k: order.append("ready")), \
             mock.patch.object(fc.fc_clean, "remove_project", lambda *a, **k: order.append("remove")), mock.patch("sys.stdout", io.StringIO()):
            fc.fc_clean.settle_clean("/m", "p", "/w", "/proj", "compose/p", {}, {}, [], False, False, "line")
        self.assertEqual(order, ["linear", "ready"])

    def test_a_merged_project_holds_the_code_of_every_finished_task(self):
        f = fc.fc_clean.merged_by_project
        self.assertEqual(f({"001": 1, "002": 1, "003": 1, "004": 1}, ["001"], {"001": "done", "002": "done", "003": "already_green", "004": None}, ("done", "already_green")), ["002", "003"])


class MergeProof(unittest.TestCase):
    """A branch counts as merged only with proof; a kept branch names its first unmatched commit."""
    def test_every_patch_in_the_target_proves_it(self):
        f = fc.fc_clean.patch_merged
        self.assertTrue(f("- a\n- b\n"))
        self.assertFalse(f("- a\n+ b\n"))
        self.assertFalse(f(""))

    def test_a_merge_that_gives_the_targets_own_tree_proves_a_recut_stack_merged(self):
        f = fc.fc_clean.changes_nothing
        self.assertTrue(f("abc123\n", "abc123"))
        self.assertFalse(f("def456\n", "abc123"))
        self.assertFalse(f("", "abc123"))

    def test_a_merged_pull_request_proves_a_squash_merge(self):
        f = fc.fc_clean.pr_evidence
        self.assertEqual(f(json.dumps([{"number": 7, "mergedAt": "2026-03-04T10:00:00Z"}])), "PR #7 merged 2026-03-04")
        self.assertIsNone(f("[]")); self.assertIsNone(f("")); self.assertIsNone(f("not json"))

    def test_no_proof_means_not_merged(self):
        def sh(args, *a, **k):
            return mock.Mock(returncode=1 if args[1] in ("rev-parse", "merge-base") else 0, stdout="")
        with mock.patch.object(fc.fc_clean.fc_core, "sh", sh):
            self.assertIsNone(fc.fc_clean.merge_evidence("/r", "b", ["main"], False))
            self.assertFalse(fc.fc_clean.merged_into("/r", "b", ["main"]))

    def test_a_squash_merged_branch_is_proved_by_its_pull_request(self):
        def sh(args, *a, **k):
            return mock.Mock(returncode=1 if args[1] in ("merge-base", "merge-tree") else 0, stdout="x\n" if args[1] == "cherry" else "")
        pr = mock.Mock(returncode=0, stdout=json.dumps([{"number": 9, "mergedAt": "2026-05-06T00:00:00Z"}]))
        with mock.patch.object(fc.fc_clean.fc_core, "sh", sh), mock.patch.object(fc.fc_clean.subprocess, "run", return_value=pr):
            self.assertEqual(fc.fc_clean.merge_evidence("/r", "b", ["main"], True), "PR #9 merged 2026-05-06")
            self.assertIsNone(fc.fc_clean.merge_evidence("/r", "b", ["main"], False))

    def test_the_first_unmatched_commit_is_named_with_how_many_follow(self):
        f = fc.fc_clean.first_plus
        self.assertEqual(f("- aaa one\n+ bbb two words\n+ ccc three\n"), ("bbb", "two words", 1))
        self.assertEqual(f("+ bbb two\n"), ("bbb", "two", 0))
        self.assertIsNone(f("- aaa one\n")); self.assertIsNone(f(""))

    def test_a_kept_branch_names_its_first_unmatched_commit(self):
        def sh(args, *a, **k):
            return mock.Mock(returncode=0, stdout={"cherry": "- a one\n+ bbbbbbb fix it\n+ c more\n", "rev-parse": "bbbbbbb\n"}.get(args[1], ""))
        with mock.patch.object(fc.fc_clean.fc_core, "sh", sh):
            self.assertEqual(fc.fc_clean.first_unmatched("/r", "b", ["main"]), "first unmatched commit bbbbbbb fix it and 1 more")


class BriefDecisions(unittest.TestCase):
    """Who writes tests when: one file, one writer at a time; the cap; collect order; no collect before test ready."""
    def test_writers_of_one_test_file_start_one_after_another(self):
        f = fc.fc_briefs.writer_blockers
        written = {"001": {"t.py"}, "002": {"t.py"}, "003": {"u.py"}}
        self.assertEqual(f(["001", "002", "003"], ["001", "002", "003"], written, {"001", "002", "003"}, set().__contains__), {"002": ["001"]})

    def test_a_writer_waits_for_an_open_one_even_when_later(self):
        f = fc.fc_briefs.writer_blockers
        written = {"001": {"t.py"}, "002": {"t.py"}}
        self.assertEqual(f(["001"], ["001", "002"], written, {"001"}, {"002"}.__contains__), {"001": ["002"]})

    def test_a_writer_whose_own_branch_is_open_waits_for_nothing(self):
        f = fc.fc_briefs.writer_blockers
        self.assertEqual(f(["002"], ["001", "002"], {"001": {"t"}, "002": {"t"}}, {"002"}, {"001", "002"}.__contains__), {})

    def test_a_waiting_earlier_writer_counts_as_queued(self):
        f = fc.fc_briefs.writer_blockers
        self.assertEqual(f(["003"], ["001", "003"], {"001": {"t"}, "003": {"t"}}, {"003", "001"}, set().__contains__), {"003": ["001"]})
        self.assertEqual(f(["003"], ["001", "003"], {"001": {"t"}, "003": {"t"}}, {"003"}, set().__contains__), {})

    def test_a_writer_writes_the_files_its_test_runs_and_those_not_on_the_branch(self):
        f = fc.fc_briefs.written_files
        paths = ["tests/test_a.py", "tests/test_b.py", "tests/pins.py"]
        self.assertEqual(f(paths, "python -m tests.run test_a.py", set()), {"tests/test_a.py"})
        self.assertEqual(f(paths, "python -m tests.run test_a.py", {"tests/pins.py"}), {"tests/test_a.py", "tests/pins.py"})
        self.assertEqual(f(paths, "make", set()), set(paths))

    def test_the_cap_on_started_writers(self):
        f = fc.fc_briefs.writer_cap
        self.assertEqual(f(None, 0, 2, False), 0)
        self.assertEqual(f(None, 5, 2, False), 5)
        self.assertEqual(f(None, 0, 2, True), 2)
        self.assertEqual(f(None, 5, 2, True), 2)
        self.assertEqual(f(None, 1, 2, True), 1)
        self.assertEqual(f(4, 1, 2, True), 4)

    def test_writers_past_the_cap_are_queued_in_task_order(self):
        f = fc.fc_briefs.split_at_cap
        ms = [{"id": "001"}, {"id": "002"}, {"id": "003"}]
        self.assertEqual(f(ms, 0, 0), (ms, []))
        self.assertEqual(f(ms, 2, 0), (ms[:2], ms[2:]))
        self.assertEqual(f(ms, 2, 1), (ms[:1], ms[1:]))
        self.assertEqual(f(ms, 2, 5), ([], ms))

    def test_a_writer_is_collected_only_after_it_reports_test_ready(self):
        f = fc.fc_briefs.test_ready
        self.assertTrue(f({"state": "done"}))
        self.assertTrue(f({"state": "working", "message": "Test READY"}))
        self.assertFalse(f({"state": "working", "message": "still going"}))
        self.assertFalse(f({}))
        self.assertIn("not reported test ready (last: working 'busy')", fc.fc_briefs.not_ready_line("002", {"state": "working", "message": "busy"}))

    def test_commits_already_picked_by_hand_are_skipped_and_the_rest_keep_their_order(self):
        sha = lambda c, who: f"{c}\0{who}"
        picked = [sha("p1", "A <a> 1 s1")]
        log = [sha("c1", "A <a> 1 s1"), sha("c2", "B <b> 2 s2"), sha("c3", "C <c> 3 s3")]
        self.assertEqual(fc.fc_briefs.to_pick(picked, log), ["c2", "c3"])
        self.assertEqual(fc.fc_briefs.to_pick([], []), [])

    def test_a_conflict_names_its_task_and_keeps_its_branch(self):
        line = fc.fc_briefs.conflict_line("002", "abcdef0123", "compose/p-tests-002", "x" * 700 + "CONFLICT")
        self.assertTrue(line.startswith("tests-002: abcdef0 conflicts with the project branch; its branch compose/p-tests-002 is kept.\n"))
        self.assertTrue(line.endswith("CONFLICT")); self.assertLess(len(line), 700)

    def collect(self, states, pick_fail=(), wrote=None):
        proj = Path(tempfile.mkdtemp()) / "p"; proj.mkdir()
        picks = []
        def sh(args, *a, **k):
            if args[1] == "cherry-pick" and args[2] != "--abort":
                picks.append(args[-1]); return mock.Mock(returncode=int(args[-1] in pick_fail), stdout="", stderr="boom")
            if args[1] == "show":
                return mock.Mock(returncode=0, stdout=(wrote or {}).get(args[-1].rsplit("b-", 1)[-1], "") , stderr="")
            out = f"c-{args[-1]}\0w <w> 1 s" if args[1] == "log" and "--right-only" in args else "0" if args[1] == "rev-list" else ""
            return mock.Mock(returncode=0, stdout=out, stderr="")
        reports = lambda p, w: [states[w.rsplit("tests-", 1)[1]]] if w.rsplit("tests-", 1)[1] in states else []
        tasks = [{"id": t, "protected": [f"{t}.test.ts"]} for t in ("003", "001", "002")]
        say, closed = [], []
        with mock.patch.object(fc.fc_briefs.fc_core, "sh", sh), mock.patch.object(fc.fc_briefs.fc_tasks, "load_tasks", return_value=tasks), \
             mock.patch.object(fc.fc_briefs, "test_open", return_value=True), mock.patch.object(fc.fc_briefs.fc_crew, "crew_reports", reports), \
             mock.patch.object(fc.fc_briefs.fc_testred, "gate", return_value=False), mock.patch.object(fc.fc_briefs.fc_herdr, "project_worktree", return_value=proj), \
             mock.patch.object(fc.fc_briefs.fc_worktree, "slots", return_value={}), mock.patch.object(fc.fc_briefs.fc_worktree, "use_treehouse", return_value=False), \
             mock.patch.object(fc.fc_briefs.fc_crew, "write_report"), mock.patch.object(fc.fc_briefs.fc_crew, "close_collected", lambda p, w: closed.append(w)), \
             mock.patch.object(fc.fc_briefs.fc_core, "say", say.append), mock.patch.object(fc.fc_briefs, "test_branch", lambda p, t: f"b-{t}"):
            try:
                fc.fc_briefs.collect_tests_locked(proj, proj, None, False)
                err = None
            except SystemExit as e:
                err = str(e)
        return picks, say, closed, err

    def test_collect_lands_ready_writers_in_task_order_and_skips_the_rest(self):
        ready, working = {"state": "done"}, {"state": "working", "message": "x"}
        picks, say, closed, err = self.collect({"001": ready, "002": working, "003": ready})
        self.assertEqual([c.rsplit("b-", 1)[1] for c in picks], ["001", "003"])
        self.assertTrue(any(l.startswith("tests-002 skipped") for l in say))
        self.assertEqual(len(closed), 2)
        self.assertIsNone(err)

    def test_a_conflict_is_named_at_the_end_while_the_rest_are_collected(self):
        ready = {"state": "done"}
        picks, say, closed, err = self.collect({"001": ready, "002": ready, "003": ready}, pick_fail=("c-HEAD...b-001",))
        self.assertEqual([c.rsplit("b-", 1)[1] for c in picks], ["001", "002", "003"])
        self.assertEqual(len(closed), 2)
        self.assertIn("tests-001:", err)

    def test_a_writer_that_wrote_another_tasks_test_file_is_not_collected_and_its_branch_is_kept(self):
        ready = {"state": "done"}
        picks, say, closed, err = self.collect({"001": ready, "002": ready}, wrote={"001": "001.test.ts\n", "002": "001.test.ts\n"})
        self.assertEqual([c.rsplit("b-", 1)[1] for c in picks], ["001"])
        self.assertEqual(len(closed), 1)
        self.assertIn("tests-002: nothing collected; it wrote 001.test.ts (protected by task 001)", err)


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

    def test_a_space_of_several_projects_never_counts_another_projects_team_lead_as_live_crew(self):
        wt = Path(tempfile.mkdtemp()).resolve(); proj = wt / ".compose" / "shop"
        (proj / "results").mkdir(parents=True)  # no recorded shared space: the tabs carry bare labels, so every tab of the space looks like its own
        self.spaces = [{"workspace_id": "w7", "label": "shop", "worktree": {"checkout_path": str(wt)}}]
        self.tabs = (("t1", "1"), ("t4", "2"))
        self.calls = []
        with mock.patch.object(fc.fc_herdr, "herdr", self.herdr), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True):
            self.assertEqual(fc.fc_clean.live_crew(proj), [])


class RemoteBranches(unittest.TestCase):
    """Clean is the post-merge step, but a delete on the remote is a push: it names the merged layers and pushes only on --delete-remote."""
    def clean(self, **kw):
        wt = Path(tempfile.mkdtemp()).resolve(); proj = wt / ".compose" / "shop"
        (proj / "results").mkdir(parents=True); (proj / "verify.json").write_text("{}")
        calls = []
        def sh(cmd, cwd, **_):
            calls.append(cmd)
            out = {"remote": "origin\n", "for-each-ref": "origin/compose/shop-layer-01\n" if "refs/remotes/*/compose/shop-layer-*" in cmd else ""}.get(cmd[1], "")
            return subprocess.CompletedProcess(cmd, 0, out, "")
        buf = io.StringIO()
        with mock.patch.object(fc.fc_core, "sh", sh), mock.patch.object(fc.fc_clean, "live_crew", return_value=[]), \
                mock.patch.object(fc.fc_clean.fc_layers, "layer_prefix", return_value="compose/shop-"), \
                mock.patch.object(fc.fc_clean, "merge_evidence", return_value="PR merged"), mock.patch.object(fc.fc_clean, "settle_clean"), \
                mock.patch("sys.stdout", buf):
            fc.fc_clean.clean_project(wt, "shop", wt, proj, explicit=True, **kw)
        return calls, buf.getvalue()

    def test_a_merged_layer_stays_on_the_remote_unless_delete_remote_is_given(self):
        calls, out = self.clean()
        self.assertFalse([c for c in calls if c[:2] == ["git", "push"]])
        self.assertIn("origin/compose/shop-layer-01 is merged", out)
        calls, out = self.clean(delete_remote=True)
        self.assertIn(["git", "push", "-q", "origin", "--delete", "compose/shop-layer-01"], calls)


if __name__ == "__main__":
    unittest.main()
