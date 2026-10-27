import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, re, shlex, signal, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

NOW = 1_800_000_000.0
at = lambda ago: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - ago))
ORCH = "hw-orchestrator"


class Stalls(unittest.TestCase):
    """Silence with work pending wakes the supervisor once; expected silence (done, waiting on the product owner) never does."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)

    def snap(self, tasks=("pending",), crew=None, status="working", last_ago=60, run=False, run_end=0, verify=None):
        return {"tasks": {f"00{i}": {"state": s} for i, s in enumerate(tasks, 1)}, "agents": {ORCH: status},
                "crew": {n: [{"at": at(ago), "state": st, "message": "parallel: started m"}] for n, (st, ago) in (crew or {}).items()},
                "run": run, "verify": verify, "run_end": run_end, "last": (NOW - last_ago, "teammate x reported working")}

    def stalls(self, snap, now=NOW):
        return fc.fc_watch.watch_stalls(self.proj, snap, 240, now_s=now)

    def test_quiet_project_fires_once(self):
        s = self.snap(crew={ORCH: ("working", 600)}, last_ago=600)
        ev = self.stalls(s)
        self.assertEqual(len(ev), 1)
        self.assertIn("stall=quiet agent=hw-orchestrator", ev[0])
        self.assertIn("herdr agent read hw-orchestrator --lines 60", ev[0])
        self.assertEqual(self.stalls(s), [])  # the same silence does not wake the supervisor again

    def test_a_team_lead_working_report_with_nothing_parallel_and_no_reason_is_a_stall_the_team_lead_watch_passes(self):
        for message in ("synced, running 001 then 002 then 003", "parallel:", "parallel: none", "parallel: nothing."):
            with self.subTest(message=message):
                self.setUp()
                s = self.snap(crew={ORCH: ("working", 30)}, last_ago=30)
                s["crew"][ORCH][0]["message"] = message
                ev = self.stalls(s)
                self.assertEqual(len(ev), 1, ev)
                self.assertIn("stall=serial agent=hw-orchestrator", ev[0])
                self.assertEqual(fc.fc_watch.watch_filter(ev, "hw", [ORCH]), ev)  # `watch --from orchestrator` wakes the engineering manager
                self.assertEqual(self.stalls(s), [])  # once per report

    def test_a_parallel_clause_that_names_work_or_a_reason_is_not_a_stall(self):
        for message in ("run started; parallel: started 001,002,003; can start now 004 test writer",
                        "parallel: none because 003 imports the type 002 creates", "parallel: none - one task left"):
            with self.subTest(message=message):
                self.setUp()
                s = self.snap(crew={ORCH: ("working", 30)}, last_ago=30)
                s["crew"][ORCH][0]["message"] = message
                self.assertEqual(self.stalls(s), [])
        s = self.snap(crew={"hw-tests-001": ("working", 30)}, last_ago=30)  # only the team lead's reports carry it
        s["crew"]["hw-tests-001"][0]["message"] = "writing tests"
        self.assertEqual(self.stalls(s), [])

    def test_a_project_without_a_team_lead_names_none(self):
        s = self.snap(crew={"hw-designer": ("working", 600)}, last_ago=600)
        s["agents"] = {}
        ev = self.stalls(s)
        self.assertEqual(len(ev), 1)
        self.assertIn("stall=quiet nothing moved", ev[0])
        self.assertNotIn("orchestrator", ev[0])
        self.assertIn("no team lead", ev[0])

    def test_the_team_lead_name_is_the_one_crew_start_gives_it(self):
        self.assertEqual(fc.fc_herdr.agent_name("autofill-hifi-design", "orchestrator"), "autofill-hifi-desig-orchestrator")
        self.assertLessEqual(len(fc.fc_herdr.agent_name("autofill-hifi-design", "orchestrator")), 32)

    def test_recent_activity_is_not_a_stall(self):
        self.assertEqual(self.stalls(self.snap(crew={ORCH: ("working", 60)}, last_ago=60)), [])

    def test_done_or_waiting_on_the_product_owner_is_not_a_stall(self):
        self.assertEqual(self.stalls(self.snap(crew={ORCH: ("done", 900)}, last_ago=900)), [])
        self.assertEqual(self.stalls(self.snap(crew={ORCH: ("needs-user", 900)}, last_ago=900)), [])

    def test_test_crews_done_and_nothing_dispatched(self):
        crew = {ORCH: ("working", 100), "hw-tests-001": ("done", 500), "hw-tests-002": ("done", 400)}
        ev = self.stalls(self.snap(crew=crew, last_ago=100))
        self.assertTrue(any("stall=undispatched" in e for e in ev), ev)

    def test_a_teammate_whose_tab_closed_is_not_working(self):
        """tests-002's tab closed before its `done` report landed: its last report says working."""
        s = self.snap(tasks=("passed",), crew={ORCH: ("working", 600), "hw-tests-002": ("working", 600)}, last_ago=600)
        s["closed"] = ["hw-tests-002"]
        ev = self.stalls(s)
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("stall=ended agent=hw-tests-002", ev[0])
        crew = {ORCH: ("working", 100), "hw-tests-001": ("done", 500), "hw-tests-002": ("working", 400)}
        s = self.snap(crew=crew, last_ago=100); s["closed"] = ["hw-tests-002"]
        ev = self.stalls(s)
        self.assertTrue(any("stall=undispatched" in e and "teammates working none" in e for e in ev), ev)

    def ended(self, ev):
        """Each `ended` line's agent name mapped to the part of the line before the usual tail."""
        heads = [e.split("; ")[0] for e in ev if "stall=ended agent=" in e]
        return {h.split("stall=ended agent=")[1].split()[0]: h for h in heads}

    def test_a_closed_teammate_not_done_is_reported_at_once(self):
        """Fires with no stall window, even while another teammate waits on the product owner."""
        crew = {ORCH: ("working", 600), "hw-tests-001": ("needs-user", 10), "hw-tests-002": ("working", 10),
                "hw-tests-003": ("blocked", 10), "hw-tests-004": ("needs-user", 600)}
        s = self.snap(crew=crew, last_ago=600)
        s["crew"]["hw-tests-003"].insert(0, {"at": at(20), "state": "done", "message": "m"})  # the last report counts
        s["closed"] = ["hw-tests-001", "hw-tests-002", "hw-tests-003"]
        ev = self.stalls(s)
        self.assertEqual(len(ev), 3, ev)  # no quiet stall: the needs-user silence still holds for the others
        got = self.ended(ev)
        self.assertEqual(sorted(got), ["hw-tests-001", "hw-tests-002", "hw-tests-003"])
        for n, st in (("hw-tests-001", "needs-user"), ("hw-tests-002", "working"), ("hw-tests-003", "blocked")):
            self.assertIn(st, got[n])

    def test_a_collected_test_writer_whose_last_report_is_test_ready_has_not_ended(self):
        """collect closes a test writer's tab after its `working "test ready"` report, the one it is told to end on."""
        crew = {ORCH: ("working", 600), "hw-tests-001": ("working", 600), "hw-tests-002": ("working", 600)}
        s = self.snap(crew=crew, last_ago=100)
        s["crew"]["hw-tests-001"][-1]["message"] = "Test ready: abc123; holdouts: empty input"
        s["closed"] = ["hw-tests-001", "hw-tests-002"]
        self.assertEqual(sorted(self.ended(self.stalls(s))), ["hw-tests-002"])  # only the writer that never got to test ready ended
        s["crew"]["hw-tests-001"][-1]["state"] = "blocked"
        s["closed"] = ["hw-tests-001"]
        self.assertEqual(sorted(self.ended(self.stalls(s))), ["hw-tests-001"])

    def test_a_closed_team_lead_or_a_done_team_lead_does_not_hide_it(self):
        s = self.snap(tasks=("passed",), crew={ORCH: ("needs-user", 10)}); s["closed"] = [ORCH]
        got = self.ended(self.stalls(s))
        self.assertEqual(list(got), [ORCH]); self.assertIn("needs-user", got[ORCH])
        s = self.snap(crew={ORCH: ("done", 10), "hw-tests-001": ("working", 10)}); s["closed"] = ["hw-tests-001"]
        self.assertEqual(list(self.ended(self.stalls(s))), ["hw-tests-001"])

    def test_a_closed_teammate_fires_once_until_it_reports_again(self):
        s = self.snap(tasks=("passed",), crew={ORCH: ("working", 10), "hw-tests-001": ("working", 30)})
        s["closed"] = ["hw-tests-001"]
        self.assertEqual(len(self.ended(self.stalls(s))), 1)
        self.assertEqual(self.stalls(s), [])
        s["crew"]["hw-tests-001"].append({"at": at(5), "state": "blocked", "message": "m"})
        got = self.ended(self.stalls(s))
        self.assertEqual(list(got), ["hw-tests-001"]); self.assertIn("blocked", got["hw-tests-001"])

    def test_a_closed_teammate_done_or_without_reports_is_not_ended(self):
        crew = {ORCH: ("working", 10), "hw-tests-001": ("done", 10), "hw-tests-003": ("working", 10)}
        s = self.snap(tasks=("passed",), crew=crew)
        s["crew"]["hw-tests-001"].insert(0, {"at": at(20), "state": "working", "message": "m"})
        s["crew"]["hw-tests-002"] = []
        s["closed"] = ["hw-tests-001", "hw-tests-002", "hw-tests-005"]  # tests-003 is open, tests-005 never reported
        self.assertEqual(self.stalls(s), [])

    def test_run_ended_with_no_next_step(self):
        ev = self.stalls(self.snap(tasks=("passed",), crew={ORCH: ("working", 900)}, last_ago=100, run_end=NOW - 300))
        self.assertTrue(any("stall=no-next-step" in e for e in ev), ev)

    def test_team_lead_idle_with_pending_tasks(self):
        s = self.snap(status="idle", crew={ORCH: ("working", 100)}, last_ago=100)
        self.assertEqual(self.stalls(s, now=NOW), [])  # it just went idle
        ev = self.stalls(s, now=NOW + 300)
        self.assertTrue(any("stall=idle agent=hw-orchestrator" in e for e in ev), ev)

    def test_held_tasks_are_still_work(self):
        s = self.snap(tasks=("held",), status="idle", crew={ORCH: ("working", 600)}, last_ago=600)
        self.assertTrue(any("stall=quiet" in e for e in self.stalls(s)))
        ev = self.stalls(s, now=NOW + 300)
        self.assertTrue(any("stall=idle" in e and "1 task(s) pending" in e for e in ev), ev)

    def test_the_engineering_manager_receives_stalls_and_the_team_lead_too(self):
        ev = ["event stall=quiet agent=hw-orchestrator x", "event crew=hw-tests-001 state=done message='m'",
              "event stall=idle agent=hw-orchestrator the team lead has been idle"]
        self.assertEqual(fc.fc_watch.watch_filter(ev, "hw", [ORCH]), [ev[0], ev[2]])
        self.assertEqual(fc.fc_watch.watch_filter(ev, "hw", []), ev[:2])  # the team lead is idle because it waits on this watch


