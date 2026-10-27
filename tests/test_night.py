import hermetic  # first: no test sees the real home
import inspect, json, os, subprocess, tempfile, time, unittest, unittest.mock
from pathlib import Path
import importlib.machinery, importlib.util, sys
# tests/test_modules.py fails a test file without the next line
os.environ["HERDR_ENV"] = ""
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_factorystate as st, fc_factoryturn as ft, fc_factory as f
import fc_config, fc_delivery, fc_plan, fc_readiness, fc_claude, fc_change, fc_reconcile
import fc_nightstate as ns
import fc_answer as fa, fc_core, fc_proc
import fc_nightprep as fp
import fc_digest as fd


BIN = Path(__file__).resolve().parents[1] / "bin"


class PlansLanded(unittest.TestCase):
    params = staticmethod(lambda fn: list(inspect.signature(fn).parameters))

    def test_the_seams_of_the_build_loop_exist_as_promised(self):
        self.assertEqual((set(st.PACE), set(ft.IDLE)), ({"day", "night"}, {"day", "night"}))  # night joins PACE when fc_nightstate loads, and IDLE when fc_nightprep does
        self.assertEqual(st.EMPTY["mode"], "day")
        self.assertEqual([self.params(g) for g in (st.pace, st.day_cap)], [["state"]] * 2)
        self.assertEqual(self.params(st.add_event), ["repo", "line"]); self.assertEqual(self.params(st.events_since), ["repo", "n"])
        self.assertEqual(self.params(st.say_once), ["state", "key", "line", "out"])
        self.assertEqual(self.params(ft.turn)[:3], ["repo", "state", "now"]); self.assertEqual(self.params(ft.in_flight), ["plan", "state"])
        self.assertEqual(self.params(ft.offer_day), ["repo", "state", "offer", "now", "out"])
        self.assertEqual(self.params(ft.compose_cli)[:2], ["args", "stdin"]); self.assertEqual(self.params(ft.prompt_lead), ["name", "text"])
        self.assertEqual(self.params(f.run_loop), ["repo", "turns"]); self.assertTrue(callable(f.verb_stop) and callable(f.alive))
        self.assertEqual(set(fc_config.FACTORY_DEFAULTS), {"day_tickets", "turn_seconds", "offer_snooze_minutes", "night_tickets", "night_until", "night_budget", "prep_ahead"})

    def test_the_roadmap_readiness_pause_and_cleanup_interfaces_exist(self):
        self.assertEqual(self.params(fc_delivery.last_reconcile), ["repo"]); self.assertEqual(self.params(fc_delivery.product_plan), ["repo"])
        self.assertEqual(self.params(fc_plan.base_built), ["state", "labels"])
        self.assertEqual(self.params(fc_readiness.story_readiness), ["repo", "story", "refresh", "write"])
        self.assertIn("effort", self.params(fc_claude.claude_json))
        self.assertEqual((self.params(fc_change.rests_on), self.params(fc_change.hold_tasks), self.params(fc_change.paused_tasks)),
                         (["proj", "refs"], ["proj", "ids", "reason"], ["proj"]))
        self.assertEqual(self.params(fc_reconcile.live_leads), ["repo"])
        out = subprocess.run([sys.executable, str(BIN / "compose.py"), "factory", "--help"], capture_output=True, text=True).stdout
        self.assertEqual([v for v in ("start", "stop", "status", "watch", "pace", "plan", "decline", "loop") if v not in out], [])


