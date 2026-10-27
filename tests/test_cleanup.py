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
