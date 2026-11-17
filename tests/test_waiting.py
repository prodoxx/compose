import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_waiting


class Store(unittest.TestCase):
    """The waiting list is one file per project: ids that are never reused, one open item per key, one line each."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        self.file = self.proj / "results" / "waiting.json"  # the results directory does not exist yet: the first item makes it

    def test_an_item_gets_the_next_id_and_one_line(self):
        a = fc_waiting.open_item(self.proj, "answer", "report:hw-orchestrator:t1", "which\n database?  " + "x" * 200, "SQLite", ids=["Q1"])
        b = fc_waiting.open_item(self.proj, "confirm", "decision:NEB-1:D7", "D7: dark theme")
        self.assertEqual((a["id"], b["id"]), ("W1", "W2"))
        self.assertEqual(len(a["line"]), 120)
        self.assertTrue(a["line"].startswith("which database? x"))
        self.assertTrue(a["line"].endswith("..."))
        self.assertEqual((a["stops"], b["stops"]), (True, False))
        self.assertEqual((a["recommendation"], a["ids"], a["seen"], a["closed"], a["closed_by"]), ("SQLite", ["Q1"], None, None, None))
        self.assertEqual(len(json.loads(self.file.read_text())["items"]), 2)

    def test_the_same_key_is_one_item(self):
        a = fc_waiting.open_item(self.proj, "go", "k", "wait for the go")
        b = fc_waiting.open_item(self.proj, "go", "k", "wait for the go again")
        self.assertEqual((a["id"], b["id"]), ("W1", "W1"))
        self.assertEqual(len(json.loads(self.file.read_text())["items"]), 1)

    def test_a_closed_item_leaves_the_open_list_and_its_id_is_not_reused(self):
        fc_waiting.open_item(self.proj, "go", "k", "wait for the go")
        self.assertTrue(fc_waiting.close_item(self.proj, "k", "settled"))
        self.assertFalse(fc_waiting.close_item(self.proj, "k", "settled"))
        self.assertEqual(fc_waiting.open_items(self.proj), [])
        closed = json.loads(self.file.read_text())["items"][0]
        self.assertEqual(closed["closed_by"], "settled")
        self.assertTrue(closed["closed"])
        self.assertEqual(fc_waiting.open_item(self.proj, "go", "k2", "again")["id"], "W2")

    def test_seen_keeps_it_open(self):
        fc_waiting.open_item(self.proj, "confirm", "k", "D1: a thing")
        self.assertTrue(fc_waiting.mark_seen(self.proj, "W1")["seen"])
        self.assertEqual([i["id"] for i in fc_waiting.open_items(self.proj)], ["W1"])
        self.assertIsNone(fc_waiting.mark_seen(self.proj, "W9"))

    def test_an_answered_question_closes_its_item_at_once(self):
        fc_waiting.open_item(self.proj, "answer", "report:a:t1", "which one?", ids=["Q1", "Q2"])
        fc_waiting.open_item(self.proj, "answer", "report:b:t2", "no ids here")
        self.assertEqual(fc_waiting.answered(self.proj, ["Q1"]), [])
        self.assertEqual(fc_waiting.open_items(self.proj)[0]["ids"], ["Q2"])
        self.assertEqual(fc_waiting.answered(self.proj, ["Q2"]), ["W1"])
        items = {i["id"]: i for i in json.loads(self.file.read_text())["items"]}
        self.assertEqual(items["W1"]["closed_by"], "decided")
        self.assertIsNone(items["W2"]["closed"])
        self.assertEqual(fc_waiting.answered(self.proj, ["Q1", "Q2"]), [])

    def test_an_unreadable_file_reads_as_empty_and_the_next_write_replaces_it(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("{")
        self.assertEqual(fc_waiting.open_items(self.proj), [])
        self.assertEqual(self.file.read_text(), "{")
        self.assertEqual(fc_waiting.open_item(self.proj, "go", "k", "go?")["id"], "W1")
        self.assertEqual(json.loads(self.file.read_text())["items"][0]["id"], "W1")

    def test_a_file_that_is_not_a_store_reads_as_empty(self):
        self.file.parent.mkdir(parents=True)
        for bad in ('[]', '{"next": "3", "items": []}', '{"next": 3, "items": {}}', '{"next": 3, "items": [1]}', '{"next": 3, "items": [{"id": "W1"}]}'):
            self.file.write_text(bad)
            self.assertEqual(fc_waiting.load(self.proj), {"next": 1, "items": []}, bad)

    def test_stoppers_come_first_then_the_oldest(self):
        def open_at(stamp, *args):
            with mock.patch("time.strftime", lambda fmt: stamp):
                fc_waiting.open_item(self.proj, *args)
        open_at("2026-01-01 10:00:00", "confirm", "c1", "confirm late")    # W1, not a stopper, opened 10:00
        open_at("2026-01-01 09:00:00", "review", "r1", "review early")     # W2, not a stopper, 09:00
        open_at("2026-01-01 09:00:00", "go", "g1", "go early")             # W3, stopper, 09:00
        open_at("2026-01-01 09:00:00", "attend", "a1", "attend early")     # W4, stopper, 09:00
        open_at("2026-01-01 09:00:00", "answer", "q1", "answer")           # W5, stopper, 09:00
        self.assertEqual([i["id"] for i in fc_waiting.open_items(self.proj)], ["W3", "W4", "W5", "W2", "W1"])
        for n in range(6, 11):  # ids sort as numbers: W10 after W9 within one second
            open_at("2026-01-01 08:00:00", "go", f"n{n}", "go")
        self.assertEqual([i["id"] for i in fc_waiting.open_items(self.proj)][:6], ["W6", "W7", "W8", "W9", "W10", "W3"])


class Conditions(unittest.TestCase):
    """What waits is read from files compose already writes: the team lead's and the manager's teammates' reports, the payment halt, the last verify."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        self.crew = self.proj / "results" / "crew"
        self.crew.mkdir(parents=True)
        self.lead = "hw-orchestrator"
        self.stance = mock.patch("fc_tasks.push_stance", return_value="ask")
        self.stance.start()
        self.addCleanup(self.stance.stop)

    def report(self, name, state, message, at="2026-11-17 10:00:00"):
        with open(self.crew / f"{name}.status", "a") as f:
            f.write(json.dumps({"at": at, "state": state, "message": message}) + "\n")

    def record(self, name, reports_to):
        (self.crew / f"{name}.json").write_text(json.dumps({"name": name, "reports_to": reports_to, "pane": "p1"}))

    def verify(self, **data):
        (self.proj / "results" / "verify.json").write_text(json.dumps(data))

    def stored(self):
        return json.loads((self.proj / "results" / "waiting.json").read_text())["items"]

    def test_a_needs_user_that_reaches_the_manager_is_an_answer_item(self):
        self.report(self.lead, "needs-user", "NEB-1: 2 questions Q1, Q2; recommendation: SQLite and Clerk")
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual((item["kind"], item["line"], item["recommendation"], item["ids"], item["stops"]),
                         ("answer", "NEB-1: 2 questions Q1, Q2", "SQLite and Clerk", ["Q1", "Q2"], True))
        self.assertEqual(item["key"], f"report:{self.lead}:2026-11-17 10:00:00")

    def test_a_teammates_ask_to_its_team_lead_is_no_item(self):
        self.record("hw-tests-001", "orchestrator")
        self.report("hw-tests-001", "needs-user", "which port?")
        self.assertEqual(fc_waiting.sync(self.proj), [])

    def test_a_planner_the_manager_started_counts(self):
        self.record("hw-planner", "manager")
        self.report("hw-planner", "blocked", "no Linear key")
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual((item["kind"], item["line"], item["recommendation"]), ("attend", "no Linear key", ""))

    def test_the_reporters_next_report_closes_it(self):
        self.report(self.lead, "needs-user", "which database?")
        self.assertEqual(len(fc_waiting.sync(self.proj)), 1)
        self.report(self.lead, "working", "carrying on", at="2026-11-17 10:05:00")
        self.assertEqual(fc_waiting.sync(self.proj), [])
        self.assertEqual(self.stored()[0]["closed_by"], "settled")

    def test_a_second_ask_is_a_new_unseen_item(self):
        self.report(self.lead, "needs-user", "first?")
        fc_waiting.sync(self.proj)
        fc_waiting.mark_seen(self.proj, "W1")
        self.report(self.lead, "needs-user", "second?", at="2026-11-17 10:05:00")
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual((item["id"], item["line"], item["seen"]), ("W2", "second?", None))

    def test_a_status_line_without_a_time_keys_on_the_name_alone(self):
        (self.crew / f"{self.lead}.status").write_text(json.dumps({"state": "needs-user", "message": "which?"}) + "\n")
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual(item["key"], f"report:{self.lead}:")

    def test_a_payment_halt_waits_until_resume(self):
        resume = self.proj / "results" / "resume.json"
        resume.write_text(json.dumps({"halted_at": "2026-11-17 10:00:00", "provider": "openrouter", "remaining": ["003", "004"]}))
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual((item["kind"], item["key"], item["line"], item["recommendation"]),
                         ("attend", "halt:2026-11-17 10:00:00", "payment halt at openrouter: 2 task(s) left", "top up, then compose resume"))
        resume.unlink()
        self.assertEqual(fc_waiting.sync(self.proj), [])
        self.assertEqual(self.stored()[0]["closed_by"], "settled")

    def test_a_passed_verify_that_waits_for_the_push_is_a_go_item(self):
        self.verify(passed=True, head="abc")
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual((item["kind"], item["key"], item["line"], item["recommendation"], item["stops"]),
                         ("go", "go:abc", "the verified stack waits for your go to push it as draft PRs", "compose stack --submit --project hw", True))
        (self.proj / "layers.json").write_text("{}")
        os.utime(self.proj / "layers.json", (time.time() + 60, time.time() + 60))
        self.assertEqual(fc_waiting.sync(self.proj), [])

    def test_a_verify_without_a_head_keys_on_its_time(self):
        self.verify(passed=True)
        os.utime(self.proj / "results" / "verify.json", (1700000000.9, 1700000000.9))
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual(item["key"], "go:1700000000")

    def test_no_go_item_when_someone_else_pushes(self):
        self.verify(passed=True, head="abc")
        for who in ("auto", "posture"):
            with mock.patch("fc_tasks.push_stance", return_value=who):
                self.assertEqual(fc_waiting.sync(self.proj), [], who)

    def test_a_no_stays_closed_and_a_new_head_asks_again(self):
        self.verify(passed=True, head="abc")
        fc_waiting.sync(self.proj)
        fc_waiting.close_item(self.proj, "go:abc", "no: later")
        self.assertEqual(fc_waiting.sync(self.proj), [])
        self.verify(passed=True, head="def")
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["W2"])

    def test_a_condition_that_comes_back_after_settling_is_a_new_item(self):
        self.verify(passed=True, head="abc")
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["W1"])
        self.verify(passed=False, head="abc")
        self.assertEqual(fc_waiting.sync(self.proj), [])
        self.assertEqual(self.stored()[0]["closed_by"], "settled")
        self.verify(passed=True, head="abc")
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["W2"])

    def test_an_agent_at_a_prompt_opens_by_its_event_and_closes_when_it_moves(self):
        with mock.patch("fc_fleet.agent_status", return_value="blocked"):
            [item] = fc_waiting.sync(self.proj, ["event agent=hw-003 status=blocked waits-at='sign-in'"])
        self.assertEqual((item["kind"], item["key"], item["line"]), ("attend", "agent:hw-003", "hw-003 waits at a prompt only you can answer (a sign-in or an Allow)"))
        with mock.patch("fc_fleet.agent_status", return_value="working"):
            self.assertEqual(fc_waiting.sync(self.proj), [])

    def test_the_host_is_asked_only_when_an_agent_item_is_open(self):
        self.report(self.lead, "needs-user", "which?")
        with mock.patch("fc_fleet.agent_status", side_effect=AssertionError("asked the host")):
            self.assertEqual(len(fc_waiting.sync(self.proj, ["event agent=hw-003 status=working"])), 1)

    def test_decisions_and_reviews_are_left_alone(self):
        fc_waiting.open_item(self.proj, "confirm", "decision:NEB-1:D7", "D7: dark theme")
        fc_waiting.open_item(self.proj, "review", "review:NEB-1", "review the plan")
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["W1", "W2"])

    def test_sync_on_a_broken_file_still_returns_what_waits(self):
        (self.proj / "results" / "waiting.json").write_text("{")
        self.report(self.lead, "needs-user", "which?")
        self.assertEqual(len(fc_waiting.sync(self.proj)), 1)
        self.assertEqual(len(self.stored()), 1)

    def test_a_crew_record_that_cannot_be_read_closes_nothing(self):
        self.record("hw-planner", "manager")
        self.report("hw-planner", "blocked", "no Linear key")
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["W1"])
        (self.crew / "hw-planner.json").write_text("{")
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["W1"])
        self.assertIsNone(self.stored()[0]["closed"])

    def test_a_source_that_cannot_be_read_closes_nothing(self):
        self.report(self.lead, "needs-user", "which database?")
        self.assertEqual(len(fc_waiting.sync(self.proj)), 1)
        status = self.crew / f"{self.lead}.status"
        status.write_text(status.read_text()[:-30])  # the last line is cut in half
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["W1"])
        self.assertIsNone(self.stored()[0]["closed"])


if __name__ == "__main__":
    unittest.main()