class Limits(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def make_proj(self, slug):
        (self.repo / ".compose" / slug / "results").mkdir(parents=True)
        (self.repo / ".compose" / slug / "verify.json").write_text("{}")

    def result(self, slug, tid, cost, mtime):
        path = self.repo / ".compose" / slug / "results" / f"{tid}.json"
        path.write_text(json.dumps({"cost": cost})); os.utime(path, (mtime, mtime))

    def test_until_is_the_next_occurrence_and_limits_survive_a_reload(self):
        at = time.mktime((2026, 10, 26, 23, 0, 0, 0, 0, -1))
        self.assertEqual(time.localtime(ns.until_epoch("07:00", at))[:5], (2026, 10, 27, 7, 0))
        self.assertEqual(time.localtime(ns.until_epoch("23:30", at))[:5], (2026, 10, 26, 23, 30))
        s = st.load_state(self.repo); s["mode"] = "night"; s["night"] = ns.new_night(at, ns.until_epoch("07:00", at), 20.0, None)
        st.save_state(self.repo, s)
        self.assertEqual(st.load_state(self.repo)["night"]["budget"], 20.0)
        self.assertEqual(st.load_state(self.repo)["prep"], {})

    def test_spend_counts_task_results_written_since_the_night_began_across_projects(self):
        self.make_proj("sign-in"); self.make_proj("billing")
        self.result("sign-in", "001", 0.40, mtime=50.0); self.result("sign-in", "002", 1.25, mtime=150.0); self.result("billing", "001", 0.75, mtime=200.0)
        self.assertAlmostEqual(ns.night_spend(self.repo, 100.0), 2.0)
        self.result("billing", "002", "nan", mtime=300.0); self.result("billing", "003", "inf", mtime=300.0)
        self.assertAlmostEqual(ns.night_spend(self.repo, 100.0), 2.0)  # a cost that is not a number of dollars counts 0, never disables the budget

    def test_the_night_drains_at_morning_or_at_the_budget_and_the_cap_follows(self):
        n = ns.new_night(0.0, 1000.0, 5.0, None)
        self.assertIsNone(ns.drain_reason(n, 999.0)); self.assertEqual(ns.drain_reason(n, 1000.0), "morning")
        n["spend"] = 5.0; self.assertEqual(ns.drain_reason(n, 10.0), "budget")
        s = {"mode": "night", "night": ns.new_night(0.0, 1000.0, 5.0, None), "day_tickets": None}
        self.assertEqual(st.pace(s), {"mode": "night", "cap": 6})
        for key, value in (("draining", "budget"), ("wait", {"until": 50.0}), ("ended", {"state": "morning"})):
            s["night"] = {**ns.new_night(0.0, 1000.0, 5.0, None), key: value}
            self.assertEqual(ns.night_cap(s), 0)

    def test_notes_are_appended_and_tailed_and_spawn_once_logs_to_compose(self):
        ns.add_note(self.repo, "first"); ns.add_note(self.repo, "second")
        self.assertEqual(ns.notes_tail(self.repo), "first\nsecond\n")
        self.assertEqual(ns.notes_tail(self.repo, 7), "second\n")
        self.assertEqual(ns.notes_path(self.repo).name, "night-notes.md")
        with unittest.mock.patch.object(ns, "subprocess") as sp:  # the module's own name only, so git still runs
            p = sp.Popen; p.return_value.pid = 4242
            self.assertEqual(ns.spawn_once(self.repo, ["status"], "x.log"), 4242)
        self.assertEqual(p.call_args[0][0][1:], [str(BIN / "compose.py"), "status"])
        self.assertTrue(p.call_args[1]["start_new_session"])


class OneLevel(unittest.TestCase):
    @staticmethod
    def feature(ident, state, blockers=()):
        return {"id": ident, "state": state, "stories": [], "blockers": [{"id": i, "state": st, "labels": []} for i, st in blockers]}

    @staticmethod
    def entry(ident, feature):
        return {"id": ident, "title": ident, "state": "Todo", "kind": "story", "feature": feature, "track": "t", "wave": 1, "waits_on": []}

    def chain_plan(self):
        feature = self.feature
        plan = {"features": {"NEB-5": feature("NEB-5", "In Review"), "NEB-9": feature("NEB-9", "Todo", [("NEB-5", "In Review")]),
                             "NEB-12": feature("NEB-12", "In Review", [("NEB-5", "In Review")]),
                             "NEB-20": feature("NEB-20", "Todo", [("NEB-12", "In Review")]), "NEB-30": feature("NEB-30", "Todo", [("NEB-2", "Done")])}}
        plan["features"]["NEB-20"]["stories"] = [{"id": "NEB-22", "state": "Todo"}]
        return plan

    def test_depth_counts_chains_of_verified_unmerged_blockers(self):
        plan = self.chain_plan()
        self.assertEqual([ns.unmerged_depth(plan, f) for f in ("NEB-5", "NEB-9", "NEB-20", "NEB-30")], [0, 1, 2, 0])

    def test_one_level_drops_deeper_entries_unless_in_flight_and_day_keeps_them(self):
        plan, entry = self.chain_plan(), self.entry
        entries = [entry("NEB-10", "NEB-9"), entry("NEB-21", "NEB-20"), entry("NEB-22", "NEB-20")]
        state = {"mode": "night", "dispatched": {"NEB-22": {"feature": "NEB-20"}}}
        self.assertEqual([e["id"] for e in ns.one_level(plan, entries, state)], ["NEB-10", "NEB-22"])
        self.assertEqual(ft.ORDER_FILTER["night"], ns.one_level)
        self.assertNotIn("day", ft.ORDER_FILTER)


    def test_a_verified_sibling_story_counts_as_a_level(self):
        # story 2 builds on story 1 verified and unmerged; at night story 3 on story 2 would be a second level
        story = lambda ident, state, *blockers: {"id": ident, "state": state, "blockers": [{"id": b, "state": st, "labels": []} for b, st in blockers]}
        plan = {"features": {"NEB-1": {**self.feature("NEB-1", "In Progress"), "stories": [
            story("NEB-2", "In Review"), story("NEB-3", "In Review", ("NEB-2", "In Review")), story("NEB-4", "Todo", ("NEB-3", "In Review")),
            story("NEB-5", "Todo", ("NEB-2", "In Review")), story("NEB-6", "Todo", ("NEB-2", "Done"))]},
            "NEB-9": {**self.feature("NEB-9", "Todo", [("NEB-1", "In Review")]), "stories": [story("NEB-10", "Todo", ("NEB-11", "In Review")), story("NEB-11", "In Review")]}}}
        entries = [self.entry(i, "NEB-1") for i in ("NEB-4", "NEB-5", "NEB-6")] + [self.entry("NEB-10", "NEB-9")]
        self.assertEqual([ns.unmerged_stories(plan, e) for e in entries], [2, 1, 0, 1])
        self.assertEqual([e["id"] for e in ns.one_level(plan, entries, {"mode": "night", "dispatched": {}})], ["NEB-5", "NEB-6"])

class UsageWaits(unittest.TestCase):
    def setUp(self):
        self.panes, self.prompted = {}, []
        self.read = lambda name: self.panes.get(name, "")
        self.prompt = lambda name, text: self.prompted.append((name, text)) or True
        self.repo = Path(tempfile.mkdtemp()).resolve()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def test_reset_times_parse_to_the_next_occurrence_plus_a_minute(self):
        at = time.mktime((2026, 10, 27, 1, 10, 0, 0, 0, -1))
        self.assertEqual(time.localtime(ns.reset_at("5-hour limit reached \u2219 resets 3am", at))[3:5], (3, 1))
        self.assertEqual(time.localtime(ns.reset_at("usage limit reached, try again at 1:30 PM", at))[3:5], (13, 31))
        self.assertEqual(ns.reset_at("rate limited, resets in 2h 15m", at), at + 2 * 3600 + 15 * 60 + 60)
        self.assertIsNone(ns.reset_at("usage limit reached", at))
        self.assertEqual([ns.fallback_wait(n) for n in (1, 2, 3, 6, 9)], [60, 120, 240, 1800, 1800])

    def test_a_limited_lead_starts_a_wait_and_the_reset_prompts_it_to_continue(self):
        night = ns.new_night(0.0, 10**10, 20.0, None)
        self.panes["sign-in-orchestrator"] = "...\nYou've hit your usage limit \u00b7 resets in 30m\n"
        lines = ns.usage_step(night, {"sign-in": "sign-in-orchestrator"}, 1000.0, self.read, self.prompt)
        self.assertEqual(night["wait"]["until"], 1000.0 + 1800 + 60)
        self.assertTrue(lines[0].startswith("event factory=usage-wait until="))
        self.assertEqual(ns.usage_step(night, {"sign-in": "sign-in-orchestrator"}, 2000.0, self.read, self.prompt), [])
        self.panes["sign-in-orchestrator"] = "done thinking\n"
        lines = ns.usage_step(night, {"sign-in": "sign-in-orchestrator"}, 2861.0, self.read, self.prompt)
        self.assertEqual(self.prompted, [("sign-in-orchestrator", ns.CONTINUE)]); self.assertIsNone(night["wait"])
        self.assertEqual((night["waits"], night["waited"]), (1, 1861.0))
        self.assertTrue(lines[0].startswith("event factory=usage-resumed agents=sign-in-orchestrator"))

    def test_no_reset_time_escalates_and_a_billing_line_ends_the_night(self):
        night = ns.new_night(0.0, 10**10, 20.0, None)
        self.panes["a-orchestrator"] = "usage limit reached\n"
        ns.usage_step(night, {"a": "a-orchestrator"}, 0.0, self.read, self.prompt); self.assertEqual(night["wait"]["until"], 60.0)
        self.panes["a-orchestrator"] = "usage limit reached again\n"
        ns.usage_step(night, {"a": "a-orchestrator"}, 61.0, self.read, self.prompt); self.assertEqual(night["wait"]["until"], 181.0)
        self.panes["a-orchestrator"] = "Error: 402 Payment Required - credit balance too low\n"; night["wait"] = None
        ns.usage_step(night, {"a": "a-orchestrator"}, 300.0, self.read, self.prompt); self.assertIsNone(night["ended"])
        lines = ns.usage_step(night, {"a": "a-orchestrator"}, 360.0, self.read, self.prompt)
        self.assertEqual(night["ended"]["state"], "no-credit"); self.assertIn("factory=night-ended state=no-credit", lines[0])

    def test_a_new_limit_at_a_due_wait_keeps_the_leads_already_waiting(self):
        night, leads = ns.new_night(0.0, 10**10, 20.0, None), {"a": "a-orchestrator", "b": "b-orchestrator"}
        self.panes["a-orchestrator"] = "usage limit reached\n"
        ns.usage_step(night, leads, 0.0, self.read, self.prompt)
        self.panes["b-orchestrator"] = "usage limit reached · resets in 10m\n"
        ns.usage_step(night, leads, 61.0, self.read, self.prompt)
        self.assertEqual(night["wait"]["agents"], ["a-orchestrator", "b-orchestrator"]); self.assertEqual(self.prompted, [])
        ns.usage_step(night, leads, 61.0 + 660, self.read, self.prompt)
        self.assertEqual(sorted(n for n, _ in self.prompted), ["a-orchestrator", "b-orchestrator"]); self.assertIsNone(night["wait"])

    def test_a_later_resume_keeps_an_earlier_lead_s_stale_banner_ignored(self):
        night, leads = ns.new_night(0.0, 10**10, 20.0, None), {"a": "a-orchestrator", "b": "b-orchestrator"}
        self.panes["a-orchestrator"] = "usage limit reached\n"
        ns.usage_step(night, leads, 0.0, self.read, self.prompt); ns.usage_step(night, leads, 61.0, self.read, self.prompt)
        self.panes["b-orchestrator"] = "usage limit reached for b\n"
        ns.usage_step(night, leads, 100.0, self.read, self.prompt); ns.usage_step(night, leads, 161.0, self.read, self.prompt)
        self.assertEqual(ns.usage_step(night, leads, 200.0, self.read, self.prompt), []); self.assertIsNone(night["wait"])

    def test_a_payment_halt_since_the_night_began_is_no_credit(self):
        proj = self.repo / ".compose" / "sign-in"
        (proj / "results").mkdir(parents=True); (proj / "verify.json").write_text("{}")
        path = proj / "results" / "resume.json"
        path.write_text(json.dumps({"provider": "openrouter"})); os.utime(path, (100.0, 100.0))
        self.assertEqual(ns.no_credit(self.repo, 0.0), "sign-in: payment halt (openrouter)")
        self.assertIsNone(ns.no_credit(self.repo, 200.0))

    PAY = "Error: 402 Payment Required - credit balance is too low"

    def look(self, night, now, name="a-orchestrator"):
        return ns.usage_step(night, {"a": name}, now, self.read, self.prompt)

    def test_ordinary_pane_text_never_ends_the_night(self):
        for text in ("src/billing/checkout.ts | 12 +++", "\u2713 returns 402 when the card is declined", "+  // top up the wallet",
                     "expect(res.status).toBe(402)", "+  // error: 402 credit balance is too low", "- Error: 402 insufficient credits"):
            night = ns.new_night(0.0, 10**10, 20.0, None)
            self.panes["a-orchestrator"] = "usage limit reached\n" + text + "\n"
            self.look(night, 0.0); self.look(night, 500.0); self.look(night, 1000.0)
            self.assertIsNone(night["ended"], text)

    def test_a_real_billing_banner_must_be_seen_twice_a_minute_apart(self):
        night = ns.new_night(0.0, 10**10, 20.0, None)
        self.panes["a-orchestrator"] = self.PAY + "\n"
        self.look(night, 0.0); self.assertIsNone(night["ended"]); self.assertIsNotNone(night["wait"])
        self.look(night, 60.0); self.assertEqual(night["ended"]["state"], "no-credit")
        night = ns.new_night(0.0, 10**10, 20.0, None)
        self.look(night, 0.0); self.panes["a-orchestrator"] = "usage limit reached, resets in 5m\n"; self.look(night, 60.0)
        self.assertEqual(night["pay_seen"], {}); self.panes["a-orchestrator"] = self.PAY + "\n"; self.look(night, 400.0)
        self.assertIsNone(night["ended"])

    def test_a_stale_banner_at_the_due_time_is_prompted_not_waited_again(self):
        night = ns.new_night(0.0, 10**10, 20.0, None)
        self.panes["a-orchestrator"] = "usage limit reached, resets in 30m\n"
        self.look(night, 0.0)
        lines = self.look(night, 1861.0)
        self.assertEqual(self.prompted, [("a-orchestrator", ns.CONTINUE)]); self.assertIsNone(night["wait"])
        self.assertTrue(lines[0].startswith("event factory=usage-resumed"))
        self.assertEqual(self.look(night, 1900.0), []); self.assertIsNone(night["wait"])
        self.panes["a-orchestrator"] = "usage limit reached, resets in 2h\n"
        self.look(night, 2000.0); self.assertEqual(night["wait"]["until"], 2000.0 + 7200 + 60)

    def test_bare_clocks_in_timestamps_and_far_resets_do_not_count(self):
        at = time.mktime((2026, 10, 27, 1, 10, 0, 0, 0, -1))
        self.assertIsNone(ns.reset_at("[01:09:58] Retrying in 4s ... 429", at)); self.assertIsNone(ns.reset_at("limit, in 3 hello", at))
        self.assertIsNotNone(ns.reset_at("limit reached, resets 3am", at))
        night = ns.new_night(at, 10**11, 20.0, None)
        self.panes["a-orchestrator"] = "usage limit reached, resets 11am\n"
        self.look(night, at); self.assertEqual(night["wait"]["until"], at + 60)

    def test_an_empty_read_or_a_failed_prompt_extends_the_wait_and_counts_nothing(self):
        night = ns.new_night(0.0, 10**10, 20.0, None)
        self.panes["a-orchestrator"] = "usage limit reached\n"; self.look(night, 0.0)
        self.panes["a-orchestrator"] = ""; self.look(night, 61.0)
        self.assertEqual((night["wait"]["until"], self.prompted, night["waits"]), (181.0, [], 0))
        self.panes["a-orchestrator"] = "idle\n"; self.prompt = lambda name, text: False
        self.look(night, 182.0); self.assertEqual((night["wait"]["until"], night["waits"]), (182.0 + 240, 0))
        self.look(night, 500.0); self.assertEqual(night["waits"], 0)
        self.prompt = lambda name, text: self.prompted.append((name, text)) or True
        self.look(night, 1000.0); self.assertEqual((night["waits"], night["wait"]), (1, None))
        night["wait"] = {"until": 0.0, "n": 0, "since": 0.0, "agents": ["gone"], "lines": {}}; self.prompted.clear()
        self.look(night, 5.0); self.assertEqual((self.prompted, night["wait"]), ([], None))

    def test_a_resume_json_that_is_not_an_object_is_reported_as_unknown(self):
        proj = self.repo / ".compose" / "x"
        (proj / "results").mkdir(parents=True); (proj / "verify.json").write_text("{}")
        (proj / "results" / "resume.json").write_text("[1]")
        self.assertEqual(ns.no_credit(self.repo, 0.0), "x: payment halt (unknown)")


class Answerer(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.proj = self.repo / ".compose" / "sign-in"
        (self.proj / "results" / "crew").mkdir(parents=True)
        (self.proj / "verify.json").write_text("{}")
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-9"}))
        (self.proj / "results" / "yolo.json").write_text(json.dumps({"mode": "night"}))
        self.state = {"night": ns.new_night(fc_core.report_time("2026-10-26 22:00:00"), 0.0, 10.0, None)}
        st.save_state(self.repo, {"mode": "night", "night": self.state["night"]})  # the worker checks the night is still on before it writes
        self.gone, self.events, self.pids = set(), [], iter(range(5000, 5100))
        def cli(args, stdin=None, pane=None):
            return json.dumps({"identifier": args[2], "title": "t", "description": "d"}) if args[:2] == ["linear", "get"] else ""
        patches = [unittest.mock.patch.object(fa.fc_nightstate, "spawn_once", side_effect=lambda *a: next(self.pids)),
                   unittest.mock.patch.object(fa.fc_claude, "claude_once"),
                   unittest.mock.patch.object(fa.fc_factoryturn, "compose_cli", side_effect=cli),
                   unittest.mock.patch.object(fa.fc_factoryturn, "prompt_lead", return_value=True),
                   unittest.mock.patch.object(fa.fc_factorystate, "add_event", side_effect=lambda repo, line: self.events.append(line)),
                   unittest.mock.patch.object(fa.fc_delivery, "product_plan", return_value={"doc": {"content": "PLAN"}}),
                   unittest.mock.patch.object(fa.os, "kill", side_effect=self.kill)]
        self.spawn, self.claude, self.cli, self.prompt = (p.start() for p in patches[:4])
        for p in patches[4:]:
            p.start()
        self.addCleanup(unittest.mock.patch.stopall)

    def kill(self, pid, sig):
        if pid in self.gone:
            raise ProcessLookupError

    def dead(self, pid):
        self.gone.add(pid)

    def report(self, state, message, at):
        with open(self.proj / "results" / "crew" / "sign-in-orchestrator.status", "a") as f:
            f.write(json.dumps({"at": at, "state": state, "message": message, "why": ""}) + "\n")

    def test_planning_questions_wait_for_the_morning_and_the_rest_get_one_answerer_each(self):
        self.report("needs-user", "shape NEB-12: no design yet", "2026-10-27 01:00:00")
        self.report("needs-user", "NEB-10: 2 questions Q1,Q2; recommendation Q1 Clerk, Q2 SQLite", "2026-10-27 01:05:00")
        lines = fa.answer_step(self.repo, self.state, 100.0)
        self.assertEqual(lines, ['event factory=for-morning project=sign-in question="shape NEB-12: no design yet"  '
                                 "(planning waits for the product owner: it is listed under Ready to plan)"])
        self.spawn.assert_called_once_with(self.repo, ["factory", "answer", "--repo", str(self.repo), "--project", "sign-in",
                                                       "--at", "2026-10-27 01:05:00", "--nth", "0"], "night-answer.log")
        self.assertEqual(fa.answer_step(self.repo, self.state, 160.0), [])  # neither is asked twice

    def test_at_most_two_answerers_run_and_a_dead_one_without_a_record_is_said_once(self):
        for i in range(3):
            self.report("needs-user", f"NEB-1{i}: question {i}", f"2026-10-27 01:0{i}:00")
        fa.answer_step(self.repo, self.state, 100.0)
        self.assertEqual(self.spawn.call_count, 2)
        self.dead(self.state["night"]["asked"]["sign-in@2026-10-27 01:00:00"]["pid"])
        lines = fa.answer_step(self.repo, self.state, 200.0)
        self.assertIn('event factory=answer-failed project=sign-in question="NEB-10: question 0"  (the answerer ended without an answer: it waits for the product owner)', lines)
        self.assertEqual(self.spawn.call_count, 3)
        self.assertNotIn("answer-failed", "".join(fa.answer_step(self.repo, self.state, 260.0)))

    def test_the_worker_records_question_answers_through_decide_and_the_rest_through_the_team_lead(self):
        self.report("needs-user", "NEB-10: 1 question Q1; recommendation Clerk", "2026-10-27 01:05:00")
        self.claude.return_value = (None, '{"never_set": false, "answer": "Q1 Clerk", "reasoning": "D2 says one store", "answers": {"Q1": "Sign in with Clerk"}}', 3, {})
        fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")
        self.assertEqual(self.cli.call_args_list[-1].args[0], ["decide", "--repo", str(self.repo), "--project", "sign-in", "--new", "Sign in with Clerk", "--closes", "Q1", "--issue", "NEB-10"])
        self.assertIn('"answers": {"<question id>": "<the decision, as a sentence>"}', fa.ASK)
        self.assertNotIn("card", fa.ASK)
        comment = next(c for c in self.cli.call_args_list if c.args[0][:2] == ["linear", "comment"])
        self.assertEqual(comment.args[0][2], "NEB-10"); self.assertIn("Answered while you were away", comment.kwargs["stdin"])
        self.prompt.assert_not_called()
        self.assertEqual(json.loads(fa.answers_path(self.repo).read_text().splitlines()[-1])["answer"], "Q1 Clerk")
        self.assertEqual(self.claude.call_args.kwargs["tools"], "Read,Grep,Glob"); self.assertEqual(self.claude.call_args.kwargs["effort"], "high")
        self.claude.return_value = (None, '{"never_set": false, "answer": "keep 30 day sessions", "reasoning": "r", "answers": {}}', 3, {})
        fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")
        self.prompt.assert_called_once_with("sign-in-orchestrator", "night: keep 30 day sessions")

    def test_a_never_set_question_is_never_answered(self):
        self.report("needs-user", "NEB-10: force-push the rebased branch?", "2026-10-27 01:05:00")
        self.claude.return_value = (None, '{"never_set": true, "why_wait": "force-push", "answer": "", "reasoning": "", "answers": {}}', 1, {})
        rec = fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")
        self.assertTrue(rec["never_set"]); self.prompt.assert_not_called()
        self.assertFalse(any(c.args[0][0] == "decide" for c in self.cli.call_args_list))
        self.assertTrue(self.events[-1].startswith('event factory=waiting project=sign-in question="NEB-10: force-push the rebased branch?"'))

    def test_the_worker_writes_nothing_once_its_night_has_ended_or_another_began(self):
        self.report("needs-user", "NEB-10: keep sessions?", "2026-10-27 01:05:00")
        for change in (lambda s: s.update(mode="day", night={**s["night"], "ended": {"state": "back", "at": 1.0}}),
                       lambda s: s.update(night=ns.new_night(s["night"]["since"] + 60, 0.0, 10.0, None))):
            st.save_state(self.repo, {"mode": "night", "night": self.state["night"]})
            def reply(*args, change=change, **kw):
                with st.edit(self.repo) as s:
                    change(s)
                return (None, '{"never_set": false, "answer": "30 days", "reasoning": "r", "answers": {"Q1": "Clerk"}}', 3, {})
            self.claude.side_effect = reply
            rec = fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")
            self.assertEqual(rec["error"], "the night ended before the answer")
            self.assertEqual([c for c in self.cli.call_args_list if c.args[0][0] != "linear" or c.args[0][1] != "get"], [])
            self.prompt.assert_not_called()
            self.assertTrue(self.events[-1].startswith('event factory=answer-failed project=sign-in question="NEB-10: keep sessions?" why="the night ended before the answer"'), self.events[-1])

    def test_no_answerer_starts_during_a_usage_wait_and_the_question_waits_for_the_reset(self):
        self.report("needs-user", "NEB-10: q", "2026-10-27 01:05:00")
        self.state["night"]["wait"] = {"until": 500.0, "n": 0, "since": 90.0, "agents": [], "lines": {}}
        self.assertEqual(fa.answer_step(self.repo, self.state, 100.0), [])
        self.spawn.assert_not_called(); self.assertEqual(self.state["night"]["asked"], {})
        self.state["night"]["wait"] = None
        fa.answer_step(self.repo, self.state, 600.0)
        self.assertEqual(self.spawn.call_args.args[1][-3:], ["2026-10-27 01:05:00", "--nth", "0"])

    def test_an_unparsable_reply_records_the_error_and_says_answer_failed(self):
        self.report("needs-user", "NEB-10: q", "2026-10-27 01:05:00")
        self.claude.return_value = (None, "no json here", 1, {})
        rec = fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")
        self.assertTrue(rec["error"]); self.assertTrue(self.events[-1].startswith("event factory=answer-failed project=sign-in"))
        self.prompt.assert_not_called()

    def reply(self, **kw):
        self.claude.return_value = (None, json.dumps({"never_set": False, "answer": "a", "reasoning": "r", "answers": {}, **kw}), 1, {})

    def test_any_never_set_object_in_the_reply_wins_over_an_example(self):
        self.report("needs-user", "NEB-10: q", "2026-10-27 01:05:00")
        self.claude.return_value = (None, 'Example: {"answer": "yes", "never_set": false} but really {"never_set": true, "answer": "", "why_wait": "x"}', 1, {})
        self.assertTrue(fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")["never_set"])
        self.prompt.assert_not_called()
        self.claude.return_value = (None, '{"answer": "yes"}', 1, {})
        self.assertTrue(fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")["error"])

    def test_a_failed_decide_after_the_comment_is_recorded_as_partial(self):
        self.report("needs-user", "NEB-10: Q1 here", "2026-10-27 01:05:00")
        self.reply(answers={"Q1": "Clerk"})
        self.cli.side_effect = lambda args, stdin=None, pane=None: (_ for _ in ()).throw(RuntimeError("boom")) if args[0] == "decide" else ""
        rec = fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")
        self.assertEqual((rec["commented"], rec["acted"]), (True, False))
        self.assertIn("answer-failed", self.events[-1]); self.assertIn("boom", self.events[-1])
        self.assertEqual(len(fa.answers_path(self.repo).read_text().splitlines()), 1)

    def test_a_lead_that_did_not_take_the_prompt_is_not_answered(self):
        self.report("needs-user", "NEB-10: q", "2026-10-27 01:05:00")
        self.reply(); self.prompt.return_value = False
        self.assertTrue(fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")["error"])
        self.assertIn("answer-failed", self.events[-1])

    def test_only_the_teams_issue_ids_count(self):
        self.report("needs-user", "should we use SHA-256 for NEB-10 tokens?", "2026-10-27 01:00:00")
        self.report("needs-user", "use UTF-8?", "2026-10-27 01:01:00")
        self.assertEqual([q["issue"] for q in fa.new_questions(self.repo, self.state["night"])], ["NEB-10", "NEB-9"])

    def test_two_reports_in_one_second_are_two_questions_and_a_repeat_is_not_asked_again(self):
        self.report("needs-user", "NEB-10: one", "2026-10-27 01:00:00")
        self.report("needs-user", "NEB-10: two", "2026-10-27 01:00:00")
        self.report("needs-user", "NEB-10: one", "2026-10-27 01:02:00")
        fa.answer_step(self.repo, self.state, 100.0)
        self.assertEqual(sorted(self.state["night"]["asked"]), ["sign-in@2026-10-27 01:00:00", "sign-in@2026-10-27 01:00:00#1"])
        self.assertEqual(self.spawn.call_args.args[1][-2:], ["--nth", "1"])
        self.reply(); self.assertEqual(fa.run_answer(self.repo, "sign-in", "2026-10-27 01:00:00", 1)["question"], "NEB-10: two")

    def test_never_words_in_the_question_or_the_answer_force_never_set(self):
        self.report("needs-user", "NEB-10: ok to force-push?", "2026-10-27 01:05:00")
        self.reply(answer="yes")
        rec = fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")
        self.assertTrue(rec["never_set"]); self.assertIn("force-push", rec["why_wait"]); self.prompt.assert_not_called()
        self.assertFalse(any(c.args[0][:2] == ["linear", "comment"] for c in self.cli.call_args_list))

    def test_answered_ids_must_be_questions_of_the_report(self):
        self.report("needs-user", "NEB-10: 1 question Q1", "2026-10-27 01:05:00")
        self.reply(answer="", answers={"D2": "x", "Q9": "B"})
        self.assertTrue(fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")["error"])
        self.reply(answers={"D2": "x", "Q1": "Clerk"})
        self.assertEqual(fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")["answers"], {"Q1": "Clerk"})

    def test_each_answer_is_its_own_decide_and_blank_answers_are_dropped(self):
        self.report("needs-user", "NEB-10: 2 questions Q1, Q2; recommendation Clerk, SQLite", "2026-10-27 01:05:00")
        self.reply(answers={"Q1": "Sign in with Clerk", "Q2": "  "})
        fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")
        decides = [c.args[0] for c in self.cli.call_args_list if c.args[0][0] == "decide"]
        self.assertEqual(decides, [["decide", "--repo", str(self.repo), "--project", "sign-in", "--new", "Sign in with Clerk", "--closes", "Q1", "--issue", "NEB-10"]])

    def test_a_torn_status_file_does_not_stop_the_step(self):
        (self.proj / "results" / "crew" / "sign-in-orchestrator.status").write_text("{torn\n")
        self.assertEqual(fa.new_questions(self.repo, self.state["night"]), [])

    def test_d_ids_are_never_questions(self):
        self.report("needs-user", "NEB-10: should D2 change? questions Q1", "2026-10-27 01:05:00")
        self.reply(answers={"D2": "x", "Q1": "Clerk"})
        fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")
        decides = [c.args[0] for c in self.cli.call_args_list if c.args[0][0] == "decide"]
        self.assertEqual(decides, [["decide", "--repo", str(self.repo), "--project", "sign-in", "--new", "Clerk", "--closes", "Q1", "--issue", "NEB-10"]])

    def test_in_flight_repeats_are_silent_and_answered_repeats_go_to_the_morning_once(self):
        self.report("needs-user", "NEB-10: same", "2026-10-27 01:00:00")
        fa.answer_step(self.repo, self.state, 100.0)
        self.report("needs-user", "NEB-10: same", "2026-10-27 01:02:00")
        self.assertEqual(fa.answer_step(self.repo, self.state, 120.0), []); self.assertEqual(self.spawn.call_count, 1)
        self.reply(); fa.run_answer(self.repo, "sign-in", "2026-10-27 01:00:00")
        self.dead(self.state["night"]["asked"]["sign-in@2026-10-27 01:00:00"]["pid"])
        want = ['event factory=for-morning project=sign-in question="NEB-10: same"  (asked again after the night answer: it waits for the product owner)']
        self.assertEqual(fa.answer_step(self.repo, self.state, 200.0), want)
        self.assertEqual(fa.answer_step(self.repo, self.state, 260.0), []); self.assertEqual(self.spawn.call_count, 1)

    def test_the_guard_is_about_actions_not_topics(self):
        for text in ("should the auth provider return a client secret field?", "which OAuth provider first?"):
            self.assertEqual(fa.never_hits(text), [], text)
        for text in ("should I force-push the rebased branch?", "add a new API key for Stripe?", "switch the model to opus?"):
            self.assertTrue(fa.never_hits(text), text)

    def test_the_guard_reads_answer_values_and_reasoning(self):
        self.report("needs-user", "NEB-10: 1 question Q1", "2026-10-27 01:05:00")
        self.reply(answer="", answers={"Q1": "purchase the Pro plan"})
        self.assertTrue(fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")["never_set"])
        self.assertFalse(any(c.args[0][0] == "decide" for c in self.cli.call_args_list))
        self.reply(reasoning="we must wipe the database first")
        self.assertTrue(fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")["never_set"])

    def test_a_timeout_is_a_recorded_failure(self):
        self.report("needs-user", "NEB-10: q", "2026-10-27 01:05:00")
        self.claude.side_effect = subprocess.TimeoutExpired("claude", 1800)
        self.assertTrue(fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")["error"])
        self.assertEqual(len(fa.answers_path(self.repo).read_text().splitlines()), 1)
        self.assertIn("answer-failed", self.events[-1])

    def test_a_recorded_key_is_not_asked_again_after_lost_state(self):
        self.report("needs-user", "NEB-10: q", "2026-10-27 01:05:00")
        self.reply(); fa.run_answer(self.repo, "sign-in", "2026-10-27 01:05:00")
        self.assertEqual(fa.answer_step(self.repo, self.state, 100.0), []); self.spawn.assert_not_called()

    def test_reports_before_the_night_are_not_questions(self):
        self.report("needs-user", "NEB-10: old", "2026-10-26 20:00:00")
        self.assertEqual(fa.new_questions(self.repo, self.state["night"]), [])

    def test_claude_once_passes_effort_and_keeps_the_tail_it_is_asked_for(self):
        unittest.mock.patch.stopall()
        run = unittest.mock.Mock(return_value=unittest.mock.Mock(stdout=json.dumps({"result": "x" * 5000}), returncode=0))
        with unittest.mock.patch.object(fc_proc, "run", run):
            reply = fc_claude.claude_once(self.repo, "p", effort="high", keep=4000)[1]
            self.assertEqual(len(reply), 4000)
            self.assertEqual(run.call_args.args[0][run.call_args.args[0].index("--effort") + 1], "high")
            self.assertEqual(len(fc_claude.claude_once(self.repo, "p")[1]), 1500)
            self.assertNotIn("--effort", run.call_args.args[0])

def entry(ident, feature, kind="story", state="Todo"):
    return {"id": ident, "title": ident, "state": state, "kind": kind, "feature": feature, "track": "t", "wave": 1, "waits_on": []}

def rd(ident, ready=True, specced=True, error=None):
    return {"id": ident, "ready": ready, "specced": specced, "has_tasks": True, "missing": [], "error": error, "needs_shaping": False}


class Prep(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.state = {"night": ns.new_night(100.0, 1e12, 10.0, None), "prep": {}}
        st.save_state(self.repo, {"mode": "night", "night": self.state["night"]})
        self.out, self.events, self.gone, self.pids = [], [], set(), iter(range(7000, 7100))
        self.entries = [entry("NEB-11", "NEB-9"), entry("NEB-12", "NEB-12", kind="feature")]
        self.results = {"NEB-11": rd("NEB-11", ready=False, specced=False)}
        self.titles = {"NEB-11": "Sign in", "NEB-12": "Billing"}
        self.descriptions = {"NEB-11": "d11", "NEB-12": "d12"}
        self.states = {}
        self.docs, self.cli_calls, self.race = {}, [], None
        self.seed, self.tprint = "PRD seed for Shop\n## Open tickets\n- NEB-11 (Todo) Sign in\n", "t1"
        def readiness(repo, ident, refresh=False, write=True):
            assert write is False, "prep must not write to the ticket"
            r = self.results[ident]
            if r.get("error") == "boom":
                raise RuntimeError("boom")
            return r
        patches = [unittest.mock.patch.object(fp.fc_factoryturn, "compose_cli", side_effect=self.cli),
                   unittest.mock.patch.object(fp.fc_claude, "claude_once"),
                   unittest.mock.patch.object(fp.fc_nightstate, "spawn_once", side_effect=lambda *a: next(self.pids)),
                   unittest.mock.patch.object(fp.fc_delivery, "product_plan", return_value={"doc": {"content": "PLAN"}, "tracks": [], "features": {}}),
                   unittest.mock.patch.object(fp.fc_plan, "order_entries", side_effect=lambda tracks, features: self.entries),
                   unittest.mock.patch.object(fp.fc_readiness, "story_readiness", side_effect=readiness),
                   unittest.mock.patch.object(fp.fc_factorystate, "add_event", side_effect=lambda repo, line: self.events.append(line)),
                   unittest.mock.patch.object(fp.os, "kill", side_effect=lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()) if pid in self.gone else None),
                   unittest.mock.patch.object(fp.fc_prd, "ticket_print", side_effect=lambda repo: self.tprint)]
        self.claude, self.spawn = (p.start() for p in patches[1:3])
        patches[0].start()
        for p in patches[3:]:
            p.start()
        self.addCleanup(unittest.mock.patch.stopall)

    def cli(self, args, stdin=None, pane=None):
        self.cli_calls.append(list(args))
        if args[:2] == ["prd", "seed"]:
            return self.seed
        if args[:2] == ["linear", "get"]:
            i = args[2]
            return json.dumps({"identifier": i, "title": self.titles.get(i, i), "description": self.descriptions.get(i, ""),
                               "state": {"name": self.states.get(i, "Todo")}, "labels": {"nodes": []}})
        if args[:3] == ["linear", "doc", "list"]:
            if "--json" in args:
                return json.dumps([{"id": f"doc-{n}", "updatedAt": "2026-10-27T01:00:00.000Z", "title": t, "url": "https://linear.app/doc/prep"} for n, t in enumerate(self.docs)])
            return "".join(f"doc-{n} 2026-10-27 {t}\n" for n, t in enumerate(self.docs))
        if args[:3] == ["linear", "doc", "create"]:
            self.docs[args[args.index("--title") + 1]] = stdin or ""
            return "https://linear.app/doc/prep\n"
        title = lambda: list(self.docs)[int(args[3].split("-")[1])]
        if args[:3] == ["linear", "doc", "get"]:
            return title() + "\n" + self.docs[title()] + "\n--- section hashes (--section \"<heading>\" --expect <hash>)\nabc ## x\n"
        if args[:3] == ["linear", "doc", "update"]:
            self.docs[title()] = stdin
            if self.race:
                self.docs[title()] = self.race(stdin)
                self.race = None
            return "updated https://linear.app/doc/prep\n"
        raise AssertionError(f"unexpected compose call {args}")

    def print_of(self, ident):
        return fp.prep_print(f"{self.titles[ident]}\n{self.descriptions[ident]}", "")

    def finish_worker(self, ident):
        self.gone.add(self.state["prep"][ident]["pid"])

    def test_candidates_start_at_the_offer_and_take_only_what_needs_planning(self):
        self.entries = [entry("NEB-10", "NEB-9"), entry("NEB-11", "NEB-9"), entry("NEB-12", "NEB-12", kind="feature"), entry("NEB-13", "NEB-9")]
        self.results = {"NEB-11": rd("NEB-11", ready=False, specced=False), "NEB-13": rd("NEB-13", ready=True)}
        self.assertEqual([(c["id"], c["layer"]) for c in fp.candidates(self.repo, "NEB-11", 3)], [("NEB-11", "story"), ("NEB-12", "stories")])

    def test_a_judge_error_skips_the_ticket(self):
        self.results["NEB-11"] = rd("NEB-11", error="boom")
        self.assertEqual([c["id"] for c in fp.candidates(self.repo, "NEB-11", 3)], ["NEB-12"])

    def test_idle_starts_one_prep_at_a_time_and_skips_what_is_prepared_and_unchanged(self):
        fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 100.0, self.out)
        self.spawn.assert_called_once_with(self.repo, ["factory", "prep", "--repo", str(self.repo), "--ticket", "NEB-11"], "night-prep.log")
        fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 160.0, self.out); self.assertEqual(self.spawn.call_count, 1)  # the worker still runs
        self.finish_worker("NEB-11"); fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 220.0, self.out)
        self.assertEqual(self.spawn.call_args.args[1][-1], "NEB-12")

    def test_idle_waits_when_the_night_drains_for_the_morning(self):
        self.state["night"]["draining"] = "morning"
        fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 100.0, self.out); self.spawn.assert_not_called()

    def test_the_worker_writes_one_section_and_never_touches_the_ticket(self):
        self.claude.return_value = (None, json.dumps({"code": ["bin/billing.py"], "built": ["checkout"], "open": ["refunds"],
                                    "questions": [{"question": "Refunds in v1?", "recommendation": "No", "why": "D4 defers them"}], "drop": False}), 4, {})
        fp.run_prep(self.repo, "NEB-12")
        doc = self.docs[fp.PREP_TITLE]
        self.assertIn("## NEB-12: Billing\n", doc); self.assertIn("1. Refunds in v1?\n   Recommendation (a guess): No - D4 defers them\n", doc)
        self.assertEqual([c for c in self.cli_calls if c[:2] in (["linear", "update"], ["linear", "create"], ["linear", "labels"])], [])
        self.assertIn("never plan", self.claude.call_args.args[1])
        self.assertIn("event factory=prepared ticket=NEB-12 questions=1", self.events[-1])

    def test_the_worker_writes_nothing_once_the_night_has_ended(self):
        def reply(*args, **kw):
            with st.edit(self.repo) as s:
                s["mode"], s["night"]["ended"] = "day", {"state": "back", "at": 1.0}
            return (None, json.dumps({"questions": [{"question": "Refunds in v1?", "recommendation": "No", "why": "D4"}], "drop": False}), 4, {})
        self.claude.side_effect = reply
        self.assertEqual(fp.run_prep(self.repo, "NEB-12"), {"ticket": "NEB-12", "status": "cut"})
        self.assertEqual([c for c in self.cli_calls if c[:3] in (["linear", "doc", "create"], ["linear", "doc", "update"])], [])
        self.assertEqual(json.loads(fp.outcomes_path(self.repo).read_text().splitlines()[-1])["status"], "cut")
        self.assertEqual(self.events, ['event factory=prep-failed ticket=NEB-12 why="the night ended before the prep"'])

    def test_a_reply_that_will_not_parse_records_nothing(self):
        self.claude.return_value = (None, 'no json "here"', 4, {})
        fp.run_prep(self.repo, "NEB-12")
        self.assertEqual(self.docs, {}); self.assertEqual(len(self.events), 1)
        self.assertTrue(self.events[0].startswith("event factory=prep-failed ticket=NEB-12 why=\"no json 'here'"))

    def test_drop_removes_the_old_section_whole(self):
        self.docs[fp.PREP_TITLE] = "## NEB-12: Billing\n\nx\n\n## NEB-11: Sign in\n\ny\n"
        self.claude.return_value = (None, json.dumps({"code": [], "built": [], "open": [], "questions": [], "drop": True}), 4, {})
        fp.run_prep(self.repo, "NEB-12")
        self.assertNotIn("NEB-12", self.docs[fp.PREP_TITLE]); self.assertIn("## NEB-11: Sign in", self.docs[fp.PREP_TITLE])
        self.assertIn("event factory=prep-dropped ticket=NEB-12", self.events[-1])
        self.assertIn("x", self.claude.call_args.args[1])  # the old section went into the prompt

    def test_refresh_drops_sections_of_tickets_now_ready_and_requeues_changed_ones(self):
        self.state["prep"] = {"NEB-11": {"print": "old", "at": 1.0, "pid": None}, "NEB-12": {"print": self.print_of("NEB-12"), "at": 1.0, "pid": None}}
        self.docs[fp.PREP_TITLE] = "## NEB-11: Sign in\n\nx\n\n## NEB-12: Billing\n\ny\n"
        self.results["NEB-11"] = rd("NEB-11", ready=True)
        fp.refresh(self.repo, self.state, ["event factory=verified story=NEB-10 project=sign-in"], 100.0, self.out)
        self.assertNotIn("NEB-11", self.docs[fp.PREP_TITLE]); self.assertNotIn("NEB-11", self.state["prep"])
        self.descriptions["NEB-12"] = "changed"; fp.refresh(self.repo, self.state, [], 100.0 + fp.REFRESH_SECONDS, self.out)
        self.assertEqual(self.state["prep"]["NEB-12"]["print"], None)  # queued: the next idle turn prepares it again

    def test_refresh_drops_a_ticket_that_is_in_progress(self):
        self.state["prep"] = {"NEB-12": {"print": self.print_of("NEB-12"), "at": 1.0, "pid": None}}
        self.docs[fp.PREP_TITLE] = "## NEB-12: Billing\n\ny\n"; self.states["NEB-12"] = "In Progress"
        fp.refresh(self.repo, self.state, ["event factory=dispatched story=NEB-12"], 100.0, self.out)
        self.assertEqual(self.state["prep"], {}); self.assertNotIn("NEB-12", self.docs[fp.PREP_TITLE])

    def test_refresh_drops_sections_of_ids_that_state_no_longer_holds(self):
        self.state["prep"] = {"NEB-12": {"print": self.print_of("NEB-12"), "at": 1.0, "pid": None}}
        self.docs[fp.PREP_TITLE] = "intro\n\n## NEB-11: Sign in\n\nx\n\n## NEB-12: Billing\n\ny\n"
        fp.refresh(self.repo, self.state, ["event factory=verified story=NEB-10"], 100.0, self.out)
        self.assertNotIn("NEB-11", self.docs[fp.PREP_TITLE]); self.assertIn("## NEB-12: Billing", self.docs[fp.PREP_TITLE])

    def test_refresh_waits_for_its_trigger_or_the_clock(self):
        self.state["prep"] = {"NEB-12": {"print": "old", "at": 1.0, "pid": None}}; self.state["prep_checked"] = 100.0
        fp.refresh(self.repo, self.state, ["event factory=offer ticket=NEB-12"], 200.0, self.out)
        self.assertEqual(self.cli_calls, [])

    def reply(self, **kw):
        found = {"code": [], "built": [], "open": [], "questions": [{"question": "Q?", "recommendation": "No", "why": "w"}], "drop": False, **kw}
        self.claude.return_value = (None, json.dumps(found), 4, {})

    def missing_prd(self, found=False):
        self.state["prd"] = {"found": found, "checked": 100.0}

    def test_idle_prepares_the_prd_first_while_it_is_missing(self):
        self.missing_prd()
        fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 100.0, self.out)
        self.spawn.assert_called_once_with(self.repo, ["factory", "prep", "--repo", str(self.repo), "--ticket", "PRD"], "night-prep.log")
        self.assertEqual((self.state["prep"]["PRD"]["layer"], self.state["prep"]["PRD"]["print"]), ("prd", "t1"))
        self.finish_worker("PRD"); fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 160.0, self.out)
        self.assertEqual(self.spawn.call_args.args[1][-1], "NEB-11")

    def test_idle_does_not_prepare_the_prd_once_it_is_found_or_unknown(self):
        for prd in ({"found": True, "checked": 100.0}, {}):
            self.state["prd"], self.state["prep"] = prd, {}
            fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 100.0, self.out)
            self.assertEqual(self.spawn.call_args.args[1][-1], "NEB-11")

    def test_a_night_turn_with_no_prd_and_no_plan_prepares_the_prd(self):
        state = {**json.loads(json.dumps(st.EMPTY)), "mode": "night", "night": self.state["night"]}
        fp.fc_delivery.product_plan.return_value = None
        with unittest.mock.patch.object(ft.fc_prd, "find_prd", return_value=None):
            lines = ft.turn(self.repo, state, 100.0)
        self.assertEqual(lines, [ft.PRD_FIRST])
        self.assertEqual(self.spawn.call_args.args[1][-1], "PRD")

    def test_the_prd_prep_writes_its_questions_from_the_seed_and_never_touches_a_ticket(self):
        self.reply(questions=[{"question": f"Q{n}?", "recommendation": "r", "why": "w"} for n in range(7)])
        self.assertEqual(fp.run_prep(self.repo, "PRD")["status"], "prepared")
        doc = self.docs[fp.PREP_TITLE]
        self.assertIn("## PRD: product requirements\n", doc); self.assertIn("5. Q4?\n", doc); self.assertNotIn("Q5?", doc)
        self.assertIn(self.seed, self.claude.call_args.args[1]); self.assertIn("never plan", self.claude.call_args.args[1])
        self.assertEqual([c for c in self.cli_calls if c[:2] != ["prd", "seed"] and c[:2] != ["linear", "doc"]], [])
        self.assertIn("event factory=prepared ticket=PRD questions=5", self.events[-1])

    def test_the_sweep_keeps_the_prd_section_while_the_prd_is_missing_and_drops_it_once_found(self):
        self.missing_prd()
        self.state["prep"] = {"PRD": {"print": "t1", "at": 1.0, "pid": None, "layer": "prd"}}
        self.docs[fp.PREP_TITLE] = "## PRD: product requirements\n\nx\n\n## NEB-12: Billing\n\ny\n"
        fp.refresh(self.repo, self.state, ["event factory=verified story=NEB-10"], 100.0, self.out)
        self.assertIn("## PRD: product requirements", self.docs[fp.PREP_TITLE]); self.assertIn("PRD", self.state["prep"])
        del self.state["prep"]["PRD"]
        fp.refresh(self.repo, self.state, ["event factory=verified story=NEB-10"], 101.0, self.out)
        self.assertIn("## PRD: product requirements", self.docs[fp.PREP_TITLE])  # no record, but still missing: kept
        self.state["prep"]["PRD"] = {"print": "t1", "at": 1.0, "pid": None, "layer": "prd"}; self.missing_prd(True)
        fp.refresh(self.repo, self.state, ["event factory=verified story=NEB-10"], 102.0, self.out)
        self.assertNotIn("PRD", self.docs[fp.PREP_TITLE]); self.assertNotIn("PRD", self.state["prep"])
        self.assertEqual([c for c in self.cli_calls if c[:3] == ["linear", "get", "PRD"]], [])

    def test_refresh_prepares_the_prd_again_when_the_tickets_changed(self):
        self.missing_prd()
        self.state["prep"] = {"PRD": {"print": "t1", "at": 1.0, "pid": None, "layer": "prd", "attempts": 1, "gave_up": True}}
        self.tprint = "t2"
        fp.refresh(self.repo, self.state, ["event factory=verified story=NEB-10"], 100.0, self.out)
        self.assertEqual((self.state["prep"]["PRD"]["print"], self.state["prep"]["PRD"]["gave_up"]), (None, False))

    def test_model_text_cannot_forge_a_heading_or_break_a_line(self):
        self.reply(questions=[{"question": "Refunds?\n## NEB-5: x", "recommendation": "# No\nyes", "why": "w"}], built=["a\n## NEB-6: y"])
        fp.run_prep(self.repo, "NEB-12")
        doc = self.docs[fp.PREP_TITLE]
        self.assertNotIn("## NEB-5:", doc.replace("1. Refunds? ## NEB-5: x", "")); self.assertNotIn("\n## NEB-6", doc)
        self.assertEqual([l for l in doc.split("\n") if l.startswith("## ")], ["## NEB-12: Billing"])

    def test_a_write_clobbered_by_another_writer_is_redone_on_the_fresh_body(self):
        self.reply()
        self.race = lambda ours: "## NEB-99: other\n\nz\n"  # the other writer, who read before ours, writes after it
        fp.run_prep(self.repo, "NEB-12")
        doc = self.docs[fp.PREP_TITLE]
        self.assertIn("## NEB-99: other", doc); self.assertIn("## NEB-12: Billing", doc)
        self.assertEqual(len([c for c in self.cli_calls if c[:3] == ["linear", "doc", "update"]]), 2)

    def test_a_drop_with_no_heading_writes_nothing(self):
        self.docs[fp.PREP_TITLE] = "## NEB-11: Sign in\n\ny\n"
        self.reply(drop=True)
        fp.run_prep(self.repo, "NEB-12")
        self.assertEqual([c for c in self.cli_calls if c[:3] == ["linear", "doc", "update"]], [])

    def test_a_pid_older_than_the_worker_timeout_counts_as_dead(self):
        fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 100.0, self.out)
        fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 100.0 + 1800 + 301, self.out)  # kill still says alive
        self.assertEqual(self.spawn.call_count, 2)

    def test_a_dead_worker_with_no_outcome_is_retried_once_after_half_an_hour_then_given_up(self):
        idle = lambda now: fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, now, self.out)
        idle(100.0); self.finish_worker("NEB-11"); idle(1000.0)
        self.assertEqual(self.spawn.call_count, 2)  # NEB-12 started; NEB-11 not yet
        self.assertEqual([c.args[1][-1] for c in self.spawn.call_args_list], ["NEB-11", "NEB-12"])
        self.finish_worker("NEB-12"); idle(1900.0)
        self.assertEqual(self.spawn.call_args.args[1][-1], "NEB-11")
        self.assertEqual(self.state["prep"]["NEB-11"]["attempts"], 1)
        self.finish_worker("NEB-11"); idle(4000.0); idle(8000.0)
        self.assertEqual([c.args[1][-1] for c in self.spawn.call_args_list].count("NEB-11"), 2)
        self.assertTrue(self.state["prep"]["NEB-11"]["gave_up"])
        self.assertEqual(fp.prep_status(self.repo, "NEB-11", self.state["prep"]["NEB-11"], 8000.0), "gave-up")

    def test_a_worker_error_records_failed(self):
        self.claude.side_effect = RuntimeError("claude down")
        self.assertEqual(fp.run_prep(self.repo, "NEB-12"), {"ticket": "NEB-12", "status": "failed"})
        last = json.loads(fp.outcomes_path(self.repo).read_text().splitlines()[-1])
        self.assertEqual((last["ticket"], last["status"]), ("NEB-12", "failed"))

    def test_prep_status_reads_the_latest_outcome_since_the_worker_began(self):
        rec = {"pid": 7000, "print": "p", "at": 100.0, "layer": "story", "attempts": 0}
        self.assertEqual(fp.prep_status(self.repo, "NEB-11", rec, 100.0), "running")
        self.gone.add(7000)
        self.assertEqual(fp.prep_status(self.repo, "NEB-11", rec, 100.0), "failed")  # it died without a word
        for status in ("prepared", "dropped", "cut", "failed"):
            fp._outcome(self.repo, "NEB-11", status)
            self.assertEqual(fp.prep_status(self.repo, "NEB-11", rec, 200.0), status)
        self.assertEqual(fp.prep_status(self.repo, "NEB-11", {**rec, "attempts": 1}, 200.0), "gave-up")
        self.assertEqual(fp.prep_status(self.repo, "NEB-11", {**rec, "at": time.time() + 60}, 200.0), "failed")  # an outcome older than the worker is not its own

    def cut_worker(self, ident):
        self.finish_worker(ident); fp._outcome(self.repo, ident, "cut")

    def test_a_cut_prep_is_requeued_at_once_and_keeps_its_attempts_every_time(self):
        idle = lambda now: fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, now, self.out)
        idle(100.0); self.state["prep"]["NEB-11"]["attempts"] = 1; self.cut_worker("NEB-11")
        fp.refresh(self.repo, self.state, [], 160.0, self.out)  # a tick, day or night, settles it before the refresh's clock
        self.assertEqual((self.state["prep"]["NEB-11"]["print"], self.state["prep"]["NEB-11"]["attempts"]), (None, 1))
        idle(200.0)
        self.assertEqual([c.args[1][-1] for c in self.spawn.call_args_list], ["NEB-11", "NEB-11"])
        self.cut_worker("NEB-11"); idle(300.0)
        self.assertEqual([c.args[1][-1] for c in self.spawn.call_args_list], ["NEB-11", "NEB-11", "NEB-11"])
        self.assertEqual(self.state["prep"]["NEB-11"]["attempts"], 1); self.assertFalse(self.state["prep"]["NEB-11"].get("gave_up"))

    def test_a_given_up_prep_is_skipped_until_its_text_changes(self):
        self.state["prep"]["NEB-11"] = {"pid": 6000, "print": self.print_of("NEB-11"), "at": 100.0, "layer": "story", "attempts": 1}
        self.gone.add(6000)
        fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 100.0 + 2 * fp.RETRY_SECONDS, self.out)
        self.assertTrue(self.state["prep"]["NEB-11"]["gave_up"])
        self.assertEqual([c.args[1][-1] for c in self.spawn.call_args_list], ["NEB-12"])
        self.state["prep"]["NEB-11"]["print"] = None  # even with no print, a given-up ticket waits for a change
        self.gone.add(self.state["prep"]["NEB-12"]["pid"]); fp._outcome(self.repo, "NEB-12", "prepared")
        fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 100.0 + 3 * fp.RETRY_SECONDS, self.out)
        self.assertEqual([c.args[1][-1] for c in self.spawn.call_args_list], ["NEB-12"])
        self.state["prep"]["NEB-11"]["print"] = self.print_of("NEB-11")
        self.descriptions["NEB-11"] = "changed"
        fp.refresh(self.repo, self.state, ["event factory=changed story=NEB-11"], 100.0 + 4 * fp.RETRY_SECONDS, self.out)
        rec = self.state["prep"]["NEB-11"]
        self.assertEqual((rec["print"], rec.get("gave_up"), rec["attempts"]), (None, False, 0))

    def test_a_deliberate_drop_is_not_retried(self):
        self.reply(drop=True)
        fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 100.0, self.out); self.finish_worker("NEB-11")
        fp.run_prep(self.repo, "NEB-11")
        self.state["prep"]["NEB-12"] = {"pid": None, "print": self.print_of("NEB-12"), "at": 100.0}
        fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 5000.0, self.out)
        self.assertEqual([c.args[1][-1] for c in self.spawn.call_args_list], ["NEB-11"])

    def test_idle_returns_while_the_night_waits_or_has_ended(self):
        for key in ("wait", "ended"):
            self.state["night"][key] = {"until": 1.0} if key == "wait" else {"state": "no-credit"}
            fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 100.0, self.out); self.spawn.assert_not_called()
            self.state["night"][key] = None

    def test_a_failure_reading_candidates_or_spawning_is_said_once_and_the_turn_goes_on(self):
        with unittest.mock.patch.object(fp.fc_delivery, "product_plan", side_effect=RuntimeError("linear down")):
            fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 100.0, self.out); fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 101.0, self.out)
        self.assertEqual(len(self.out), 1); self.assertIn("linear down", self.out[0])
        self.spawn.side_effect = OSError("no fork"); self.state["prep_error"] = None
        fp.prep_idle(self.repo, self.state, {"id": "NEB-11"}, 102.0, self.out)

    def test_a_needs_shaping_story_is_a_candidate(self):
        self.results["NEB-11"] = {**rd("NEB-11", ready=False, specced=None), "needs_shaping": True}
        self.assertEqual([c["id"] for c in fp.candidates(self.repo, "NEB-11", 3)], ["NEB-11", "NEB-12"])

    def test_an_event_is_always_one_line(self):
        self.claude.return_value = (None, "two\nlines  of   text", 4, {})
        fp.run_prep(self.repo, "NEB-12")
        self.assertNotIn("\n", self.events[0]); self.assertIn("two lines of text", self.events[0])