class WatchTimeout(unittest.TestCase):
    def test_timeout_ends_with_a_state_summary(self):
        snap = {"tasks": {"001": {"state": "pending"}}, "agents": {},
                "crew": {"hw-tests-001": [{"at": time.strftime("%Y-%m-%d %H:%M:%S"), "state": "working", "message": "m"}]},
                "run": False, "verify": None, "run_end": 0, "last": (0, "")}
        a = SimpleNamespace(repo=".", now=False, from_=None, timeout=0.01, interval=0.01, stall=10_000)
        out = []
        with mock.patch.object(fc.fc_core, "project_dir", return_value=Path(tempfile.mkdtemp()) / "hw"), mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}), \
                mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=snap), mock.patch.object(fc.fc_proc, "keep_awake") as awake, mock.patch("builtins.print", out.append):
            with self.assertRaises(SystemExit) as e:
                fc.fc_watch.cmd_watch(a)
        awake.assert_called_once_with()  # a sleeping Mac sleeps the team the watch waits on
        self.assertEqual(e.exception.code, 2)
        self.assertIn("event timeout", out[-1])
        self.assertIn("pending tasks 1, teammates working hw-tests-001, run not live, verify not run", out[-1])
        snap["closed"] = ["hw-tests-001"]
        with mock.patch.object(fc.fc_core, "project_dir", return_value=Path(tempfile.mkdtemp()) / "hw"), mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}), \
                mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=snap), mock.patch("builtins.print", out.append):
            fc.fc_watch.cmd_watch(a)  # the ended event wakes the watch at once, before any timeout
        self.assertIn("event stall=ended agent=hw-tests-001", out[-1])  # a closed teammate is reported as ended, not left counted as working

    def test_the_snapshot_names_teammates_whose_tab_is_closed(self):
        proj = Path(tempfile.mkdtemp()) / "hw"; (proj / "results" / "crew").mkdir(parents=True)
        for n in ("hw-tests-001", "hw-tests-002"):
            (proj / "results" / "crew" / f"{n}.json").write_text(json.dumps({"name": n, "space": "w9"}))
        live = {"agents": [{"name": "hw-tests-001", "workspace_id": "w9", "agent_status": "working"},
                           {"name": "hw-tests-002", "workspace_id": "w1", "agent_status": "working"}]}  # same name, another space
        for answer, closed in ((live, ["hw-tests-002"]), (None, [])):  # herdr cannot say: nobody counts as closed
            with mock.patch.object(fc.fc_herdr, "herdr", return_value=answer), mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[]):
                self.assertEqual(fc.fc_watch.watch_snapshot(Path("."), proj)["closed"], closed)

    def test_a_teammate_still_booting_is_closed_only_when_its_tab_is(self):
        """crew start writes the record before herdr lists the agent: until it took its brief, only a closed tab ends it."""
        proj = Path(tempfile.mkdtemp()) / "hw"; (proj / "results" / "crew").mkdir(parents=True)
        for n, tab in (("hw-tests-001", "w9:t3"), ("hw-tests-002", "w9:t4")):
            (proj / "results" / "crew" / f"{n}.json").write_text(json.dumps({"name": n, "space": "w9", "tab": tab, "booting": True}))
        tabs = lambda *a, **k: ({"tab": {"tab_id": "w9:t3"}}, None) if a == ("tab", "get", "w9:t3") else (None, "tab_not_found")
        with mock.patch.object(fc.fc_herdr, "herdr", return_value={"agents": []}), mock.patch.object(fc.fc_herdr, "herdr_error", tabs), \
                mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[]):
            self.assertEqual(fc.fc_watch.watch_snapshot(Path("."), proj)["closed"], ["hw-tests-002"])

    def test_a_teammate_just_up_is_not_judged_by_a_list_from_before(self):
        """A watch loop keeps herdr's agent list for seconds; crew_up in that time must not make a running teammate look closed."""
        proj = Path(tempfile.mkdtemp()) / "hw"; (proj / "results" / "crew").mkdir(parents=True)
        (proj / "results" / "crew" / "hw-tests-001.json").write_text(json.dumps({"name": "hw-tests-001", "space": "w9", "tab": "w9:t3", "booting": True}))
        agents, listed = fc.fc_watch.Agents(), {"agents": []}
        with mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: listed), mock.patch.object(fc.fc_herdr, "herdr_error", return_value=({}, None)), \
                mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[]):
            self.assertEqual(fc.fc_watch.watch_snapshot(Path("."), proj, agents)["closed"], [])  # booting, its tab is there
            listed = {"agents": [{"name": "hw-tests-001", "workspace_id": "w9", "agent_status": "working"}]}  # herdr lists it now
            fc.fc_crew.crew_up(proj, "hw-tests-001")
            self.assertEqual(fc.fc_watch.watch_snapshot(Path("."), proj, agents)["closed"], [])

    def test_the_snapshot_sees_a_team_lead_whose_name_was_cut(self):
        proj = Path(tempfile.mkdtemp()) / "clean-recut-branches"; (proj / "results").mkdir(parents=True)
        live = {"agents": [{"name": "clean-recut-branche-orchestrator", "workspace_id": "w9", "agent_status": "working"},
                           {"name": "clean-recut-branches-groundwork", "workspace_id": "w9", "agent_status": "idle"},
                           {"name": "clean-bugs-orchestrator", "workspace_id": "w9", "agent_status": "done"}]}
        with mock.patch.object(fc.fc_herdr, "herdr", return_value=live), mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[]):
            snap = fc.fc_watch.watch_snapshot(Path("."), proj)
        self.assertEqual(snap["agents"], {"clean-recut-branche-orchestrator": "working", "clean-recut-branches-groundwork": "idle"})
        snap.update({"tasks": {"001": {"state": "pending"}}, "last": (NOW - 600, "teammate x reported working")})
        ev = fc.fc_watch.watch_stalls(proj, snap, 240, now_s=NOW)
        self.assertIn("team lead working", ev[0])
        self.assertIn("herdr agent prompt clean-recut-branche-orchestrator", ev[0])


def wsnap(state="pending", run=False, ended=None, halt=None):
    return {"tasks": {"001": {"state": state, "attempts": 1, "cost": 0.0, "note": "", "linear": "", "title": "t"}}, "agents": {},
            "crew": {}, "run": run, "ended": ended, "verify": None, "halt": halt, "run_end": 0, "last": [0, ""]}


class WatchMemory(unittest.TestCase):
    """Each watcher remembers what it last reported, so what happens while no watch runs is reported by the next one."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"

    def watch(self, snap, role=None, **kw):
        a = SimpleNamespace(repo=".", now=False, from_=None, timeout=0.05, interval=0.01, stall=10_000, rewake=False)
        a.__dict__.update(kw)
        out = []
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_ROLE"} | ({"COMPOSE_ROLE": role} if role is not None else {})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=snap), \
                mock.patch("builtins.print", out.append), mock.patch.dict(os.environ, env, clear=True):
            try:
                fc.fc_watch.cmd_watch(a)
            except SystemExit:
                pass
        return out

    def test_a_change_between_two_watches_is_reported_by_the_next(self):
        self.watch(wsnap("running"), now=True)
        out = self.watch(wsnap("passed"))  # the task passed while nobody watched
        self.assertTrue(any("event task=001 state=passed" in line for line in out), out)
        self.assertIn("event timeout", self.watch(wsnap("passed"))[-1])  # reported once, not again

    def test_now_marks_the_current_state_seen(self):
        self.assertTrue(any("task=001 state=passed" in line for line in self.watch(wsnap("passed"), now=True)))
        self.assertIn("event timeout", self.watch(wsnap("passed"))[-1])

    def test_a_wake_passes_its_events_to_the_product_owners_attention(self):
        snap = wsnap(); later = wsnap()
        later["crew"] = {ORCH: [{"at": at(0), "state": "needs-user", "message": "which db?"}]}
        seen = []
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_ROLE"}  # the engineering manager's watch: no role
        with mock.patch.object(fc.fc_crew, "manager_attention", lambda proj, ev: seen.append(ev)), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(fc.fc_watch, "watch_snapshot", side_effect=[snap, later, later, later]):
            a = SimpleNamespace(repo=".", now=False, from_="orchestrator", timeout=0.05, interval=0.01, stall=10_000, rewake=False)
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("builtins.print"):
                with contextlib.suppress(SystemExit):
                    fc.fc_watch.cmd_watch(a)
        self.assertEqual(seen, [[f"event crew={ORCH} state=needs-user message='which db?'"]])

    def test_the_engineering_managers_watch_passes_teammates_it_started_whatever_from_says(self):
        """The one watch (key `manager`, `--from orchestrator` while a team lead is live) also wakes on a planner or designer the
        engineering manager started itself: `crew start` recorded that they report to it. A teammate the team lead started does not."""
        (self.proj / "results" / "crew").mkdir(parents=True)
        for who, up in (("hw-planner", "manager"), ("hw-tests", "orchestrator")):
            (self.proj / "results" / "crew" / f"{who}.json").write_text(json.dumps({"name": who, "role": who[3:], "title": who[3:], "reports_to": up}))
        snap = wsnap(); later = wsnap()
        later["crew"] = {"hw-planner": [{"at": at(0), "state": "needs-user", "message": "3 stories; accept-all"}],
                         "hw-tests": [{"at": at(0), "state": "needs-user", "message": "which fixture?"}]}
        seen = []
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_ROLE"}
        with mock.patch.object(fc.fc_crew, "manager_attention", lambda proj, ev: seen.append(ev)), \
                mock.patch.object(fc.fc_watch, "watch_snapshot", side_effect=[snap, later, later, later]), mock.patch.dict(os.environ, env, clear=True):
            a = SimpleNamespace(repo=".", now=False, from_="orchestrator", timeout=0.05, interval=0.01, stall=10_000, rewake=False)
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("builtins.print"):
                with contextlib.suppress(SystemExit):
                    fc.fc_watch.cmd_watch(a)
        self.assertEqual(seen, [["event crew=hw-planner state=needs-user message='3 stories; accept-all'"]])

    def later(self):
        snap = wsnap("passed")
        snap["crew"] = {"hw-x": [{"at": at(0), "state": "tool-bug", "message": "crash"}],
                        "hw-y": [{"at": at(0), "state": "working", "message": "half way"}]}
        return snap

    def test_now_counts_a_teammate_whose_only_report_is_started_as_running(self):
        snap = wsnap("pending")
        snap["crew"] = {"hw-explore": [{"at": at(0), "state": "working", "message": "started"}]}
        out = self.watch(snap, now=True)
        self.assertIn("event crew=hw-explore state=working message='started'", out)
        self.assertNotIn("event idle  nothing running", out)

    def test_watches_in_one_role_share_memory_whatever_their_from(self):
        self.watch(wsnap("running"), now=True, role="engineering-manager")
        self.watch(wsnap("running"), now=True, role="engineering-manager", from_="orchestrator")
        self.assertTrue(any("crew=hw-x state=tool-bug" in line for line in self.watch(self.later(), role="engineering-manager")))
        out = self.watch(self.later(), role="engineering-manager", from_="orchestrator")  # already reported in this role
        self.assertFalse(any("crew=hw-x" in line for line in out), out)
        self.assertIn("event timeout", out[-1])

    def test_now_with_from_marks_the_state_seen_for_the_whole_role(self):
        self.watch(wsnap("running"), now=True)
        now = self.watch(self.later(), now=True, from_="orchestrator")
        self.assertFalse(any("crew=hw-y" in line for line in now), now)  # --from still filters what is printed
        self.assertTrue(any("crew=hw-x state=tool-bug" in line for line in now), now)
        self.assertIn("event timeout", self.watch(self.later())[-1])

    def test_each_role_keeps_its_own_memory(self):
        for role in ("orchestrator", "engineering-manager"):
            self.watch(wsnap("running"), now=True, role=role)
        for role in ("orchestrator", "engineering-manager"):
            self.assertTrue(any("crew=hw-x state=tool-bug" in line for line in self.watch(self.later(), role=role)), role)
            self.assertIn("event timeout", self.watch(self.later(), role=role)[-1])

    def test_the_memory_file_is_named_for_the_role(self):
        self.watch(wsnap(), now=True, role="engineering-manager", from_="orchestrator")
        self.watch(wsnap(), now=True)
        self.watch(wsnap(), now=True, role="", from_="x,orchestrator")
        files = sorted(f.name for f in (self.proj / "results").glob("watch-*.json"))
        self.assertEqual(files, ["watch-engineering-manager.json", "watch-manager.json"])

    def test_a_payment_halt_is_an_event(self):
        ev = fc.fc_watch.watch_events(wsnap(run=True), wsnap(halt=["2026-10-11 10:00:00", "openrouter", 2]))
        self.assertTrue(any(e.startswith("event halt=payment provider=openrouter remaining=2") for e in ev), ev)

    def test_a_task_whose_chain_stopped_is_an_event(self):
        ev = fc.fc_watch.watch_events(wsnap(run=True), wsnap("unchained", run=True))
        self.assertTrue(any(e.startswith("event task=001 state=unchained") for e in ev), ev)

    def test_checking_out_the_stack_top_and_back_reports_nothing(self):
        """A task's state is measured against the project branch, not whatever the worktree has checked out: peeking at
        the stack top (where every layer is an ancestor) and coming back must not replay passed and hand_done tasks."""
        repo = Path(tempfile.mkdtemp())
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "compose/hw"); git("commit", "-q", "--allow-empty", "-m", "init")
        for tid in ("001", "002"):
            git("checkout", "-q", "-b", f"fast/hw/{tid}"); git("commit", "-q", "--allow-empty", "-m", tid)
        git("checkout", "-q", "compose/hw")
        proj = repo / ".compose" / "hw"; (proj / "results").mkdir(parents=True)
        (proj / "verify.json").write_text("{}")
        (proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [{"id": "001", "title": "a"}, {"id": "002", "title": "b"}]}))
        (proj / "results" / "001.json").write_text(json.dumps({"status": "passed"}))
        (proj / "results" / "002.json").write_text(json.dumps({"status": "hand_done"}))
        snaps = []
        with mock.patch.object(fc.fc_herdr, "herdr", return_value={}):
            for ref in ("compose/hw", "fast/hw/002", "compose/hw"):
                git("checkout", "-q", ref)
                snaps.append(fc.fc_watch.watch_snapshot(repo, proj))
        self.assertEqual({t: v["state"] for t, v in snaps[0]["tasks"].items()}, {"001": "passed", "002": "hand_done"})
        self.assertEqual(fc.fc_watch.watch_events(snaps[0], snaps[1]) + fc.fc_watch.watch_events(snaps[1], snaps[2]), [])

    def test_now_reports_a_crashed_run(self):
        out = self.watch(wsnap("running", ended="crashed"), now=True)
        self.assertTrue(any("run=ended" in line and "CRASHED" in line for line in out), out)


class Rewake(unittest.TestCase):
    """The team lead's Stop hook: park one watch per turn end, wake the idle session with the event on stderr, exit 2."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        self.env = {"COMPOSE_ROLE": "orchestrator", "COMPOSE_PROJECT": "hw", "COMPOSE_REPO": "/r", "CLAUDE_CODE_ENTRYPOINT": "cli"}

    def rewake(self, snaps, env=None, session="s1"):
        """The hook reads its JSON from stdin: feed it here, since the real stdin may be a pipe nobody writes to (then the hook waits forever)."""
        err = io.StringIO()
        with mock.patch.dict(os.environ, env or self.env), mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj) as pd, \
                mock.patch.object(fc.fc_watch, "watch_snapshot", side_effect=snaps), mock.patch.object(fc.fc_watch, "WATCH_INTERVAL", 0.01), mock.patch.object(fc.fc_watch, "WATCH_SETTLE", 0), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": session}))), contextlib.redirect_stderr(err):
            try:
                fc.fc_watch.cmd_rewake(SimpleNamespace())
                code = 0
            except SystemExit as e:
                code = e.code
        return code, err.getvalue(), pd

    def test_an_event_wakes_the_session(self):
        code, err, _ = self.rewake([wsnap("running"), wsnap("running"), wsnap("passed")])
        self.assertEqual(code, 2)
        self.assertIn("event task=001 state=passed", err)

    def test_sessions_that_are_not_the_team_lead_return_at_once(self):
        code, err, pd = self.rewake([], env={"COMPOSE_ROLE": "crew", "COMPOSE_PROJECT": "hw"})
        self.assertEqual((code, err), (0, ""))
        pd.assert_not_called()

    def test_a_headless_session_returns_at_once(self):
        code, err, pd = self.rewake([], env={**self.env, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})  # what Claude Code sets for `claude -p`
        self.assertEqual((code, err), (0, ""))
        pd.assert_not_called()

    def test_a_wake_carries_the_skill_change_notice(self):
        skill = Path(tempfile.mkdtemp()).resolve()
        (skill / "SKILL.md").write_text("v1\n")
        with mock.patch.object(fc.fc_hooks, "SKILL_DIR", skill), mock.patch.object(fc.fc_hooks, "SKILL_SEEN", skill / "seen"):
            fc.fc_hooks.skill_notice("lead")  # the version the team lead read
            (skill / "SKILL.md").write_text("v2\n")
            code, err, _ = self.rewake([wsnap("running"), wsnap("passed")], session="lead")
        self.assertEqual(code, 2)
        self.assertIn(f"compose's skill changed since you read it: {skill / 'SKILL.md'}", err)

    def test_a_second_hook_leaves_the_parked_one_alone(self):
        held = fc.fc_watch.watch_lock(self.proj, "orchestrator", wait=False)
        self.assertIsNotNone(held)
        code, err, _ = self.rewake([wsnap("passed")])
        self.assertEqual((code, err), (0, ""))
        held.close()


