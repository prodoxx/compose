import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
import shutil, subprocess, sys, time
from types import SimpleNamespace
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_load


def snap(proj):
    return {"tasks": {}, "agents": {}, "run": False, "verify": None,
            "crew": {f.stem: fc.fc_crew.crew_reports(proj, f.stem) for f in sorted((proj / "results" / "crew").glob("*.status"))},
            "refused": {f.stem: fc.fc_reports.crew_refusals(proj, f.stem) for f in sorted((proj / "results" / "crew").glob("*.refused"))}}


_patches = [mock.patch.object(time, "sleep"),  # crew start waits for the agent to be working; a fake herdr answering {} never is, so do not sleep through the wait
            mock.patch.object(fc.fc_mcp, "installed_servers", side_effect=lambda cwd: []),  # no test runs the real `claude mcp list`
            mock.patch.object(fc.fc_pin, "cli_commit", return_value="abc1234"),  # the CLI's own checkout is never asked for its commit
            mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}),  # every start and watch here is the team lead's unless the test clears it: the caller's own role never decides who a teammate reports to
            mock.patch.dict(os.environ)]  # restores the herdr variables setUpModule drops
HERDR_VARS = ("HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID")  # run inside herdr, a `done` report would start a real tab close against the live socket


def setUpModule():
    for p in _patches:
        p.start()
    for v in HERDR_VARS:
        os.environ.pop(v, None)


def tearDownModule():
    for p in reversed(_patches):  # last started first: the env snapshot taken after COMPOSE_ROLE was set must not restore it
        p.stop()