class Digest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve() / "neb"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.since = time.mktime((2026, 10, 26, 23, 0, 0, 0, 0, -1))
        self.state = {"night": ns.new_night(self.since, self.since + 8 * 3600, 20.0, None), "prep": {},
                      "dispatched": {"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "story", "at": self.since}}}
        self.docs = {"id": "d1", "url": "", "content": ""}
        plan = {"doc": {"content": "PLAN"}, "tracks": [], "features": {}}
        entries = [{"id": "NEB-10", "title": "Sign in with Google", "state": "Todo", "kind": "story", "feature": "NEB-9", "track": "auth", "wave": 1, "waits_on": []}]
        self.product_plan = unittest.mock.patch.object(fd.fc_delivery, "product_plan", return_value=plan).start()
        unittest.mock.patch.object(fd.fc_plan, "order_entries", side_effect=lambda tracks, features: entries).start()
        self.last_reconcile = unittest.mock.patch.object(fd.fc_delivery, "last_reconcile", return_value=None).start()
        self.paused = unittest.mock.patch.object(fd.fc_change, "paused_tasks", return_value={}).start()
        unittest.mock.patch.object(fd.fc_core, "all_projects", return_value={"sign-in": (self.repo, self.repo / ".compose" / "sign-in")}).start()
        unittest.mock.patch.object(fd.fc_tasks, "yolo_state", return_value=None).start()
        unittest.mock.patch.object(fd.fc_yolo, "digest_sections", return_value=[]).start()
        self.real_find = fd.fc_nightprep.find_doc
        self.find = unittest.mock.patch.object(fd.fc_nightprep, "find_doc", side_effect=lambda repo: dict(self.docs)).start()
        unittest.mock.patch.object(fd.fc_nightprep, "prep_doc", side_effect=AssertionError("the digest must not create the prep document")).start()
        self.addCleanup(unittest.mock.patch.stopall)

    def event(self, line):
        st.add_event(self.repo, line)

    def prepped(self, ident, status="prepared", layer="stories", **kw):
        self.state["prep"][ident] = {"pid": 4000 + int(ident.split("-")[1] if "-" in ident else 0), "print": "x", "at": 1.0, "layer": layer, "attempts": 0, **kw}
        if status:
            fp._outcome(self.repo, ident, status)

    def answer(self, at=None, **kw):
        rec = {"key": "k", "project": "sign-in", "issue": "NEB-10", "question": "q", "answer": "a", "reasoning": "", "answers": {}, "never_set": False,
               "why_wait": "", "model": "m", "at": at or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.since + 60)), "error": "",
               "commented": False, "acted": False, **kw}
        path = fa.answers_path(self.repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as out:
            out.write(json.dumps(rec) + "\n")

    def event_at(self, at, line):
        with unittest.mock.patch.object(st.time, "time", return_value=at):
            st.add_event(self.repo, line)

    def test_events_and_answers_are_bounded_by_the_night_on_both_sides(self):
        end = self.since + 3600
        self.event_at(self.since - 10, "event factory=verified story=NEB-1 project=sign-in")
        self.event_at(self.since + 10, "event factory=verified story=NEB-2 project=sign-in")
        self.event_at(end + 10, "event factory=verified story=NEB-3 project=sign-in")
        self.assertEqual(fd.night_events(self.repo, self.since), [l for l in fd.night_events(self.repo, self.since) if "NEB-1 " not in l])
        self.assertEqual([_f for _f in (fd._field(l, "story") for l in fd.night_events(self.repo, self.since))], ["NEB-2", "NEB-3"])
        self.assertEqual([fd._field(l, "story") for l in fd.night_events(self.repo, self.since, end)], ["NEB-2"])
        self.answer(question="in?")
        self.answer(question="late?", at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end + 600)))
        self.state["night"]["ended"] = {"state": "morning", "at": end}
        self.assertEqual(fd.review(self.repo, self.state, []), ["To confirm: in? -> a (night answerer; overruling it is a plan change)"])
        text = fd.render(self.repo, self.state, end + 7200)
        self.assertIn("NEB-2", text); self.assertNotIn("NEB-3", text); self.assertNotIn("NEB-1 ", text)

    def test_a_replan_without_a_why_has_no_empty_parentheses(self):
        self.event("event factory=changed story=NEB-11  (nothing running rests on it)")
        self.assertEqual(fd.review(self.repo, self.state, fd.night_events(self.repo, 0.0)), ["Re-plan: NEB-11 changed"])

    def test_a_key_is_not_found_inside_a_quoted_value(self):
        self.assertEqual(fd._field('event factory=held story=NEB-1 why="the story=NEB-9 moved"', "story"), "NEB-1")
        self.assertEqual(fd._field('event factory=turn-failed why="x story=NEB-9 y"', "story"), "")

    def test_review_covers_projects_nothing_was_dispatched_to(self):
        self.state["dispatched"] = {}
        self.paused.return_value = {"003": {"reason": "waits on D2"}}
        self.assertEqual(fd.review(self.repo, self.state, []), ["Paused: sign-in 003 (waits on D2)"])

    def test_render_survives_the_events_source_and_the_plan_failing(self):
        self.event_at(self.since + 10, "event factory=verified story=NEB-10 project=sign-in")
        with unittest.mock.patch.object(fd.fc_factorystate, "events_since", side_effect=OSError("disk gone")):
            text = fd.render(self.repo, self.state, self.since + 60)
        self.assertIn("night events unavailable: disk gone", text)
        self.assertEqual([h for h in fd.HEADS if f"\n\n{h}\n" not in text], [])
        self.product_plan.side_effect = RuntimeError("linear down")
        self.assertIn("placement unavailable: linear down", fd.render(self.repo, self.state, self.since + 60))

    def test_verified_work_is_placed_in_the_delivery_plan(self):
        self.event("event factory=verified story=NEB-10 project=sign-in  (verified: ...)")
        self.assertEqual(fd.verified(self.repo, fd.night_events(self.repo, 0.0)),
                         ["NEB-10 Sign in with Google  (feature NEB-9, wave 1, track auth; project sign-in)"])
        self.product_plan.side_effect = SystemExit("delivery: timed out")
        self.assertEqual(fd.verified(self.repo, fd.night_events(self.repo, 0.0)), ["NEB-10  (project sign-in; placement unavailable: delivery: timed out)"])

    def test_review_lists_answers_to_confirm_proposed_fixes_replans_paused_and_failures(self):
        self.answer(issue="NEB-10", question="NEB-10: which store?", answer="one store")
        self.last_reconcile.return_value = {"proposed": ["NEB-30 is not in the plan: add it or cancel it."], "at": "2026-10-27 02:00:00"}
        self.event('event factory=held story=NEB-11 tasks=002 why="D1 changed on NEB-9"  (...)')
        self.paused.return_value = {"002": {"reason": "factory: D1 changed on NEB-9"}}
        self.event('event factory=turn-failed why="delivery reconcile: timed out"  (the next turn tries again)')
        out = fd.review(self.repo, self.state, fd.night_events(self.repo, 0.0))
        self.assertEqual(out[:3], ["To confirm: NEB-10: which store? -> one store (night answerer; overruling it is a plan change)",
                                   "Proposed fix: NEB-30 is not in the plan: add it or cancel it.",
                                   'Re-plan: NEB-11 held (D1 changed on NEB-9)'])
        self.assertIn("Paused: sign-in 002 (factory: D1 changed on NEB-9)", out)
        self.assertIn("Failed or stuck: delivery reconcile: timed out", out)

    def test_review_skips_old_and_failed_answers_and_lists_never_set_ones_as_waiting(self):
        self.answer(at="2026-10-26 12:00:00", question="old?")
        self.answer(error="boom", question="broken?")
        self.answer(question="which bank?", never_set=True, why_wait="money")
        self.assertEqual(fd.review(self.repo, self.state, []), ["Waiting on you: sign-in: which bank? (never-set: money)"])

    def test_review_includes_the_posture_decisions_and_digest_items_of_a_project(self):
        self.state["dispatched"]["NEB-10"]["project"] = "sign-in"
        fd.fc_tasks.yolo_state.return_value = {"mode": "yolo", "decisions": [{"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.since + 5)), "text": "db: postgres"},
                                                                          {"ts": "2020-01-01 00:00:00", "text": "old"}]}
        fd.fc_yolo.digest_sections.return_value = [("Waiting on you", ["001: needs-user  delete?"]), ("Done", ["x"]), ("Tried and failed", ["002 t  failed"])]
        self.assertEqual(fd.review(self.repo, self.state, []),
                         ["To confirm: sign-in db: postgres", "Waiting on you: sign-in: 001: needs-user  delete?", "Failed or stuck: sign-in: 002 t  failed"])

    def test_a_source_that_fails_is_marked_unavailable_and_the_digest_still_renders(self):
        self.event("event factory=verified story=NEB-10 project=sign-in  (verified)")
        self.last_reconcile.side_effect = ValueError("bad json")
        self.paused.side_effect = OSError("gone")
        self.find.side_effect = RuntimeError("linear down")
        self.prepped("NEB-12")
        text = fd.render(self.repo, self.state, self.since + 60)
        self.assertIn("last reconcile unavailable: bad json", text)
        self.assertIn("paused tasks of sign-in unavailable: gone", text)
        self.assertIn("- NEB-12 (stories): prepared questions unavailable (linear down)", text)
        self.assertIn("morning digest for neb: night 23:00 to ", text)

    def test_ready_to_plan_counts_the_prepared_questions_and_lists_planning_waits(self):
        self.docs.update(url="https://linear.app/d", content="Intro\n\n## NEB-12: Billing\n\nLayer: stories.\n\nQuestions:\n1. Which?\n   Recommendation (a guess): x - y\n2. When?\n   Recommendation (a guess): z - w\n")
        self.prepped("NEB-12")
        self.state["prep"]["NEB-13"] = {"pid": 5, "print": None, "at": 1.0, "layer": "story"}
        self.state["night"]["morning_msgs"] = ["sign-in|plan the stories for NEB-9?"]
        self.assertEqual(fd.ready_to_plan(self.repo, self.state),
                         ["Prep document: https://linear.app/d", "NEB-12 Billing (stories): 2 questions prepared", "Planning waits: sign-in: plan the stories for NEB-9?"])

    def test_ready_to_plan_lists_the_prd_first(self):
        self.docs.update(url="https://linear.app/d", content="## NEB-12: Billing\n\nQuestions:\n1. Which?\n\n## PRD: product requirements\n\nQuestions:\n1. Who?\n2. Why?\n")
        self.prepped("NEB-12"); self.prepped("PRD", layer="prd")
        self.assertEqual(fd.ready_to_plan(self.repo, self.state)[:3],
                         ["Prep document: https://linear.app/d", "PRD product requirements (prd): 2 questions prepared", "NEB-12 Billing (stories): 1 question prepared"])

    def test_ready_to_plan_says_how_each_prep_ended_before_any_tick_settles_it(self):
        self.docs.update(url="https://linear.app/doc/prep", content="## NEB-12: Billing\n\nQuestions:\n1. Which?\n")
        alive = {4016}
        unittest.mock.patch.object(fp.os, "kill", side_effect=lambda pid, sig: None if pid in alive else (_ for _ in ()).throw(ProcessLookupError())).start()
        self.prepped("NEB-12"); self.prepped("NEB-13", "dropped", "story"); self.prepped("NEB-14", "cut", "story")
        self.prepped("NEB-15", None, "story"); self.prepped("NEB-16", None, "stories", at=self.since); self.prepped("NEB-17", "failed", "story", attempts=1)
        self.prepped("NEB-18", None, "story", gave_up=True, attempts=1)
        self.assertEqual(fd.ready_to_plan(self.repo, self.state, self.since + 60),
                         ["Prep document: https://linear.app/doc/prep",
                          "NEB-12 Billing (stories): 1 question prepared",
                          "NEB-14 (story): prep was cut off when the night ended; it runs again next night",
                          "NEB-15 (story): prep failed; it is tried once more next night",
                          "NEB-16 (stories): still being prepared",
                          "NEB-17 (story): prep failed twice; see .compose/night-prep.log",
                          "NEB-18 (story): prep failed twice; see .compose/night-prep.log"])
        self.assertIn("Ready to plan\n- Prep document: https://linear.app/doc/prep\n- NEB-12 Billing", fd.render(self.repo, self.state, self.since + 60))

    def test_ready_to_plan_names_no_document_when_nothing_was_prepared_or_it_is_missing(self):
        self.docs.update(url="https://linear.app/doc/prep")
        self.prepped("NEB-13", "dropped", "story")
        self.assertEqual(fd.ready_to_plan(self.repo, self.state), [])
        self.prepped("NEB-14", "cut", "story"); self.docs.update(id=None, url="")
        self.assertEqual(fd.ready_to_plan(self.repo, self.state), ["NEB-14 (story): prep was cut off when the night ended; it runs again next night"])

    def test_ready_to_plan_never_creates_a_missing_document(self):
        calls = []
        self.find.side_effect = self.real_find
        self.prepped("NEB-12")
        with unittest.mock.patch.object(fp.fc_factoryturn, "compose_cli", side_effect=lambda args, stdin=None: calls.append(list(args)) or "[]"):
            self.assertEqual(fd.ready_to_plan(self.repo, self.state), ["NEB-12 (stories): prepared questions unavailable (document missing)"])
        self.assertEqual(calls, [["linear", "doc", "list", "--json", "--repo", str(self.repo)]])

    def test_the_digest_reads_this_night_s_archived_posture_once_it_is_lifted(self):
        results = self.repo / ".compose" / "sign-in" / "results"
        results.mkdir(parents=True)
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.since))
        stem = "yolo-" + since.replace(" ", "T").replace(":", "")
        decided = lambda text: [{"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.since + 5)), "text": text}]
        for name, y in ((f"{stem}.json", {"since": since, "mode": "night", "decisions": decided("first")}),
                        (f"{stem}-2.json", {"since": since, "mode": "night", "decisions": decided("second")}),
                        (f"{stem}-10.json", {"since": since, "mode": "night", "decisions": decided("tenth")}),
                        (f"{stem}-11.json", {"since": "2026-01-01 00:00:00", "mode": "night", "decisions": decided("other night")}),
                        ("yolo-2026-10-25T230000.json", {"since": "2026-10-25 23:00:00", "mode": "night", "decisions": decided("last night")})):
            (results / name).write_text(json.dumps(y))
        fd.fc_yolo.digest_sections.side_effect = lambda repo, proj, y: [("Waiting on you", [f"001 {y['decisions'][0]['text']}"])]
        self.assertEqual(fd.review(self.repo, self.state, []), ["To confirm: sign-in tenth", "Waiting on you: sign-in: 001 tenth"])
        fd.fc_tasks.yolo_state.return_value = {"since": "2026-10-27 09:00:00", "mode": "yolo", "decisions": []}
        self.assertEqual(fd.review(self.repo, self.state, []), ["To confirm: sign-in tenth", "Waiting on you: sign-in: 001 tenth"])

    def test_render_has_the_three_heads_the_end_state_and_the_spend(self):
        self.state["night"].update(ended={"state": "budget", "at": self.since + 3600}, spend=20.31, waits=1, waited=1861.0)
        self.docs["content"] = "## NEB-12: Billing\n\nQuestions:\n1. Which?\n"
        self.prepped("NEB-12")
        text = fd.render(self.repo, self.state, self.since + 7200)
        self.assertTrue(text.startswith(f"morning digest for {self.repo.name}: night 23:00 to 00:00, ended: budget\n"))
        self.assertIn("spend $20.31 of $20.00; usage waits: 1 (31m)", text)
        self.assertEqual([h for h in fd.HEADS if f"\n\n{h}\n" not in text], [])
        self.assertIn("Ready to plan\n- NEB-12 Billing (stories): 1 question prepared", text)
        self.assertIn("Verified and waiting for your merge\n- none", text)
        self.assertTrue(text.rstrip().endswith(f"Night notes: {ns.notes_path(self.repo)}"))
        self.assertNotIn(chr(0x2014), text)



class Night(unittest.TestCase):
    """The switch, the per-turn tick, and the two-stage stop, through cmd_factory and fc_night.tick."""
    def setUp(self):
        global fn
        import fc_night as fn
        import argparse, fcntl
        self.argparse, self.fcntl = argparse, fcntl
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        self.repo = Path(d.name).resolve() / "neb"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        for slug, feature in (("sign-in", "NEB-9"), ("billing", "NEB-20")):
            self.make(slug, feature)
        self.sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self.sleeper.wait); self.addCleanup(self.sleeper.kill)
        mock = unittest.mock
        self.turn = mock.Mock(return_value=[])
        self.rests_on, self.hold = mock.Mock(return_value={}), mock.Mock(return_value=[])
        self.keep_awake, self.slept, self.after_wake = mock.Mock(return_value=None), mock.Mock(return_value=0), mock.Mock(return_value=[])
        self.jev, self.answer_step, self.refresh = mock.Mock(), mock.Mock(return_value=[]), mock.Mock()
        self.leads, self.read = mock.Mock(return_value={}), mock.Mock(return_value="")
        for mod, name, m in ((fn.fc_factoryturn, "turn", self.turn), (fn.fc_reconcile, "live_leads", self.leads), (fn.fc_herdr, "herdr_text", self.read),
                             (fn.fc_proc, "keep_awake", self.keep_awake), (fn.fc_proc, "slept", self.slept), (fn.fc_sleep, "run_after_wake", self.after_wake),
                             (fn.fc_models, "jev_ask", self.jev), (fn.fc_change, "rests_on", self.rests_on), (fn.fc_change, "hold_tasks", self.hold),
                             (fn.fc_answer, "answer_step", self.answer_step), (fn.fc_nightprep, "refresh", self.refresh),
                             (fd.fc_delivery, "product_plan", mock.Mock(side_effect=RuntimeError("no plan here"))),
                             (fd.fc_delivery, "last_reconcile", mock.Mock(return_value=None)), (fd.fc_change, "paused_tasks", mock.Mock(return_value={})),
                             (fd.fc_nightprep, "find_doc", mock.Mock(side_effect=RuntimeError("no doc here"))),
                             (fd.fc_yolo, "digest_sections", mock.Mock(return_value=[]))):
            mock.patch.object(mod, name, m).start()
        mock.patch.dict(fc_config.FACTORY, {"turn_seconds": 0}).start()
        mock.patch.dict(fn.CLOCK, {"at": None}).start()
        self.addCleanup(mock.patch.stopall)

    def make(self, slug, feature):
        (self.proj(slug) / "results").mkdir(parents=True)
        (self.proj(slug) / "verify.json").write_text("{}")
        (self.proj(slug) / "linear.json").write_text(json.dumps({"feature": feature}))

    def proj(self, slug):
        return self.repo / ".compose" / slug

    def save(self, **kv):
        s = st.load_state(self.repo); s.update(kv); st.save_state(self.repo, s)

    def load(self):
        return st.load_state(self.repo)

    def hold_lock(self):
        """Stand in for a live loop: hold the repo's loop lock until the test ends."""
        lock = open(f.lock_path(self.repo), "a"); self.fcntl.flock(lock, self.fcntl.LOCK_EX)
        self.addCleanup(lock.close)

    def posture(self, slug, mode):
        (self.proj(slug) / "results" / "yolo.json").write_text(json.dumps({"since": "2026-10-26 20:00:00", "decisions": [], "mode": mode}))

    def unposture(self, slug):
        (self.proj(slug) / "results" / "yolo.json").unlink()

    def result(self, slug, tid, cost, mtime):
        path = self.proj(slug) / "results" / f"{tid}.json"
        path.write_text(json.dumps({"cost": cost})); os.utime(path, (mtime, mtime))

    def verb(self, action, **kw):
        import contextlib, io
        args = dict(action=action, target=None, hold=None, done=False, timeout=None, json=False, repo=str(self.repo), until=None, budget=None,
                    stop_when=None, now=False, at=None, nth=0, ticket=None, project=None)
        args.update(kw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            f.cmd_factory(self.argparse.Namespace(**args))
        return out.getvalue()

    def night_on(self, **kw):
        self.save(pid=os.getpid())
        self.verb("night", **kw)
        self.state = self.load()

    def test_night_needs_the_loop_and_no_away_and_says_its_limits(self):
        with self.assertRaises(SystemExit) as e:
            self.verb("night")
        self.assertEqual(e.exception.code, "factory: night needs the loop running: compose factory start first")
        self.save(pid=os.getpid()); self.posture("billing", "away")
        with self.assertRaises(SystemExit) as e:
            self.verb("night")
        self.assertEqual(e.exception.code, "factory: away is on for billing; end it first (compose away off --project billing), night takes over answering")
        self.unposture("billing"); out = self.verb("night", until="07:00", budget=12.0, stop_when="sign-in is verified").splitlines()
        self.assertRegex(out[0], r"^factory: night on until 07:00 \(in \d+h \d+m\), budget \$12\.00 of real spend, stop when: sign-in is verified; up to 6 tickets at once under the load hold$")
        self.assertEqual(out[1], "questions on approved tickets go to the night answerer (claude:claude-opus-5-5, high); planning and the never-set wait for you")
        self.assertEqual(out[2], "back: your first message ends it with compose factory day, which prints the morning digest")
        self.assertEqual((self.load()["mode"], fc.fc_tasks.yolo_state(self.proj("sign-in"))["mode"]), ("night", "night"))

    def test_night_refuses_a_second_night_and_clears_planning_and_declines(self):
        self.save(planning={"id": "NEB-30", "at": 1.0}, declined_until=time.time() + 600)
        self.night_on(until="07:00")
        self.assertEqual((self.state["planning"], self.state["declined_until"]), (None, None))
        with self.assertRaises(SystemExit) as e:
            self.verb("night")
        self.assertRegex(e.exception.code, r"^factory: night is already on since \d\d:\d\d until 07:00; compose factory day ends it$")

    def test_a_tick_drains_at_the_budget_and_ends_when_nothing_dispatched_is_in_flight(self):
        self.night_on(budget=1.0); self.result("sign-in", "001", 1.5, mtime=time.time())
        self.state["dispatched"] = {"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}}
        lines = fn.tick(self.repo, self.state, time.time())
        self.assertIn("event factory=draining why=budget spend=1.50 budget=1.00  (nothing new starts; running work finishes; the night ends when nothing is in flight)", lines)
        self.assertEqual(fc.fc_factorystate.pace(self.state)["cap"], 0); self.assertIsNone(self.state["night"]["ended"])
        self.state["dispatched"] = {}; lines = fn.tick(self.repo, self.state, time.time())
        self.assertIn("event factory=night-ended state=budget  (compose factory day prints the morning digest)", lines)
        self.assertEqual(len([l for l in lines if "draining" in l]), 0)

    def test_stop_is_graceful_first_and_immediate_second_keeping_the_work(self):
        self.night_on(); self.hold_lock(); self.save(pid=self.sleeper.pid, dispatched={"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}})
        self.assertEqual(self.verb("stop").strip(), "factory: stopping gracefully: nothing new starts, running work finishes, and questions are still answered; "
                                                    "run compose factory stop again to stop at once")
        self.assertIsNone(self.sleeper.poll()); self.assertEqual(self.load()["night"]["draining"], "stopped")
        self.rests_on.return_value = {"002": "under NEB-10"}
        self.assertEqual(self.verb("stop").splitlines()[0], "factory: stopped at once; held 1 task, its work is kept on its ref (compose change keep|part|discard settles it)")
        self.hold.assert_called_once_with(self.proj("sign-in"), ["002"], "night stopped at once by the product owner")
        self.assertEqual(self.load()["night"]["ended"]["state"], "stopped at once"); self.sleeper.wait(timeout=5)

    def test_a_graceful_stop_ends_the_loop_once_the_drain_finishes(self):
        self.night_on(); self.verb("stop")
        self.assertIsNone(self.load()["stopped"])
        f.run_loop(self.repo, turns=3)
        s = self.load()
        self.assertEqual(s["night"]["ended"]["state"], "stopped"); self.assertIsNotNone(s["stopped"]); self.assertEqual(self.turn.call_count, 1)

    def test_a_stop_at_once_lifts_the_night_postures(self):
        self.night_on(); self.hold_lock(); self.save(pid=self.sleeper.pid)
        self.make("admin", "NEB-40"); self.posture("admin", "away")
        self.verb("stop", now=True)
        for slug in ("sign-in", "billing"):
            self.assertIsNone(fc.fc_tasks.yolo_state(self.proj(slug)))
            self.assertEqual(len(list((self.proj(slug) / "results").glob("yolo-*.json"))), 1)
        self.assertEqual(fc.fc_tasks.yolo_state(self.proj("admin"))["mode"], "away")
        self.sleeper.wait(timeout=5)

    def test_the_digest_keeps_each_project_s_items_after_the_next_tick_lifts_the_postures(self):
        self.night_on()
        path = self.proj("sign-in") / "results" / "yolo.json"
        y = json.loads(path.read_text()); y["decisions"].append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": "db: postgres"}); path.write_text(json.dumps(y))
        fd.fc_yolo.digest_sections.side_effect = lambda repo, proj, y: ([("Waiting on you", [f"001: needs-user  delete? ({y['mode']})"]), ("Tried and failed", ["002 t  failed"])]
                                                                         if proj.name == "sign-in" else [])
        s = self.load(); s["night"]["until"] = time.time() - 1; st.save_state(self.repo, s)
        f.run_loop(self.repo, turns=2)
        self.assertEqual(self.load()["night"]["ended"]["state"], "morning"); self.assertIsNone(fc.fc_tasks.yolo_state(self.proj("sign-in")))
        out = self.verb("day")
        for item in ("- To confirm: sign-in db: postgres", "- Waiting on you: sign-in: 001: needs-user  delete? (night)", "- Failed or stuck: sign-in: 002 t  failed"):
            self.assertIn(item, out)

    def test_stop_now_stops_at_once_on_the_first_stop(self):
        self.night_on(); self.hold_lock(); self.save(pid=self.sleeper.pid)
        self.assertEqual(self.verb("stop", now=True).splitlines()[0], "factory: stopped at once; held 0 tasks, their work is kept on their refs (compose change keep|part|discard settles it)")
        self.assertEqual(self.load()["night"]["ended"]["state"], "stopped at once"); self.sleeper.wait(timeout=5)

    def test_day_ends_the_night_prints_and_saves_the_digest_and_lifts_the_posture(self):
        self.night_on(); out = self.verb("day")
        self.assertTrue(out.startswith(f"morning digest for {self.repo.name}:"))
        self.assertEqual(self.load()["mode"], "day"); self.assertIsNone(fc.fc_tasks.yolo_state(self.proj("sign-in")))
        self.assertEqual(len(list((self.repo / ".compose" / "digests").glob("night-*.txt"))), 1)
        self.assertIn("ended: back", out); self.assertTrue(ns.notes_tail(self.repo).rstrip().endswith("ended: back"))
        self.assertEqual(len(list((self.proj("sign-in") / "results").glob("yolo-*.json"))), 1)
        with self.assertRaises(SystemExit) as e:
            self.verb("day")
        self.assertEqual(e.exception.code, "factory: night is not on")

    def test_digest_prints_and_changes_nothing(self):
        self.night_on(); before = self.load()
        self.assertIn("ended: still on", self.verb("digest"))
        self.assertEqual(self.load(), before)

    def test_a_tick_keeps_the_mac_awake_and_recovers_runs_after_a_sleep(self):
        self.night_on(); self.state["dispatched"] = {"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}}
        fn.CLOCK["at"] = fc_proc.clock(); self.slept.return_value = 3600
        now = time.time(); lines = fn.tick(self.repo, self.state, now)
        self.keep_awake.assert_called_once_with()
        self.assertTrue(any(l.startswith("event factory=woke slept=") for l in lines))
        self.after_wake.assert_called_once_with(self.repo, self.proj("sign-in"), (now - 3600, now), now)
        self.assertIsNotNone(fn.CLOCK["at"])

    def test_the_stop_condition_drains_after_a_verified_story(self):
        self.night_on(stop_when="sign-in is verified")
        self.state["dispatched"] = {"NEB-11": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}}
        self.turn.return_value = ["event factory=verified story=NEB-10 project=sign-in  (verified)"]
        self.jev.return_value = {"answers": {"stop": {"choice": "NOT_MET", "probabilities": {"MET": 0.5, "NOT_MET": 0.5}}}}
        fn.tick(self.repo, self.state, time.time()); self.assertIsNone(self.state["night"]["draining"])
        self.assertEqual(self.jev.call_args.args[2], {"condition": "sign-in is verified", "verified": self.turn.return_value})
        self.jev.return_value = {"answers": {"stop": {"choice": "MET", "probabilities": {"MET": 0.8, "NOT_MET": 0.2}}}}
        lines = fn.tick(self.repo, self.state, time.time())
        self.assertEqual(self.state["night"]["draining"], "stop-when")
        self.assertIn("event factory=draining why=stop-when  (nothing new starts; running work finishes; the night ends when nothing is in flight)", lines)

    def test_stop_met_is_none_when_jev_cannot_say(self):
        self.jev.side_effect = RuntimeError("typesafe down")
        self.assertIsNone(fn.stop_met("x", ["event factory=verified story=NEB-10"]))
        self.jev.side_effect = None; self.jev.return_value = {"answers": {}}
        self.assertIsNone(fn.stop_met("x", ["event factory=verified story=NEB-10"]))

    def test_night_posture_reaches_projects_dispatched_later(self):
        self.night_on(); self.unposture("billing"); self.make("admin", "NEB-40"); self.posture("admin", "away")
        self.state["dispatched"] = {"NEB-21": {"feature": "NEB-20", "project": "billing", "kind": "build", "at": 1.0},
                                    "NEB-41": {"feature": "NEB-40", "project": "admin", "kind": "build", "at": 1.0}}
        fn.tick(self.repo, self.state, time.time())
        self.assertEqual(fc.fc_tasks.yolo_state(self.proj("billing"))["mode"], "night")
        self.assertEqual(fc.fc_tasks.yolo_state(self.proj("admin"))["mode"], "away")
        self.assertFalse(fn.posture_on(self.proj("admin"), "2026-10-26 22:00:00"))
        self.assertIsNone(fn.posture_off(self.proj("admin"))); self.assertEqual(fc.fc_tasks.yolo_state(self.proj("admin"))["mode"], "away")

    def test_no_credit_ends_the_night_at_once(self):
        self.night_on(); caffeinate = unittest.mock.Mock(**{"poll.return_value": None})
        (self.proj("billing") / "results" / "resume.json").write_text(json.dumps({"provider": "openrouter"}))
        with unittest.mock.patch.dict(fc_proc.AWAKE, {os.getpid(): caffeinate}):
            lines = fn.tick(self.repo, self.state, time.time())
            self.assertNotIn(os.getpid(), fc_proc.AWAKE)
        self.assertTrue(any("event factory=night-ended state=no-credit" in l for l in lines))
        caffeinate.terminate.assert_called_once_with(); self.turn.assert_not_called()
        self.assertEqual(self.state["night"]["ended"]["state"], "no-credit")

    def test_a_day_tick_is_the_turn_then_the_refresh(self):
        self.state = self.load(); self.state["prep"] = {"NEB-12": {"pid": None, "print": "x", "at": 1.0}}
        self.turn.return_value = ["event factory=dispatched story=NEB-10"]
        self.assertEqual(fn.tick(self.repo, self.state, 5.0), ["event factory=dispatched story=NEB-10"])
        self.keep_awake.assert_not_called(); self.answer_step.assert_not_called()
        self.assertEqual(self.refresh.call_args.args[:4], (self.repo, self.state, ["event factory=dispatched story=NEB-10"], 5.0))

    def test_the_usage_step_reads_the_live_leads_and_prompts_them(self):
        self.night_on(); self.leads.return_value = {"sign-in": "sign-in-orchestrator"}
        fn.tick(self.repo, self.state, time.time())
        self.read.assert_called_once_with("agent", "read", "sign-in-orchestrator", "--source", "recent-unwrapped", "--lines", "12")

    def race(self, during, turns=1, **kw):
        """One loop turn while a verb writes the state on disk: what the verb changed must survive the turn's save."""
        def turn(repo, state, now, **_):
            self.verb(during, **kw)
            if state.get("night"):
                state["night"]["spend"] = 0.5  # the turn's own change to the night
            return []
        self.turn.side_effect = turn
        f.run_loop(self.repo, turns=turns)
        return self.load()

    def test_a_night_switched_on_during_a_turn_survives_it(self):
        s = self.race("night")
        self.assertEqual(s["mode"], "night"); self.assertIsNotNone(s["night"]); self.assertEqual(s["night"]["spend"], 0.0)

    def test_a_day_during_a_turn_survives_it(self):
        self.night_on()
        s = self.race("day")
        self.assertEqual((s["mode"], s["night"]["ended"]["state"]), ("day", "back"))

    def test_a_graceful_stop_during_a_turn_survives_it(self):
        self.night_on()
        s = self.race("stop")
        self.assertEqual((s["night"]["draining"], s["night"]["stops"], s["night"]["spend"]), ("stopped", 1, 0.5))


    def test_a_stop_at_once_during_a_turn_survives_it_and_ends_the_loop(self):
        self.night_on(); self.verb("stop")
        s = self.race("stop", turns=2)
        self.assertEqual((s["night"]["ended"]["state"], s["night"]["stops"], s["night"]["spend"]), ("stopped at once", 2, 0.5))
        self.assertIsNotNone(s["stopped"]); self.assertEqual(self.turn.call_count, 1)

    def test_a_day_during_the_wake_leaves_no_night_posture_after_the_next_tick(self):
        self.night_on(); self.state["dispatched"] = {"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}}
        fn.CLOCK["at"] = fc_proc.clock(); self.slept.return_value = 60
        self.after_wake.side_effect = lambda *args: self.verb("day") and []
        fn.tick(self.repo, self.state, time.time())
        self.slept.return_value = 0; fn.tick(self.repo, self.load(), time.time())
        self.assertIsNone(fc.fc_tasks.yolo_state(self.proj("sign-in")))

    def test_no_credit_is_checked_before_the_wake_recovery(self):
        self.night_on(); self.state["dispatched"] = {"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}}
        (self.proj("billing") / "results" / "resume.json").write_text(json.dumps({"provider": "openrouter"}))
        fn.CLOCK["at"] = fc_proc.clock(); self.slept.return_value = 3600
        fn.tick(self.repo, self.state, time.time())
        self.after_wake.assert_not_called(); self.assertEqual(self.state["night"]["ended"]["state"], "no-credit")

    def test_a_graceful_stop_keeps_the_drain_reason_already_set(self):
        self.night_on(); self.hold_lock(); s = self.load(); s["night"]["draining"] = "budget"; st.save_state(self.repo, s)
        self.verb("stop")
        self.assertEqual((self.load()["night"]["draining"], self.load()["night"]["stops"]), ("budget", 1))

    def test_the_night_ended_note_is_written_once_when_a_verb_and_the_tick_both_end_it(self):
        self.night_on(); self.state["night"]["draining"] = "stopped"
        self.turn.side_effect = lambda *args, **kw: self.verb("day") and []
        fn.tick(self.repo, self.state, time.time())
        self.assertEqual(ns.notes_tail(self.repo).count("night ended"), 1)
        self.state = self.load(); self.state["night"]["ended"] = None; self.state["mode"] = "night"; st.save_state(self.repo, self.state)
        self.turn.side_effect = None; self.state["night"]["draining"] = "budget"
        fn.tick(self.repo, self.state, time.time()); self.verb("day")
        self.assertEqual(ns.notes_tail(self.repo).count("night ended"), 2)

    def test_posture_off_never_overwrites_an_archive(self):
        for _ in range(3):
            fn.posture_on(self.proj("sign-in"), "2026-10-26 22:00:00"); fn.posture_off(self.proj("sign-in"))
        names = sorted(p.name for p in (self.proj("sign-in") / "results").glob("yolo-*.json"))
        self.assertEqual(names, ["yolo-2026-10-26T220000-2.json", "yolo-2026-10-26T220000-3.json", "yolo-2026-10-26T220000.json"])

    def test_a_stop_with_no_live_loop_skips_the_graceful_stage(self):
        self.night_on(); self.save(pid=None)
        self.assertEqual(self.verb("stop").strip(), "factory: stopped; running work finishes")
        self.assertIsNone(self.load()["night"]["draining"]); self.assertIsNotNone(self.load()["stopped"])

    def test_stop_hold_at_night_stops_at_once(self):
        self.night_on(); self.hold_lock(); self.save(pid=self.sleeper.pid, dispatched={"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}})
        self.rests_on.return_value = {"002": "under NEB-10"}
        self.assertEqual(self.verb("stop", hold="rethink sign-in").splitlines(), ["factory: stopped at once; held 1 task, its work is kept on its ref (compose change keep|part|discard settles it)"])
        self.hold.assert_called_once_with(self.proj("sign-in"), ["002"], "night stopped at once by the product owner: rethink sign-in")
        self.assertEqual(self.load()["night"]["ended"]["state"], "stopped at once"); self.sleeper.wait(timeout=5)

if __name__ == "__main__":
    unittest.main()