class Reenter(unittest.TestCase):
    """SessionStart hook after a compaction: point a compose teammate back at its brief, the board, and Linear."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        self.env = {"COMPOSE_ROLE": "tests-021", "COMPOSE_PROJECT": "hw", "COMPOSE_REPO": "/r", "CLAUDE_CODE_ENTRYPOINT": "cli"}

    def reenter(self, env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj) as pd, \
                contextlib.redirect_stdout(out):
            fc.fc_watch.cmd_reenter(SimpleNamespace())
        return out.getvalue(), pd

    def test_a_teammate_is_pointed_at_its_brief_the_board_and_linear(self):
        out, _ = self.reenter(self.env)
        self.assertIn(str(self.proj / "results" / "crew" / f"{fc.fc_herdr.agent_name('hw', 'tests-021')}.brief.md"), out)
        self.assertIn("hw-tests-021.brief.md", out)
        self.assertIn("0r", out)
        self.assertIn("compose status --repo /r --project hw", out)
        self.assertIn("compose watch --repo /r --project hw --now", out)
        self.assertIn("Decisions so far", out)
        self.assertIn("Plan", out)

    def test_the_team_lead_gets_the_same_reentry(self):
        out, _ = self.reenter({**self.env, "COMPOSE_ROLE": "orchestrator"})
        self.assertIn("hw-orchestrator.brief.md", out)

    def test_a_crew_teammate_is_pointed_at_its_own_brief_not_its_roles(self):
        """Every unguarded teammate's tab carries COMPOSE_ROLE=crew: the brief is named for the teammate, `<slug>-<role>.brief.md`."""
        crew = self.proj / "results" / "crew"; crew.mkdir()
        (crew / "hw-designer.brief.md").write_text("brief")
        out, _ = self.reenter({**self.env, "COMPOSE_ROLE": "crew", "COMPOSE_TEAMMATE": "hw-designer"})
        self.assertIn(str(crew / "hw-designer.brief.md"), out)
        self.assertNotIn("hw-crew", out)
        # a session started before its tab carried the name: its record says which pane it runs in
        (crew / "hw-designer.json").write_text(json.dumps({"name": "hw-designer", "pane": "w9:p4"}))
        (crew / "hw-research.json").write_text(json.dumps({"name": "hw-research", "pane": "w9:p5"}))
        out, _ = self.reenter({**self.env, "COMPOSE_ROLE": "crew", "HERDR_PANE_ID": "w9:p4"})
        self.assertIn(str(crew / "hw-designer.brief.md"), out)
        self.assertNotIn("hw-crew", out)

    def test_no_role_or_a_headless_session_prints_nothing(self):
        for env in ({k: v for k, v in self.env.items() if k != "COMPOSE_ROLE"}, {**self.env, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}):
            out, pd = self.reenter(env)
            self.assertEqual(out, "")
            pd.assert_not_called()

    def test_the_plugin_runs_it_on_a_compaction(self):
        root = Path(fc.__file__).resolve().parents[1]
        hooks = json.loads((root / "hooks" / "hooks.json").read_text())["hooks"]
        cmds = lambda ev, m=None: [h["command"] for e in hooks.get(ev, []) if e.get("matcher") == m for h in e["hooks"]]
        self.assertTrue(any(c.endswith(" reenter") for c in cmds("SessionStart", "compact")), hooks.get("SessionStart"))
        self.assertTrue(cmds("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit|Bash")[0].endswith(" guard"))
        self.assertTrue(cmds("PreToolUse", "mcp__plugin_figma_figma__.*|mcp__claude_ai_Figma__.*")[0].endswith(" guard"))  # the Figma connector: writes only
        self.assertTrue(cmds("PostToolUse", "Skill")[0].endswith(" skillcheck"))
        self.assertTrue(cmds("UserPromptSubmit")[0].endswith(" skillcheck"))
        self.assertTrue(cmds("Stop")[0].endswith(" rewake"))


class Detach(unittest.TestCase):
    """run, verify, and resume work in their own session: a closed herdr tab or a dead Claude session ends only the
    follower that streams their log, never the work."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.log = self.dir / "run.log"

    def test_the_log_streams_here_and_the_exit_code_comes_back(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fc.follow([sys.executable, "-c", "print('layer 001 passed'); raise SystemExit(3)"], self.log)
        self.assertEqual(code, 3)
        self.assertIn("layer 001 passed", out.getvalue())
        self.assertIn("layer 001 passed", self.log.read_text())

    def test_a_group_left_with_only_its_zombie_leader_ends_the_follow(self):
        """macOS answers EPERM, not ESRCH, when the work's group has only its zombie leader left: the follow ends with
        the work's exit code instead of crashing on the probe."""
        done, real = Path(f"{self.log}.exit"), os.killpg
        def killpg(group, sig):
            if sig == 0 and done.exists() and done.read_text().strip():
                raise PermissionError(1, "Operation not permitted")
            return real(group, sig)
        out = io.StringIO()
        with mock.patch.object(os, "killpg", killpg), contextlib.redirect_stdout(out):
            code = fc.follow([sys.executable, "-c", "print('layer 001 passed'); raise SystemExit(3)"], self.log)
        self.assertEqual(code, 3)
        self.assertIn("layer 001 passed", out.getvalue())

    def test_the_work_runs_in_its_own_session(self):
        with contextlib.redirect_stdout(io.StringIO()):
            fc.follow([sys.executable, "-c", "import os; print(os.getsid(0))"], self.log)
        self.assertNotEqual(int(self.log.read_text().split()[0]), os.getsid(0))

    def test_a_compose_verify_started_by_a_detached_verify_detaches_too(self):
        """The detach marker is the CLI's own: an e2e driver that runs compose gets a run in its own session, never one in
        the outer verify's process group, where stopping it would stop the outer verify."""
        cli = Path(__file__).resolve().parents[1] / "bin" / "compose.py"
        verify = lambda repo: [sys.executable, str(cli), "verify", "--repo", str(repo), "--project", "p", "--skip-review", "--fix", "0"]
        def repo(driver):
            r = Path(tempfile.mkdtemp())
            for args in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"]):
                subprocess.run(["git", *args], cwd=r, check=True)
            (r / ".compose" / "p").mkdir(parents=True)
            (r / ".compose" / "p" / "verify.json").write_text(json.dumps({"review": False, "jev_acceptance": False, "e2e_env": {"BU_CDP_URL": "http://127.0.0.1:1", "BU_NAME": "d"},  # no browser
                                                                             "e2e": [{"name": "d", "driver": driver}]}))
            return r
        outer = repo(f"{shlex.join(verify(repo('true')))} > {self.dir}/nested.out 2>&1")
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_DETACHED"}
        p = subprocess.run(verify(outer), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("runs in its own session", (self.dir / "nested.out").read_text())

    def test_a_sigint_stops_a_detached_verify_started_with_sigint_ignored_and_its_suite(self):
        """A background job of a non-interactive shell starts with SIGINT ignored, and so did the verify and the suite under it: a
        `kill -INT` stopped neither. Now it ends the verify, the suite, and what the suite started, and the log says why."""
        cli = Path(__file__).resolve().parents[1] / "bin" / "compose.py"
        repo = Path(tempfile.mkdtemp())
        for args in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *args], cwd=repo, check=True)
        (repo / ".compose" / "p").mkdir(parents=True)
        suite = f"echo $$ > {self.dir}/suite.pid; ps -o ppid= -p $$ > {self.dir}/verify.pid; sleep 300 & echo $! > {self.dir}/child.pid; wait"  # the stub suite: sleeps
        (repo / ".compose" / "p" / "verify.json").write_text(json.dumps({"review": False, "jev_acceptance": False, "checks": [suite]}))
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_DETACHED"}
        follower = subprocess.Popen([sys.executable, str(cli), "verify", "--repo", str(repo), "--project", "p", "--skip-review", "--skip-e2e", "--fix", "0"],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
        pids = {n: self.dir / f"{n}.pid" for n in ("suite", "verify", "child")}
        alive = lambda pid: subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() not in ("", "Z")
        def wait_for(until):  # generous deadline, polled: a loaded machine can take seconds to start python
            for _ in range(300):
                if until():
                    return True
                time.sleep(0.1)
            return False
        def pid_of(name):
            return int(pids[name].read_text()) if pids[name].exists() and pids[name].read_text().strip() else 0
        self.addCleanup(lambda: [os.kill(pid, 9) for pid in (pid_of(n) for n in pids) if pid and alive(pid)])
        self.assertTrue(wait_for(lambda: all(pid_of(n) for n in pids)), "the stub suite never started")
        os.kill(pid_of("verify"), signal.SIGINT)
        self.assertTrue(wait_for(lambda: not any(alive(pid_of(n)) for n in pids)), "verify or its suite survived the SIGINT")
        self.assertEqual(follower.wait(30), 130)
        self.assertIn("verify stopped by Ctrl-C", (repo / ".compose" / "p" / "results" / "verify.log").read_text())

    def test_killing_the_follower_leaves_the_work_running(self):
        done = self.dir / "done"
        started = self.dir / "started"
        work = f"import time; open({str(started)!r}, 'w').write('1'); time.sleep(1.5); open({str(done)!r}, 'w').write('ok')"
        cli = Path(__file__).resolve().parents[1] / "bin" / "compose.py"
        follower = subprocess.Popen([sys.executable, "-c",
            "import importlib.machinery as m, importlib.util as u, sys; "
            f"s = u.spec_from_loader('fc', m.SourceFileLoader('fc', {str(cli)!r})); fc = u.module_from_spec(s); s.loader.exec_module(fc); "
            f"fc.follow([sys.executable, '-c', {work!r}], fc.Path({str(self.log)!r}))"], stdout=subprocess.DEVNULL)
        def wait_for(path):  # generous deadline, polled: a loaded machine can take seconds to start python
            for _ in range(300):
                if path.exists():
                    return
                time.sleep(0.1)
        wait_for(started)  # kill only once the work is running, or the follower may die before it ever spawned it
        def tree(pid):  # what Claude Code does to a background task when its tab closes: the whole process tree
            kids = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
            return [pid] + [p for k in kids for p in tree(int(k))]
        for pid in tree(follower.pid):
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        follower.wait()
        wait_for(done)
        self.assertEqual(done.read_text(), "ok")

    def test_now_says_a_run_is_live(self):
        proj = Path(tempfile.mkdtemp()) / "hw"
        out = []
        a = SimpleNamespace(repo=".", now=True, from_=None)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=wsnap("running", run=True)), \
                mock.patch("builtins.print", out.append), mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}):
            fc.fc_watch.cmd_watch(a)
        self.assertTrue(any(line.startswith("event run=live") for line in out), out)