class Crew(unittest.TestCase):
    def setUp(self):
        wt = Path(tempfile.mkdtemp())
        self.proj = wt / ".compose" / "shop"
        self.proj.mkdir(parents=True)

    def report(self, who, state, msg):
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("builtins.print"):
            fc.fc_crew.cmd_report(type("A", (), {"as_": who, "state": state, "message": msg.split()}))

    def test_reports_wake_the_right_watcher(self):
        fc.fc_crew.write_report(self.proj, "shop-q-neb-12", "working", "started")
        fc.fc_crew.write_report(self.proj, "shop-orchestrator", "working", "started")
        before = snap(self.proj)
        self.report("shop-q-neb-12", "needs-user", "Which payment provider? I recommend Stripe")
        self.report("shop-orchestrator", "needs-user", "Tasks are ready for your go")
        ev = fc.fc_watch.watch_events(before, snap(self.proj))
        self.assertEqual(len(ev), 2)
        team_lead_view = fc.fc_watch.watch_filter(ev, "shop", [])  # everything but its own report, which goes up
        self.assertEqual(team_lead_view, ["event crew=shop-q-neb-12 state=needs-user message='Which payment provider? I recommend Stripe'"])
        engineering_manager_view = fc.fc_watch.watch_filter(ev, "shop", ["shop-orchestrator"])
        self.assertEqual(engineering_manager_view, ["event crew=shop-orchestrator state=needs-user message='Tasks are ready for your go'"])
        self.assertEqual(fc.fc_watch.watch_events(snap(self.proj), snap(self.proj)), [])  # nothing new, no event

    def test_a_teammate_the_engineering_manager_started_wakes_its_watch_whatever_from_says(self):
        """A planner or designer the engineering manager started itself (no team lead, or an epic) reports to it, so its watch
        (`--from orchestrator` when the project has a team lead) passes their events too, and the product owner is alerted;
        a teammate the team lead started still goes up through the team lead only."""
        for who, up in (("shop-planner", "manager"), ("shop-designer", "manager"), ("shop-tests", "orchestrator"), ("shop-old", None)):
            rec = {"name": who, "role": who[5:], "title": who[5:], "space": "w9", "tab": "t", "pane": "p"} | ({"reports_to": up} if up else {})
            (fc.fc_crew.crew_dir(self.proj) / f"{who}.json").write_text(json.dumps(rec))  # shop-old: a record from before reports_to existed
            fc.fc_crew.write_report(self.proj, who, "working", "started")
        self.assertEqual(fc.fc_crew.crew_reporting_to(self.proj, "manager"), ["shop-designer", "shop-planner"])
        self.assertEqual(fc.fc_crew.crew_reporting_to(self.proj, "orchestrator"), ["shop-tests"])
        before = snap(self.proj)
        self.report("shop-planner", "needs-user", "3 stories NEB-4..6; recommendation accept-all")
        self.report("shop-designer", "blocked", "Framelink token missing")
        self.report("shop-tests", "needs-user", "which fixture?")
        self.report("shop-old", "needs-user", "legacy")
        ev = fc.fc_watch.watch_events(before, snap(self.proj)) + ["event agent=shop-designer status=blocked  (read: ...)"]
        direct = fc.fc_crew.crew_reporting_to(self.proj, "manager")
        manager = fc.fc_watch.watch_filter(ev, "shop", ["shop-orchestrator"], direct)
        self.assertEqual(manager, [e for e in ev if "shop-planner" in e or "shop-designer" in e])
        self.assertEqual(fc.fc_watch.watch_filter(ev, "shop", ["shop-orchestrator"]), [])  # the record decides, never --from alone
        self.assertEqual(fc.fc_watch.watch_filter(ev, "shop", [], direct), ev)  # the team lead's view is unchanged
        notified = []
        with mock.patch.object(fc.fc_crew, "desktop_notify", lambda title, body: notified.append((title, body))), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMPOSE_ROLE", None)
            fc.fc_crew.manager_attention(self.proj, manager)
        self.assertEqual(sorted(notified), [("shop: the engineering manager is blocked", "Framelink token missing  (from shop-designer)"),
                                            ("shop: the engineering manager needs you", "3 stories NEB-4..6; recommendation accept-all  (from shop-planner)")])

    def test_start_records_who_the_teammate_reports_to(self):
        """The starting process says who the teammate reports to: the engineering manager has no COMPOSE_ROLE, the team lead's tab
        carries `orchestrator`. The record keeps it and the brief names the same one."""
        def herdr(*args, **kw):
            if args[:2] == ("agent", "list"):
                return {"agents": []}
            if args[:2] == ("tab", "create"):
                return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}}
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "working"}}
            return {}
        for role, env, up, says in (("planner", {}, "manager", "You report to the engineering manager, who relays to the product owner."),
                                    ("designer", {"COMPOSE_ROLE": "orchestrator"}, "orchestrator", "You report to shop-orchestrator, your team lead.")):
            a = type("A", (), {"action": "start", "role": role, "title": role, "brief": "Go."})
            clean = {k: v for k, v in os.environ.items() if k != "COMPOSE_ROLE"} | env | {"HERDR_PANE_ID": "w1:p2"}
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                    mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                    mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), \
                    mock.patch.object(fc.fc_crew.fc_crewpick, "crew_pick_full", return_value=("claude", None, "test", [])), mock.patch.dict(os.environ, clean, clear=True), mock.patch("builtins.print"):
                fc.fc_crew.cmd_crew(a)
            self.assertEqual(fc.fc_crew.crew_record(self.proj, f"shop-{role}")["reports_to"], up)
            self.assertEqual(fc.fc_crew.crew_record(self.proj, f"shop-{role}")["started_by"], "w1:p2")  # the starting session's herdr pane: two engineering managers are told apart
            brief = (self.proj / "results" / "crew" / f"shop-{role}.brief.md").read_text()
            self.assertIn(says, brief)
            self.assertIn("Never loop or poll `compose linear`", brief)  # every crew brief, whatever the role

    def test_start_stops_on_a_low_battery_before_claiming_or_opening_anything_unless_forced(self):
        calls = []
        a = type("A", (), {"action": "start", "role": "planner", "title": "planner", "brief": "Go."})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", lambda *x, **k: calls.append(x) or {}), \
                mock.patch.object(fc.fc_crew.fc_checks, "battery_stop", return_value="the Mac is on battery at 9%") as stop, self.assertRaises(SystemExit) as e:
            fc.fc_crew.cmd_crew(a)
        self.assertEqual(str(e.exception), "crew start stopped, nothing was started: the Mac is on battery at 9%")
        self.assertEqual((calls, stop.call_args.args), ([], (False,)))
        a.force = True
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", lambda *x, **k: calls.append(x) or {}), \
                mock.patch.object(fc.fc_crew.fc_checks, "battery_stop", return_value=None) as stop, self.assertRaises(SystemExit):
            fc.fc_crew.cmd_crew(a)  # goes on to the herdr work, which this fake cannot finish
        self.assertEqual(stop.call_args.args, (True,))

    def test_bad_state_refused(self):
        with self.assertRaises(SystemExit):
            self.report("shop-q-1", "finished", "x")

    def test_start_opens_tab_starts_claude_and_briefs_it(self):
        calls = []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "list"):
                return {"agents": []}
            if args[:2] == ("tab", "create"):
                return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}}
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "working"}}
            if args[:2] == ("pane", "process-info"):  # claude, in front of its shell
                return {"process_info": {"shell_pid": 10, "foreground_process_group_id": 4242, "foreground_processes": [{"pid": 10}, {"pid": 4242}]}}
            return {}
        a = type("A", (), {"action": "start", "role": "q-NEB-12", "title": "Payment provider", "brief": "Resolve NEB-12 with the product owner."})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), \
                mock.patch.object(fc.fc_proc, "keep_awake") as awake, mock.patch("builtins.print"):
            fc.fc_crew.cmd_crew(a)
        awake.assert_called_once_with(4242)  # the Mac stays awake for as long as the teammate's session lives
        verbs = [c[:2] for c in calls]
        self.assertEqual(verbs, [("agent", "list"), ("tab", "create"), ("pane", "rename"), ("agent", "start"),
                                 ("pane", "process-info"), ("pane", "process-info"), ("agent", "get"),  # the start's renice and the record read the pane's processes; the brief waits for Claude's input box
                                 ("agent", "prompt"), ("pane", "report-metadata"), ("agent", "get"), ("pane", "process-info")])
        self.assertEqual(calls[2], ("pane", "rename", "w9:p7", "Payment provider"))  # the pane carries its tab's label
        self.assertIn("COMPOSE_ROLE=crew", calls[1])  # only the team lead's tab is marked for the guard
        self.assertIn("COMPOSE_PROJECT=shop", calls[1])  # the Stop hook's watch finds its project from the tab
        self.assertIn("COMPOSE_TEAMMATE=shop-q-neb-12", calls[1])  # the compaction hook finds its brief from the tab: COMPOSE_ROLE is only `crew`
        self.assertIn(f"COMPOSE_REPO={fc.fc_herdr.project_worktree(self.proj)}", calls[1])
        start = calls[3]
        self.assertEqual(start[2], "shop-q-neb-12")
        self.assertIn("claude", start)
        self.assertEqual(json.loads(start[start.index("--settings") + 1])["attribution"], {"commit": "", "pr": ""})  # no co-author trailer on a crew commit
        self.assertFalse(any("brief.md" in str(x) for x in start))  # a session launched on a prompt works on it at once and never shows herdr the idle box its start waits for
        self.assertEqual(calls[7][2], "shop-q-neb-12")
        self.assertIn("brief.md", calls[7][3])  # the brief goes in once the session is idle and its box is on screen
        prompt = (self.proj / "results" / "crew" / "shop-q-neb-12.brief.md").read_text()
        self.assertIn("compose report", prompt)
        self.assertIn("shop-orchestrator, your team lead", prompt)
        self.assertTrue(prompt.endswith("Resolve NEB-12 with the product owner."))
        self.assertEqual(fc.fc_crew.crew_reports(self.proj, "shop-q-neb-12")[0]["state"], "working")

    def test_started_is_written_before_the_agent_can_report(self):
        booting = []
        def herdr(*args, **kw):
            if args[:2] == ("agent", "start"):  # a fast teammate finishes before start returns
                booting.append(fc.fc_crew.crew_record(self.proj, "shop-eol").get("booting"))
                fc.fc_crew.write_report(self.proj, "shop-eol", "done", "one commit")
            if args[:2] == ("agent", "list"):
                return {"agents": []}
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "working"}}
            return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}} if args[:2] == ("tab", "create") else {}
        a = type("A", (), {"action": "start", "role": "eol", "title": "eol", "brief": "Do it."})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), mock.patch("builtins.print"):
            fc.fc_crew.cmd_crew(a)
        self.assertEqual([r["state"] for r in fc.fc_crew.crew_reports(self.proj, "shop-eol")], ["working", "done"])
        self.assertEqual(booting, [True])  # herdr does not list it yet: watch must not take it for a closed tab
        self.assertNotIn("booting", fc.fc_crew.crew_record(self.proj, "shop-eol"))  # up: from now on a missing agent means it ended

    def start_with_timeout(self, appears_after, pane_busy=False, finishes=False):
        """crew start where herdr's agent start times out; the agent shows in the agent list on the `appears_after`-th look (None: never).
        `pane_busy`: the pane's shell is running something (a launching agent) even while the list shows no agent.
        `finishes`: the teammate does its whole task and reports done while the start still waits, so its tab is gone when the start gives up."""
        calls, looks, out = [], [], []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "list"):
                looks.append(1)
                up = appears_after is not None and len(looks) >= appears_after and any(c[:2] == ("agent", "start") for c in calls)
                return {"agents": [{"name": "claude", "pane_id": "w9:p7"}] if up else []}
            if args[:2] == ("agent", "start"):
                if finishes:
                    fc.fc_crew.write_report(self.proj, "shop-eol", "done", "fast-forwarded the branch")
                raise RuntimeError("herdr agent start: timeout")
            if args[:2] == ("tab", "create"):
                return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}}
            if args[:2] == ("pane", "process-info") and finishes:
                return None
            if args[:2] == ("agent", "get"):
                up = (appears_after is not None and len(looks) >= appears_after) or (pane_busy and ("pane", "process-info") in [c[:2] for c in calls])
                return {"agent": {"agent_status": "working"}} if up else None
            if args[:2] == ("pane", "process-info"):
                return {"process_info": {"shell_pid": 1, "foreground_process_group_id": 99 if pane_busy else 1,
                                         "foreground_processes": [{"pid": 99, "name": "claude"}] if pane_busy else []}}
            return {}
        a = type("A", (), {"action": "start", "role": "eol", "title": "eol", "brief": "Do it."})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), \
                mock.patch("builtins.print", side_effect=lambda *x, **k: out.append(" ".join(map(str, x)))):
            try:
                fc.fc_crew.cmd_crew(a)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out, calls

    def test_a_start_that_timed_out_but_came_up_still_succeeds_with_one_warning(self):
        code, out, calls = self.start_with_timeout(appears_after=3)
        self.assertEqual(code, 0)
        self.assertIn(("agent", "rename", "w9:p7", "shop-eol"), calls)
        self.assertEqual(len([l for l in out if "timed out" in l]), 1)
        self.assertTrue(any(l.startswith("teammate shop-eol started") for l in out))
        self.assertTrue((self.proj / "results" / "crew" / "shop-eol.json").exists())

    def test_a_start_that_timed_out_and_came_up_after_the_wait_is_kept(self):
        code, out, calls = self.start_with_timeout(appears_after=12)  # one look before the start, then the 10 of the wait miss it, the last check finds it
        self.assertEqual(code, 0)
        self.assertNotIn(("tab", "close", "w9:t3"), calls)
        self.assertEqual(len([l for l in out if "timed out" in l]), 1)
        self.assertTrue((self.proj / "results" / "crew" / "shop-eol.json").exists())

    def test_a_start_that_timed_out_with_a_process_launching_in_its_pane_is_kept(self):
        code, out, calls = self.start_with_timeout(appears_after=None, pane_busy=True)
        self.assertEqual(code, 0)
        self.assertNotIn(("tab", "close", "w9:t3"), calls)
        self.assertEqual(len([l for l in out if "timed out" in l]), 1)
        self.assertTrue((self.proj / "results" / "crew" / "shop-eol.json").exists())

    def test_a_start_that_timed_out_after_the_teammate_already_finished_is_kept(self):
        code, out, calls = self.start_with_timeout(appears_after=None, finishes=True)
        self.assertEqual(code, 0)
        self.assertFalse(any("did not start" in l for l in out))
        self.assertNotIn(("tab", "close", "w9:t3"), calls)
        self.assertEqual([r["state"] for r in fc.fc_crew.crew_reports(self.proj, "shop-eol")], ["working", "done"])
        self.assertTrue((self.proj / "results" / "crew" / "shop-eol.json").exists())

    def test_a_report_from_a_teammate_whose_record_is_missing_registers_it_again(self):
        live = {"agent": {"name": "shop-eol", "pane_id": "w9:p7", "tab_id": "w9:t3", "workspace_id": "w9", "cwd": "/x"}}
        with mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: live if a[:2] == ("agent", "get") else {}):
            self.report("shop-eol", "done", "finished")
        c = fc.fc_crew.crew_record(self.proj, "shop-eol")
        self.assertEqual((c["pane"], c["tab"], c["space"]), ("w9:p7", "w9:t3", "w9"))

    def test_a_start_that_timed_out_and_never_came_up_exits_cleanly_and_registers_nothing(self):
        code, out, calls = self.start_with_timeout(appears_after=None)
        self.assertIsInstance(code, str)  # a plain message, not a traceback
        self.assertIn("timeout", code)
        self.assertIn(("tab", "close", "w9:t3"), calls)
        self.assertEqual(list((self.proj / "results" / "crew").glob("shop-eol.*")), [])

    def start_without_brief(self, prompt_fails=None, late=False, screen="❯ \n"):
        """crew start where the session comes up but never takes its brief: herdr answers idle on every look and the wait for the start
        gives up (`late`: the look after it gives up finds it working), or herdr rejects the prompt itself with the error code `prompt_fails`
        (`agent_blocked`: the session sits at a dialog, `screen` is its pane)."""
        calls, out = [], []
        fc.fc_crew.write_report(self.proj, "shop-eol", "done", "an earlier session of the same name")
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "list"):
                return {"agents": []}
            if args[:2] == ("tab", "create"):
                return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}}
            if args[:2] == ("agent", "prompt") and prompt_fails:
                raise RuntimeError(f"herdr agent prompt: {prompt_fails}")
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "blocked" if prompt_fails == "agent_blocked" else "working" if late and gave_up else "idle"}}
            return {}
        gave_up = False
        def await_start(agent, first=None):
            nonlocal gave_up
            gave_up = True
            raise RuntimeError(f"{agent} did not start on its brief after 3 retries")
        a = type("A", (), {"action": "start", "role": "eol", "title": "eol", "brief": "Do it."})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                mock.patch.object(fc.fc_herdr, "herdr_text", return_value=screen), mock.patch.object(fc.fc_herdr, "herdr_await_start", side_effect=await_start), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), \
                mock.patch("builtins.print", side_effect=lambda *x, **k: out.append(" ".join(map(str, x)))):
            try:
                fc.fc_crew.cmd_crew(a)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out, calls

    def test_a_session_that_never_takes_its_brief_is_closed_so_a_retry_starts_fresh(self):
        """The session came up, the brief never landed (Enter and the resend did nothing): the start closes the tab it opened and
        removes the record, so `crew start` run again is not refused as already running."""
        code, out, calls = self.start_without_brief()
        self.assertIsInstance(code, str)  # a plain message, not a traceback
        self.assertIn("shop-eol did not start on its brief", code)
        self.assertIn("run the same crew start again", code)
        self.assertIn(("tab", "close", "w9:t3"), calls)
        self.assertEqual(list((self.proj / "results" / "crew").glob("shop-eol.*")), [])

    def test_a_session_that_rejects_its_brief_is_closed_too(self):
        code, out, calls = self.start_without_brief(prompt_fails="agent_not_found")
        self.assertIsInstance(code, str)
        self.assertIn("agent_not_found", code)
        self.assertIn(("tab", "close", "w9:t3"), calls)
        self.assertEqual(list((self.proj / "results" / "crew").glob("shop-eol.*")), [])

    MCP_DIALOG = ("➜  compose claude --no-chrome --permission-mode auto\n\n" + "─" * 60 + "\n"
                  "  New MCP server found in this project: db\n\n"
                  "  MCP servers may execute code or access system resources. All tool calls require approval.\n\n"
                  "    Use this MCP server\n    Use this and all future MCP servers in this project\n"
                  "  ❯ Continue without using this MCP server\n\n  Enter to confirm · Esc to cancel\n")

    def test_a_session_waiting_at_a_dialog_keeps_its_tab_and_says_which_dialog(self):
        """A session herdr calls blocked before its brief sits at a Claude Code dialog that is the product owner's to answer: crew start
        names it from the pane, reports it, and leaves the tab and the record in place, rather than closing it and asking for a retry."""
        code, out, calls = self.start_without_brief(prompt_fails="agent_blocked", screen=self.MCP_DIALOG)
        self.assertIsInstance(code, str)
        self.assertIn("New MCP server found in this project: db", code)
        self.assertIn("Continue without using this MCP server", code)
        self.assertNotIn("Enter to confirm", code)
        self.assertIn("herdr agent prompt shop-eol", code)  # how the brief goes in once the dialog is answered
        self.assertNotIn("run the same crew start again", code)
        self.assertNotIn(("tab", "close", "w9:t3"), calls)
        self.assertTrue((self.proj / "results" / "crew" / "shop-eol.json").exists())
        last = fc.fc_crew.crew_reports(self.proj, "shop-eol")[-1]
        self.assertEqual(last["state"], "blocked")
        self.assertIn("New MCP server found in this project: db", last["message"])

    def test_a_session_that_starts_on_the_last_look_is_kept(self):
        code, out, calls = self.start_without_brief(late=True)
        self.assertEqual(code, 0)
        self.assertNotIn(("tab", "close", "w9:t3"), calls)
        self.assertTrue((self.proj / "results" / "crew" / "shop-eol.json").exists())

    def test_guarded_roles_mark_their_own_tab_and_every_other_role_is_crew(self):
        for role, env in (("planner", "planner"), ("planner-neb-7", "planner"), ("orchestrator", "orchestrator"), ("architect", "crew"), ("q-planner", "crew")):
            calls = []
            def herdr(*args, **kw):
                calls.append(args)
                if args[:2] == ("agent", "list"):
                    return {"agents": []}
                if args[:2] == ("agent", "get"):
                    return {"agent": {"agent_status": "working"}}
                return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}} if args[:2] == ("tab", "create") else {}
            a = type("A", (), {"action": "start", "role": role, "title": role, "brief": "Plan the feature."})
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                    mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                    mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), mock.patch("builtins.print"):
                fc.fc_crew.cmd_crew(a)
            self.assertEqual(calls[1][:2], ("tab", "create"))
            self.assertIn(f"COMPOSE_ROLE={env}", calls[1], role)
            self.assertEqual(calls[3][2], f"shop-{role}")

    def test_a_story_planner_is_named_after_its_ticket_so_it_runs_beside_the_feature_planner(self):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-1"}))
        def named(issue):
            out = io.StringIO()
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), contextlib.redirect_stdout(out):
                fc.fc_briefs.cmd_planner(type("A", (), {"extra": None, "issue": issue}))
            return out.getvalue().splitlines()[1]
        self.assertTrue(named(None).endswith("you are shop-planner."))
        self.assertTrue(named("neb-1").endswith("you are shop-planner."))
        self.assertTrue(named("NEB-7").endswith("you are shop-planner-neb-7."))
        self.assertTrue(named("NEB-8").endswith("you are shop-planner-neb-8."))

    def test_same_named_agent_in_another_space_neither_blocks_start_nor_shows_in_list(self):
        stranger = {"name": "shop-explore", "workspace_id": "wC", "agent_status": "idle"}
        calls = []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "list"):
                return {"agents": [stranger]}
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "working"}}
            if args[:2] == ("tab", "create"):
                return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}}
            return {}
        start = type("A", (), {"action": "start", "role": "explore", "title": "Explore", "brief": "Look around."})
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), \
                mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), mock.patch("sys.stdout", out):
            fc.fc_crew.cmd_crew(start)
        self.assertIn("starting this one as shop-explore-2", out.getvalue())
        self.assertEqual(next(c for c in calls if c[:2] == ("agent", "start"))[2], "shop-explore-2")
        self.assertEqual(fc.fc_crew.crew_record(self.proj, "shop-explore")["agent"], "shop-explore-2")
        # the stranger is not the crew: it is not shown as live, the project's own numbered agent is
        mine = {"name": "shop-explore-2", "workspace_id": "w9", "agent_status": "working"}
        for agents, shown in (([stranger], "closed"), ([stranger, mine], "working")):
            out = io.StringIO()
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: {"agents": agents}), \
                    mock.patch("sys.stdout", out):
                fc.fc_crew.cmd_crew(type("A", (), {"action": "list"}))
            self.assertIn(shown, out.getvalue())
            self.assertNotIn("idle", out.getvalue())
        # its last report says working, but its tab is closed: it is not working
        (self.proj / "results" / "crew" / "shop-explore.status").write_text(json.dumps({"at": "2026-10-17 13:22:59", "state": "working", "message": "half way"}) + "\n")
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: {"agents": [stranger]}), \
                mock.patch("sys.stdout", out):
            fc.fc_crew.cmd_crew(type("A", (), {"action": "list"}))
        self.assertNotIn("working", out.getvalue()); self.assertIn("half way", out.getvalue())

    def test_list_shows_a_stopped_claude_as_suspended_not_as_the_status_herdr_kept(self):
        """#407: a claude stopped by a sleep or Ctrl-Z stays listed as done or blocked; crew list says it is suspended."""
        (fc.fc_crew.crew_dir(self.proj) / "shop-explore.json").write_text(json.dumps({"name": "shop-explore", "agent": "shop-explore", "title": "t", "space": "w9", "tab": "w9:t1"}))
        agent = {"name": "shop-explore", "workspace_id": "w9", "agent_status": "done", "pane_id": "p1"}
        for stat, shown in (("T\n", "SUSPENDED"), ("S+\n", "done")):
            out = io.StringIO()
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: {"agents": [agent]}), \
                    mock.patch.object(fc.fc_herdr, "pane_processes", return_value=[{"pid": 7}]), mock.patch("sys.stdout", out), \
                    mock.patch.object(fc.fc_herdr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stat, "")):
                fc.fc_crew.cmd_crew(type("A", (), {"action": "list"}))
            self.assertIn(shown, out.getvalue())

    def test_list_shows_ended_for_a_closed_teammate_whose_last_report_is_not_done(self):
        d = fc.fc_crew.crew_dir(self.proj)
        teammates = {  # name: (reports oldest first, open as (agent name, space) or None)
            "shop-a": (["needs-user"], None), "shop-b": (["blocked"], None), "shop-c": (["working"], None),
            "shop-d": (["tool-bug"], None), "shop-e": (["contradiction"], None), "shop-f": (["done"], None),
            "shop-g": ([], None), "shop-h": (["done", "needs-user"], None), "shop-i": (["needs-user", "done"], None),
            "shop-j": (["needs-user"], ("shop-j", "w9")), "shop-k": (["blocked"], ("shop-k-2", "w9")),
            "shop-l": (["needs-user"], ("shop-l", "wC")), "shop-m": ([], ("shop-m", "w9")),
        }
        agents = []
        for name, (states, live) in teammates.items():
            rec = {"name": name, "title": "t", "space": "w9", "tab": f"w9:{name}"}
            if live and live[0] != name:
                rec["agent"] = live[0]
            (d / f"{name}.json").write_text(json.dumps(rec))
            for s in states:
                fc.fc_crew.write_report(self.proj, name, s, f"{s} msg")
            if live:
                agents.append({"name": live[0], "workspace_id": live[1], "agent_status": "working"})

        def listed(answer):
            out = io.StringIO()
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), \
                    mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: answer), mock.patch("sys.stdout", out):
                fc.fc_crew.cmd_crew(type("A", (), {"action": "list"}))
            return {line.split()[0]: line.split()[1] for line in out.getvalue().splitlines() if line.strip()}

        self.assertEqual(listed({"agents": agents}), {
            "shop-a": "ended", "shop-b": "ended", "shop-c": "ended", "shop-d": "ended", "shop-e": "ended",
            "shop-f": "done", "shop-g": "-", "shop-h": "ended", "shop-i": "done",
            "shop-j": "needs-user", "shop-k": "blocked", "shop-l": "ended", "shop-m": "-",
        })
        # herdr cannot say who is open: nobody counts as closed, so every teammate shows its last report
        self.assertEqual(listed(None), {
            "shop-a": "needs-user", "shop-b": "blocked", "shop-c": "working", "shop-d": "tool-bug", "shop-e": "contradiction",
            "shop-f": "done", "shop-g": "-", "shop-h": "needs-user", "shop-i": "done",
            "shop-j": "needs-user", "shop-k": "blocked", "shop-l": "needs-user", "shop-m": "-",
        })

    def test_the_team_lead_is_told_the_parallel_clause_and_warned_when_a_working_report_lacks_it(self):
        self.assertIn("parallel: none because <reason>", fc.fc_crew.crew_preamble(self.proj, "shop-orchestrator", "orchestrator"))
        self.assertNotIn("parallel:", fc.fc_crew.crew_preamble(self.proj, "shop-tests", "tests"))
        for who, msg, warned in (("shop-orchestrator", "running 001 then 002", True), ("shop-orchestrator", "parallel: started 001,002", False),
                                 ("shop-tests", "writing tests", False)):
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("builtins.print") as out:
                fc.fc_crew.cmd_report(type("A", (), {"as_": who, "state": "working", "message": msg.split()}))
            self.assertEqual(any("parallel:" in str(c) for c in out.call_args_list[:-1]), warned, (who, msg))

    def test_start_in_an_untrusted_worktree_says_so_before_opening_a_tab(self):
        calls = []
        a = type("A", (), {"action": "start", "role": "orchestrator", "title": None, "brief": "x"})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", lambda *x, **k: calls.append(x)), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_claude, "claude_trusted", return_value=False), \
                self.assertRaises(SystemExit) as e:
            fc.fc_crew.cmd_crew(a)
        self.assertIn("never trusted", str(e.exception.code))
        self.assertNotIn(("tab", "create"), [c[:2] for c in calls])  # no half-started tab left behind

    def test_start_outside_herdr_says_so(self):
        a = type("A", (), {"action": "start", "role": "orchestrator", "title": None, "brief": "x"})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.dict(os.environ, {"HERDR_ENV": ""}), \
                self.assertRaises(SystemExit):
            fc.fc_crew.cmd_crew(a)

    def test_stop_waits_for_done_unless_forced(self):
        (fc.fc_crew.crew_dir(self.proj) / "shop-q-1.json").write_text('{"tab": "w9:t3"}')
        fc.fc_crew.write_report(self.proj, "shop-q-1", "needs-user", "Which one?")
        closed = []
        def stop(force):
            a = type("A", (), {"action": "stop", "role": "q-1", "force": force})
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("builtins.print"), \
                    mock.patch.object(fc.fc_herdr, "herdr", lambda *args, **kw: closed.append(args)):
                fc.fc_crew.cmd_crew(a)
        with self.assertRaises(SystemExit) as e:
            stop(False)
        self.assertIn("needs-user", str(e.exception))
        self.assertEqual(closed, [])
        stop(True)
        self.assertEqual(closed, [("tab", "close", "w9:t3")])
        closed.clear()
        fc.fc_crew.write_report(self.proj, "shop-q-1", "done", "Resolved NEB-12")
        stop(False)
        self.assertEqual(closed, [("tab", "close", "w9:t3")])


