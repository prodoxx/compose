import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, sys, tempfile, time, unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock
# tests/test_modules.py fails a test file without the next line
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
lead, crew = fc.fc_lead, fc.fc_crew

NAME = "shop-orchestrator"
AGENT = {"name": NAME, "workspace_id": "w1", "agent_status": "idle"}


class Base(unittest.TestCase):
    def setUp(self):
        wt = Path(tempfile.mkdtemp()).resolve()
        self.proj = wt / ".compose" / "shop"
        (self.proj / "results" / "crew").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-9", "stories": ["NEB-10"]}))
        self.enter(mock.patch.object(fc.fc_update, "live_on", return_value=[]))
        self.enter(mock.patch.object(fc.fc_tasks, "run_alive", return_value=False))
        self.enter(mock.patch.object(fc.fc_tasks, "fixing", return_value=False))
        self.enter(mock.patch.object(time, "sleep"))
        self.agents = [dict(AGENT)]
        self.calls = []
        self.record(NAME, "orchestrator", title="orchestrator")
        self.report(NAME, "working", "started")
        self.report(NAME, "done", "NEB-10 verified")
        self.handoff("## Open threads\nnone\n")

    def enter(self, p):
        self.addCleanup(p.stop)
        return p.start()

    def record(self, name, role, title=None, **more):
        (self.proj / "results" / "crew" / f"{name}.json").write_text(json.dumps(
            {"name": name, "agent": name, "role": role, "title": title or role, "model": "claude:x", "model_from": "models.crew.default", "space": "w1", "tab": f"t-{name}", **more}))

    def report(self, name, state, msg=""):
        crew.write_report(self.proj, name, state, msg)

    def handoff(self, text):
        f = crew.handoff_path(self.proj, NAME)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        os.utime(f, (time.time() + 5, time.time() + 5))  # after the reports above, which share a second with it

    def gaps(self):
        return lead.gaps(self.proj, self.agents)