class TestHang(unittest.TestCase):
    def test_a_hanging_test_fails_fast_with_a_reason(self):
        import tempfile, time
        t0 = time.time()
        r = fc.fc_implement.run_test({"test": "sleep 30", "test_timeout": "1"}, tempfile.mkdtemp())
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(r.returncode, 124)
        self.assertIn("hangs", r.stderr)


class Scratch(unittest.TestCase):
    def test_remove_scratch_removes_every_scratch_config_made_and_forgets_them(self):
        a, b = fc.fc_core.scratch_config(), fc.fc_core.scratch_config()
        fc.fc_core.remove_scratch()
        self.assertEqual((Path(a).parent.exists(), Path(b).parent.exists(), fc.fc_core.SCRATCH_CONFIGS), (False, False, []))


class Timed(unittest.TestCase):
    def test_a_terminated_timed_removes_its_scratch_config_and_ends_its_command(self):
        """A closed pane, a timed limit above it, or a kill sends SIGTERM: left to die bare the CLI skips atexit, so its scratch
        ~/.compose stays in the temp folder and the command runs on without it. It exits 143 with both gone."""
        import subprocess as sp, time
        n = 4017 + 2 * os.getpid()  # unique to this run while it lives (another suite's process has another pid)
        tmp = Path(tempfile.mkdtemp())
        p = sp.Popen([sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "compose.py"), "timed", "60", "--", f"sleep {n}"],
                     env={**os.environ, "TMPDIR": str(tmp)}, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        alive = lambda: sp.run(["pgrep", "-f", f"sleep {n}( |$)"], capture_output=True).stdout.strip() != b""
        for _ in range(100):
            if any(e.name.startswith("compose-config-") for e in tmp.iterdir()) and alive():
                break
            time.sleep(0.1)
        else:
            self.fail("the command never started")
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=10), 143)
        time.sleep(0.5)
        self.assertEqual([e.name for e in tmp.iterdir()], [])
        self.assertFalse(alive())

    def test_timed_runs_several_arguments_as_given_and_one_as_a_shell_line(self):
        import subprocess as sp
        timed = lambda *cmd: sp.run([str(Path(__file__).resolve().parents[1] / "bin" / "compose"), "timed", "30", "--", *cmd],
                                    capture_output=True, text=True)
        r = timed("python3", "-c", "import sys; print(sys.argv[1:])", "a b", "$HOME", "'q'")
        self.assertEqual((r.returncode, r.stdout), (0, "['a b', '$HOME', \"'q'\"]\n"), r.stderr)
        r = timed("echo one && echo two")
        self.assertEqual((r.returncode, r.stdout), (0, "one\ntwo\n"), r.stderr)

    def test_timed_passes_the_callers_stdin_on_but_never_a_terminal(self):
        import pty, subprocess as sp
        cat = lambda **stdin: sp.run([str(Path(__file__).resolve().parents[1] / "bin" / "compose"), "timed", "5", "--", "cat"],
                                     capture_output=True, text=True, **stdin)
        r = cat(input='{"feature": {}}')
        self.assertEqual((r.returncode, r.stdout), (0, '{"feature": {}}'), r.stderr)
        main, term = pty.openpty()
        try:
            r = cat(stdin=term)  # a test reading stdin must not wait on the keyboard until the limit
        finally:
            os.close(main)
            os.close(term)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_a_compose_command_under_timed_that_needs_a_key_says_to_run_it_directly(self):
        compose = str(Path(__file__).resolve().parents[1] / "bin" / "compose")
        r = subprocess.run([compose, "timed", "10", "--", compose, "linear", "project", "--repo", tempfile.mkdtemp()], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("compose linear needs a Linear key, but runs on a scratch config with no workspace and no keys, as every command under "
                      "`compose timed` does: run compose's own commands directly", r.stderr)
        self.assertNotIn("compose workspace key", r.stderr)  # a key stored there would land in the scratch config


class JevLeash(unittest.TestCase):
    def setUp(self):
        k = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ts-test"}); k.start(); self.addCleanup(k.stop)  # what apply_config sets from auth.json

    def test_a_jev_that_gives_up_is_a_tool_error_not_an_app_failure(self):
        self.assertTrue(fc.fc_jev.tool_error("JEV_GAVE_UP: jev ended blocked after 60 steps\n"))
        self.assertTrue(fc.fc_jev.tool_error("JEV_GAVE_UP: jev did not reach its goal within 150s"))
        self.assertFalse(fc.fc_jev.tool_error("AssertionError: score was 2 of 3"))

    def test_a_jev_case_is_killed_at_its_limit_and_its_assert_still_runs(self):
        with mock.patch.object(fc.fc_config, "JEV_DIR", Path(tempfile.mkdtemp())), \
             mock.patch.object(fc.fc_jev, "run_held", side_effect=lambda cmd, limit, env, then: (subprocess.CompletedProcess("x", 124, "", "hung"), then())) as t:
            r = fc.fc_jev.jev_e2e({"name": "c", "url": "http://x", "goal": "g", "assert": "true", "timeout": 5}, Path("/tmp"))
        self.assertEqual(t.call_args[0][1], 5)
        self.assertIn("JEV_GAVE_UP", r["stderr"]); self.assertTrue(fc.fc_jev.tool_error(r["stderr"]))
        self.assertEqual((r["exit"], r["assert_exit"]), (124, 0)); self.assertNotIn("run", r)

    USAGE = {"typesafe_calls": 0, "typesafe_in": 0, "typesafe_out": 0, "text_calls": 0, "text_in": 0, "text_out": 0}

    def jev(self, assert_code, status):
        """jev exits 0 only on a reached goal; verify runs the assert after it, whatever jev's status."""
        stdout = "JEV_RESULT " + json.dumps({"status": status, "elapsed_ms": 413, "steps": 7, "url": "about:blank", "usage": self.USAGE}) + "\n"
        with mock.patch.object(fc.fc_config, "JEV_DIR", Path(tempfile.mkdtemp())), \
             mock.patch.object(fc.fc_jev, "run_held", side_effect=lambda cmd, limit, env, then: (subprocess.CompletedProcess("x", int(status != "done"), stdout, ""), then())):
            return fc.fc_jev.jev_e2e({"name": "c", "url": "about:blank", "goal": "g", "assert": f"exit {assert_code}"}, Path(tempfile.mkdtemp()))

    def test_a_blocked_jev_fails_the_case_even_when_its_assert_passes(self):
        r = self.jev(0, "blocked")
        self.assertNotEqual(r["exit"], 0); self.assertEqual(r["assert_exit"], 0)
        self.assertTrue(r["stderr"].startswith("JEV_GAVE_UP: jev ended blocked after 7 steps\n"))
        self.assertTrue(fc.fc_jev.tool_error(r["stderr"]))
        self.assertEqual(r["run"]["status"], "blocked")

    def test_a_blocked_jev_keeps_a_failing_asserts_code(self):
        for code in (1, 3):
            r = self.jev(code, "blocked")
            self.assertEqual((r["exit"], r["assert_exit"]), (code, code))

    def test_any_status_but_done_fails_the_case(self):
        r = self.jev(0, "error")
        self.assertNotEqual(r["exit"], 0); self.assertEqual(r["assert_exit"], 0)
        self.assertTrue(fc.fc_jev.tool_error(r["stderr"]))

    def test_a_done_jev_takes_the_asserts_code_as_its_verdict(self):
        for code in (0, 3):
            r = self.jev(code, "done")
            self.assertEqual((r["exit"], r["assert_exit"]), (code, code))
            self.assertFalse(fc.fc_jev.tool_error(r["stderr"]))

    def test_a_driver_case_has_no_assert_exit(self):
        r = fc.fc_jev.jev_e2e({"name": "d", "driver": "true", "assert": "true"}, Path(tempfile.mkdtemp()))
        self.assertEqual(sorted(r), ["exit", "name", "stderr", "stdout"]); self.assertEqual(r["exit"], 0)


class JevGaveUpStage(unittest.TestCase):
    """The e2e line ends with the assert's exit when the case record has one; the verdict and retry stay as they are."""
    GAVE_UP = {"exit": 1, "assert_exit": 0, "stdout": "", "stderr": "JEV_GAVE_UP: jev ended blocked after 0 steps\n",
               "run": {"status": "blocked", "steps": 0, "elapsed_ms": 413}}
    DONE = {"exit": 0, "assert_exit": 0, "stdout": "", "stderr": "", "run": {"status": "done", "steps": 3, "elapsed_ms": 900}}

    def stage(self, record):
        calls = []
        def fake(case, repo):
            calls.append(case["name"]); return dict(record)
        report, findings, out = {"e2e": []}, [], io.StringIO()
        cfg = {"e2e": [{"name": "flow", "url": "about:blank", "goal": "g", "assert": "true"}], "e2e_env": {"BU_CDP_URL": "http://127.0.0.1:1"}}
        with mock.patch.object(fc.fc_jev, "jev_e2e", fake), contextlib.redirect_stdout(out):
            ok = fc.fc_verify.e2e_stage(Path(tempfile.mkdtemp()), cfg, {}, report, findings)
        lines = [l for l in out.getvalue().splitlines() if "e2e    " in l and "retrying" not in l]
        self.assertEqual(len(lines), 1, out.getvalue())
        return ok, calls, findings, lines[0]

    def test_a_gave_up_case_shows_tool_error_and_the_asserts_exit(self):
        ok, calls, findings, line = self.stage(self.GAVE_UP)
        self.assertIn("TOOL ERROR", line); self.assertTrue(line.endswith(" assert=0"), line)
        self.assertEqual((ok, calls, findings), (False, ["flow", "flow"], []))

    def test_a_done_case_shows_ok_and_the_asserts_exit(self):
        ok, calls, findings, line = self.stage(self.DONE)
        self.assertIn(" ok ", line); self.assertTrue(line.endswith("status=done assert=0"), line)
        self.assertEqual((ok, calls, findings), (True, ["flow"], []))

    def test_a_failing_assert_shows_its_own_code_and_is_a_finding(self):
        ok, calls, findings, line = self.stage({**self.DONE, "exit": 3, "assert_exit": 3})
        self.assertIn("FAIL", line); self.assertTrue(line.endswith(" assert=3"), line)
        self.assertEqual((ok, calls, len(findings)), (False, ["flow"], 1))

    def test_a_record_without_assert_exit_prints_no_assert(self):
        killed = {"exit": 124, "stdout": "", "stderr": "JEV_GAVE_UP: jev did not reach its goal within 150s\nhung"}
        driver = {"name": "flow", "exit": 0, "stdout": "", "stderr": ""}
        for record, verdict in ((killed, "TOOL ERROR"), (driver, " ok ")):
            line = self.stage(record)[3]
            self.assertIn(verdict, line); self.assertNotIn("assert=", line); self.assertNotIn("status=", line)


class RunStop(unittest.TestCase):
    """Task 003 (NEB-920): Ctrl-C during run_tasks stops at once, exits 130, keeps run.json; no attempt or fallback after the stop; the herdr hook."""
    def setUp(self):
        self.addCleanup(fc.fc_proc.STOP.clear); self.addCleanup(fc.fc_proc.HOOKS.clear); self.addCleanup(fc.fc_proc.RUNNING.clear)
        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))

    def ctrl_c(self, stop=None):
        proj, release, finished, exits, err = Path(tempfile.mkdtemp()), threading.Event(), threading.Event(), [], io.StringIO()
        task = lambda *a: release.wait(5) or finished.set() or {}  # still running at the exit: the stop waits on no task
        exit = lambda code: exits.append((code, finished.is_set())) or release.set() or sys.exit(code)
        with mock.patch("concurrent.futures.wait", side_effect=KeyboardInterrupt), mock.patch.object(fc.fc_run.os, "_exit", exit), \
                mock.patch.object(fc.fc_run, "preflight"), mock.patch.object(fc.fc_herdr, "herdr_space"), mock.patch.object(fc.fc_proc, "stop", side_effect=stop) as stopped, \
                mock.patch.object(fc.fc_implement, "run_task", task), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                fc.fc_run.run_tasks(proj, proj, [{"id": "001", "model": "pi:p/primary"}], SimpleNamespace(attempts=1, parallel=1, trunk="main", no_stack=True))
            except BaseException as e:  # a loose KeyboardInterrupt would end the whole unittest run
                self.assertIsInstance(e, SystemExit, err.getvalue())
        self.assertEqual((stopped.call_count, exits), (1, [(130, False)]))
        self.assertEqual(re.findall(r"\[\d\d:\d\d:\d\d\] (.+)\n", err.getvalue()), ["run stopped by Ctrl-C; `compose resume` continues it"], err.getvalue())
        self.assertNotIn("Traceback", err.getvalue()); self.assertTrue((proj / "results" / "run.json").exists())
        return proj

    def test_ctrl_c_stops_says_so_and_exits_130_without_waiting_on_the_task(self):
        self.ctrl_c()

    def test_watch_calls_a_ctrl_c_run_interrupted_and_a_run_that_just_died_crashed(self):
        proj = self.ctrl_c()
        with mock.patch.object(fc.fc_tasks, "pid_alive", return_value=False):  # the run process is gone
            self.assertEqual(fc.fc_tasks.run_ended(proj), "interrupted")
            ev = fc.fc_watch.watch_events(wsnap("running", run=True), wsnap("running", ended=fc.fc_tasks.run_ended(proj)))
            self.assertTrue("INTERRUPTED" in ev[0] and "`compose resume`" in ev[0] and "CRASHED" not in ev[0], ev)
            (proj / "results" / "run.json").write_text(json.dumps({"pid": 999999}))  # no stop recorded
            self.assertEqual(fc.fc_tasks.run_ended(proj), "crashed")
        (proj / "results" / "run.json").write_text(json.dumps({"pid": os.getpid()}))
        self.assertIsNone(fc.fc_tasks.run_ended(proj))  # a live run has not ended

    def test_a_second_ctrl_c_during_the_stop_changes_nothing(self):
        self.ctrl_c(stop=lambda: os.kill(os.getpid(), signal.SIGINT))

    def run_one(self, effect):
        repo, calls = Path(tempfile.mkdtemp()), []; proj = repo / "proj"
        for c in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"]):
            subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
        task = {"id": "001", "title": "t", "files": ["a.txt"], "protected": [], "test": "false", "model": "pi:p/primary", "after": "", "linear": "", "body": "## Goal\nx"}
        def impl(cwd, model, prompt, session=None, task=None, timeout=None):
            calls.append(model); fc.fc_proc.STOP.set()
            if effect: raise effect
            return "s", "", 1, {"tokens_in": 1, "tokens_out": 1, "cost": 0.0}
        with mock.patch.object(fc.fc_implement, "implementer", impl), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), \
                mock.patch.object(fc.fc_config, "DEFAULT_MODEL", "pi:p/primary"), mock.patch.object(fc.fc_config, "FALLBACK_MODEL", "pi:p/backup"), \
                mock.patch.object(fc.fc_config, "FALLBACK_AFTER", 1), mock.patch.object(fc.fc_config, "TEST_READER", None), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}), \
                contextlib.suppress(fc.fc_proc.Stopped):
            fc.fc_implement.run_task(repo, proj, task, 3)
            self.assertIsNone(effect, "an implementer failure after the stop raises Stopped")
        self.assertEqual(calls, ["pi:p/primary"])  # no further attempt, no fallback
        return json.loads((proj / "results" / "001.json").read_text()), fc.fc_status.task_state(repo, proj, task, "")["state"]

    def test_after_the_stop_an_implementer_error_or_timeout_is_no_attempt_and_no_turn_starts_another(self):
        for effect in (RuntimeError("pi exited"), subprocess.TimeoutExpired("pi", 1), None):  # the stop lands in the turn, which then ends as `effect`
            with self.subTest(effect=type(effect).__name__):
                (result, state), _ = self.run_one(effect), fc.fc_proc.STOP.clear()
                if effect: self.assertEqual((result["status"], result["log"], state), ("running", [], "interrupted"))

    def test_a_herdr_agent_gets_esc_then_its_pane_closed_on_the_stop(self):
        calls, hooked, root, name = [], [], Path(tempfile.mkdtemp()), fc.fc_herdr.agent_name("shop", "028")
        def prompt(*a, **k):  # the stop lands while the agent works on its prompt
            n = len(calls); hooked.extend(fc.fc_proc.HOOKS); fc.fc_proc.HOOKS.get(name, lambda: None)(); hooked.append(calls[n:])
            return None, "stopped"
        with mock.patch.object(fc.fc_herdr, "herdr", lambda *a: calls.append(a) or {}), mock.patch.object(fc.fc_herdr, "herdr_agent_start", return_value={}), \
                mock.patch.object(fc.fc_herdr, "herdr_implementer_pane", return_value="p1"), mock.patch.object(fc.fc_herdr, "herdr_error", prompt), self.assertRaises(RuntimeError):
            fc.fc_implement.herdr_pi(str(root / "wt"), "openrouter/x/y", "hi", None, {"meta": {"id": "028", "title": "t"}, "proj": root / "shop", "space": "w9"})
        self.assertEqual(hooked, [name, [("agent", "send-keys", name, "esc"), ("pane", "close", "p1")]])