class Attention(unittest.TestCase):
    """The CLI, not a session's memory, gets the product owner's attention."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        self.proj.mkdir(parents=True)
        for role, title in (("q-neb-1", "Payment provider"), ("q-neb-2", "Refund window"), ("q-neb-3", "Currency"), ("tests", "tests")):
            name = f"shop-{role}"
            (fc.fc_crew.crew_dir(self.proj) / f"{name}.json").write_text(json.dumps(
                {"name": name, "role": role, "title": title, "space": "w9", "tab": f"w9:{role}", "pane": f"p-{role}"}))
            fc.fc_crew.write_report(self.proj, name, "working", "started")
        fc.fc_crew.write_report(self.proj, "shop-q-neb-3", "done", "Currency: USD only")
        self.calls = []

    def report(self, who, state, msg):
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("builtins.print"), \
                mock.patch.object(fc.fc_herdr, "herdr", lambda *args, **kw: self.calls.append(args)):
            fc.fc_crew.cmd_report(type("A", (), {"as_": who, "state": state, "message": msg.split()}))

    def test_needs_user_badges_the_pane_without_notifying_or_focusing(self):
        """A teammate's question only badges its pane: the product owner hears of it once, from the engineering manager."""
        self.report("shop-q-neb-1", "needs-user", "Stripe or Adyen?")
        self.assertEqual([c[:2] for c in self.calls], [("pane", "report-metadata")])
        self.assertIn("blocked=needs you", self.calls[0])

    def test_orchestrator_report_does_not_notify_either(self):
        (fc.fc_crew.crew_dir(self.proj) / "shop-orchestrator.json").write_text(json.dumps(
            {"name": "shop-orchestrator", "role": "orchestrator", "title": "orchestrator", "space": "w9", "tab": "w9:o", "pane": "p-o"}))
        self.report("shop-orchestrator", "needs-user", "Stripe or Adyen? I recommend Stripe")
        self.assertEqual([c[:2] for c in self.calls], [("pane", "report-metadata")])

    def test_only_the_engineering_manager_notifies_and_only_on_a_question(self):
        """The notification fires when the report wakes the engineering manager's watch, the one watcher with no crew role, and the
        CLI sends it to the desktop itself: never through `herdr notification show`, whose `[ui.toast] delivery` is the same setting
        as herdr's own toast for every agent, so turning those off would turn this off too."""
        ev = ["event crew=shop-orchestrator state=needs-user message='Stripe or Adyen? I recommend Stripe'",
              "event crew=shop-q-neb-1 state=done message='Provider: Stripe'",
              "event stall=idle agent=shop-orchestrator the team lead has been idle"]
        runs = []
        with mock.patch.object(fc.fc_herdr, "herdr", lambda *args, **kw: self.calls.append(args)), mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(subprocess, "run", lambda cmd, **kw: runs.append(cmd)):
            with mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}):
                fc.fc_crew.manager_attention(self.proj, ev)  # the team lead watching its teammates
            self.assertEqual((self.calls, runs), ([], []))
            with mock.patch.dict(os.environ, {"COMPOSE_ROLE": ""}):
                os.environ.pop("COMPOSE_ROLE")
                fc.fc_crew.manager_attention(self.proj, ev)
        self.assertEqual(self.calls, [])  # nothing goes through herdr
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][:4], ["/bin/launchctl", "asuser", str(os.getuid()), "/usr/bin/osascript"])
        self.assertEqual(runs[0][-3:], ["--", "shop: the engineering manager needs you", "Stripe or Adyen? I recommend Stripe  (from shop-orchestrator)"])
        self.assertIn("sound name", " ".join(runs[0][:-3]))

    def test_desktop_notify_runs_in_the_gui_session_on_macos(self):
        """herdr panes run in launchd's Background session, where Notification Center drops notifications without an error, so
        osascript goes through `launchctl asuser` into the user's GUI session."""
        runs = []
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(subprocess, "run", lambda cmd, **kw: runs.append(cmd)):
            self.assertTrue(fc.fc_crew.desktop_notify("t", "b"))
        self.assertEqual(runs[0][:4], ["/bin/launchctl", "asuser", str(os.getuid()), "/usr/bin/osascript"])

    def test_desktop_notify_passes_the_message_as_an_argument_never_in_the_script(self):
        """A report message is data: quotes, backticks, and a leading dash land verbatim in one argv item after `--`, and no
        `-e` statement contains it."""
        body = """-rm "x" `id` $(id) 'y'"""
        runs = []
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(subprocess, "run", lambda cmd, **kw: runs.append(cmd)):
            self.assertTrue(fc.fc_crew.desktop_notify("shop: the engineering manager is blocked", body))
        self.assertEqual(runs[0][-3:], ["--", "shop: the engineering manager is blocked", body])
        self.assertFalse(any("id" in s for s in runs[0][:-3]))
        runs.clear()
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(subprocess, "run", lambda cmd, **kw: runs.append(cmd)):
            with mock.patch.object(shutil, "which", lambda name: "/usr/bin/notify-send"):
                self.assertTrue(fc.fc_crew.desktop_notify("t", body))
            with mock.patch.object(shutil, "which", lambda name: None):
                self.assertFalse(fc.fc_crew.desktop_notify("t", body))
        self.assertEqual(runs, [["notify-send", "--", "t", body]])

    def test_done_prompts_no_other_crew(self):
        """Decisions live on the map in Linear, recorded by the one architect teammate; nothing is broadcast between teammates."""
        self.report("shop-q-neb-1", "done", "Provider: Stripe (NEB-1)")
        self.assertEqual([c for c in self.calls if c[:2] == ("agent", "prompt")], [])

    def test_focus_jumps_on_request(self):
        a = type("A", (), {"action": "focus", "role": "q-neb-2"})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("builtins.print"), \
                mock.patch.object(fc.fc_herdr, "herdr", lambda *args, **kw: self.calls.append(args)):
            fc.fc_crew.cmd_crew(a)
        self.assertEqual(self.calls, [("workspace", "focus", "w9"), ("tab", "focus", "w9:q-neb-2")])