class Lead(Base):
    def test_a_lead_that_reported_done_with_nothing_running_and_a_current_handoff_can_be_replaced(self):
        self.assertEqual(self.gaps(), (False, []))

    def test_a_lead_that_has_not_reported_done_is_not_replaced(self):
        self.report(NAME, "working", "run started; parallel: none because the run is the work")
        midturn, why = self.gaps()
        self.assertFalse(midturn)
        self.assertEqual(len(why), 1)
        self.assertIn("working, not done", why[0])

    def test_an_unanswered_question_is_named(self):
        self.report(NAME, "needs-user", "NEB-10: which provider? I recommend Stripe")
        _, why = self.gaps()
        self.assertIn("needs-user", why[0])
        self.assertIn("which provider", why[0])

    def test_a_lead_at_a_prompt_is_not_replaced(self):
        self.agents[0]["agent_status"] = "blocked"
        self.assertIn("it waits at a prompt", self.gaps()[1])

    def test_a_lead_in_a_turn_is_midturn(self):
        self.agents[0]["agent_status"] = "working"
        self.assertTrue(self.gaps()[0])

    def test_a_live_run_or_verify_holds_the_lead(self):
        with mock.patch.object(fc.fc_update, "live_on", return_value=[{"kind": "verify", "pid": 42}]):
            self.assertEqual(self.gaps()[1], ["a verify is running (pid 42)"])
        with mock.patch.object(fc.fc_tasks, "fixing", return_value=True):
            self.assertEqual(self.gaps()[1], ["a run or a verify fix round is going"])

    def test_a_teammate_that_has_not_finished_holds_the_lead_until_it_is_done_or_its_tab_is_gone(self):
        self.record("shop-planner", "planner", tab="t-planner")
        self.report("shop-planner", "working", "specing NEB-11")
        self.agents.append({"name": "shop-planner", "workspace_id": "w1", "agent_status": "working"})
        self.assertEqual(self.gaps()[1], ["teammates are live: shop-planner"])
        self.report("shop-planner", "done", "stories drafted")
        self.assertEqual(self.gaps()[1], [])
        self.report("shop-planner", "working", "again")
        self.agents.pop()  # its tab closed
        self.assertEqual(self.gaps()[1], [])

    def test_a_handoff_that_is_missing_holds_the_lead(self):
        crew.handoff_path(self.proj, NAME).unlink()
        self.assertEqual(len(self.gaps()[1]), 1)
        self.assertIn("no handoff file", self.gaps()[1][0])

    def test_the_verdict_for_a_story_is_fresh_at_a_boundary_wait_mid_turn_and_keep_otherwise(self):
        listed = lambda *a, **k: {"agents": list(self.agents)} if a[:2] == ("agent", "list") else {}
        with mock.patch.object(fc.fc_herdr, "herdr", side_effect=listed):
            self.assertEqual(lead.verdict(self.proj), ("fresh", ""))
            self.agents[0]["agent_status"] = "working"
            self.assertEqual(lead.verdict(self.proj), ("wait", f"{NAME} is mid-turn"))
            self.agents[0]["agent_status"] = "idle"
            self.report(NAME, "needs-user", "NEB-10: which provider?")
            how, why = lead.verdict(self.proj)
            self.assertEqual(how, "keep")
            self.assertIn("which provider", why)
            self.agents.clear()
            self.assertEqual(lead.verdict(self.proj), ("keep", ""))
        with mock.patch.object(fc.fc_herdr, "herdr", return_value=None):  # herdr does not answer: not a boundary, and nothing to say about it
            self.assertEqual(lead.verdict(self.proj), ("keep", ""))

    def test_the_fresh_leads_brief_points_at_the_files_and_ends_with_the_job(self):
        text = lead.restart_brief(self.proj, "factory: build story NEB-11 of feature NEB-9 (Shop).")
        self.assertIn(str(crew.handoff_path(self.proj, NAME)), text)
        self.assertIn("feature NEB-9", text)
        self.assertIn(f"compose status --repo {self.proj.parents[1]} --project shop", text)
        self.assertIn(lead.LEAD_READS, text)
        self.assertIn("Where the handoff and this state disagree, the state wins", text)
        self.assertTrue(text.rstrip().endswith("factory: build story NEB-11 of feature NEB-9 (Shop)."))
        self.assertNotIn(chr(0x2014), text)

    def test_the_fresh_leads_brief_says_when_there_is_no_handoff_or_job(self):
        crew.handoff_path(self.proj, NAME).unlink()
        text = lead.restart_brief(self.proj)
        self.assertIn("It does not exist: rebuild", text)
        self.assertIn("No new job comes with this brief", text)