class ReadyEvent(unittest.TestCase):
    """Issues that became ready wake the watch with one line naming only the new ones, for the product owner to hear."""
    LINE = "event ready=NEB-2,NEB-3  (can start now: NEB-2 B, NEB-3 C; tell the product owner)"

    def events(self, before, after):
        b, a = wsnap(), wsnap()
        if before is not None:
            b["ready"] = before
        a["ready"] = after
        return fc.fc_watch.watch_events(b, a)

    def test_only_newly_ready_ids_give_the_line(self):
        self.assertEqual(self.events({"NEB-1": "A"}, {"NEB-1": "A", "NEB-2": "B", "NEB-3": "C"}), [self.LINE])
        self.assertEqual(self.events(None, {"NEB-2": "B", "NEB-3": "C"}), [self.LINE])  # an older snapshot has no ready key
        self.assertEqual(self.events({"NEB-1": "A"}, {"NEB-2": "B", "NEB-3": "C"}), [self.LINE])  # one left, two came: only the new
        self.assertTrue(self.events({}, {"NEB-3": "C", "NEB-2": "B"})[0].startswith("event ready=NEB-3,NEB-2  ("))  # list order, not sorted
        for before, after in (({"NEB-1": "A", "NEB-2": "B"}, {"NEB-1": "A"}), ({"NEB-1": "A"}, {"NEB-1": "A"}), ({"NEB-1": "A"}, {}), ({}, {})):
            self.assertEqual(self.events(before, after), [])

    def snapshot(self, text=None):
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        if text is not None:
            (proj / "results" / "ready.json").write_text(text)
        with mock.patch.object(fc.fc_herdr, "herdr", return_value=None), mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[]):
            return fc.fc_watch.watch_snapshot(Path("."), proj)["ready"]

    def test_the_snapshot_reads_ready_json(self):
        items = [{"id": "NEB-3", "title": "C"}, {"id": "NEB-2", "title": "B"}]
        self.assertEqual(list(self.snapshot(json.dumps(items, indent=2)).items()), [("NEB-3", "C"), ("NEB-2", "B")])
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(self.snapshot("[{not json"), {})

    def watch(self, *snaps, **kw):
        a = SimpleNamespace(repo=".", now=False, from_=None, timeout=0.05, interval=0.01, stall=10_000, rewake=False)
        a.__dict__.update(kw)
        out, it = [], iter(snaps)
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_ROLE"}  # the engineering manager's watch: the one that notifies
        with mock.patch.object(fc.fc_core, "project_dir", return_value=Path(tempfile.mkdtemp()) / "p"), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(fc.fc_watch, "watch_snapshot", side_effect=lambda *_: next(it, snaps[-1])), mock.patch("builtins.print", out.append):
            with contextlib.suppress(SystemExit):
                fc.fc_watch.cmd_watch(a)
        return out

    def test_now_and_from_print_the_line_and_nothing_pops_up(self):
        none, ready = wsnap(), wsnap()
        none["ready"], ready["ready"] = {}, {"NEB-2": "B", "NEB-3": "C"}
        notify = mock.Mock()
        with mock.patch.object(fc.fc_crew, "desktop_notify", notify):
            self.assertIn(self.LINE, self.watch(ready, now=True))
            out = self.watch(none, ready, from_="orchestrator")
        self.assertTrue(any(line.endswith("] " + self.LINE) for line in out), out)
        notify.assert_not_called()
        self.assertEqual(fc.fc_watch.watch_filter([self.LINE], "p", ["p-orchestrator"]), [self.LINE])


class ManagerSessions(unittest.TestCase):
    """Two engineering manager sessions on one project, each in its own herdr pane: A (w1:p1) started a designer, B (w1:p2) a
    planner and the team lead, whose test writer reports to it. Each watch wakes only on its own session's teammates."""
    A, B = "w1:p1", "w1:p2"

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        crew = self.proj / "results" / "crew"; crew.mkdir(parents=True)
        for name, role, up, by in (("hw-designer", "designer", "manager", self.A), ("hw-planner", "planner", "manager", self.B),
                                   (ORCH, "orchestrator", "manager", self.B), ("hw-tests-001", "tests-001", "orchestrator", "w1:p9")):
            (crew / f"{name}.json").write_text(json.dumps({"name": name, "role": role, "title": role, "space": "w1", "tab": "t", "pane": "p",
                                                           "reports_to": up, "started_by": by}))
            fc.fc_crew.write_report(self.proj, name, "working", "started")
        self.live = [self.A, self.B]

    def agents(self, *_a, **_k):
        names = [f.stem for f in (self.proj / "results" / "crew").glob("*.json")]
        return {"agents": [{"name": n, "workspace_id": "w1", "agent_status": "working", "pane_id": f"w1:x{i}"} for i, n in enumerate(names)]
                + [{"name": f"claude{i}", "workspace_id": "w0", "agent_status": "idle", "pane_id": p} for i, p in enumerate(self.live)]}

    def watch(self, pane, from_=None, now=False):
        a = SimpleNamespace(repo=".", now=now, from_=from_, timeout=0.05, interval=0.01, stall=10_000, rewake=False)
        out = []
        env = {k: v for k, v in os.environ.items() if k not in ("COMPOSE_ROLE", "HERDR_PANE_ID")} | {"HERDR_PANE_ID": pane}
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", self.agents), \
                mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[]), mock.patch.object(fc.fc_crew, "desktop_notify"), \
                mock.patch("builtins.print", out.append), mock.patch.dict(os.environ, env, clear=True):
            with contextlib.suppress(SystemExit):
                fc.fc_watch.cmd_watch(a)
        return out

    def woke_on(self, out):
        return sorted({m.group(1) for line in out if (m := re.search(r"event (?:crew|stall=ended agent)=(\S+)", line))})

    def test_each_session_wakes_on_its_own_teammates_only(self):
        self.watch(self.A, "designer", now=True); self.watch(self.B, "orchestrator", now=True)
        for name, state in (("hw-planner", "needs-user"), ("hw-designer", "needs-user"), ("hw-tests-001", "tool-bug")):
            fc.fc_crew.write_report(self.proj, name, state, "x")
        self.assertEqual(self.woke_on(self.watch(self.A, "designer")), ["hw-designer"])  # not B's planner, not a tool-bug in B's team lead's tree
        self.assertEqual(self.woke_on(self.watch(self.B, "orchestrator")), ["hw-planner", "hw-tests-001"])  # A's watch did not swallow them
        self.assertEqual(sorted(f.name for f in (self.proj / "results").glob("watch-*.json")), ["watch-manager-w1-p1.json", "watch-manager-w1-p2.json"])
        fc.fc_crew.write_report(self.proj, "hw-planner", "needs-user", "y")
        self.assertEqual(self.woke_on(self.watch(self.A)), [])  # no --from: still never another session's teammate
        fc.fc_crew.write_report(self.proj, "hw-planner", "needs-user", "z")
        self.assertEqual(self.woke_on(self.watch(self.A, "planner")), ["hw-planner"])  # named in --from: it asked for it

    def test_a_bare_watch_hears_its_own_team_lead_and_never_another_sessions(self):
        """An engineering manager's watch started without `--from` is the `--from orchestrator` watch once a team lead exists, so
        the team lead's needs-user, blocked and done wake it; another live session's team lead still does not."""
        self.watch(self.A, now=True); self.watch(self.B, now=True)
        fc.fc_crew.write_report(self.proj, ORCH, "needs-user", "4 open questions")
        self.assertEqual(self.woke_on(self.watch(self.B)), [ORCH])
        self.assertEqual(self.woke_on(self.watch(self.A)), [])

    def test_the_team_leads_own_bare_watch_skips_its_reports_after_a_restart_lost_its_role(self):
        """A team lead restored after a restart runs without COMPOSE_ROLE: its record's pane says who it is, so it never listens to itself."""
        rec = self.proj / "results" / "crew" / f"{ORCH}.json"
        rec.write_text(json.dumps({**json.loads(rec.read_text()), "pane": "w1:p7"}))
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in ("COMPOSE_ROLE", "HERDR_PANE_ID")}, clear=True); env.start(); self.addCleanup(env.stop)
        self.assertEqual(fc.fc_watch.lead_sources(self.proj), [ORCH])
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p7"}):
            self.assertEqual(fc.fc_watch.lead_sources(self.proj), [])
        with mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}):
            self.assertEqual(fc.fc_watch.lead_sources(self.proj), [])
        rec.write_text(json.dumps({**json.loads(rec.read_text()), "closed": "done"}))
        self.assertEqual(fc.fc_watch.lead_sources(self.proj), [])

    def test_a_teammate_whose_session_ended_is_adopted(self):
        """B's session closed: its planner and team lead are anyone's, so a new engineering manager session picks the project up."""
        self.watch(self.A, "designer", now=True)
        self.live = [self.A]
        fc.fc_crew.write_report(self.proj, "hw-planner", "needs-user", "x")
        fc.fc_crew.write_report(self.proj, "hw-tests-001", "tool-bug", "x")
        self.assertEqual(self.woke_on(self.watch(self.A, "designer")), ["hw-planner", "hw-tests-001"])

    def test_owners_follow_the_live_session_and_the_team_lead(self):
        records = [json.loads(f.read_text()) for f in sorted((self.proj / "results" / "crew").glob("*.json"))]
        owner = lambda live: fc.fc_watch.crew_owners(records, None if live is None else [{"pane_id": p} for p in live])
        self.assertEqual(owner([self.A, self.B]), {"hw-designer": self.A, "hw-planner": self.B, ORCH: self.B, "hw-tests-001": self.B})
        self.assertEqual(owner([self.B]), {"hw-planner": self.B, ORCH: self.B, "hw-tests-001": self.B})
        self.assertEqual(owner(None), owner([self.A, self.B]))  # herdr cannot say: every session counts as live
        records.append({"name": "hw-old", "reports_to": "manager"})  # from before started_by: nobody's, as before
        self.assertNotIn("hw-old", owner([self.A, self.B]))


