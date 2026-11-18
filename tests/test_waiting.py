import hermetic  # first: no test sees the real home
import argparse, contextlib, importlib.machinery, importlib.util, io, json, os, re, sys, tempfile, time, unittest
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
        self.assertEqual((a["id"], b["id"]), ("WAIT1", "WAIT2"))
        self.assertEqual(len(a["line"]), 120)
        self.assertTrue(a["line"].startswith("which database? x"))
        self.assertTrue(a["line"].endswith("..."))
        self.assertEqual((a["stops"], b["stops"]), (True, False))
        self.assertEqual((a["recommendation"], a["ids"], a["seen"], a["closed"], a["closed_by"]), ("SQLite", ["Q1"], None, None, None))
        self.assertEqual(len(json.loads(self.file.read_text())["items"]), 2)

    def test_the_same_key_is_one_item(self):
        a = fc_waiting.open_item(self.proj, "go", "k", "wait for the go")
        b = fc_waiting.open_item(self.proj, "go", "k", "wait for the go again")
        self.assertEqual((a["id"], b["id"]), ("WAIT1", "WAIT1"))
        self.assertEqual(len(json.loads(self.file.read_text())["items"]), 1)

    def test_a_closed_item_leaves_the_open_list_and_its_id_is_not_reused(self):
        fc_waiting.open_item(self.proj, "go", "k", "wait for the go")
        self.assertTrue(fc_waiting.close_item(self.proj, "k", "settled"))
        self.assertFalse(fc_waiting.close_item(self.proj, "k", "settled"))
        self.assertEqual(fc_waiting.open_items(self.proj), [])
        closed = json.loads(self.file.read_text())["items"][0]
        self.assertEqual(closed["closed_by"], "settled")
        self.assertTrue(closed["closed"])
        self.assertEqual(fc_waiting.open_item(self.proj, "go", "k2", "again")["id"], "WAIT2")

    def test_seen_keeps_it_open(self):
        fc_waiting.open_item(self.proj, "confirm", "k", "D1: a thing")
        self.assertTrue(fc_waiting.mark_seen(self.proj, "WAIT1")["seen"])
        self.assertEqual([i["id"] for i in fc_waiting.open_items(self.proj)], ["WAIT1"])
        self.assertIsNone(fc_waiting.mark_seen(self.proj, "WAIT9"))

    def test_a_waiting_id_is_never_read_as_a_question_id(self):
        item = fc_waiting.open_item(self.proj, "confirm", "decision:NEB-1:D7", "D7: dark theme")
        self.assertEqual(item["id"], "WAIT1")
        self.assertIsNone(re.fullmatch(fc.fc_ask.CARD_ID, item["id"]))  # a map may number its questions W1, W2, ...: an item never looks like one
        self.assertEqual(fc.fc_ask.question_ids(f"NEB-1: 1 questions {item['id']}"), [])
        self.assertIsNone(fc.fc_decide.ANSWER.match(f"{item['id']}: yes"))  # nor is `decide "WAIT1: yes"` an answer to a question

    def test_an_answered_question_closes_its_item_at_once(self):
        fc_waiting.open_item(self.proj, "answer", "report:a:t1", "which one?", ids=["Q1", "Q2"])
        fc_waiting.open_item(self.proj, "answer", "report:b:t2", "no ids here")
        self.assertEqual(fc_waiting.answered(self.proj, ["Q1"]), [])
        self.assertEqual(fc_waiting.open_items(self.proj)[0]["ids"], ["Q2"])
        self.assertEqual(fc_waiting.answered(self.proj, ["Q2"]), ["WAIT1"])
        items = {i["id"]: i for i in json.loads(self.file.read_text())["items"]}
        self.assertEqual(items["WAIT1"]["closed_by"], "decided")
        self.assertIsNone(items["WAIT2"]["closed"])
        self.assertEqual(fc_waiting.answered(self.proj, ["Q1", "Q2"]), [])

    def test_an_ask_of_no_known_issue_loses_only_the_ids_a_decide_settled(self):
        fc_waiting.open_item(self.proj, "answer", "report:a:t1", "which ones? 2 questions Q1, Q2", ids=["Q1", "Q2"], issue=None)
        fc_waiting.open_item(self.proj, "answer", "report:b:t2", "NEB-1: 2 questions Q1, Q2", ids=["Q1", "Q2"], issue="NEB-1")
        self.assertEqual(fc_waiting.answered(self.proj, ["Q1"], "NEB-9"), [])  # an unrelated map
        self.assertEqual(self.ids(), {"report:a:t1": ["Q2"], "report:b:t2": ["Q1", "Q2"]})
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-1", [], {"NEB-1": {"Q2"}}.get), [])  # the sweep reaches only that map's own asks
        self.assertEqual(self.ids(), {"report:a:t1": ["Q2"], "report:b:t2": ["Q2"]})

    def ids(self):
        """key -> the question ids each ask still holds."""
        return {i["key"]: i["ids"] for i in fc_waiting.load(self.proj)["items"]}

    def story_ask(self, ids, issue="NEB-2", feature="NEB-1"):
        """An ask headed by a story of the feature: `report` let it through on the story's map or the feature's."""
        fc_waiting.open_item(self.proj, "answer", "report:a:t1", f"{issue}: questions {', '.join(ids)}", ids=ids, issue=issue)
        (self.proj / "linear.json").write_text(json.dumps({"feature": feature}))

    def test_an_ask_of_the_decided_map_keeps_a_question_that_is_open_on_the_feature(self):
        self.story_ask(["Q2", "Q7"])  # Q2 is written on the feature only, Q7 on no map any more
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-2", [], {"NEB-2": set(), "NEB-1": {"Q2"}}.get), [])
        self.assertEqual(self.ids(), {"report:a:t1": ["Q2"]})
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-2", [], {"NEB-2": set(), "NEB-1": set()}.get), ["WAIT1"])  # the feature settled it since

    def test_an_ask_of_the_decided_map_loses_what_was_settled_though_a_late_read_of_the_feature_still_shows_it(self):
        self.story_ask(["Q2", "Q3"])
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-2", ["Q2"], {"NEB-2": set(), "NEB-1": {"Q2", "Q3"}}.get), [])
        self.assertEqual(self.ids(), {"report:a:t1": ["Q3"]})

    def test_an_ask_of_the_decided_map_keeps_what_a_feature_that_cannot_be_read_may_hold(self):
        self.story_ask(["Q2"])
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-2", [], {"NEB-2": set()}.get), [])
        self.assertEqual(self.ids(), {"report:a:t1": ["Q2"]})

    def test_an_ask_of_another_issue_closes_when_the_question_it_names_was_only_on_the_decided_map(self):
        self.story_ask(["Q2"])  # the story's own map never held Q2: the ask was let through on the feature's
        self.assertEqual(fc_waiting.answered(self.proj, ["Q2"], "NEB-1"), [])  # not an ask of that map
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-1", ["Q2"], {"NEB-1": set(), "NEB-2": set()}.get), ["WAIT1"])
        self.assertEqual(fc_waiting.load(self.proj)["items"][0]["closed_by"], "decided")

    def test_an_ask_of_another_issue_keeps_a_question_its_own_map_still_holds(self):
        self.story_ask(["Q1", "Q2"], issue="NEB-9")  # every map starts at Q1: NEB-9's Q1 is another question
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-1", ["Q1"], {"NEB-1": {"Q2"}, "NEB-9": {"Q1", "Q2"}}.get), [])
        self.assertEqual(self.ids(), {"report:a:t1": ["Q1", "Q2"]})
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-1", ["Q1"], {"NEB-1": {"Q2"}}.get), [])  # a map that cannot be read: it stays
        self.assertEqual(self.ids(), {"report:a:t1": ["Q1", "Q2"]})
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-1", ["Q1"], {"NEB-1": {"Q2"}, "NEB-9": {"Q2"}}.get), [])  # its map no longer holds Q1
        self.assertEqual(self.ids(), {"report:a:t1": ["Q2"]})

    def test_an_ask_of_another_issue_also_loses_an_id_that_was_decided_before_it_was_asked(self):
        self.story_ask(["Q1", "Q2"])  # `report` lets an ask through on one open question: Q1 was decided on the feature already
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-1", ["Q2"], {"NEB-1": set(), "NEB-2": set()}.get), ["WAIT1"])

    def test_an_ask_of_a_map_the_decide_cleared_loses_what_was_settled_whatever_a_late_read_shows(self):
        self.story_ask(["Q1", "Q2", "Q3"])  # Q1 decided before the ask, Q2 settled now and cleared from the story's map, Q3 still open on it
        late = {"NEB-1": set(), "NEB-2": {"Q2", "Q3"}}  # the read of the story's map is behind the write that cleared Q2
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-1", ["Q2"], late.get, cleared=["NEB-2"]), [])
        self.assertEqual(self.ids(), {"report:a:t1": ["Q3"]})

    def test_an_ask_of_a_cleared_map_that_cannot_be_read_loses_only_what_was_settled(self):
        self.story_ask(["Q1", "Q2"])
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-1", ["Q2"], {"NEB-1": set()}.get, cleared=["NEB-2"]), [])
        self.assertEqual(self.ids(), {"report:a:t1": ["Q1"]})

    def test_no_map_is_read_while_the_list_is_locked(self):
        self.story_ask(["Q2"])
        held, lock = [], fc_waiting._locked
        @contextlib.contextmanager
        def locked(proj):
            with lock(proj):
                held.append(proj)
                try:
                    yield
                finally:
                    held.pop()
        def open_on(issue):
            self.assertEqual(held, [], f"{issue} was read under the lock: a slow Linear would hold up the prompt hook, the watch and ack")
            return set()
        with mock.patch.object(fc_waiting, "_locked", locked):
            self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-1", ["Q2"], open_on), ["WAIT1"])

    def test_an_ask_of_another_issue_that_names_nothing_settled_is_not_read(self):
        self.story_ask(["Q5"], issue="NEB-9")
        read = []
        self.assertEqual(fc_waiting.settle_asks(self.proj, "NEB-1", ["Q1"], lambda issue: read.append(issue) or set()), [])
        self.assertEqual((read, self.ids()), ([], {"report:a:t1": ["Q5"]}))

    def test_an_unreadable_file_reads_as_empty_and_the_next_write_replaces_it(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("{")
        self.assertEqual(fc_waiting.open_items(self.proj), [])
        self.assertEqual(self.file.read_text(), "{")
        self.assertEqual(fc_waiting.open_item(self.proj, "go", "k", "go?")["id"], "WAIT1")
        self.assertEqual(json.loads(self.file.read_text())["items"][0]["id"], "WAIT1")

    def test_a_file_that_is_not_a_store_reads_as_empty(self):
        self.file.parent.mkdir(parents=True)
        for bad in ('[]', '{"next": "3", "items": []}', '{"next": 3, "items": {}}', '{"next": 3, "items": [1]}', '{"next": 3, "items": [{"id": "WAIT1"}]}',
                    '{"next": 3, "items": [{"id": "WAIT1", "key": "k", "line": "l", "stops": true}]}',  # no kind: every reader would crash on it
                    '{"next": 3, "items": [{"id": "WAIT1", "key": "k", "kind": "go", "stops": true}]}',
                    '{"next": 3, "items": [{"id": "WAIT1", "key": "k", "kind": "go", "line": "l"}]}'):
            self.file.write_text(bad)
            self.assertEqual(fc_waiting.load(self.proj), {"next": 1, "items": []}, bad)

    def test_stoppers_come_first_then_the_oldest(self):
        def open_at(stamp, *args):
            with mock.patch("time.strftime", lambda fmt: stamp):
                fc_waiting.open_item(self.proj, *args)
        open_at("2026-01-01 10:00:00", "confirm", "c1", "confirm late")    # WAIT1, not a stopper, opened 10:00
        open_at("2026-01-01 09:00:00", "review", "r1", "review early")     # WAIT2, not a stopper, 09:00
        open_at("2026-01-01 09:00:00", "go", "g1", "go early")             # WAIT3, stopper, 09:00
        open_at("2026-01-01 09:00:00", "attend", "a1", "attend early")     # WAIT4, stopper, 09:00
        open_at("2026-01-01 09:00:00", "answer", "q1", "answer")           # WAIT5, stopper, 09:00
        self.assertEqual([i["id"] for i in fc_waiting.open_items(self.proj)], ["WAIT3", "WAIT4", "WAIT5", "WAIT2", "WAIT1"])
        for n in range(6, 11):  # ids sort as numbers: WAIT10 after WAIT9 within one second
            open_at("2026-01-01 08:00:00", "go", f"n{n}", "go")
        self.assertEqual([i["id"] for i in fc_waiting.open_items(self.proj)][:6], ["WAIT6", "WAIT7", "WAIT8", "WAIT9", "WAIT10", "WAIT3"])


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
        with contextlib.redirect_stderr(io.StringIO()) as err:
            [item] = fc_waiting.sync(self.proj)
        self.assertEqual(err.getvalue(), "")
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

    def test_an_ask_that_names_questions_stays_until_they_are_decided(self):
        self.report(self.lead, "needs-user", "NEB-1: 2 questions Q1, Q2")
        fc_waiting.sync(self.proj)
        self.report(self.lead, "working", "carrying on", at="2026-11-17 10:05:00")
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual((item["id"], item["ids"]), ("WAIT1", ["Q1", "Q2"]))
        self.assertEqual(fc_waiting.answered(self.proj, ["Q1", "Q2"], "NEB-1"), ["WAIT1"])
        self.assertEqual(fc_waiting.sync(self.proj), [])

    def test_a_reminder_of_an_open_ask_is_the_same_item(self):
        self.report(self.lead, "needs-user", "NEB-1: 2 questions Q1, Q2")
        fc_waiting.sync(self.proj)
        self.report(self.lead, "needs-user", "reminder: NEB-1: 2 questions Q1, Q2", at="2026-11-17 11:00:00")
        self.assertEqual([(i["id"], i["ids"]) for i in fc_waiting.sync(self.proj)], [("WAIT1", ["Q1", "Q2"])])
        self.report(self.lead, "needs-user", "NEB-1: 2 questions Q2, Q3", at="2026-11-17 11:05:00")  # a new question: a new ask
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["WAIT1", "WAIT2"])

    def test_an_item_closed_by_a_decision_stays_closed_while_its_report_is_still_the_last_line(self):
        self.report(self.lead, "needs-user", "NEB-1: 1 question Q1")
        fc_waiting.sync(self.proj)
        fc_waiting.answered(self.proj, ["Q1"], "NEB-1")
        self.assertEqual(fc_waiting.sync(self.proj), [])
        self.assertEqual([(i["id"], i["closed_by"]) for i in self.stored()], [("WAIT1", "decided")])

    def test_an_ask_the_watch_saw_opens_though_the_reporter_reported_again_since(self):
        self.report(self.lead, "needs-user", "NEB-1: 2 questions Q1, Q2; recommendation: SQLite")
        self.report(self.lead, "done", "handed over", at="2026-11-17 10:00:05")
        event = f"event crew={self.lead} state=needs-user message={'NEB-1: 2 questions Q1, Q2; recommendation: SQLite'!r}  yolo=on (answer it)"
        [item] = fc_waiting.sync(self.proj, [event])
        self.assertEqual((item["key"], item["ids"], item["line"], item["issue"]), (f"report:{self.lead}:2026-11-17 10:00:00", ["Q1", "Q2"], "NEB-1: 2 questions Q1, Q2", "NEB-1"))
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj, [event])], ["WAIT1"])  # the same ask read again is the same item

    def test_an_ask_without_an_issue_at_its_head_is_about_the_feature_else_no_known_issue(self):
        self.report(self.lead, "needs-user", "2 questions Q1, Q2")
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-7"}))
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual(item["issue"], "NEB-7")
        self.assertEqual(fc_waiting._issue(self.proj, "reminder: change C2: NEB-9: 1 question C2-1"), "NEB-9")
        for broken in ("{", "[]", json.dumps({"feature": ""})):
            (self.proj / "linear.json").write_text(broken)
            self.assertIsNone(fc_waiting._issue(self.proj, "2 questions Q1, Q2"), broken)
        (self.proj / "linear.json").unlink()
        self.assertIsNone(fc_waiting._issue(self.proj, "2 questions Q1, Q2"))

    def test_an_ask_the_watch_saw_with_no_status_line_opens_under_the_time_now(self):
        with mock.patch.object(fc_waiting, "_stamp", return_value="2026-11-17 11:00:00"):
            [item] = fc_waiting.sync(self.proj, [f"event crew={self.lead} state=needs-user message={'NEB-1: 1 question Q4'!r}"])
        self.assertEqual(item["key"], f"report:{self.lead}:2026-11-17 11:00:00")

    def test_an_event_ask_that_names_no_question_or_reaches_no_one_opens_nothing(self):
        self.record("hw-tests-001", "orchestrator")
        for e in (f"event crew={self.lead} state=needs-user message={'which port?'!r}",
                  f"event crew=hw-tests-001 state=needs-user message={'NEB-1: 1 question Q1'!r}"):
            self.assertEqual(fc_waiting.sync(self.proj, [e]), [], e)

    def test_a_closed_teammates_last_ask_waits_only_when_it_names_questions(self):
        (self.crew / "hw-planner.json").write_text(json.dumps({"name": "hw-planner", "reports_to": "manager", "closed": {"at": "t", "why": "done"}}))
        self.report("hw-planner", "needs-user", "which database?")
        self.assertEqual(fc_waiting.sync(self.proj), [])
        self.report("hw-planner", "needs-user", "NEB-1: 1 question Q1", at="2026-11-17 10:05:00")
        self.assertEqual([i["ids"] for i in fc_waiting.sync(self.proj)], [["Q1"]])

    def test_a_crew_record_that_is_not_an_object_is_a_read_error(self):
        self.record("hw-planner", "manager")
        self.report("hw-planner", "blocked", "no Linear key")
        fc_waiting.sync(self.proj)
        (self.crew / "hw-planner.json").write_text("[]")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["WAIT1"])
        self.assertTrue(err.getvalue().startswith("waiting list not updated ("))
        self.assertIsNone(fc_waiting._reporter_pane(self.proj, "report:hw-planner:t"))
        with self.assertRaises(ValueError):
            fc_waiting._crew_record(self.proj, "hw-planner")
        self.assertIsNone(fc_waiting._crew_record(self.proj, "hw-nobody"))

    def test_a_second_ask_is_a_new_unseen_item(self):
        self.report(self.lead, "needs-user", "first?")
        fc_waiting.sync(self.proj)
        fc_waiting.mark_seen(self.proj, "WAIT1")
        self.report(self.lead, "needs-user", "second?", at="2026-11-17 10:05:00")
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual((item["id"], item["line"], item["seen"]), ("WAIT2", "second?", None))

    def test_a_status_line_without_a_time_keys_on_the_name_alone(self):
        (self.crew / f"{self.lead}.status").write_text(json.dumps({"state": "needs-user", "message": "which?"}) + "\n")
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual(item["key"], f"report:{self.lead}:")

    def test_an_ask_the_watch_saw_on_a_line_without_a_time_is_the_same_item(self):
        (self.crew / f"{self.lead}.status").write_text(json.dumps({"state": "needs-user", "message": "NEB-1: 1 question Q1"}) + "\n")
        [item] = fc_waiting.sync(self.proj, [f"event crew={self.lead} state=needs-user message={'NEB-1: 1 question Q1'!r}"])
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

    def test_a_halt_waits_on_no_one_while_its_resume_runs_and_again_once_the_resume_died(self):
        resume = self.proj / "results" / "resume.json"
        halt = {"halted_at": "2026-11-17 10:00:00", "provider": "openrouter", "remaining": ["003"]}
        resume.write_text(json.dumps({**halt, "resuming": os.getpid()}))
        self.assertEqual(fc_waiting.sync(self.proj), [])
        resume.write_text(json.dumps({**halt, "resuming": 999999}))  # the resume died: the file stays, with a dead pid
        with mock.patch("fc_tasks.pid_alive", return_value=False):
            [item] = fc_waiting.sync(self.proj)
        self.assertEqual(item["key"], "halt:2026-11-17 10:00:00")

    def test_a_halt_with_no_task_left_names_no_count(self):
        (self.proj / "results" / "resume.json").write_text(json.dumps({"halted_at": "t", "provider": "openrouter", "remaining": []}))
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual(item["line"], "payment halt at openrouter")

    def test_a_passed_verify_that_waits_for_the_push_is_a_go_item(self):
        self.verify(passed=True, head="abc")
        [item] = fc_waiting.sync(self.proj)
        self.assertEqual((item["kind"], item["key"], item["line"], item["recommendation"], item["stops"]),
                         ("go", "go:abc", "the verified stack waits for your go to push it as draft PRs", "compose stack --submit --project hw", True))
        (self.proj / "layers.json").write_text("{}")
        os.utime(self.proj / "layers.json", (time.time() + 60, time.time() + 60))
        self.assertEqual(fc_waiting.sync(self.proj), [])

    def test_a_pushed_head_is_not_asked_again_by_a_second_verify(self):
        self.verify(passed=True, head="abc")
        fc_waiting.sync(self.proj)
        (self.proj / "layers.json").write_text("{}")
        os.utime(self.proj / "layers.json", (time.time() + 60, time.time() + 60))
        self.assertEqual(fc_waiting.sync(self.proj), [])
        self.assertEqual(self.stored()[0]["closed_by"], "pushed")
        self.verify(passed=True, head="abc")  # verified again, newer than the push
        os.utime(self.proj / "results" / "verify.json", (time.time() + 120, time.time() + 120))
        self.assertEqual(fc_waiting.sync(self.proj), [])
        self.verify(passed=True, head="def")
        os.utime(self.proj / "results" / "verify.json", (time.time() + 120, time.time() + 120))
        self.assertEqual([i["key"] for i in fc_waiting.sync(self.proj)], ["go:def"])

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
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["WAIT2"])

    def test_a_condition_that_comes_back_after_settling_is_a_new_item(self):
        self.verify(passed=True, head="abc")
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["WAIT1"])
        self.verify(passed=False, head="abc")
        self.assertEqual(fc_waiting.sync(self.proj), [])
        self.assertEqual(self.stored()[0]["closed_by"], "settled")
        self.verify(passed=True, head="abc")
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["WAIT2"])

    def host(self, **status):
        """fc_fleet.agents patched to list these agents with these statuses (None: the host did not answer)."""
        listed = None if status.get("none") else [{"name": n.replace("_", "-"), "agent_status": s} for n, s in status.items()]
        return mock.patch("fc_fleet.agents", return_value=listed)

    def test_an_agent_at_a_prompt_opens_by_its_event_and_closes_when_it_moves(self):
        with self.host(hw_003="blocked"):
            [item] = fc_waiting.sync(self.proj, ["event agent=hw-003 status=blocked waits-at='Allow Frame0 to open?'  (read: compose crew read ...)"])
        self.assertEqual((item["kind"], item["key"], item["line"]), ("attend", "agent:hw-003", "hw-003 waits at a prompt: Allow Frame0 to open?"))
        with self.host(hw_003="working"):
            self.assertEqual(fc_waiting.sync(self.proj), [])

    def test_an_agent_event_without_its_dialog_says_only_that_it_waits(self):
        with self.host(hw_003="blocked"):
            [item] = fc_waiting.sync(self.proj, ["event agent=hw-003 status=blocked  (read: compose crew read ...)"])
        self.assertEqual(item["line"], "hw-003 waits at a prompt")

    def test_an_agent_the_host_no_longer_lists_closes_and_a_host_that_does_not_answer_closes_nothing(self):
        with self.host(hw_003="blocked"):
            fc_waiting.sync(self.proj, ["event agent=hw-003 status=blocked"])
        with self.host(none=True):
            self.assertEqual([i["key"] for i in fc_waiting.sync(self.proj)], ["agent:hw-003"])
        with self.host(hw_004="blocked"):
            self.assertEqual(fc_waiting.sync(self.proj), [])

    def test_an_agent_event_opens_its_item_though_a_source_cannot_be_read(self):
        (self.crew / f"{self.lead}.status").write_text("{")
        with self.host(hw_003="blocked"), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual([i["key"] for i in fc_waiting.sync(self.proj, ["event agent=hw-003 status=blocked"])], ["agent:hw-003"])
        self.assertTrue(err.getvalue().startswith("waiting list not updated ("))

    def test_a_sync_that_does_not_probe_leaves_an_agent_item_alone(self):
        with self.host(hw_003="blocked"):
            fc_waiting.sync(self.proj, ["event agent=hw-003 status=blocked waits-at='sign-in'"])
        with mock.patch("fc_fleet.agents", side_effect=AssertionError("host asked")), mock.patch("fc_fleet.agent_status", side_effect=AssertionError("host asked")):
            self.assertEqual([i["key"] for i in fc_waiting.sync(self.proj, probe=False)], ["agent:hw-003"])
        with self.host(hw_003="working"):
            self.assertEqual(fc_waiting.sync(self.proj), [])

    def test_the_host_is_asked_only_when_an_agent_item_is_open(self):
        self.report(self.lead, "needs-user", "which?")
        with mock.patch("fc_fleet.agents", side_effect=AssertionError("asked the host")):
            self.assertEqual(len(fc_waiting.sync(self.proj, ["event agent=hw-003 status=working"])), 1)

    def test_decisions_and_reviews_are_left_alone(self):
        fc_waiting.open_item(self.proj, "confirm", "decision:NEB-1:D7", "D7: dark theme")
        fc_waiting.open_item(self.proj, "review", "review:NEB-1", "review the plan")
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["WAIT1", "WAIT2"])

    def test_sync_on_a_broken_file_still_returns_what_waits(self):
        (self.proj / "results" / "waiting.json").write_text("{")
        self.report(self.lead, "needs-user", "which?")
        self.assertEqual(len(fc_waiting.sync(self.proj)), 1)
        self.assertEqual(len(self.stored()), 1)

    def test_a_crew_record_that_cannot_be_read_closes_nothing(self):
        self.record("hw-planner", "manager")
        self.report("hw-planner", "blocked", "no Linear key")
        self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["WAIT1"])
        (self.crew / "hw-planner.json").write_text("{")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["WAIT1"])
        self.assertIsNone(self.stored()[0]["closed"])

    def test_a_source_that_cannot_be_read_closes_nothing(self):
        self.report(self.lead, "needs-user", "which database?")
        self.assertEqual(len(fc_waiting.sync(self.proj)), 1)
        status = self.crew / f"{self.lead}.status"
        status.write_text(status.read_text()[:-30])  # the last line is cut in half
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual([i["id"] for i in fc_waiting.sync(self.proj)], ["WAIT1"])
        self.assertIsNone(self.stored()[0]["closed"])
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertTrue(err.getvalue().startswith("waiting list not updated ("))