class Guard(unittest.TestCase):
    """The team lead writes Linear and the CLI's JSON; code, tests, and docs are teammate work."""
    def setUp(self):
        self.repo, self.other = Path(tempfile.mkdtemp()).resolve(), Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose" / "shop").mkdir(parents=True)
        def checkout(path):  # the repo and the other folder are checkouts, nothing else is
            here = Path(path).resolve()
            root = next((r for r in (self.repo, self.other) if r == here or r in here.parents), None)
            return fc.fc_core.Checkout((root, root / ".git", root / ".git")) if root else None
        p = mock.patch.object(fc.fc_core, "checkout", checkout); p.start(); self.addCleanup(p.stop)  # the edit guard asks fc_core.checkout, never git

    def test_team_lead_cannot_edit_repo_files(self):
        for f in ("src/app.go", "tests/app_test.go", "README.md", "package.json", ".compose/shop/notes.md"):
            self.assertIsNotNone(fc.fc_hooks.guard_decision("Edit", str(self.repo / f), "orchestrator"), f)

    def test_the_edit_guard_asks_git_nothing_and_lets_a_folder_outside_every_checkout_through(self):
        with mock.patch.object(fc.fc_core, "sh", side_effect=AssertionError("git asked")), mock.patch.object(fc.fc_core, "checkout", wraps=fc.fc_core.checkout) as asked:
            for role in ("orchestrator", "planner"):
                self.assertIsNotNone(fc.fc_hooks.guard_decision("Write", str(self.repo / "src" / "new" / "app.go"), role), role)  # a folder not made yet: its nearest parent is the checkout
                self.assertIsNotNone(fc.fc_hooks.guard_decision("Write", str(self.other / "app.go"), role), role)
                self.assertIsNone(fc.fc_hooks.guard_decision("Write", "/nonexistent-dir/brief.txt", role), role)
            self.assertEqual(asked.call_count, 6)

    def test_team_lead_keeps_its_own_files(self):
        for f in (".compose/shop/linear.json", ".compose/shop/verify.json", ".compose/linear.json", ".compose/shop/pr/001-002.md"):
            self.assertIsNone(fc.fc_hooks.guard_decision("Write", str(self.repo / f), "orchestrator"), f)
        for role in ("orchestrator", "planner"):
            self.assertIsNone(fc.fc_hooks.guard_decision("Write", str(self.repo / ".compose/shop/lavish/shape.html"), role), role)
            self.assertIsNone(fc.fc_hooks.guard_decision("Write", str(self.repo / ".compose/shop/scratch/sync.json"), role), role)
        self.assertIsNotNone(fc.fc_hooks.guard_decision("Write", str(self.repo / ".compose/shop/scratch.md"), "planner"))
        self.assertIsNone(fc.fc_hooks.guard_decision("Write", str(Path(tempfile.mkdtemp()) / "brief.txt"), "orchestrator"))  # scratch outside a repo

    def test_crew_and_unmarked_sessions_are_untouched(self):
        for role in ("crew", None, ""):
            self.assertIsNone(fc.fc_hooks.guard_decision("Edit", str(self.repo / "src/app.go"), role))
        self.assertIsNone(fc.fc_hooks.guard_decision("Read", str(self.repo / "src/app.go"), "orchestrator"))

    def test_planner_is_guarded_like_the_team_lead(self):
        self.assertEqual(fc.fc_hooks.GUARDED_ROLES, ("orchestrator", "planner"))
        self.assertEqual(fc.fc_config.PLANNING_ROLES, ("architect",))  # the guard change does not touch who gets `planner`
        for tool in fc.fc_hooks.GUARDED_TOOLS:
            for role in fc.fc_hooks.GUARDED_ROLES:
                why = fc.fc_hooks.guard_decision(tool, str(self.repo / "src/app.go"), role) or ""
                self.assertIn("team lead", why, (tool, role))
                self.assertIn("planner", why.lower(), (tool, role))
        for f in (".compose/shop/linear.json", ".compose/shop/verify.json", ".compose/linear.json"):
            self.assertIsNone(fc.fc_hooks.guard_decision("Write", str(self.repo / f), "planner"), f)
        self.assertIsNone(fc.fc_hooks.guard_decision("Read", str(self.repo / "src/app.go"), "planner"))
        self.assertIsNone(fc.fc_hooks.guard_decision("Write", str(Path(tempfile.mkdtemp()) / "plan.md"), "planner"))  # scratch outside a repo
        for role in ("architect", "q-planner", "planners"):  # only the exact guarded roles
            self.assertIsNone(fc.fc_hooks.guard_decision("Edit", str(self.repo / "src/app.go"), role), role)

    def test_a_crew_session_cannot_switch_branches_in_the_shared_project_worktree(self):
        other = self.other
        decide = lambda command, cwd=self.repo, role="crew": fc.fc_hooks.switch_decision(command, str(cwd), role, str(self.repo))
        for command in ("git checkout feature", "git switch -c work", "git -c core.x=1 checkout main", f"cd {other} && cd {self.repo} && git checkout x", "echo hi; git switch main"):
            self.assertIn("move the team lead's HEAD", decide(command) or "", command)
        for command in ("git checkout -- src/app.go", "git checkout -p", "git status", f"git -C {other} checkout x", f"cd {other} && git switch x", "echo git checkout"):
            self.assertIsNone(decide(command), command)
        self.assertIsNone(decide("git checkout x", cwd=other))  # the session works in a worktree of its own
        for role in ("orchestrator", "planner", None):
            self.assertIsNone(decide("git checkout x", role=role), role)
        out = io.StringIO()
        ev = json.dumps({"tool_name": "Bash", "cwd": str(self.repo), "tool_input": {"command": "git switch main"}})
        with mock.patch.dict(os.environ, {"COMPOSE_ROLE": "crew", "COMPOSE_REPO": str(self.repo)}), mock.patch.object(sys, "stdin", io.StringIO(ev)), mock.patch.object(sys, "stdout", out):
            fc.fc_hooks.cmd_guard(None)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_hook_output_denies(self):
        ev = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(self.repo / "x.go")}})
        for role, denied in (("orchestrator", True), ("planner", True), ("architect", False)):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"COMPOSE_ROLE": role}), mock.patch.object(sys, "stdin", io.StringIO(ev)), \
                    mock.patch.object(sys, "stdout", out):
                fc.fc_hooks.cmd_guard(None)
            if denied:
                self.assertTrue(out.getvalue(), role)
                self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny", role)
            else:
                self.assertEqual(out.getvalue(), "", role)