class Restart(Base):
    def setUp(self):
        super().setUp()
        self.enter(mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj))
        self.enter(mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True))
        self.enter(mock.patch.object(fc.fc_herdr, "herdr_manager_claim"))
        self.enter(mock.patch.object(fc.fc_herdr, "herdr", side_effect=self.herdr))
        self.started = []
        self.enter(mock.patch.object(crew, "cmd_crew", side_effect=lambda a: self.started.append(a)))
        self.out = io.StringIO()
        self.enter(mock.patch("sys.stdout", self.out))

    def herdr(self, *args, **kw):
        self.calls.append(args)
        if args[:2] == ("agent", "list"):
            return {"agents": list(self.agents)}
        if args[:2] == ("tab", "close"):
            self.agents[:] = [ag for ag in self.agents if ag["name"] != NAME]
        return {}

    def args(self, **kw):
        return Namespace(**{"action": "restart", "role": "orchestrator", "brief": None, "force": False, "title": None, "model": None, "takeover": False, "repo": None, "project": "shop", **kw})

    def closed(self):
        return [c for c in self.calls if c[:2] == ("tab", "close")]

    def test_a_clean_restart_closes_the_tab_and_starts_a_fresh_lead_under_the_same_name(self):
        brief = crew.crew_dir(self.proj) / f"{NAME}.brief.md"
        brief.write_text("the first brief\n")
        lead.cmd_crew(self.args(brief="factory: build story NEB-11."))
        self.assertEqual(self.closed(), [("tab", "close", f"t-{NAME}")])
        (a,) = self.started
        self.assertEqual((a.action, a.role, a.title, a.model), ("start", "orchestrator", "orchestrator", None))
        self.assertIn(str(crew.handoff_path(self.proj, NAME)), a.brief)
        self.assertTrue(a.brief.rstrip().endswith("factory: build story NEB-11."))
        self.assertEqual(lead.first_brief_path(self.proj, NAME).read_text(), "the first brief\n")
        self.assertEqual(crew.crew_record(self.proj, NAME)["closed"]["why"], "crew restart")
        last = crew.crew_reports(self.proj, NAME)[-1]
        self.assertEqual(last["state"], "working")
        self.assertIn("restarted fresh", last["message"])

    def test_the_first_brief_is_kept_through_later_restarts(self):
        brief = crew.crew_dir(self.proj) / f"{NAME}.brief.md"
        brief.write_text("the first brief\n")
        lead.cmd_crew(self.args())
        brief.write_text("the restart brief\n")
        self.agents.append(dict(AGENT))
        self.report(NAME, "done", "NEB-11 verified")
        self.handoff("## Open threads\nnone\n")
        lead.cmd_crew(self.args())
        self.assertEqual(lead.first_brief_path(self.proj, NAME).read_text(), "the first brief\n")

    def test_a_model_the_lead_was_started_with_by_flag_is_kept(self):
        self.record(NAME, "orchestrator", title="lead", model="claude:big", model_from="--model")
        lead.cmd_crew(self.args())
        (a,) = self.started
        self.assertEqual((a.model, a.title), ("claude:big", "lead"))

    def test_a_lead_mid_turn_is_never_restarted_not_even_with_force(self):
        self.agents[0]["agent_status"] = "working"
        a = self.args()
        a.force = True
        with self.assertRaises(SystemExit) as cm:
            lead.cmd_crew(a)
        self.assertIn("mid-turn", str(cm.exception))
        self.assertEqual((self.closed(), self.started), ([], []))

    def test_work_in_flight_refuses_a_restart_unless_forced(self):
        self.report(NAME, "working", "run started")
        with self.assertRaises(SystemExit) as cm:
            lead.cmd_crew(self.args())
        self.assertIn("not done", str(cm.exception))
        self.assertIn("--force", str(cm.exception))
        self.assertEqual((self.closed(), self.started), ([], []))
        a = self.args()
        a.force = True
        lead.cmd_crew(a)
        self.assertEqual(len(self.closed()), 1)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(crew.crew_record(self.proj, NAME)["closed"]["why"], "crew restart --force")

    def test_no_live_lead_says_how_to_start_one(self):
        self.agents.clear()
        with self.assertRaises(SystemExit) as cm:
            lead.cmd_crew(self.args())
        self.assertIn("no live team lead", str(cm.exception))
        self.assertIn("crew start", str(cm.exception))

    def test_only_the_team_lead_restarts(self):
        a = self.args()
        a.role = "planner"
        with self.assertRaises(SystemExit) as cm:
            lead.cmd_crew(a)
        self.assertIn("only the team lead restarts", str(cm.exception))
        self.assertEqual(self.closed(), [])

    def test_a_replacement_that_does_not_start_says_the_old_lead_is_closed_and_how_to_start_again(self):
        crew.cmd_crew.side_effect = SystemExit("teammate shop-orchestrator did not start: boom")
        with self.assertRaises(SystemExit) as cm:
            lead.cmd_crew(self.args())
        msg = str(cm.exception)
        self.assertIn("was closed but its fresh replacement did not start", msg)
        self.assertIn("boom", msg)
        self.assertIn("crew start", msg)

    def test_a_closed_lead_that_herdr_still_lists_is_not_started_over(self):
        with mock.patch.object(fc.fc_lead, "GONE_SECONDS", 0), mock.patch.object(fc.fc_herdr, "herdr", side_effect=lambda *a, **k: {"agents": [dict(AGENT)]} if a[:2] == ("agent", "list") else {}):
            with self.assertRaises(SystemExit) as cm:
                lead.cmd_crew(self.args())
        self.assertIn("still lists it", str(cm.exception))
        self.assertEqual(self.started, [])

    def test_the_other_actions_go_to_crew(self):
        a = self.args()
        a.action = "list"
        lead.cmd_crew(a)
        self.assertEqual(self.started, [a])


if __name__ == "__main__":
    unittest.main()