class Commands(unittest.TestCase):
    """`compose waiting` reads every project of the repo (or this session's), `compose ack` marks one seen, `compose waiting no` closes a go."""
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.hw, self.shop = root / "hw", root / "shop"
        for p in (self.hw, self.shop):
            (p / "results" / "crew").mkdir(parents=True)
        self.projects = {"hw": (root, self.hw), "shop": (root, self.shop)}
        self.records = {}
        for target, new in (("fc_core.all_projects", mock.Mock(side_effect=lambda repo: dict(self.projects))),
                            ("fc_fleet.manager_record", mock.Mock(side_effect=lambda proj: self.records.get(Path(proj).name, {}))),
                            ("fc_fleet.pane_label", mock.Mock())):
            patch = mock.patch.object(fc.__dict__[target.split(".")[0]], target.split(".")[1], new)
            self.addCleanup(patch.stop)
            setattr(self, target.split(".")[1], patch.start())
        env = mock.patch.dict(os.environ, {"HERDR_PANE_ID": "", "HC_TERMINAL_ID": "", "REX_BLOCK": "", "COMPOSE_NO_HERDR": ""})
        env.start()
        self.addCleanup(env.stop)

    def args(self, **kw):
        base = dict(repo=".", project=None, words=[], footer=False, all=False, quote=None, id=None)
        return argparse.Namespace(**{**base, **kw})

    def run_cmd(self, fn, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(self.args(**kw))
        return out.getvalue().splitlines()

    def open(self, proj, kind, key, line="a line", rec=""):
        return fc_waiting.open_item(proj, kind, key, line, rec)

    def test_the_list_is_at_most_seven_lines(self):
        for n in range(9):
            self.open(self.hw, "confirm", f"decision:NEB-1:D{n}", f"D{n}: a thing")
        items = fc_waiting.gather(".")
        self.assertEqual(len(items), 9)
        lines = fc_waiting.listing(items)
        self.assertEqual(len(lines), 7)
        self.assertEqual(lines[-1], "+3 more: compose waiting --all")
        self.assertEqual(len(fc_waiting.listing(items, cap=None)), 9)
        self.assertEqual(fc_waiting.listing([]), ["nothing waits on you"])
        self.assertEqual(self.run_cmd(fc.fc_waiting.cmd_waiting)[-1], "+3 more: compose waiting --all")
        self.assertEqual(len(self.run_cmd(fc.fc_waiting.cmd_waiting, all=True)), 9)

    def test_a_line_names_the_item_its_age_and_its_recommendation(self):
        item = self.open(self.hw, "answer", "k1", "which database?", "SQLite")
        self.open(self.shop, "confirm", "k2", "D1: a thing")
        fc_waiting.mark_seen(self.shop, "WAIT1")
        now = time.time()
        for age, text in ((0, "0m"), (5 * 60, "5m"), (2 * 3600 + 10 * 60, "2h 10m"), (3 * 86400 + 3600, "3d")):
            item["opened"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - age))
            with mock.patch("time.time", return_value=now):
                self.assertEqual(fc_waiting.listing([{**item, "project": "hw"}])[0], f"WAIT1  hw  answer  {text}  which database?  (recommendation: SQLite)")
        seen = fc_waiting.gather(".")
        self.assertEqual([i["project"] for i in seen], ["hw", "shop"])  # the stopper first
        self.assertTrue(fc_waiting.listing(seen)[1].endswith("D1: a thing [seen]"))
        self.assertNotIn("recommendation", fc_waiting.listing(seen)[1])

    def test_the_footer_keeps_a_go_or_attend_recommendation_short_and_leaves_an_asks_out(self):
        self.open(self.hw, "answer", "k1", "which database?", "SQLite, since hosting hangs on it")
        self.open(self.hw, "go", "k2", "the stack waits for your go", "compose stack --submit --project hw")
        self.open(self.hw, "attend", "report:hw-planner:t", "no Linear key", "top up at the provider's page, then run compose resume for the project")
        items = [{**i, "project": "hw"} for i in fc_waiting.open_items(self.hw)]
        self.assertEqual(fc_waiting.footer(items), ["Waiting on you:", "- WAIT1 hw: which database?",
                                                    "- WAIT2 hw: the stack waits for your go (recommendation: compose stack --submit --project hw)",
                                                    "- WAIT3 hw: no Linear key (recommendation: top up at the provider's page, then run compose resume fo...)"])
        self.assertIn("(recommendation: SQLite, since hosting hangs on it)", fc_waiting.listing(fc_waiting.gather("."))[0])  # the manager's own view keeps it

    def test_the_footer_never_cuts_a_command_the_cli_wrote(self):
        slug = "a-project-slug-well-over-twenty-seven"
        go = f"compose stack --submit --project {slug}"
        self.open(self.hw, "go", "go:abc", "the stack waits for your go", go)
        self.open(self.hw, "attend", "report:hw-orchestrator:t", "no Linear key", "add the key to the config, then report again from the team lead pane")
        foot = fc_waiting.footer([{**i, "project": "hw"} for i in fc_waiting.open_items(self.hw)])  # as opened, before a sync reads the files
        self.assertIn(f"- WAIT1 hw: the stack waits for your go (recommendation: {go})", foot)
        self.assertIn("- WAIT2 hw: no Linear key (recommendation: add the key to the config, then report again from the tea...)", foot)

    def test_the_footer_is_at_most_four_lines_and_only_unseen_stoppers(self):
        for n in range(5):
            self.open(self.hw, "answer", f"k{n}", f"question {n}", "yes")
        for n in range(2):
            self.open(self.shop, "confirm", f"d{n}", f"D{n}: a thing")
        foot = fc_waiting.footer(fc_waiting.gather("."))
        self.assertEqual(foot, ["Waiting on you:", "- WAIT1 hw: question 0", "- WAIT2 hw: question 1", "+5 more: compose waiting"])  # an ask is asked open
        self.assertEqual(self.run_cmd(fc.fc_waiting.cmd_waiting, footer=True), foot)
        alone = Path(tempfile.mkdtemp()) / "solo"
        self.projects = {"solo": (alone.parent, alone)}
        for n in range(3):
            self.open(alone, "go", f"g{n}", f"go {n}")
        three = fc_waiting.footer(fc_waiting.gather("."))
        self.assertEqual(three, ["Waiting on you:", "- WAIT1 solo: go 0", "- WAIT2 solo: go 1", "- WAIT3 solo: go 2"])
        for n in range(1, 4):
            fc_waiting.mark_seen(alone, f"WAIT{n}")
        self.assertEqual(fc_waiting.footer(fc_waiting.gather(".")), [])
        self.assertEqual(self.run_cmd(fc.fc_waiting.cmd_waiting, footer=True), [])

    def test_only_this_sessions_projects_are_gathered(self):
        self.open(self.hw, "confirm", "a", "in hw")
        self.open(self.shop, "confirm", "b", "in shop")
        self.records = {"hw": {"session": "w1:p1"}, "shop": {"session": "w9:p9"}}
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1"}):
            self.assertEqual([i["project"] for i in fc_waiting.gather(".")], ["hw"])
            self.assertEqual([i["project"] for i in fc_waiting.gather(".", project="shop")], ["shop"])
        self.assertEqual(sorted(i["project"] for i in fc_waiting.gather(".")), ["hw", "shop"])
        self.records = {"hw": {"session": "w1:p1"}}  # shop has no record
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1"}):
            self.assertEqual(sorted(i["project"] for i in fc_waiting.gather(".")), ["hw", "shop"])
        with self.assertRaises(SystemExit) as e:
            fc_waiting.gather(".", project="nope")
        self.assertIn("hw", str(e.exception))
        self.assertIn("shop", str(e.exception))

    def test_ack_marks_seen_and_relabels_the_reporters_pane(self):
        crew = self.hw / "results" / "crew"
        (crew / "hw-planner.status").write_text(json.dumps({"at": "2026-11-17 10:00:00", "state": "needs-user", "message": "which database?"}) + "\n")
        (crew / "hw-planner.json").write_text(json.dumps({"name": "hw-planner", "reports_to": "manager", "pane": "p7"}))
        self.open(self.shop, "confirm", "d1", "D1: a thing")  # shop's WAIT1 is not the report
        with mock.patch.object(fc_waiting.fc_tasks, "push_stance", return_value="ask"):
            out = self.run_cmd(fc.fc_waiting.cmd_ack, id="WAIT1", project="hw")
            self.assertEqual(out, ["seen WAIT1 (hw): which database?; it stays on the list until it is closed, and leaves the footer"])
            self.pane_label.assert_called_once_with("p7", state_labels={"blocked": "seen"})
            self.assertTrue(fc_waiting.open_items(self.hw)[0]["seen"])
            (crew / "hw-planner.json").write_text(json.dumps({"name": "hw-planner", "reports_to": "manager"}))  # no pane: no relabel, no error
            self.pane_label.reset_mock()
            self.run_cmd(fc.fc_waiting.cmd_ack, id="WAIT1", project="hw")
            self.pane_label.assert_not_called()
        with self.assertRaises(SystemExit) as e:
            self.run_cmd(fc.fc_waiting.cmd_ack, id="WAIT9")
        self.assertEqual(str(e.exception), "no open item WAIT9; compose waiting lists them")

    def test_ack_refuses_an_id_two_projects_share(self):
        self.open(self.hw, "confirm", "a", "in hw")
        self.open(self.shop, "confirm", "b", "in shop")
        with self.assertRaises(SystemExit) as e:
            self.run_cmd(fc.fc_waiting.cmd_ack, id="WAIT1")
        self.assertIn("compose ack --project hw WAIT1", str(e.exception))
        self.assertIn("compose ack --project shop WAIT1", str(e.exception))
        self.assertIsNone(fc_waiting.open_items(self.hw)[0]["seen"])
        self.run_cmd(fc.fc_waiting.cmd_ack, id="WAIT1", project="shop")
        self.assertTrue(fc_waiting.open_items(self.shop)[0]["seen"])

    def test_no_closes_a_go_item_with_their_words_and_refuses_other_kinds(self):
        for p in (self.hw, self.shop):  # a passed verify that waits for the push is a go item
            (p / "results" / "verify.json").write_text(json.dumps({"passed": True, "head": 1}))
        stance = mock.patch.object(fc_waiting.fc_tasks, "push_stance", return_value="ask")
        stance.start()
        self.addCleanup(stance.stop)
        self.open(self.hw, "confirm", "d1", "D1: a thing")  # WAIT1 in hw
        fc_waiting.gather(".")  # the go items open: WAIT1 in shop, WAIT2 in hw
        with self.assertRaises(SystemExit) as e:  # no words
            self.run_cmd(fc.fc_waiting.cmd_waiting, words=["no", "WAIT2"], project="hw")
        self.assertIn("--quote", str(e.exception))
        with self.assertRaises(SystemExit) as e:  # WAIT1 of hw is a confirm, not a go
            self.run_cmd(fc.fc_waiting.cmd_waiting, words=["no", "WAIT1"], project="hw", quote="not that")
        self.assertIn("WAIT1 is a confirm item", str(e.exception))
        with self.assertRaises(SystemExit) as e:  # unknown
            self.run_cmd(fc.fc_waiting.cmd_waiting, words=["no", "WAIT9"], project="hw", quote="not that")
        self.assertEqual(str(e.exception), "no open item WAIT9; compose waiting lists them")
        with self.assertRaises(SystemExit) as e:  # WAIT1 is open in both projects
            self.run_cmd(fc.fc_waiting.cmd_waiting, words=["no", "WAIT1"], quote="not yet")
        self.assertIn("compose waiting no --project hw WAIT1", str(e.exception))
        self.assertIn("compose waiting no --project shop WAIT1", str(e.exception))
        self.assertEqual(len(fc_waiting.open_items(self.hw)), 2)
        out = self.run_cmd(fc.fc_waiting.cmd_waiting, words=["no", "WAIT2"], project="hw", quote="not yet")
        self.assertEqual(out, ['closed WAIT2 (hw): the product owner said no: "not yet"'])
        closed = json.loads((self.hw / "results" / "waiting.json").read_text())["items"][1]
        self.assertEqual((closed["closed_by"], bool(closed["closed"])), ("no: not yet", True))
        self.assertEqual([i["id"] for i in fc_waiting.gather(".", project="hw")], ["WAIT1"])  # the same verify does not ask again


if __name__ == "__main__":
    unittest.main()