class ToolBug(unittest.TestCase):
    """A failure of compose itself goes to the engineering manager, who owns fixing the tool, and to a log across projects."""
    setUp, report = Crew.setUp, Crew.report

    def test_tool_bug_is_recorded_logged_and_wakes_the_engineering_manager(self):
        log = Path(tempfile.mkdtemp()) / "tool-bugs.jsonl"
        fc.fc_crew.write_report(self.proj, "shop-tests", "working", "started")
        before = snap(self.proj)
        with mock.patch.object(fc.fc_crew, "TOOL_BUGS", log), mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(stdout="abc1234\n")):  # the CLI's own commit
            self.report("shop-tests", "tool-bug", "run crashed on index.lock; worked around with --parallel 1")
        self.assertEqual(fc.fc_crew.crew_reports(self.proj, "shop-tests")[-1]["state"], "tool-bug")
        entry = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual((entry["project"], entry["reporter"]), ("shop", "shop-tests"))
        self.assertTrue(entry["cli_commit"] and Path(entry["cli_repo"], "bin", "compose").exists())
        ev = fc.fc_watch.watch_events(before, snap(self.proj))
        self.assertEqual(len(fc.fc_watch.watch_filter(ev, "shop", ["shop-orchestrator"])), 1)  # the engineering manager wakes on a teammate's tool-bug too
        self.assertEqual(len(fc.fc_watch.watch_filter(ev, "shop", [])), 1)  # and the team lead sees it

    def test_crew_can_start_in_another_directory(self):
        calls = []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "working"}}
            if args[:2] == ("tab", "create"):
                return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}}
            return {}
        a = type("A", (), {"action": "start", "role": "toolsmith", "title": "toolsmith", "brief": "Fix it.", "cwd": "/tmp/compose"})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), mock.patch("builtins.print"):
            fc.fc_crew.cmd_crew(a)
        tab = next(c for c in calls if c[:2] == ("tab", "create"))
        self.assertEqual(tab[tab.index("--cwd") + 1], "/tmp/compose")

    def start_on_repo(self, repo, cwd=None):
        """crew start --repo with herdr faked; (herdr calls, the exit message or None). Claude Code trusts only the project worktree."""
        calls = []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "working"}}
            if args[:2] == ("tab", "create"):
                return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}}
            return {}
        wt = fc.fc_herdr.project_worktree(self.proj)
        a = type("A", (), {"action": "start", "role": "toolsmith", "title": "toolsmith", "brief": "Fix it.", "work_repo": str(repo), "cwd": cwd})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), \
                mock.patch.object(fc.fc_crew.fc_crewpick, "crew_pick_full", return_value=("claude", None, "test", [])), \
                mock.patch.object(fc.fc_claude, "claude_trusted", lambda p: Path(p).resolve() == wt), mock.patch("builtins.print"):
            try:
                fc.fc_crew.cmd_crew(a)
            except SystemExit as e:
                return calls, str(e.code)
        return calls, None

    def test_repo_starts_the_session_in_the_trusted_project_worktree_and_reaches_an_untrusted_repo_by_add_dir(self):
        """A session started in a folder Claude Code never trusted waits at the trust prompt (the CLI's own checkout,
        ~/.compose/src, usually is one), so --work-repo puts only the work there: the toolsmith's worktree of that repo."""
        repo = Path(tempfile.mkdtemp()).resolve()
        work = repo / ".claude" / "worktrees" / "shop-toolsmith-1"
        with mock.patch.object(fc.fc_worktree, "common_dir", return_value=repo / ".git"), mock.patch.object(fc.fc_worktree, "repo_worktree", return_value=(work, "shop-toolsmith-1")):
            calls, failed = self.start_on_repo(repo)
        self.assertIsNone(failed)
        wt, rec = fc.fc_herdr.project_worktree(self.proj), fc.fc_crew.crew_record(self.proj, "shop-toolsmith")
        tab = next(c for c in calls if c[:2] == ("tab", "create"))
        self.assertEqual(tab[tab.index("--cwd") + 1], str(wt))
        self.assertEqual((Path(rec["worktree"]), rec["branch"], rec["cwd"]), (work, "shop-toolsmith-1", str(wt)))
        start = next(c for c in calls if c[:2] == ("agent", "start"))
        self.assertEqual(start[start.index("--add-dir") + 1], str(work))
        self.assertTrue(start[start.index("--add-dir") + 2].startswith("--"))  # --add-dir takes many paths: a flag must follow it
        brief = (self.proj / "results" / "crew" / "shop-toolsmith.brief.md").read_text()
        self.assertIn(f"Your session runs in the project worktree {wt}", brief)
        self.assertIn(f"own worktree {work} of {repo}, on branch {rec['branch']}", brief)
        self.assertIn(f"git -C {work}", brief)

    def test_the_cli_passes_work_repo_apart_from_the_project_repo(self):
        argv = ["compose", "crew", "start", "--project", "shop", "--role", "toolsmith", "--work-repo", "/x/src", "--brief", "-"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(fc.fc_config, "apply_config", return_value="default"), \
                mock.patch.object(fc.fc_crew, "cmd_crew") as crew:
            fc.main()
        a = crew.call_args.args[0]
        self.assertEqual((a.work_repo, a.repo, a.cwd), ("/x/src", ".", None))

    def test_repo_refuses_a_plain_folder_or_a_cwd_too_before_opening_a_tab(self):
        plain = Path(tempfile.mkdtemp())
        for repo, cwd, says in ((plain, None, "not a git repo"), (plain, "/tmp", "--cwd or --work-repo, not both")):
            with self.subTest(says):
                calls, failed = self.start_on_repo(repo, cwd)
                self.assertIn(says, failed or "")
                self.assertNotIn(("tab", "create"), [c[:2] for c in calls])

    def test_an_untrusted_cwd_repo_points_to_work_repo(self):
        repo = Path(tempfile.mkdtemp()).resolve()
        with mock.patch.object(fc.fc_worktree, "common_dir", return_value=repo / ".git"), mock.patch.object(fc.fc_claude, "claude_trusted", return_value=False), self.assertRaises(SystemExit) as e:
            fc.fc_crew.crew_workdir(str(repo), fc.fc_herdr.project_worktree(self.proj), "shop-research")
        self.assertIn(f"--work-repo {repo}", str(e.exception.code))


class ToolsmithWorktree(unittest.TestCase):
    def test_any_role_in_the_project_repo_gets_its_own_worktree_and_leaves_checkouts_alone(self):
        repo = Path(tempfile.mkdtemp()).resolve(); slot, plain = repo.parent / (repo.name + "-slot"), Path(tempfile.mkdtemp())
        made = mock.Mock(side_effect=lambda cwd, name: (repo / ".claude" / "worktrees" / name, name + "-1"))
        with mock.patch.object(fc.fc_worktree, "common_dir", lambda p: repo / ".git" if Path(p) != plain else None), mock.patch.object(fc.fc_worktree, "repo_worktree", made), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True):
            path, branch = fc.fc_crew.crew_workdir(str(repo), slot, "shop-toolsmith2")
            other, other_branch = fc.fc_crew.crew_workdir(str(slot), slot, "shop-explore")
            same, none = fc.fc_crew.crew_workdir(str(slot), slot, "shop-tests-001", leased=True)
            bare = fc.fc_crew.crew_workdir(str(plain), slot, "shop-research")
        self.assertEqual([c.args for c in made.call_args_list], [(str(repo), "shop-toolsmith2"), (str(slot), "shop-explore")])  # a worktree of its own, off the checkout it was pointed at
        self.assertEqual((path.parent, other.parent), (repo / ".claude" / "worktrees",) * 2)  # under the main checkout, never nested in the project worktree
        self.assertTrue(branch.startswith("shop-toolsmith2") and other_branch.startswith("shop-explore"))
        self.assertEqual((same, none), (slot, None))  # a worktree leased for this teammate runs where it was pointed
        self.assertEqual(bare, (plain, None))  # and so does a plain folder


class EndedSlots(unittest.TestCase):
    """A teammate's treehouse slot goes back to the pool once its tab is gone, and only then: a pool of finished teammates' slots
    left a new crew with nothing to lease."""
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, self.base, True)
        self.repo, self.slot = self.base / "tool", self.base / "slot"
        self.proj = self.base / "app" / ".compose" / "shop"; self.proj.mkdir(parents=True)
        self.repo.mkdir(); self.slot.mkdir()
        self.holder, self.agents, self.returned, self.dirty = "compose:shop-toolsmith", [], [], []
        for p in (mock.patch.object(fc.fc_worktree, "common_dir", lambda path: self.repo / ".git"), mock.patch.object(fc.fc_worktree, "dirty_paths", lambda path: self.dirty),
                  mock.patch.object(fc.fc_core, "all_projects", return_value={})):
            p.start(); self.addCleanup(p.stop)
        self.record("shop-toolsmith", worktree=str(self.slot), branch="shop-toolsmith-1", cwd=str(self.base / "app"))

    def record(self, name, **c):
        (fc.fc_crew.crew_dir(self.proj) / f"{name}.json").write_text(json.dumps({"name": name, "agent": name, "title": "t", "space": "w1", "tab": "w1:t2", **c}))

    def treehouse(self, repo, *args, check=True):
        if args[0] == "return":
            self.returned.append(Path(args[-1]))
        status = [{"path": str(self.slot), "status": "leased", "lease_holder": self.holder}]
        return subprocess.CompletedProcess(args, 0, json.dumps(status) if args[0] == "status" else "", "")

    def release(self):
        with mock.patch.object(fc.fc_worktree, "treehouse", self.treehouse), mock.patch.object(fc.fc_herdr, "herdr", return_value={"agents": self.agents}), \
                mock.patch("builtins.print"):
            fc.fc_crew.release_ended_slots(self.proj, self.agents or [{"name": "shop-orchestrator", "workspace_id": "w1"}])
        return self.returned

    def test_the_slot_of_a_teammate_whose_tab_is_gone_goes_back_and_its_branch_stays(self):
        self.assertEqual(self.release(), [self.slot])

    def test_a_running_teammate_a_dirty_slot_and_a_slot_leased_to_someone_else_stay(self):
        self.agents = [{"name": "shop-toolsmith", "workspace_id": "w1"}]
        self.assertEqual(self.release(), [])
        self.agents = []
        self.dirty = ["wip.txt"]
        self.assertEqual(self.release(), [])
        self.dirty = []
        self.holder = "compose:shop-other"
        self.assertEqual(self.release(), [])

    def test_crew_list_gives_the_slot_back(self):
        a = type("A", (), {"action": "list", "role": None})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_worktree, "treehouse", self.treehouse), \
                mock.patch.object(fc.fc_herdr, "herdr", return_value={"agents": [{"name": "shop-orchestrator", "workspace_id": "w1"}]}), mock.patch("builtins.print"):
            fc.fc_crew.cmd_crew(a)
        self.assertEqual(self.returned, [self.slot])

    def test_a_team_lead_in_the_project_worktree_has_no_slot_to_give_back(self):
        self.record("shop-toolsmith", cwd=str(self.slot))  # no branch, no worktree: it ran where it was pointed
        self.assertEqual(self.release(), [])

    def test_an_ended_team_lead_started_in_a_project_worktree_never_gives_that_worktree_back(self):
        """#248: a record with a branch whose cwd is a live project's worktree (--repo <project worktree>) looks like a teammate slot."""
        self.record("shop-toolsmith", cwd=str(self.slot), branch="shop-lead-1")
        self.holder = "compose:shop-toolsmith"  # even a holder that matches the record's name
        with mock.patch.object(fc.fc_core, "all_projects", return_value={"live": (str(self.slot), None)}):
            self.assertEqual(self.release(), [])