class OwnerDigest(unittest.TestCase):
    """The product owner hears every state change once, in one line, and nothing that repeats: `status --digest` and `event tell=`."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results" / "crew").mkdir(parents=True)

    def test_verify_why_names_the_stage_the_count_and_a_tool_error(self):
        why = fc.fc_status.verify_why
        self.assertEqual(why({}), "")
        self.assertEqual(why({"rounds": [{"checks": [{"exit": 0}, {"exit": 1}], "e2e": []}]}), "checks 1/2 failed")
        self.assertEqual(why({"rounds": [{"checks": [], "e2e": [{"exit": 2, "tool_error": True}]}]}), "e2e 1/1 failed - tool errors, not the code")
        self.assertEqual(why({"rounds": [{"checks": [{"exit": 1, "tool_error": True}], "e2e": [{"exit": 1}]}]}), "checks 1/1 failed, e2e 1/1 failed")
        r = {"checks": [{"exit": 0}], "e2e": [], "review": {"pass": False, "confirmed": ["a", "b"]}}
        self.assertEqual(why({"rounds": [r, r], "rejected_fixes": ["x"]}),
                         "review 2 confirmed, a fix touched files outside the stack after 1 fix round(s)")
        r2 = {"round": 1, "files": ["a.go"], "why": "w", "next": "cut a new top task"}
        self.assertEqual(why({"rounds": [r, r], "rejected_fixes": [r2]}),
                         "review 2 confirmed, a fix needs a lower layer's file: cut a new top task after 1 fix round(s)")

    def test_the_digest_is_one_honest_line(self):
        rows = [{"id": "001", "state": "merged"}, {"id": "002", "state": "failed"}, {"id": "003", "state": "pending"}]
        (self.proj / "results" / "verify.json").write_text(json.dumps({"passed": False, "rounds": [{"checks": [{"exit": 1}], "e2e": []}]}))
        (self.proj / "results" / "crew" / "hw-planner.status").write_text(json.dumps({"state": "needs-user", "message": "which database?"}) + "\n")
        with mock.patch.object(fc.fc_status, "board", return_value={"rows": rows}):
            line = fc.fc_status.digest(self.proj, self.proj)
        self.assertNotIn("\n", line)
        self.assertRegex(line, r"^hw: 1/3 tasks done, failed 002; no run; verify FAIL at \d\d:\d\d \(checks 1/1 failed\); hw-planner asks: which database\?$")

    def test_status_digest_prints_one_line_per_project_and_nothing_else(self):
        out = []
        with mock.patch.object(fc.fc_status, "board", return_value={"rows": []}), mock.patch("builtins.print", out.append), \
                mock.patch.object(fc.fc_core, "all_projects", return_value={"hw": (self.proj, self.proj)}):
            fc.fc_status.cmd_status(SimpleNamespace(repo=str(self.proj), project="hw", digest=True, all=False, watch=False, json=False))
        self.assertEqual(len(out), 1, out)
        self.assertTrue(out[0].startswith("hw: 0/0 tasks done; no run"), out)

    def tell(self, before, after):
        return fc.fc_watch.owner_lines("hw", before, after)

    def test_a_failed_verify_says_why_and_a_passed_one_says_in_review(self):
        a, b = wsnap(), wsnap()
        b["verify"], b["verify_why"] = (1, False), "checks 1/3 failed"
        self.assertEqual(self.tell(a, b), ["event tell='verify failed: checks 1/3 failed'"])
        self.assertEqual(self.tell(b, b), [])  # nothing repeats
        c = wsnap(); c["verify"] = (2, True)
        self.assertEqual(self.tell(b, c), ["event tell='verify passed - the feature is In Review'"])

    def test_a_run_ending_and_a_fix_loop_starting_are_told_once(self):
        live, ended = wsnap("running", run=True), wsnap("failed")
        self.assertEqual(self.tell(live, ended), ["event tell='run ended: 0/1 passed, failed 001'"])
        self.assertEqual(self.tell(live, wsnap("running", ended="crashed")), ["event tell='run crashed before finishing'"])
        self.assertEqual(self.tell(live, wsnap("running", ended="interrupted")), ["event tell='run stopped by Ctrl-C - compose resume continues it'"])
        failed = wsnap(); failed["verify"] = (1, False)
        restarted = wsnap("running", run=True); restarted["verify"] = (1, False)
        self.assertEqual(self.tell(failed, restarted), ["event tell='fix loop started: the run restarted after the failed verify'"])
        self.assertEqual(self.tell(restarted, restarted), [])

    def test_the_team_lead_done_is_told_and_working_is_not(self):
        a, b = wsnap(), wsnap()
        a["crew"] = {ORCH: [{"at": at(9), "state": "working", "message": "x"}]}
        b["crew"] = {ORCH: a["crew"][ORCH] + [{"at": at(1), "state": "working", "message": "still going"}]}
        self.assertEqual(self.tell(a, b), [])
        c = {**b, "crew": {ORCH: b["crew"][ORCH] + [{"at": at(0), "state": "done", "message": "shipped"}]}}
        self.assertEqual(self.tell(b, c), ["event tell='done: shipped'"])

    def test_a_running_task_is_not_an_event_but_a_final_one_is(self):
        self.assertEqual(fc.fc_watch.watch_events(wsnap("pending"), wsnap("running")), [])
        self.assertTrue(fc.fc_watch.watch_events(wsnap("running"), wsnap("passed")))

    def test_the_tell_lines_pass_the_filter(self):
        line = "event tell='verify failed: checks 1/3 failed'"
        self.assertEqual(fc.fc_watch.watch_filter([line], "hw", [ORCH]), [line])

    def test_the_mac_plugged_in_after_a_battery_stop_wakes_every_watch(self):
        line = "event power=ac held=03:14:28  (the Mac is plugged in: start again, with no ask, whatever a low battery stopped)"
        self.assertEqual((fc.fc_watch.watch_filter([line], "hw", [ORCH]), fc.fc_watch.watch_filter([line], "hw", [])), ([line], [line]))
        self.assertTrue(fc.fc_watch.ATTENTION.search(line))  # the engineering manager's quiet watch wakes for it

    def test_progress_pings_are_info_and_the_rest_may_be_urgent(self):
        is_info, urgent = fc.fc_watch.is_info, fc.fc_watch.URGENT
        self.assertTrue(is_info("event crew=hw-designer state=working message=drawing page 2"))
        self.assertFalse(is_info("event crew=hw-tests-001 state=working message=Test ready: abc"))
        self.assertFalse(is_info("event crew=hw-designer state=done message=x"))
        for e in ("event stall=quiet x", "event halt=payment", "event crew=a state=needs-user message=q", "event crew=a state=tool-bug message=q"):
            self.assertTrue(urgent.search(e), e)
        self.assertFalse(urgent.search("event task=001 state=passed"))


class QuietWatch(unittest.TestCase):
    """Fewer wake-ups: progress pings wait for a wake (or INFO_HOLD), a burst of events is one wake, and the watch never times out."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        self.proj.mkdir(parents=True)

    def snap(self, *reports, state="pending"):
        s = wsnap(state)
        s["crew"] = {"hw-designer": [{"at": at(60 - i), "state": st, "message": m} for i, (st, m) in enumerate(reports)]}
        return s

    def watch(self, base, snaps, hold=300, **kw):
        fc.fc_watch.watch_save(self.proj, "manager", base=base)
        a = SimpleNamespace(repo=".", now=False, from_=None, timeout=0, interval=0.01, stall=10_000, rewake=False, settle=0)
        a.__dict__.update(kw)
        out, it = [], iter(snaps)
        snapshot = lambda *_a, **_k: next(it, snaps[-1])
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_ROLE"}
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_watch, "watch_snapshot", snapshot), \
                mock.patch.object(fc.fc_watch, "watch_key", return_value="manager"), mock.patch.object(fc.fc_watch, "INFO_HOLD", hold), \
                mock.patch.object(fc.fc_crew, "desktop_notify"), mock.patch("builtins.print", out.append), mock.patch.dict(os.environ, env, clear=True):
            with contextlib.suppress(SystemExit):
                fc.fc_watch.cmd_watch(a)
        return out

    def test_a_progress_ping_alone_does_not_wake_but_travels_with_the_next_wake(self):
        base = self.snap(("working", "started"))
        ping = self.snap(("working", "started"), ("working", "drawing page 2"))
        done = self.snap(("working", "started"), ("working", "drawing page 2"), ("done", "all pages"))
        out = self.watch(base, [ping, ping, ping, done])
        self.assertEqual(len([l for l in out if "event crew=" in l]), 2, out)  # one wake carries both lines
        self.assertTrue(any("drawing page 2" in l for l in out) and any("state=done" in l for l in out), out)

    def test_a_held_ping_wakes_after_info_hold(self):
        base = self.snap(("working", "started"))
        ping = self.snap(("working", "started"), ("working", "drawing page 2"))
        out = self.watch(base, [ping], hold=0)
        self.assertTrue(any("drawing page 2" in l for l in out), out)

    def test_a_held_ping_alone_never_wakes_before_info_hold(self):
        base = self.snap(("working", "started"))
        ping = self.snap(("working", "started"), ("working", "drawing page 2"))
        out = self.watch(base, [ping], timeout=0.2)
        self.assertFalse(any("drawing page 2" in l for l in out), out)

    def test_a_burst_is_one_wake(self):
        base = self.snap(("working", "started"), state="running")
        first = self.snap(("working", "started"), state="passed")
        burst = self.snap(("working", "started"), ("done", "all pages"), state="passed")
        with mock.patch.object(fc.fc_watch.time, "sleep"):
            out = self.watch(base, [first, burst], settle=5)
        self.assertTrue(any("task=001 state=passed" in l for l in out), out)
        self.assertTrue(any("crew=hw-designer state=done" in l for l in out), out)  # arrived during the settle


class AttentionWatch(unittest.TestCase):
    """The engineering manager's `--from orchestrator` watch (and any watch given --attention) wakes only for what needs attention."""
    def setUp(self):
        self.quiet = QuietWatch("test_a_held_ping_wakes_after_info_hold")
        self.quiet.setUp()

    def snap(self, *reports, state="pending"):
        s = wsnap(state)
        s["crew"] = {"hw-orchestrator": [{"at": at(60 - i), "state": st, "message": f"{m}; parallel: started m" if st == "working" else m}
                                         for i, (st, m) in enumerate(reports)]}
        return s

    def watch(self, base, snaps, **kw):
        return self.quiet.watch(base, snaps, hold=0, **kw)

    def test_the_orchestrator_watch_holds_a_progress_ping_past_the_hold(self):
        base, ping = self.snap(("working", "started")), self.snap(("working", "started"), ("working", "tests green"))
        self.assertFalse(any("tests green" in l for l in self.watch(base, [ping], from_="orchestrator", timeout=0.2)))

    def test_held_progress_travels_with_the_next_attention_line(self):
        base = self.snap(("working", "started"))
        ping = self.snap(("working", "started"), ("working", "tests green"))
        for last in (("done", "all green"), ("needs-user", "which db?"), ("tool-bug", "crashed")):
            out = self.watch(base, [ping, ping, self.snap(("working", "started"), ("working", "tests green"), last)], from_="orchestrator")
            self.assertTrue(any("tests green" in l for l in out) and any(f"state={last[0]} " in l for l in out), (last, out))

    def test_all_wakes_on_every_line_again(self):
        base, ping = self.snap(("working", "started")), self.snap(("working", "started"), ("working", "tests green"))
        self.assertTrue(any("tests green" in l for l in self.watch(base, [ping], from_="orchestrator", all=True)))

    def test_attention_quiets_a_watch_without_from_and_task_progress_waits_for_a_done(self):
        base = self.snap(("working", "started"), state="running")
        passed = self.snap(("working", "started"), state="passed")
        done = self.snap(("working", "started"), ("done", "all green"), state="passed")
        self.assertFalse(any("task=001" in l for l in self.watch(base, [passed], attention=True, timeout=0.2)))
        self.assertTrue(any("task=001 state=passed" in l for l in self.watch(base, [passed], timeout=0.2)))
        out = self.watch(base, [passed, passed, done], attention=True)
        self.assertTrue(any("task=001 state=passed" in l for l in out) and any("tell='done: all green'" in l for l in out), out)  # the team lead's own report reaches the engineering manager as a tell

    def test_a_stall_and_a_sign_in_screen_are_attention(self):
        self.assertTrue(all(fc.fc_watch.ATTENTION.search(e) or fc.fc_watch.URGENT.search(e) for e in
                            ("event stall=silent agent=hw-x", "event halt=payment provider=openai", "event crew=hw-x state=done message='x'",
                             "event agent=hw-x status=blocked  sign-in screen", "event tell='done: x'")))
        self.assertFalse(any(fc.fc_watch.ATTENTION.search(e) or fc.fc_watch.URGENT.search(e) for e in
                             ("event crew=hw-x state=working message='x'", "event task=001 state=passed", "event verify=pass")))

    def test_a_mac_sleep_and_a_limit_or_silent_stall_wake_the_attention_watch(self):
        for e in ("event sleep=woke from=01:00 to=02:00 slept=60m00.0s", "event sleep=resumed run=x", "event sleep=run-lost reason=x",
                  "event stall=limit agent=hw-x line='usage limit reached'", "event stall=silent agent=hw-x"):
            self.assertTrue(fc.fc_watch.ATTENTION.search(e) or fc.fc_watch.URGENT.search(e), e)