class AwaitStart(unittest.TestCase):
    """The brief is the session's initial prompt, so a prompt sent right after start cannot come first. crew start returns only
    once the agent is working on it, presses Enter if the box kept the text, sends the brief again if the box lost it, and names
    the agent when it never starts."""
    FIRST = "Your brief is the file /p/shop-tests-003.brief.md. Read it now and follow it."

    def run_await(self, statuses, first=None):
        calls, seen = [], iter(statuses)
        def herdr(*args, check=False, timeout=120):
            calls.append(args)
            return {"agent": {"agent_status": next(seen)}} if args[:2] == ("agent", "get") else {}
        with mock.patch.object(fc.fc_herdr, "herdr", herdr), mock.patch.object(time, "sleep"):
            fc.fc_herdr.herdr_await_start("shop-tests-003", first)
        return [c for c in calls if c[:2] in (("agent", "send-keys"), ("agent", "prompt"))]

    def test_a_slow_to_ready_agent_is_waited_for_without_enter(self):
        self.assertEqual(self.run_await(["idle"] * 5 + ["working"]), [])
        self.assertEqual(self.run_await(["done"]), [])

    def test_a_brief_left_in_the_box_is_submitted_with_enter(self):
        self.assertEqual(self.run_await(["idle"] * 10 + ["working"], self.FIRST), [("agent", "send-keys", "shop-tests-003", "enter")])

    def test_a_brief_the_box_lost_is_sent_again_once_enter_did_nothing(self):
        self.assertEqual(self.run_await(["idle"] * 13 + ["working"], self.FIRST),
                         [("agent", "send-keys", "shop-tests-003", "enter"), ("agent", "prompt", "shop-tests-003", self.FIRST)])

    def test_a_brief_that_never_starts_fails_loudly_with_the_agent_name(self):
        with self.assertRaises(RuntimeError) as e:
            self.run_await(["idle"] * 100, self.FIRST)
        self.assertIn("shop-tests-003 did not start", str(e.exception))
        self.assertIn("herdr agent prompt shop-tests-003", str(e.exception))


class Ready(unittest.TestCase):
    """`herdr agent start` calls a Claude session ready while its pane still shows the shell command, seconds before the input box
    exists; a brief typed then is kept unsent or lost. crew start waits for the box (the `❯` line) before the brief, and not past
    an agent already on a turn or blocked on a dialog."""
    SHELL = "➜  dotfiles git:(main) claude --model claude-fable-5-1 --no-chrome\n"
    BOX = "─────\n❯ Try \"edit <filepath> to...\"\n─────\n  ⏵⏵ auto mode on\n"

    def run_ready(self, status, screens, timeout=60):
        reads, shown = [], iter(screens)
        with mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: {"agent": {"agent_status": status}} if a[:2] == ("agent", "get") else {}), \
                mock.patch.object(fc.fc_herdr, "herdr_text", lambda *a, **k: reads.append(a) or next(shown, "")), mock.patch.object(time, "sleep"):
            fc.fc_herdr.herdr_ready("shop-tests-003", timeout=timeout)
        return reads

    def test_the_brief_waits_for_the_input_box(self):
        reads = self.run_ready("idle", [self.SHELL, self.SHELL, self.BOX, self.BOX])
        self.assertEqual(len(reads), 3)
        self.assertEqual(reads[0][:3], ("agent", "read", "shop-tests-003"))

    def test_an_agent_on_a_turn_or_blocked_is_not_waited_for(self):
        self.assertEqual(self.run_ready("working", [self.SHELL]), [])
        self.assertEqual(self.run_ready("blocked", [self.SHELL]), [])

    def test_a_box_that_never_shows_stops_the_wait_at_the_timeout(self):
        self.assertEqual(len(self.run_ready("idle", [self.SHELL] * 50, timeout=10)), 5)

    def test_crew_start_waits_for_the_box_before_the_brief(self):
        wt = Path(tempfile.mkdtemp()); proj = wt / ".compose" / "shop"; proj.mkdir(parents=True)
        calls = []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "working"}}
            if args[:2] == ("tab", "create"):
                return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}}
            return {}
        a = type("A", (), {"action": "start", "role": "tests", "title": "tests", "brief": "Write them.", "cwd": None})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                mock.patch.object(fc.fc_herdr, "herdr_ready", lambda agent, **k: calls.append(("ready", agent))), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), mock.patch("builtins.print"):
            fc.fc_crew.cmd_crew(a)
        heads = [c[:2] for c in calls]
        self.assertLess(heads.index(("agent", "start")), heads.index(("ready", "shop-tests")))
        self.assertLess(heads.index(("ready", "shop-tests")), heads.index(("agent", "prompt")))


class StartPaneBusy(unittest.TestCase):
    """Teammates started in parallel race each other: a new pane answers agent_pane_busy until its shell is up, which is retried, not a failed start."""
    def run_start(self, busy):
        calls = []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "start") and len([c for c in calls if c[:2] == args[:2]]) <= busy:
                raise RuntimeError("herdr agent start: agent_pane_busy")
            return {}
        with mock.patch.object(fc.fc_herdr, "herdr", herdr):
            fc.fc_herdr.herdr_start_crew("shop-design-086", "--kind", "claude")
        return len(calls)

    def test_a_busy_pane_is_retried_until_it_starts(self):
        self.assertEqual(self.run_start(busy=3), 4)

    def test_a_name_not_yet_registered_is_retried_like_a_busy_pane(self):
        calls = []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "start") and len(calls) == 1:
                raise RuntimeError("herdr agent start: agent_name_not_found")
            return {"agents": []} if args[:2] == ("agent", "list") else {}
        with mock.patch.object(fc.fc_herdr, "herdr", herdr):
            fc.fc_herdr.herdr_start_crew("shop-design-086", "--kind", "claude", "--pane", "wJ:p3")
        self.assertEqual([c[:2] for c in calls].count(("agent", "start")), 2)

    def test_a_name_not_found_start_that_made_a_session_names_it_and_never_starts_a_second(self):
        calls = []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "start"):
                raise RuntimeError("herdr agent start: agent_name_not_found")
            return {"agents": [{"name": "claude", "pane_id": "wH:p19"}]} if args[:2] == ("agent", "list") else {}
        with mock.patch.object(fc.fc_herdr, "herdr", herdr):
            fc.fc_herdr.herdr_start_crew("shop-design-123", "--kind", "claude", "--pane", "wH:p19")
        self.assertEqual([c[:2] for c in calls].count(("agent", "start")), 1)
        self.assertIn(("agent", "rename", "wH:p19", "shop-design-123"), calls)

    def test_a_pane_busy_for_good_fails_loudly(self):
        with self.assertRaisesRegex(RuntimeError, "agent_pane_busy"):
            self.run_start(busy=99)

    def run_timeout(self, live):
        calls = []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "start"):
                raise RuntimeError("herdr agent start: timeout")
            return {"agents": live} if args[:2] == ("agent", "list") else {}
        with mock.patch.object(fc.fc_herdr, "herdr", herdr):
            fc.fc_herdr.herdr_start_crew("shop-design-086", "--kind", "claude", "--pane", "wJ:p3")
        return calls

    def test_a_start_that_timed_out_but_came_up_is_named_and_kept(self):
        calls = self.run_timeout([{"name": "claude", "pane_id": "wJ:p3"}])
        self.assertIn(("agent", "rename", "wJ:p3", "shop-design-086"), calls)

    def test_a_start_that_timed_out_and_never_came_up_fails_loudly(self):
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.run_timeout([])


class AtomicState(unittest.TestCase):
    """A reader of the sync cache sees the old file or the new one, never an empty or partial one; watch retries a transient bad read."""
    def test_a_reader_never_sees_a_partial_write(self):
        import threading
        path = Path(tempfile.mkdtemp()) / "tasks.json"
        fc.fc_core.atomic_write(path, json.dumps({"n": 0}))
        stop, bad = threading.Event(), []
        def read():
            while not stop.is_set():
                try:
                    json.loads(path.read_text())
                except ValueError as e:
                    bad.append(e)
        t = threading.Thread(target=read); t.start()
        for i in range(50):
            fc.fc_core.atomic_write(path, json.dumps({"n": i, "pad": "x" * 10000}))
        stop.set(); t.join()
        self.assertEqual(bad, [])
        self.assertEqual([f.name for f in path.parent.iterdir()], ["tasks.json"])  # no temp file left behind

    def test_watch_retries_one_bad_read_of_the_cache(self):
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        reads = iter([json.JSONDecodeError("Expecting value", "", 0), {"rows": []}])
        def board(repo, p):
            r = next(reads)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(fc.fc_status, "board", board), mock.patch.object(fc.fc_herdr, "herdr", return_value={}):
            self.assertEqual(fc.fc_watch.watch_snapshot(proj.parent, proj)["tasks"], {})


class RestrictedStart(unittest.TestCase):
    """A Claude teammate starts with only its role's MCP servers and skills, never a Frame0 server, and the briefs name their files."""
    SETTINGS = {"model": "opus", "enabledPlugins": {"compose@fc": True, "figma@claude-plugins-official": True, "superpowers@sp": True, "ponytail@p": False,
                                                    "compose-extra@x": True, "my-compose@x": True, "figmatic@x": True}}
    P = ["claude_ai_Claude_Docs", "claude_ai_Figma", "claude_ai_Google_Calendar", "claude_ai_Linear", "claude_ai_Notion", "frame0", "framelink-figma", "graft", "plugin_product-management_figma", "trail"]

    def start(self, role, model="claude:claude-opus-5-5", effort=None, mcpjson=None, **kw):
        proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        proj.mkdir(parents=True)
        if mcpjson is not None:  # the session's cwd (crew_workdir below) holds this .mcp.json
            (proj / ".mcp.json").write_text(json.dumps({"mcpServers": mcpjson}))
        home = Path(tempfile.mkdtemp())
        if self.SETTINGS is not None:  # None: the user has no settings file
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text(json.dumps(self.SETTINGS))
        calls = []
        def herdr(*args, **k):
            calls.append(args)
            return {"agents": []} if args[:2] == ("agent", "list") else {"agent": {"agent_status": "working"}} if args[:2] == ("agent", "get") else \
                {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}} if args[:2] == ("tab", "create") else {}
        a = type("A", (), dict(action="start", role=role, title="t", brief="b", **kw))
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), \
                mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), \
                mock.patch.object(fc.fc_crew.fc_crewpick, "crew_pick_full", return_value=(model, effort, "", [])), mock.patch.object(fc.fc_crew, "crew_workdir", return_value=(str(proj), None)), \
                mock.patch.object(fc.fc_mcp, "installed_servers", side_effect=lambda cwd: self.P), mock.patch.object(fc.fc_herdr, "PI_HERDR_EXT", proj / "missing"), mock.patch("builtins.print"), \
                mock.patch("pathlib.Path.home", return_value=home):
            fc.fc_crew.cmd_crew(a)
        start = next(c for c in calls if c[:2] == ("agent", "start"))
        return list(start[start.index("--") + 1:])

    def patterns(self, args):
        self.assertIn("--disallowedTools", args)
        i = args.index("--disallowedTools") + 1
        return set(args[i:next(j for j in range(i, len(args)) if args[j].startswith("--"))])

    def plugins_off(self, role):
        ep = json.loads((lambda a: a[a.index("--settings") + 1])(self.start(role)))["enabledPlugins"]
        self.assertNotIn(False, [ep.get("compose@fc")])  # compose stays on
        return {k for k, v in ep.items() if v is False}

    def test_planner_gets_crew_settings_and_drops_the_other_servers(self):
        args = self.start("planner")
        settings = json.loads(args[args.index("--settings") + 1])
        self.assertEqual({k: settings[k] for k in ("attribution", "env")}, {"attribution": {"commit": "", "pr": ""}, "env": {"ENABLE_TOOL_SEARCH": "true", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"}})
        self.assertFalse({"--strict-mcp-config", "--mcp-config"} & set(args))
        self.assertEqual(self.patterns(args), {f"mcp__{p}__*" for p in self.P})
        self.assertLess(args.index("--disallowedTools"), args.index("--allowedTools"))
        self.assertEqual(args[-3:-2], ["--allowedTools"])  # it swallows what follows, so it stays last

    def test_a_session_approves_no_mcpjson_server_whatever_the_repo_wires(self):
        settings = lambda args: json.loads(args[args.index("--settings") + 1])
        for mcpjson in ({"graft": {"command": "graft", "args": ["mcp"]}}, {"trail": {"command": "trail", "args": ["mcp"]}}, None):
            for role in ("orchestrator", "implementer", "designer"):
                with self.subTest(mcpjson=mcpjson, role=role):
                    got = settings(self.start(role, mcpjson=mcpjson))
                    self.assertNotIn("enabledMcpjsonServers", got)
                    self.assertLessEqual({"attribution", "env", "enabledPlugins"}, set(got))

    def test_designer_keeps_figma_but_not_linear_graft_or_trail(self):
        dropped = self.patterns(self.start("designer"))
        for p in ("framelink-figma", "claude_ai_Figma"):
            self.assertNotIn(f"mcp__{p}__*", dropped)
        self.assertLessEqual({"mcp__claude_ai_Linear__*", "mcp__graft__*", "mcp__trail__*", "mcp__claude_ai_Notion__*", "mcp__plugin_product-management_figma__*"}, dropped)  # only the Figma plugin's own server is kept

    def test_no_session_starts_a_frame0_server(self):
        """A server on the argv starts with the session and idles for its whole life (every architect and designer ran one, drawn or not);
        `compose frame0` reaches the app without one, so the app runs only from `frame0 up` to `down`."""
        for role in ("designer", "architect", "planner", "orchestrator", "design-003", "design-review", "f0-helper"):
            with self.subTest(role=role):
                args = self.start(role)
                self.assertNotIn("--mcp-config", args)
                self.assertIn("mcp__frame0__*", self.patterns(args))  # a `frame0` server someone registered is denied like any other
                self.assertEqual(args[-3:], ["--allowedTools", "Bash(npx -y lavish-axi:*)", "Bash(compose frame0:*)"])  # the commands run with no prompt

    def test_only_design_roles_keep_skills(self):
        for role, off in [("orchestrator", True), ("planner", True), ("architect", True), ("explore", True), ("toolsmith", True),
                          ("designer", False), ("design-003", False), ("design-review", False)]:
            with self.subTest(role=role):
                self.assertEqual("--disable-slash-commands" in self.start(role), off)

    def test_every_plugin_but_compose_is_off(self):
        rest = {"superpowers@sp", "ponytail@p", "compose-extra@x", "my-compose@x", "figmatic@x"}
        for role in ("planner", "orchestrator", "toolsmith", "design-x"):
            with self.subTest(role=role):
                self.assertEqual(self.plugins_off(role), rest | {"figma@claude-plugins-official"})
        for role in ("designer", "design-003", "design-review"):  # the designer line keeps figma
            with self.subTest(role=role):
                self.assertEqual(self.plugins_off(role), rest)

    def test_no_chrome_and_trust_framing_follow_model_and_effort(self):
        trust = ["--no-chrome", "--append-system-prompt", fc.fc_crew.TRUST_PROMPT]
        self.assertEqual(self.start("architect", effort="high")[:8], ["--model", "claude-opus-5-5", "--effort", "high", *trust, "--settings"])
        self.assertEqual(self.start("planner", effort="high")[:8], ["--model", "claude-opus-5-5", "--effort", "high", *trust, "--settings"])
        self.assertEqual(self.start("designer")[:6], ["--model", "claude-opus-5-5", *trust, "--settings"])

    def tail(self, args):
        """What follows the --settings value: the patterns when any, the skills switch, then auto mode and the allow list."""
        return args[args.index("--settings") + 2:]

    def test_no_patterns_means_no_bare_disallowed_tools(self):
        allow = ["--permission-mode", "auto", "--allowedTools", "Bash(npx -y lavish-axi:*)", "Bash(compose frame0:*)"]
        for servers, role in [([], "planner"), ([], "designer"), (["claude_ai_Figma", "framelink-figma"], "designer")]:
            with self.subTest(servers=servers, role=role):
                self.P = servers
                skills = [] if role == "designer" else ["--disable-slash-commands"]
                self.assertEqual(self.tail(self.start(role)), skills + allow)  # a bare flag would swallow what follows

    def test_patterns_sit_between_settings_and_the_allow_list(self):
        self.P = ["claude_ai_Linear", "claude_ai_Notion", "trail", "frame0"]
        tail = self.tail(self.start("planner"))
        self.assertEqual(tail[0], "--disallowedTools")
        self.assertEqual(set(tail[1:5]), {"mcp__claude_ai_Linear__*", "mcp__claude_ai_Notion__*", "mcp__trail__*", "mcp__frame0__*"})
        self.assertEqual(tail[5:], ["--disable-slash-commands", "--permission-mode", "auto", "--allowedTools", "Bash(npx -y lavish-axi:*)", "Bash(compose frame0:*)"])

    def test_every_claude_teammate_compacts_at_150k_through_its_settings_env(self):
        for role in ("orchestrator", "planner", "architect", "explore", "groundwork", "designer", "toolsmith", "design-003", "design-review"):
            with self.subTest(role=role):
                args = self.start(role)
                env = json.loads(args[args.index("--settings") + 1])["env"]
                self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "150000")  # the env var outranks --autocompact and the user's own settings
                self.assertNotIn("--autocompact", args)

LINEAR_LINE = ("Linear is the record: read and write it with `compose linear` (get, list, create, update, comment), never a Linear MCP tool; "
               "write what you learn and decide there, never to files.")


class PiStart(unittest.TestCase):
    def start(self, ext):
        """(argv after `--` of the pi architect's `agent start`, its project dir, its brief); `ext` names an existing herdr extension."""
        proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"; proj.mkdir(parents=True)
        calls = []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "list"):
                return {"agents": []}
            if args[:2] == ("tab", "create"):
                return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}}
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "working"}}
            return {}
        if ext:
            (proj / ext).write_text("")
        a = type("A", (), {"action": "start", "role": "architect", "title": "t", "brief": "b", "model": "pi:x/y"})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), \
                mock.patch.object(fc.fc_models, "model_available", return_value=(True, "")), mock.patch.object(fc.fc_herdr, "PI_HERDR_EXT", proj / (ext or "missing.ts")), \
                mock.patch.object(fc.fc_mcp, "installed_servers", return_value=["claude_ai_Linear", "graft"]), \
                mock.patch("builtins.print"):
            fc.fc_crew.cmd_crew(a)
        start = next(c for c in calls if c[:2] == ("agent", "start"))
        return list(start[start.index("--") + 1:]), proj, (fc.fc_crew.crew_dir(proj) / "shop-architect.brief.md").read_text()

    def test_pi_teammate_starts_with_no_mcp_and_no_extension_packages(self):
        for ext in (None, "herdr-agent-state.ts"):
            with self.subTest(ext=ext):
                args, proj, brief = self.start(ext)
                pairs = list(zip(args, args[1:]))
                self.assertFalse(any("builtin:mcp" in v for v in args))  # no MCP servers, so no Linear MCP tool
                self.assertEqual([v for k, v in pairs if k == "-e"], [str(proj / ext)] if ext else [])  # only the herdr extension, when it exists
                for flag in ("--no-extensions", "--no-prompt-templates", "--skill"):
                    self.assertIn(flag, args)  # the user's npm extension packages and prompt templates stay off
                self.assertEqual(brief.count(LINEAR_LINE), 1)  # the pi teammate is told to use compose linear instead
        self.assertNotIn("builtin:mcp", fc.fc_models.probe_command("pi:x/y"))  # the implementer probe is unchanged



class PreambleNamesTrail(unittest.TestCase):
    """Every teammate's preamble sends it to trail for file:line evidence; the tab still carries graft's env names."""

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        self.proj.mkdir(parents=True)

    def test_crew_start_still_passes_the_graft_env_to_the_tab(self):
        calls = []
        def herdr(*args, **kw):
            calls.append(args)
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "working"}}
            return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}} if args[:2] == ("tab", "create") else {}
        a = type("A", (), {"action": "start", "role": "tests-001", "title": "tests 001", "brief": "Write the test."})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), \
                mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), mock.patch("builtins.print"):
            fc.fc_crew.cmd_crew(a)
        tab = next(c for c in calls if c[:2] == ("tab", "create"))
        envs = [tab[i + 1] for i, v in enumerate(tab) if v == "--env"]
        self.assertIn("GRAFT_NO_GITIGNORE=1", envs)
        self.assertIn("GRAFT_NO_IGNORE=1", envs)
        brief = " ".join((fc.fc_crew.crew_dir(self.proj) / "shop-tests-001.brief.md").read_text().split())
        self.assertIn("trail output with file:line", brief)  # the brief the session reads carries the new preamble
        self.assertTrue(brief.endswith("Write the test."))