class NetworkDrop(unittest.TestCase):
    """A teammate whose last turn ended on an API or network error sits idle with its work unreported: the watch surfaces it once."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)

    def snap(self, status="idle", state="working", closed=()):
        return {"tasks": {"001": {"state": "pending"}}, "agents": {"hw-designer": status}, "closed": list(closed),
                "crew": {"hw-designer": [{"at": at(0), "state": state, "message": "m"}]}, "run": False, "verify": None, "run_end": 0, "last": (NOW, "x")}

    def stalls(self, snap, now, pane):
        with mock.patch.object(fc.fc_herdr, "herdr_text", return_value=pane):
            ev = fc.fc_watch.watch_stalls(self.proj, snap, 10_000, now_s=now)
        return [e for e in ev if "stall=network" in e]

    def test_an_idle_teammate_after_an_api_error_is_surfaced_once_with_the_nudge(self):
        err = "> drawing\nAPI Error: Unable to connect to API (ENOTFOUND)\n"
        self.assertEqual(self.stalls(self.snap(), NOW, err), [])  # idle just now: a normal pause
        ev = self.stalls(self.snap(), NOW + fc.fc_watch.NET_IDLE + 1, err)
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("agent=hw-designer", ev[0])
        self.assertIn("The network dropped and is back; continue exactly where you stopped; re-check any write that may not have landed", ev[0])
        self.assertIn("herdr agent prompt hw-designer", ev[0])
        self.assertEqual(self.stalls(self.snap(), NOW + 500, err), [])  # once per idle spell

    def test_a_quiet_pane_a_done_or_closed_or_working_teammate_is_not_a_network_drop(self):
        later = NOW + fc.fc_watch.NET_IDLE + 1
        for snap, pane in ((self.snap(), "> drawing\nwaiting for you\n"), (self.snap(state="done"), "API Error: x"),
                           (self.snap(closed=["hw-designer"]), "API Error: x"), (self.snap("working"), "API Error: x")):
            self.setUp()
            self.stalls(snap, NOW, pane)
            self.assertEqual(self.stalls(snap, later, pane), [], snap)

    def test_the_network_stall_is_urgent_and_passes_the_filter(self):
        line = "event stall=network agent=hw-designer x"
        self.assertTrue(fc.fc_watch.URGENT.search(line))
        self.assertEqual(fc.fc_watch.watch_filter([line], "hw", ["hw-designer"]), [line])


class ClosableIdle(unittest.TestCase):
    """A teammate that finished its turn, reported only working, and sat idle for the stall window is named closable once, with a nudge that ends in done."""
    snap, setUp = NetworkDrop.snap, NetworkDrop.setUp

    def closable(self, snap, now, writer=False):
        if writer:
            snap = {**snap, "agents": {"hw-tests-001": "idle"}, "crew": {"hw-tests-001": [{"at": at(0), "state": "working", "message": "test ready"}]}}
        with mock.patch.object(fc.fc_herdr, "herdr_text", return_value="> done for now\n"):
            return [e for e in fc.fc_watch.watch_stalls(self.proj, snap, 240, now_s=now) if "stall=closable" in e]

    def test_an_idle_working_teammate_is_named_closable_once_after_the_stall_window(self):
        self.assertEqual(self.closable(self.snap(), NOW), [])
        self.assertEqual(self.closable(self.snap(), NOW + 239), [])
        ev = self.closable(self.snap(), NOW + 241)
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("agent=hw-designer", ev[0])
        self.assertIn("herdr agent prompt hw-designer \"If nothing is left for you, report done\"", ev[0])
        self.assertNotIn("--force", ev[0])
        self.assertTrue(fc.fc_watch.URGENT.search(ev[0]))
        self.assertEqual(self.closable(self.snap(), NOW + 600), [])  # once per idle spell

    def test_a_working_done_closed_or_collectable_teammate_is_not_closable(self):
        self.closable(self.snap(), NOW)
        for snap, writer in ((self.snap("working"), False), (self.snap(state="done"), False), (self.snap(closed=["hw-designer"]), False), (self.snap(), True)):
            self.setUp()
            self.closable(snap, NOW, writer)
            self.assertEqual(self.closable(snap, NOW + 500, writer), [], snap)


class OneManager(unittest.TestCase):
    """One engineering manager session per project: another one's `crew start` or `watch` refuses unless it says --takeover."""
    A, B = "w1:p1", "w1:p2"

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results" / "crew").mkdir(parents=True)
        (self.proj / "results" / "crew" / "hw-designer.json").write_text(json.dumps({"name": "hw-designer", "started_by": self.A}))
        self.live = [self.A, self.B]

    def listed(self, *_a, **_k):
        return {"agents": [{"pane_id": p, "name": f"claude{i}"} for i, p in enumerate(self.live)]}

    def claim(self, pane, takeover=False, role=None, herdr_on=True):
        env = {"HERDR_PANE_ID": pane} | ({"COMPOSE_ROLE": role} if role else {})
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(fc.fc_herdr, "herdr", self.listed), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=herdr_on):
            fc.fc_herdr.herdr_manager_claim(self.proj, takeover)

    def owner(self):
        return fc.fc_herdr.herdr_manager_record(self.proj).get("session")

    def test_the_first_session_claims_and_a_second_refuses_naming_the_first(self):
        self.claim(self.A)
        self.assertEqual(self.owner(), self.A)
        self.claim(self.A)  # the owner again: nothing changes
        with self.assertRaises(SystemExit) as e:
            self.claim(self.B)
        self.assertIn(self.A, str(e.exception))
        self.assertIn("--takeover", str(e.exception))
        self.assertEqual(self.owner(), self.A)

    def test_takeover_moves_the_claim_and_the_owners_teammates(self):
        self.claim(self.A)
        self.claim(self.B, takeover=True)
        self.assertEqual(self.owner(), self.B)
        self.assertEqual(json.loads((self.proj / "results" / "crew" / "hw-designer.json").read_text())["started_by"], self.B)

    def test_an_owner_whose_pane_is_gone_is_replaced_silently(self):
        self.claim(self.A)
        self.live = [self.B]
        self.claim(self.B)
        self.assertEqual(self.owner(), self.B)

    def test_a_crew_role_and_a_session_outside_herdr_claim_nothing(self):
        self.claim(self.A)
        self.claim(self.B, role="orchestrator")
        self.claim(self.B, herdr_on=False)
        self.assertEqual(self.owner(), self.A)

    def test_watch_now_says_who_owns_the_project_without_claiming_it(self):
        self.claim(self.A)
        out = []
        a = SimpleNamespace(repo=".", now=True, from_=None, timeout=0, interval=0.01, stall=10_000, rewake=False, settle=0)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=wsnap()), \
                mock.patch.object(fc.fc_herdr, "herdr", self.listed), mock.patch("builtins.print", out.append), \
                mock.patch.dict(os.environ, {"HERDR_PANE_ID": self.B}, clear=True):
            fc.fc_watch.cmd_watch(a)
        self.assertTrue(any(l.startswith(f"event owner=other pane={self.A}") for l in out), out)
        self.assertEqual(self.owner(), self.A)


class RestoredTeammate(unittest.TestCase):
    """A teammate's session that comes back without the env its tab was created with (a computer restart) is still that teammate."""
    PANE = "w10:pK"

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results" / "crew").mkdir(parents=True)
        (self.proj / "results" / "crew" / "hw-orchestrator.json").write_text(json.dumps({"name": "hw-orchestrator", "role": "orchestrator", "pane": self.PANE}))
        (self.proj / "results" / "crew" / "hw-tests-001.json").write_text(json.dumps({"name": "hw-tests-001", "role": "tests-001", "pane": "w10:pT"}))

    def restore(self, pane, env=None):
        env = {"HERDR_PANE_ID": pane} | (env or {})
        with mock.patch.dict(os.environ, env, clear=True):
            fc.fc_hooks.restore_crew_env(self.proj)
            return dict(os.environ)

    def test_the_team_lead_pane_gets_its_role_back_and_claims_no_manager_seat(self):
        env = self.restore(self.PANE)
        self.assertEqual((env["COMPOSE_ROLE"], env["COMPOSE_TEAMMATE"], env["COMPOSE_PROJECT"]), ("orchestrator", "hw-orchestrator", "hw"))
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True):
            fc.fc_herdr.herdr_manager_claim(self.proj)  # no SystemExit, no manager.json
        self.assertEqual(fc.fc_herdr.herdr_manager_record(self.proj), {})

    def test_a_crew_teammate_gets_the_crew_role_and_a_manager_pane_gets_nothing(self):
        self.assertEqual(self.restore("w10:pT")["COMPOSE_ROLE"], "crew")
        self.assertNotIn("COMPOSE_ROLE", self.restore("w1Z:p1"))

    def test_a_role_already_set_is_kept(self):
        self.assertEqual(self.restore(self.PANE, {"COMPOSE_ROLE": "crew"})["COMPOSE_ROLE"], "crew")

    def test_the_edit_guard_finds_the_role_of_a_restored_team_lead_from_the_pane(self):
        repo = self.proj.parent
        fc.fc_core.sh(["git", "init", "-q"], repo)
        (repo / ".compose").mkdir()
        self.proj.rename(repo / ".compose" / "hw")
        (repo / ".compose" / "hw" / "verify.json").write_text("{}")
        def guard(pane):
            ev, out = {"tool_name": "Write", "cwd": str(repo), "tool_input": {"file_path": str(repo / "a.go")}}, io.StringIO()
            with mock.patch.dict(os.environ, {"HERDR_PANE_ID": pane}, clear=True), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(ev))), mock.patch.object(sys, "stdout", out):
                fc.fc_hooks.cmd_guard(None)
            return out.getvalue()
        self.assertIn("never edit code", guard(self.PANE))
        self.assertEqual((guard("w10:pT"), guard("w1Z:p1")), ("", ""))  # a crew teammate and the engineering manager

    def test_a_restored_team_lead_gets_its_watch_at_a_turn_end_and_other_panes_get_none(self):
        repo = self.proj.parent
        fc.fc_core.sh(["git", "init", "-q"], repo)
        (repo / ".compose").mkdir()
        self.proj = self.proj.rename(repo / ".compose" / "hw")
        (self.proj / "verify.json").write_text("{}")
        def rewake(pane, snaps):
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"HERDR_PANE_ID": pane, "CLAUDE_CODE_ENTRYPOINT": "cli"}, clear=True), \
                    mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), \
                    mock.patch.object(fc.fc_watch, "watch_snapshot", side_effect=snaps), mock.patch.object(fc.fc_watch, "WATCH_INTERVAL", 0.01), mock.patch.object(fc.fc_watch, "WATCH_SETTLE", 0), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": str(repo)}))), contextlib.redirect_stderr(err):
                try:
                    fc.fc_watch.cmd_rewake(SimpleNamespace())
                    code = 0
                except SystemExit as e:
                    code = e.code
            return code, err.getvalue()
        code, err = rewake(self.PANE, [wsnap("running"), wsnap("running"), wsnap("passed")])
        self.assertEqual(code, 2)
        self.assertIn("event task=001 state=passed", err)
        self.assertEqual((rewake("w10:pT", []), rewake("w1Z:p1", [])), ((0, ""), (0, "")))  # a crew teammate and the engineering manager


class SharedGuidance(unittest.TestCase):
    def test_every_teammate_brief_carries_the_wait_and_shell_rules(self):
        proj = Path(tempfile.mkdtemp()) / "hw"; proj.mkdir(parents=True)
        for name, title, up in (("hw-orchestrator", "orchestrator", None), ("hw-tests-001", "tests 001", "orchestrator"), ("hw-planner", "planner", "manager")):
            pre = fc.fc_crew.crew_preamble(proj, name, title, up)
            for needle in ("not word-split", "ToolSearch", "`sleep` longer than a few seconds is blocked", "cannot wake you", "background command that exits when it is true"):
                self.assertIn(needle, pre, (name, needle))