class CrewLoadWait(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        self.proj.mkdir(parents=True)
        self.calls = []

    def herdr(self, *args, **kw):
        self.calls.append(args)
        return {"agents": []} if args[:2] == ("agent", "list") else {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}} if args[:2] == ("tab", "create") else {"agent": {"agent_status": "working"}}

    def crew(self, action, wait, force=None, battery=None):
        a = type("A", (), {"action": action, "role": "planner", "title": "planner", "brief": "Go.", "force": False} | ({} if force is None else {"force": force}))
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", self.herdr), mock.patch.object(fc_load, "wait", wait) as w, \
                mock.patch.object(fc.fc_crew.fc_checks, "battery_stop", return_value=battery), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), \
                mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), \
                mock.patch.object(fc.fc_crew.fc_crewpick, "crew_pick_full", return_value=("claude", None, "test", [])), mock.patch("builtins.print"), contextlib.suppress(SystemExit):
            fc.fc_crew.cmd_crew(a)
        return w

    def test_start_waits_before_opening_anything_then_starts(self):
        w = self.crew("start", mock.Mock(side_effect=lambda say, force=False: self.calls.append("waited")))
        self.assertEqual(self.calls[0], "waited")  # nothing was opened while it waited
        self.assertIn(("tab", "create"), [c[:2] for c in self.calls[1:]])  # then the teammate started
        self.assertEqual(w.call_count, 1)

    def test_wait_gets_say_and_force(self):
        for force, want in ((None, False), (True, True)):
            w = self.crew("start", mock.Mock(), force)
            self.assertTrue(callable(w.call_args.args[0]))
            self.assertEqual(w.call_args.args[1], want)

    def test_a_refused_wait_or_low_battery_starts_nothing_and_other_actions_never_wait(self):
        self.crew("start", mock.Mock(side_effect=SystemExit("held")))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.crew("start", mock.Mock(), battery="9%").call_count, 0)  # the battery check comes first
        for action in ("list", "focus", "stop"):
            self.assertEqual(self.crew(action, mock.Mock()).call_count, 0, action)


class CrewRecorded(unittest.TestCase):
    def start(self, model, pids, fail=False):
        """(the record call args or None, the pane agent_pids was asked about, the teammate's recorded cwd); the exit is swallowed."""
        import fc_started
        proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"; proj.mkdir(parents=True)
        def herdr(*args, **kw):
            return {"agents": []} if args[:2] == ("agent", "list") else {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}} if args[:2] == ("tab", "create") else {"agent": {"agent_status": "working"}}
        a = type("A", (), {"action": "start", "role": "planner", "title": "planner", "brief": "Go."})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), \
                mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), mock.patch.object(fc.fc_crew.fc_crewpick, "crew_pick_full", return_value=(model, None, "test", [])), \
                mock.patch.object(fc.fc_herdr, "herdr_start_crew", side_effect=RuntimeError("no start") if fail else None), mock.patch.object(fc.fc_herdr, "pane_launching", return_value=False), \
                mock.patch.object(fc.fc_herdr, "agent_pids", return_value=pids) as ap, mock.patch.object(fc_started, "record") as rec, mock.patch("builtins.print"), contextlib.suppress(SystemExit):
            fc.fc_crew.cmd_crew(a)
        cwd = (fc.fc_crew.crew_record(proj, "shop-planner") or {}).get("cwd")
        return (rec.call_args if rec.called else None), (ap.call_args.args[0] if ap.called else None), cwd, proj, rec.call_count

    def test_both_harness_paths_record_the_lowest_pid_with_project_and_cwd(self):
        for model in ("claude", "pi:x/y"):
            with self.subTest(model=model):
                call, pane, cwd, proj, count = self.start(model, [412, 77, 900])
                self.assertEqual((count, pane, bool(cwd)), (1, "w9:p7", True))
                self.assertEqual((call.args[:3], call.kwargs.get("cwd"), call.kwargs.get("waiter")), ((77, "teammate", str(proj)), cwd, None))

    def test_a_failed_start_or_no_pid_records_nothing(self):
        for model, pids, fail in (("claude", [412], True), ("pi:x/y", [412], True), ("claude", [], False), ("pi:x/y", [], False)):
            with self.subTest(model=model, pids=pids, fail=fail):
                self.assertEqual(self.start(model, pids, fail)[4], 0)


class RefusedReports(unittest.TestCase):
    """A report compose refuses leaves a record and a watch event: the team lead learns the teammate tried, and why it failed."""
    def setUp(self):
        wt = Path(tempfile.mkdtemp())
        self.proj = wt / ".compose" / "shop"
        self.proj.mkdir(parents=True)
        for name in ("shop-orchestrator", "shop-q-neb-1", "shop-tests-003"):
            (fc.fc_crew.crew_dir(self.proj) / f"{name}.json").write_text(json.dumps({"name": name, "role": name[5:], "title": name[5:], "space": "w9", "tab": f"w9:{name}", "pane": "p", "reports_to": "orchestrator"}))
            fc.fc_crew.write_report(self.proj, name, "working", "started")

    def report(self, who, state, msg):
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("builtins.print"), \
                mock.patch.object(fc.fc_herdr, "herdr", lambda *args, **kw: None):
            try:
                fc.fc_crew.cmd_report(type("A", (), {"as_": who, "state": state, "message": msg.split()}))
            except SystemExit as e:
                return str(e)

    def test_a_bad_state_is_refused_recorded_and_an_urgent_event(self):
        before = snap(self.proj)
        why = self.report("shop-q-neb-1", "finished", "all good")
        self.assertIn("REPORT REFUSED (shop-q-neb-1 finished): state is one of", why)
        self.assertEqual([r["why"][:16] for r in fc.fc_reports.crew_refusals(self.proj, "shop-q-neb-1")], ["state is one of "])
        self.assertEqual(fc.fc_crew.crew_reports(self.proj, "shop-q-neb-1")[-1]["message"], "started")  # nothing was sent
        ev = fc.fc_watch.watch_events(before, snap(self.proj))
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0].startswith("event crew=shop-q-neb-1 report=refused state=finished why='state is one of"), ev[0])
        self.assertTrue(fc.fc_watch.URGENT.search(ev[0]))
        self.assertEqual(fc.fc_watch.watch_filter(ev, "shop", []), ev)  # the team lead's watch gets it
        self.assertEqual(fc.fc_watch.watch_events(snap(self.proj), snap(self.proj)), [])

    def test_a_question_asked_twice_is_refused_with_the_gate_reason(self):
        self.assertIsNone(self.report("shop-q-neb-1", "needs-user", "NEB-1: 2 questions Q1-Q2 on the map; recommendation Clerk, SQLite"))
        self.assertIsNone(self.report("shop-q-neb-1", "needs-user", "NEB-1: 2 questions Q1-Q2 on the map; recommendation Clerk, SQLite"))
        refusals = fc.fc_reports.crew_refusals(self.proj, "shop-q-neb-1")
        self.assertEqual(len(refusals), 1)
        self.assertIn("already asked", refusals[0]["why"])
        self.assertEqual([r["state"] for r in fc.fc_crew.crew_reports(self.proj, "shop-q-neb-1")], ["working", "needs-user"])

    def test_a_test_writers_test_ready_starts_the_collect_when_its_turn_ends(self):
        spawned = []
        with mock.patch.object(fc.fc_crew.subprocess, "Popen", lambda args, **kw: spawned.append((args, kw))), mock.patch.object(fc.fc_herdr, "herdr_env", lambda: True):
            self.report("shop-tests-003", "working", "test ready: abc1234; holdouts: empty cart")
            self.report("shop-q-neb-1", "working", "test ready for review")  # not a test writer
            self.report("shop-tests-003", "working", "still writing")
        self.assertEqual(len(spawned), 1)
        args, kw = spawned[0]
        self.assertEqual(args[:2], ["sh", "-c"])
        self.assertIn("agent wait", args[2])
        self.assertIn("testcrews --collect --only", args[2])
        self.assertIn('report --repo "$4" --project "$5" --as "$2" blocked', args[2])
        self.assertEqual(args[5:], ["shop-tests-003", "003", str(self.proj.resolve().parents[1]), "shop", str(self.proj / "results" / "collect.log")])
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(fc.fc_reports.test_writer_id(self.proj, "shop-tests-003"), "003")
        self.assertIsNone(fc.fc_reports.test_writer_id(self.proj, "other-tests-003"))
        self.assertIsNone(fc.fc_reports.test_writer_id(self.proj, "shop-orchestrator"))

    def test_outside_herdr_nothing_is_spawned(self):
        spawned = []
        with mock.patch.object(fc.fc_crew.subprocess, "Popen", lambda args, **kw: spawned.append(args)), mock.patch.object(fc.fc_herdr, "herdr_env", lambda: False):
            self.report("shop-tests-003", "working", "test ready: abc1234")
        self.assertEqual(spawned, [])


class FigmaReads(unittest.TestCase):
    """fc_hooks.figma_decision: in a compose session the official Figma connector is for writes (`use_figma` and the
    `create_session` it needs); every read on it is denied and pointed at Framelink."""
    SERVERS = ("plugin_figma_figma", "claude_ai_Figma")
    READS = ("get_screenshot", "get_design_context", "get_metadata", "whoami", "search_design_system", "get_libraries", "create_new_file", "get_figjam")

    def test_writes_pass_and_reads_are_denied_for_every_compose_role(self):
        for server in self.SERVERS:
            for role in ("crew", "orchestrator", "planner-ENG-1"):
                for name in ("use_figma", "create_session"):
                    self.assertIsNone(fc.fc_hooks.figma_decision(f"mcp__{server}__{name}", role))
                for name in self.READS:
                    why = fc.fc_hooks.figma_decision(f"mcp__{server}__{name}", role)
                    self.assertTrue(why and f"`{name}` is a read" in why, (server, role, name, why))
                    self.assertIn("compose: the Figma connector is for writes only (`use_figma`, and the `create_session` it needs)", why)
                    for hint in ("`mcp__framelink-figma__get_figma_data` for a node tree", "`mcp__framelink-figma__download_figma_images` for images", "`compose figma tree`"):
                        self.assertIn(hint, why)

    def test_outside_a_compose_session_and_other_servers_nothing_is_denied(self):
        for role in (None, ""):
            self.assertIsNone(fc.fc_hooks.figma_decision("mcp__plugin_figma_figma__get_screenshot", role))
        for tool in ("mcp__framelink-figma__get_figma_data", "mcp__framelink-figma__download_figma_images", "mcp__claude_ai_Linear__get_issue", "Read", "Bash",
                     "mcp__plugin_figma_figma", "mcp__plugin_figma_figmax__get_screenshot", "", None):
            self.assertIsNone(fc.fc_hooks.figma_decision(tool, "crew"), tool)