class WatchBaseFile(unittest.TestCase):  # the memory is written only on a change; its base snapshot has its own file
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        self.json, self.base = (self.proj / "results" / f"watch-m.{x}" for x in ("json", "base"))

    def watch(self, snap, repo=".", timeout=0.3):
        a = SimpleNamespace(repo=repo, now=False, from_=None, timeout=timeout, interval=0.01, stall=10_000, rewake=False, settle=0)
        out = []
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_watch, "watch_key", return_value="m"), \
                mock.patch.object(fc.fc_watch, "watch_snapshot", snap), mock.patch.object(fc.fc_crew, "desktop_notify"), \
                mock.patch("builtins.print", out.append), mock.patch.dict(os.environ, {"COMPOSE_ROLE": ""}):
            with contextlib.suppress(SystemExit):
                fc.fc_watch.cmd_watch(a)
        return out

    def test_stalls_save_only_on_a_change(self):
        snap = {"tasks": {"001": {"state": "pending"}}, "agents": {ORCH: "working"}, "crew": {}, "run": False, "run_end": 0, "last": (NOW - 600, "x")}
        self.assertEqual(len(fc.fc_watch.watch_stalls(self.proj, snap, 240, now_s=NOW, key="m")), 1)
        self.assertIn("quiet:", self.json.read_text())  # a stall that fired is saved
        os.utime(self.json, ns=(1, 1))
        self.assertEqual(fc.fc_watch.watch_stalls(self.proj, snap, 240, now_s=NOW + 1, key="m"), [])
        self.assertEqual(self.json.stat().st_mtime_ns, 1)  # nothing new: unwritten

    def test_the_base_is_saved_beside_the_memory_not_in_it(self):
        fc.fc_watch.watch_save(self.proj, "m", fired=["a"], base=wsnap("passed"))
        self.assertEqual(json.loads(self.json.read_text()), {"fired": ["a"]})
        self.assertEqual(json.loads(self.base.read_text()), wsnap("passed"))
        fc.fc_watch.watch_save(self.proj, "m", woke=[1, 2])
        self.assertEqual(json.loads(self.json.read_text()), {"fired": ["a"], "woke": [1, 2]})  # the memory's other keys save as before
        self.assertEqual(fc.fc_watch.watch_base(self.proj, "m"), wsnap("passed"))

    def test_the_base_falls_back_to_an_older_memory_file_then_to_none(self):
        self.assertIsNone(fc.fc_watch.watch_base(self.proj, "m"))
        self.json.write_text(json.dumps({"fired": [], "base": wsnap("running")}))
        self.assertEqual(fc.fc_watch.watch_base(self.proj, "m"), wsnap("running"))
        self.base.write_text(json.dumps(wsnap("passed")))
        self.assertEqual(fc.fc_watch.watch_base(self.proj, "m"), wsnap("passed"))  # the base file wins

    def test_a_watch_starts_from_the_base_file_or_the_older_memory(self):
        for f, mem in ((self.base, wsnap("running")), (self.json, {"base": wsnap("running")})):
            with self.subTest(f.name):
                self.base.unlink(missing_ok=True); self.json.unlink(missing_ok=True)
                f.write_text(json.dumps(mem))
                self.assertTrue(any("event task=001 state=passed" in l for l in self.watch(lambda *a, **k: wsnap("passed"))))

    def test_a_quiet_loop_runs_no_git_and_writes_no_file_after_its_first_tick(self):
        repo, runs, marks, real, run = Path(tempfile.mkdtemp()), [], [], fc.fc_watch.watch_snapshot, subprocess.run
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for a in (("init", "-q", "-b", "main"), ("commit", "-q", "--allow-empty", "-m", "init")):
            run(["git", *a], cwd=repo, check=True, capture_output=True, env={**os.environ, **env})
        self.proj = repo / ".compose" / "hw"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": "001", "title": "t"}]}))
        def snapshot(*a, **k):
            marks.append((len(runs), {f.name: f.stat().st_mtime_ns for f in (self.proj / "results").iterdir()}))
            if len(marks) > 5:
                raise SystemExit  # six ticks seen
            return real(*a, **k)
        spy = lambda cmd, *a, **k: (runs.append(cmd) if cmd[0] == "git" else None, run(cmd, *a, **k))[1]
        with mock.patch.object(fc.fc_herdr, "herdr", return_value={"agents": []}):
            fc.fc_watch.watch_save(self.proj, "m", base=real(repo, self.proj))
            with mock.patch.object(subprocess, "run", spy):
                self.watch(snapshot, repo=str(repo), timeout=0)
        self.assertEqual(len(marks), 6)
        self.assertEqual(marks[-1], marks[2], "git ran or a file was written after the first ticks")  # tick 1 may write once, which the board stamp sees in tick 2


class WatchLoadEvents(unittest.TestCase):  # the load log reaches each watcher once, as event lines
    def setUp(self):
        import fc_load
        self.load, self.proj = fc_load, Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        self.json = self.proj / "results" / "watch-m.json"
        for p in (mock.patch("os.cpu_count", return_value=8), mock.patch("os.getloadavg", return_value=(9.0,) * 3)):  # between low (8) and warn (10): update() changes nothing
            p.start(); self.addCleanup(p.stop)

    def events(self, key="m"):
        return fc.fc_watch.load_lines(self.proj, key)  # the shed adds its own lines on a tick (test_shed)

    def test_a_hold_and_a_release_are_each_reported_once_after_the_first_call(self):
        self.load.update(avg=13.0); self.load.update(avg=7.0)
        self.assertEqual(self.events(), [])  # the first call saves where the log is
        self.assertEqual(json.loads(self.json.read_text())["load_n"], 2)
        self.load.update(avg=13.0)
        held, = self.events()
        m = re.fullmatch(r"event load=held avg=13\.0 high=(\d+\.\d) cores=8  \(compose starts nothing new until the load falls below (\d+\.\d)\)", held)
        self.assertTrue(m, held)
        self.assertEqual(self.events(), [])
        self.load.update(avg=7.0)
        self.assertEqual(self.events(), [f"event load=released avg=7.0 low={m.group(2)}"])
        self.assertEqual(self.events(), [])
        self.assertEqual(json.loads(self.json.read_text())["load_n"], 4)

    def test_a_watcher_behind_the_log_gets_every_entry_in_order_and_a_new_one_starts_at_the_end(self):
        fc.fc_watch.watch_save(self.proj, "m", load_n=0)
        self.load.update(avg=13.0); self.load.update(avg=7.0)
        late = self.events()
        self.assertEqual([l.split()[1] for l in late], ["load=held", "load=released"])
        self.assertEqual(self.events("other"), [])  # no earlier n saved: its first call
        self.assertEqual(self.events(), [])

    def test_nothing_is_written_when_nothing_changed(self):
        self.load.update(avg=13.0)
        self.events()
        os.utime(self.json, ns=(1, 1))
        self.events(); self.events()
        self.assertEqual(self.json.stat().st_mtime_ns, 1)

    def test_load_lines_wake_a_quiet_watcher(self):
        for l in ("event load=held avg=13.0 high=12.0 cores=8  (compose starts nothing new until the load falls below 8.0)", "event load=released avg=7.0 low=8.0"):
            self.assertTrue(fc.fc_watch.ATTENTION.search(l), l)

    def test_cmd_watch_prints_the_load_lines(self):
        line, out = "event load=released avg=7.0 low=8.0", []
        a = SimpleNamespace(repo=".", now=False, from_=None, timeout=0.3, interval=0.01, stall=10_000, rewake=False, settle=0, attention=True)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_watch, "watch_key", return_value="m"), \
                mock.patch.object(fc.fc_watch, "watch_snapshot", lambda *a, **k: wsnap("pending")), mock.patch.object(fc.fc_crew, "desktop_notify"), \
                mock.patch.object(fc.fc_watch, "load_events", side_effect=[[line], []]), \
                mock.patch("builtins.print", out.append), mock.patch.dict(os.environ, {"COMPOSE_ROLE": ""}):
            with contextlib.suppress(SystemExit):
                fc.fc_watch.cmd_watch(a)
        self.assertTrue(any(line in str(o) for o in out), out)


class WatchLoadWarn(unittest.TestCase):  # a warn or a cleared entry is told to the manager's pane and desktop once
    def setUp(self):
        import fc_load
        self.load, self.proj, self.calls = fc_load, Path(tempfile.mkdtemp()) / "hw", []
        (self.proj / "results").mkdir(parents=True)
        self.claim, self.notify = mock.Mock(return_value=True), mock.Mock()
        for p in (mock.patch("os.cpu_count", return_value=8), mock.patch("os.getloadavg", lambda: (self.avg,) * 3),  # the last entry's avg: update() then adds nothing
                  mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: self.calls.append(a)), mock.patch.object(fc.fc_crew, "desktop_notify", self.notify),
                  mock.patch.object(fc_load, "claim", self.claim)):
            p.start(); self.addCleanup(p.stop)
        fc.fc_watch.watch_save(self.proj, "m", load_n=0)

    def log(self, *entries, manager="%7"):
        self.avg = entries[-1][1]
        (Path.home() / ".compose").mkdir(exist_ok=True)
        manager and (self.proj / "results" / "manager.json").write_text(json.dumps({"session": manager}))
        (Path.home() / ".compose" / "load.json").write_text(json.dumps({"held": False, "since": None, "n": len(entries), "warn": entries[-1][0] == "warn", "claimed": [],
            "memory": None, "log": [{"n": i, "at": time.time(), "kind": k, "avg": a} for i, (k, a) in enumerate(entries, 1)]}))

    def events(self):
        return fc.fc_watch.load_events(self.proj, "m")

    def metadata(self):
        return [c for c in self.calls if c[:2] == ("pane", "report-metadata")]

    def test_a_warn_is_told_sets_the_pane_token_and_notifies(self):
        self.log(("warn", 11.04))
        tell, = self.events()
        m = re.fullmatch(r"event tell='(load 11\.0 on 8 cores is above the warn mark (\d+\.\d): compose holds new starts above (\d+\.\d))'", tell)
        self.assertTrue(m, tell)
        self.assertEqual(self.metadata(), [("pane", "report-metadata", "%7", "--source", "compose-load", "--token", "load=11.0 on 8 cores")])
        self.claim.assert_called_once_with(1)
        self.notify.assert_called_once_with("compose: load", m.group(1))

    def test_a_clear_is_told_removes_the_pane_token_and_notifies(self):
        self.log(("warn", 11.0), ("cleared", 6.55))
        warn, tell = self.events()
        m = re.fullmatch(r"event tell='(load cleared: 6\.[56] on 8 cores, below (\d+\.\d))'", tell)
        self.assertTrue(m, tell)
        self.assertEqual(self.metadata()[-1], ("pane", "report-metadata", "%7", "--source", "compose-load", "--clear-token", "load"))
        self.assertEqual([c.args for c in self.notify.call_args_list][-1], ("compose: load", m.group(1)))

    def test_without_the_claim_only_the_pane_and_the_line_are_told(self):
        self.claim.return_value = False
        self.log(("warn", 11.0))
        self.assertEqual((len(self.events()), len(self.metadata())), (1, 1))
        self.notify.assert_not_called()

    def test_without_a_manager_pane_the_line_and_the_notification_still_go_out(self):
        self.log(("warn", 11.0), manager=None)
        self.assertEqual((len(self.events()), self.metadata()), (1, []))
        self.notify.assert_called_once()

    def test_each_crossing_fires_once(self):
        self.log(("warn", 11.0))
        self.assertEqual(len(self.events()), 1)
        self.assertEqual((self.events(), self.events()), ([], []))
        self.assertEqual((len(self.metadata()), self.notify.call_count), (1, 1))


class WatchShed(unittest.TestCase):  # the shed's lines ride along with the load lines on every tick
    def setUp(self):
        import fc_load, fc_shed
        self.load, self.proj = fc_load, Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        self.tick = mock.Mock(return_value=[])
        for p in (mock.patch("os.cpu_count", return_value=8), mock.patch("os.getloadavg", return_value=(9.0,) * 3), mock.patch.object(fc_shed, "tick", self.tick),  # 9.0: above low, below warn and high, so update() adds no entry
                  mock.patch.object(fc.fc_herdr, "herdr"), mock.patch.object(fc.fc_crew, "desktop_notify"), mock.patch.object(fc_load, "claim", return_value=True)):
            p.start(); self.addCleanup(p.stop)

    def events(self):
        return fc.fc_watch.load_events(self.proj, "m")

    def test_the_shed_lines_come_after_the_load_lines(self):
        fc.fc_watch.watch_save(self.proj, "m", load_n=0)
        self.load.update(avg=13.0)
        self.tick.return_value = ["event shed=freed pid=1", "event shed=hold n=1"]
        out = self.events()
        self.assertIn("load=held", out[0])
        self.assertEqual(out[-2:], self.tick.return_value)  # whatever else the load log adds, the shed's two lines are last

    def test_an_idle_shed_adds_nothing_and_is_asked_on_every_call(self):
        self.assertEqual((self.events(), self.events(), self.events()), ([], [], []))
        self.assertEqual(self.tick.call_count, 3)

    def test_the_shed_lines_come_with_no_load_change_and_on_the_first_call(self):
        self.tick.return_value = ["event shed=freed pid=1"]
        self.assertEqual((self.events(), self.events()), (self.tick.return_value,) * 2)

    def test_a_shed_line_wakes_a_quiet_watcher(self):
        self.assertTrue(fc.fc_watch.ATTENTION.search("event shed=freed pid=1"))


if __name__ == "__main__":
    unittest.main()
