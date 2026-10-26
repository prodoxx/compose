import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

MAP = """## Destination

A shop.

## Decisions so far

- Q0: Stripe (product owner, 2026-10-20; said: "stripe")
- Q3: dark theme (yolo recommendation, unconfirmed, 2026-10-21)
- D4: one store (product owner, 2026-10-20; said: "one")

## Not yet specified

- Q1: Which auth? - Recommendation: Clerk - every screen signs in
- Q2: Which database? - Recommendation: SQLite - hosting hangs on it

## Out of scope

- payments v2
"""
OLD_CARDS = """## Decisions so far

- Q0: Stripe (product owner, 2026-10-20; said: "stripe")

## Not yet specified

Q1. Which auth?
Why: every screen signs in.
A. Clerk - affects: billing, vendor
B. Own JWT - affects: two tasks of work
Recommendation: A - less to build

Q2. Database?
A. Postgres - affects: hosting
B. SQLite - affects: deploy only
Recommendation: B

## Out of scope

- payments v2
"""
TODAY = time.strftime("%Y-%m-%d")


class FakeLinear:
    """Linear as two calls use it: a read of the map and a description write."""
    def __init__(self, body):
        self.body = body
        self.writes = []
        self.children = []

    def gql(self, query, variables):
        if "issueUpdate" in query:
            self.writes.append(variables["in"]["description"])
            self.body = variables["in"]["description"]
            return {"issueUpdate": {"success": True}}
        if "children" in query:
            return {"issue": {"children": {"nodes": self.children, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
        return {"issue": {"id": "uuid-1", "identifier": "NEB-1", "description": self.body}}


class Decide(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-1"}))
        self.linear = FakeLinear(MAP)

    def run_cmd(self, fn, *answers, quote=None, accept_all=False, issue=None, change=None, new=None, replaces=None, closes=None, drop=None):
        out = io.StringIO()
        a = SimpleNamespace(repo=".", project="shop", answers=list(answers), quote=quote, accept_all=accept_all, issue=issue, change=change,
                            new=new or [], replaces=replaces, closes=closes or [], drop=drop or [])
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_linear, "linear_gql", self.linear.gql), \
                mock.patch.object(fc.fc_linear_cmd, "SETTLE_SECONDS", 0), contextlib.redirect_stdout(out):
            fn(a)
        return out.getvalue()

    def decide(self, *answers, **kw):
        return self.run_cmd(fc.fc_decide.cmd_decide, *answers, **kw)

    def decisions(self):
        return self.run_cmd(fc.fc_decide.cmd_decisions)

    def test_the_decisions_page_shows_open_questions_with_their_recommendations_then_the_decisions_to_confirm(self):
        (self.proj / "results" / "asked.json").write_text(json.dumps({"manager@/NEB-1/Q1": {"at": "2026-10-24 09:00:00"}}))
        out = self.decisions()
        self.assertIn("shop: 2 open question(s) on NEB-1, 1 decision(s) to confirm", out)
        self.assertIn("Q1: Which auth?  (asked 2026-10-24 09:00:00)\n   Recommendation: Clerk\n   Why: every screen signs in", out)
        self.assertIn("Q2: Which database?  (not asked yet)", out)
        self.assertIn("Decisions to confirm (1):\n- Q3: dark theme (yolo recommendation, unconfirmed, 2026-10-21)", out)
        self.assertNotIn("Q0: Stripe", out)
        self.assertNotIn("affects", out)
        self.assertIn('compose decide --project shop --new "<decision>" --quote "<their words>" --closes <id>', out)
        self.assertIn("--drop <id>", out)
        self.assertIn("--accept-all", out)
        self.assertEqual(self.linear.writes, [])

    def test_the_page_names_a_question_without_a_recommendation(self):
        self.linear.body = "## Not yet specified\n- Q5: Colors?\n"
        self.assertIn("Q5: Colors?  (not asked yet)\n   Recommendation: (none)", self.decisions())

    def test_the_page_reads_an_old_card_as_its_question_and_recommended_option(self):
        self.linear.body = OLD_CARDS
        out = self.decisions()
        self.assertIn("Q1: Which auth?  (not asked yet)\n   Recommendation: A. Clerk\n   Why: every screen signs in.", out)
        self.assertIn("Q2: Database?  (not asked yet)\n   Recommendation: B. SQLite", out)
        self.assertNotIn("Own JWT", out)

    def test_an_empty_page_says_nothing_is_open(self):
        self.linear.body = "## Not yet specified\n\n## Out of scope\n"
        self.assertIn("no open questions: Not yet specified is empty", self.decisions())

    def test_a_decision_is_recorded_only_with_the_product_owners_words(self):
        with self.assertRaises(SystemExit) as e:
            self.decide(new=["Clerk"], closes=["Q1"])
        self.assertIn("recorded only with the product owner's words", str(e.exception.code))
        self.assertIn("--quote", str(e.exception.code))
        self.assertEqual(self.linear.writes, [])

    def test_an_answer_closes_its_question_into_the_next_d_line_and_names_what_is_left(self):
        out = self.decide(new=["Clerk, it is the least to build"], closes=["Q1"], quote='Clerk, "less to build" wins')
        body = self.linear.body
        self.assertIn(f'- D5: Clerk, it is the least to build (closes Q1) (product owner, {TODAY}; said: "Clerk, \'less to build\' wins")', body)
        self.assertNotIn("Which auth?", body)
        self.assertIn("## Not yet specified\n\n- Q2: Which database?", body)
        self.assertIn("recorded on NEB-1: D5: Clerk", out)
        self.assertIn("open questions on NEB-1 (1):\n- Q2: Which database?", out)
        self.assertIn("tell the team lead: herdr agent prompt shop-orchestrator", out)
        self.assertNotIn("\u2014", body + out)

    def test_one_answer_may_close_several_questions_and_the_last_leaves_none_open(self):
        out = self.decide(new=["Clerk on SQLite"], closes=["Q1", "Q2"], quote="clerk and sqlite")
        self.assertIn("- D5: Clerk on SQLite (closes Q1, Q2) (product owner,", self.linear.body)
        self.assertIn("## Not yet specified\n\n## Out of scope\n\n- payments v2\n", self.linear.body)
        self.assertIn("no open questions on NEB-1", out)

    def test_closing_a_question_that_is_not_open_refuses_naming_the_open_ones(self):
        self.refuses("Q7 is not an open question on NEB-1; the open ones are Q1, Q2", new=["x"], closes=["Q7"], quote="x")

    def test_closes_goes_with_exactly_one_new_decision(self):
        self.refuses("--closes", closes=["Q1"], quote="x")
        self.refuses("--closes", new=["x", "y"], closes=["Q1"], quote="x")

    def test_a_question_the_answers_made_moot_is_dropped_with_a_decision_line(self):
        out = self.decide(drop=["Q2"], quote="we decided no database for now")
        self.assertIn(f'- D5: no longer applies: Which database? (closes Q2) (product owner, {TODAY}; said: "we decided no database for now")', self.linear.body)
        self.assertNotIn("- Q2: Which database?", self.linear.body)
        self.assertIn("open questions on NEB-1 (1):\n- Q1: Which auth?", out)

    def test_dropping_a_question_that_is_not_open_refuses(self):
        self.refuses("Q9 is not an open question on NEB-1", drop=["Q9"], quote="x")

    def test_a_follow_up_with_no_question_line_is_a_plain_new_decision(self):
        out = self.decide(new=["billing waits for auth"], quote="auth first")
        self.assertIn("- D5: billing waits for auth (product owner,", self.linear.body)
        self.assertIn("- Q1: Which auth?", self.linear.body)
        self.assertIn("open questions on NEB-1 (2):", out)

    def test_an_approval_is_refused_while_questions_are_open_neb_2181(self):
        for text in ("stories NEB-1 approved", "Design for checkout approved", "tech doc NEB-1 is approved", "the PRD v2 approved"):
            self.refuses("NEB-1 still has open questions: Q1, Q2", new=[text], quote="looks good")
        self.refuses('--closes <id>', new=["stories NEB-1 approved"], quote="looks good")

    def test_an_approval_goes_through_once_every_question_is_closed(self):
        self.decide(new=["Clerk"], closes=["Q1"], quote="clerk")
        self.decide(drop=["Q2"], quote="no db")
        out = self.decide(new=["stories NEB-1 approved"], quote="ship it")
        self.assertIn("- D7: stories NEB-1 approved (product owner,", self.linear.body)
        self.assertIn("no open questions on NEB-1", out)

    def test_an_approval_may_close_the_last_questions_itself(self):
        self.decide(new=["stories NEB-1 approved, Clerk and SQLite as recommended"], closes=["Q1", "Q2"], quote="all as you said, approved")
        self.assertIn("- D5: stories NEB-1 approved, Clerk and SQLite as recommended (closes Q1, Q2) (product owner,", self.linear.body)

    def test_accept_all_records_each_recommendation_as_its_own_unconfirmed_line_and_logs_it(self):
        y = {"mode": "away", "since": "2026-10-24 08:00:00", "decisions": [], "words": "", "push": False}
        (self.proj / "results" / "yolo.json").write_text(json.dumps(y))
        out = self.decide(accept_all=True)
        self.assertIn(f"- D5: Clerk (closes Q1) (away recommendation, unconfirmed, {TODAY})", self.linear.body)
        self.assertIn(f"- D6: SQLite (closes Q2) (away recommendation, unconfirmed, {TODAY})", self.linear.body)
        self.assertNotIn("Which auth?", self.linear.body)
        logged = json.loads((self.proj / "results" / "yolo.json").read_text())["decisions"]
        self.assertEqual([d["text"].split(" (")[0] for d in logged], ["D5: Clerk", "D6: SQLite"])
        self.assertIn("no open questions on NEB-1", out)
        self.decide("D5", quote="yes, Clerk")
        self.assertIn(f'- D5: Clerk (closes Q1) (product owner, {TODAY}; said: "yes, Clerk")', self.linear.body)
        self.assertIn("- D6: SQLite (closes Q2) (away recommendation", self.linear.body)

    def test_accept_all_needs_a_recommendation_on_every_question(self):
        self.linear.body = MAP.replace(" - Recommendation: SQLite - hosting hangs on it", "")
        self.refuses("Q2 carry none", quote="all yours", accept_all=True)

    def test_accept_all_leaves_the_questions_closed_in_the_same_command(self):
        self.decide(new=["Postgres"], closes=["Q2"], quote="postgres, and the rest as you say", accept_all=True)
        body = self.linear.body
        self.assertIn("- D5: Postgres (closes Q2) (product owner,", body)
        self.assertIn("- D6: Clerk (closes Q1) (product owner,", body)
        self.assertNotIn("SQLite (closes", body)

    def test_under_yolo_a_new_decision_without_a_quote_is_unconfirmed_and_logged(self):
        self.yolo()
        self.decide(new=["x"], closes=["Q1"])
        self.assertIn(f"- D5: x (closes Q1) (yolo recommendation, unconfirmed, {TODAY})", self.linear.body)
        logged = json.loads((self.proj / "results" / "yolo.json").read_text())["decisions"]
        self.assertEqual([d["text"].split(" (")[0] for d in logged], ["D5: x"])

    def test_the_next_d_id_skips_struck_lines_and_ignores_d_numbers_inside_a_quote(self):
        self.linear.body = MAP.replace("- Q0: Stripe", '- D1: x (product owner, 2026-10-01; said: "like D12")\n- ~~D6: old~~ (C1)\n- Q0: Stripe')
        self.decide(new=["two\nlines"], quote="ok")
        self.assertIn("- D7: two lines (product owner,", self.linear.body)

    def test_several_new_decisions_from_one_message_land_in_order(self):
        out = self.decide(new=["billing waits for auth", "day pace is capped"], quote="auth first, and cap the day")
        self.assertIn(f'- D5: billing waits for auth (product owner, {TODAY}; said: "auth first, and cap the day")', self.linear.body)
        self.assertIn("- D6: day pace is capped (product owner,", self.linear.body)
        self.assertIn("recorded on NEB-1: D6: day pace is capped", out)

    def test_new_on_a_map_without_decisions_so_far_creates_the_section(self):
        self.linear.body = "## Destination\n\nA shop.\n"
        out = self.decide(new=["one store"], quote="one store")
        self.assertIn("## Decisions so far\n\n- D1: one store (product owner,", self.linear.body)
        self.assertIn("no open questions on NEB-1", out)

    def test_a_hyphenated_or_lettered_question_id_closes_like_any_other(self):
        self.linear.body = "## Not yet specified\n\n- C1-8: Which? - Recommendation: one - x\n- REC-19: Other? - Recommendation: two - y\n"
        self.decide(new=["two"], closes=["C1-8", "REC-19"], quote="two")
        self.assertIn(f"- D1: two (closes C1-8, REC-19) (product owner, {TODAY}", self.linear.body)
        self.assertNotIn("Which?", self.linear.body)

    def recorded(self, *lines):
        self.linear.body = "## Destination\n\nA shop.\n\n## Decisions so far\n\n" + "\n".join(lines) + "\n\n## Not yet specified\n\n## Out of scope\n"

    def test_a_quote_confirms_an_unconfirmed_line(self):
        self.recorded("- Q3: A. dark (yolo recommendation, unconfirmed, 2026-10-21)", "- D2: one store (yolo recommendation, unconfirmed, 2026-10-21)")
        out = self.decide("Q3", "D2", quote="dark is fine, one store")
        self.assertIn(f'- Q3: A. dark (product owner, {TODAY}; said: "dark is fine, one store")', self.linear.body)
        self.assertIn(f'- D2: one store (product owner, {TODAY}; said:', self.linear.body)
        self.assertNotIn("unconfirmed", self.linear.body)
        self.assertNotIn("the new form", out)

    def test_confirming_a_confirmed_line_refuses(self):
        self.recorded('- D2: one store (product owner, 2026-10-20; said: "ok")')
        self.refuses("already decided", "D2", quote="yes")
        self.refuses("compose change start", "D2", quote="yes")

    def test_a_bare_id_with_nothing_recorded_refuses(self):
        self.refuses("Q9", "Q9", quote="yes")

    def test_a_bare_id_naming_an_open_question_says_how_to_answer_it(self):
        self.refuses("--closes Q1", "Q1", quote="yes")

    def test_a_bare_id_without_a_quote_refuses_under_yolo(self):
        self.recorded("- D1: one store (yolo recommendation, unconfirmed, 2026-10-21)")
        self.yolo()
        self.refuses("--quote", "D1")

    def test_replaces_strikes_the_old_line_and_adds_the_next(self):
        self.recorded('- D1: one store (product owner, 2026-10-20; said: "ok")', '- D2: dark (product owner, 2026-10-20; said: "ok")')
        self.decide(new=["two stores"], replaces="D1", quote="two")
        body = self.linear.body
        self.assertIn('- ~~D1: one store (product owner, 2026-10-20; said: "ok")~~ (replaced by D3)', body)
        self.assertIn(f'- D3: two stores (product owner, {TODAY}; said: "two")', body)
        self.assertIn("- D2: dark", body)

    def test_replaces_may_name_an_old_q_line(self):
        self.decide(new=["Paddle"], replaces="Q0", quote="paddle")
        self.assertIn('- ~~Q0: Stripe (product owner, 2026-10-20; said: "stripe")~~ (replaced by D5)', self.linear.body)

    def test_replaces_refuses_once_the_feature_has_children_unless_a_change_of_course_is_named(self):
        self.recorded('- D1: one store (product owner, 2026-10-20; said: "ok")')
        self.linear.children = [{"id": "c1"}]
        self.refuses("NEB-1 already has stories or tasks, so D1 changes through a change of course: compose change start", new=["x"], replaces="D1", quote="q")

    def test_replacing_a_confirmed_line_needs_a_quote_under_yolo(self):
        self.recorded('- D1: one store (product owner, 2026-10-20; said: "ok")')
        self.yolo()
        self.refuses("--quote", new=["x"], replaces="D1")

    def test_a_replaced_id_is_not_reused(self):
        self.recorded('- D1: one store (product owner, 2026-10-20; said: "ok")')
        self.decide(new=["two stores"], replaces="D1", quote="two")
        self.decide(new=["three stores"], replaces="D2", quote="three")
        self.assertIn("(replaced by D2)", self.linear.body)
        self.assertIn("(replaced by D3)", self.linear.body)
        self.assertIn("- D3: three stores", self.linear.body)

    def test_replaces_needs_a_live_line_and_one_new(self):
        self.recorded('- ~~D1: old (product owner, 2026-10-20; said: "ok")~~ (replaced by D2)', '- D2: one store (product owner, 2026-10-20; said: "ok")')
        for kw in ({"new": ["x"], "replaces": "D9"}, {"new": ["x"], "replaces": "D1"}, {"new": [], "replaces": "D2"}, {"new": ["x", "y"], "replaces": "D2"}):
            self.refuses("", quote="q", **kw)

    def test_replaces_under_yolo_without_a_quote_is_unconfirmed_and_logged(self):
        self.recorded("- D1: one store (yolo recommendation, unconfirmed, 2026-10-21)")
        self.yolo()
        self.decide(new=["two stores"], replaces="D1")
        self.assertIn("- D2: two stores (yolo recommendation, unconfirmed,", self.linear.body)
        self.assertIn("(replaced by D2)", self.linear.body)
        self.assertEqual(len(json.loads((self.proj / "results" / "yolo.json").read_text())["decisions"]), 1)

    CHANGE_Q = "- C1-2: Stripe fees are too high for micro-payments; move to Paddle? (replaces D4) - Recommendation: Paddle - 30 cents a sale\n"

    def change_map(self):
        self.linear.body = MAP.replace("- Q2: Which database?", self.CHANGE_Q + "- Q2: Which database?")
        self.linear.children = [{"identifier": "NEB-2", "state": {"name": "Todo"}}]

    def test_a_change_of_course_answer_that_changes_a_decision_strikes_it_with_the_change_id(self):
        self.change_map()
        self.linear.body = self.linear.body.replace("- Q3: dark", "- ~~Q-1: light theme (product owner, 2026-10-19)~~ (C0)\n- Q3: dark")
        out = self.decide(new=["Paddle for every sale"], closes=["C1-2"], replaces="D4", change="C1", quote="go with Paddle")
        body = self.linear.body
        self.assertIn('- ~~D4: one store (product owner, 2026-10-20; said: "one")~~ (C1)\n', body)
        self.assertIn(f'- D5: Paddle for every sale (closes C1-2) (C1; product owner, {TODAY}; said: "go with Paddle")', body)
        self.assertIn("- ~~Q-1: light theme (product owner, 2026-10-19)~~ (C0)\n", body, "a line struck by an earlier change stays")
        self.assertNotIn("Stripe fees", body)
        self.assertIn("- Q1: Which auth?", body, "the other questions stay open")
        self.assertIn("recorded on NEB-1: D5: Paddle for every sale (closes C1-2) (C1;", out)

    def test_an_answer_that_keeps_the_decision_strikes_nothing(self):
        self.change_map()
        self.decide(new=["D4 stands: fees are fine at our volume"], closes=["C1-2"], change="C1", quote="keep it")
        body = self.linear.body
        self.assertIn('- D4: one store (product owner, 2026-10-20; said: "one")\n', body)
        self.assertNotIn("~~", body)
        self.assertIn(f"- D5: D4 stands: fees are fine at our volume (closes C1-2) (C1; product owner, {TODAY}", body)

    def test_a_questions_replaces_pointer_is_never_acted_on_silently(self):
        self.change_map()
        self.decide(new=["Paddle"], closes=["C1-2"], change="C1", quote="paddle")
        self.assertNotIn("~~", self.linear.body)

    def test_an_old_supersedes_line_strikes_nothing_by_itself(self):
        self.linear.body = OLD_CARDS.replace("## Out of scope", "W1. Fees too high?\nSupersedes: Q0: Stripe\nA. keep it - affects: none\nB. Paddle - affects: checkout\nRecommendation: B\n\n## Out of scope")
        self.decide("W1: B", quote="paddle", change="C2")
        self.assertIn('- Q0: Stripe (product owner, 2026-10-20; said: "stripe")\n', self.linear.body)
        self.assertNotIn("~~", self.linear.body)

    def test_an_old_card_block_closes_whole_with_closes(self):
        self.linear.body = OLD_CARDS
        out = self.decide(new=["Own JWT, we keep auth in house"], closes=["Q1"], quote="our own JWT")
        body = self.linear.body
        self.assertIn("- D1: Own JWT, we keep auth in house (closes Q1) (product owner,", body)
        for gone in ("Which auth?", "every screen signs in", "Clerk", "less to build"):
            self.assertNotIn(gone, body)
        self.assertIn("## Not yet specified\n\nQ2. Database?\nA. Postgres - affects: hosting\nB. SQLite - affects: deploy only\nRecommendation: B\n\n## Out of scope", body)
        self.assertIn("open questions on NEB-1 (1):\n- Q2: Database?", out)

    def test_the_old_letter_answer_still_records_and_names_the_new_form(self):
        self.linear.body = OLD_CARDS
        out = self.decide("Q1: A", "Q2: B, but keep backups", quote="A, and B with backups")
        body = self.linear.body
        self.assertIn(f'- Q1: A. Clerk (product owner, {TODAY}; said: "A, and B with backups")', body)
        self.assertIn("- Q2: B. SQLite (but keep backups) (product owner,", body)
        self.assertNotIn("Q1. Which auth?", body)
        self.assertIn("## Not yet specified\n\n## Out of scope", body)
        self.assertIn('note: the new form is `compose decide --project shop --new "<decision>" --quote "<their words>" --closes <id>`', out)
        self.assertIn("no open questions on NEB-1", out)
        for words, want in (("Q1: C", "no option C"), ("Q7: A", "no question Q7 under Not yet specified"), ("Q1 A", "is not an answer")):
            self.linear.body, self.linear.writes = OLD_CARDS, []
            self.refuses(want, words, quote="C")

    def test_a_multi_line_quote_records_one_line_and_replaces_strikes_it_cleanly(self):
        self.recorded('- D1: one store (product owner, 2026-10-20; said: "ok")')
        self.decide(new=["two stores"], quote='two,\nand "soon"\r\n  please')
        said = f'(product owner, {TODAY}; said: "two, and \'soon\' please")'
        self.assertIn(f"- D2: two stores {said}\n", self.linear.body)
        self.decide(new=["three stores"], replaces="D2", quote="three\nnow")
        dec = fc.fc_change.map_section(self.linear.body, "Decisions so far").splitlines()
        self.assertEqual(dec[1:], [f"- ~~D2: two stores {said}~~ (replaced by D3)", f'- D3: three stores (product owner, {TODAY}; said: "three now")'])

    def test_canceled_children_do_not_block_replaces(self):
        self.recorded('- D1: one store (product owner, 2026-10-20; said: "ok")')
        self.linear.children = [{"identifier": "NEB-2", "state": {"name": "Canceled"}}, {"identifier": "NEB-3", "state": {"name": "cancelled"}}]
        self.decide(new=["two stores"], replaces="D1", quote="two")
        self.assertIn("(replaced by D2)", self.linear.body)
        self.linear.children.append({"identifier": "NEB-4", "state": {"name": "Todo"}})
        self.linear.writes = []
        self.refuses("already has stories or tasks", new=["three stores"], replaces="D2", quote="three")

    def test_the_wake_up_note_names_both_go_lines(self):
        out = self.decide(new=["one store"], quote="one")
        note = ("recorded on NEB-1: D5; read Decisions so far. While shaping, wait for `shaped <id>`; a `stories <id> approved` line is "
                "the planner's go (tell the planner, or the change architect, if live)")
        self.assertIn(f'tell the team lead: herdr agent prompt shop-orchestrator "{note}"', out)

    def yolo(self):
        (self.proj / "results" / "yolo.json").write_text(json.dumps({"mode": "yolo", "since": "2026-10-24 08:00:00", "decisions": [], "words": "", "push": False}))

    def refuses(self, want, *a, **kw):
        before = self.linear.body
        with self.assertRaises(SystemExit) as e:
            self.decide(*a, **kw)
        self.assertIn(want, str(e.exception.code))
        self.assertEqual((self.linear.writes, self.linear.body), ([], before))
    def test_yolo_tag_is_the_shared_one(self):
        self.assertIs(fc.fc_yolo.YOLO_TAG, fc.fc_decide.UNCONFIRMED)


class Flags(unittest.TestCase):
    """compose decide takes --closes and --drop, each repeatable, and its help speaks of questions."""

    def help(self, *args):
        out = subprocess.run([sys.executable, str(Path(fc.__file__)), *args, "--help"], capture_output=True, text=True, env={**os.environ, "COLUMNS": "400"})
        return out.stdout

    def test_decide_parses_closes_and_drop(self):
        text = self.help("decide")
        for flag in ("--closes", "--drop", "--new", "--replaces", "--accept-all"):
            self.assertIn(flag, text)
        self.assertNotIn("card", text)

    def test_the_top_help_names_questions_not_cards(self):
        self.assertNotIn("card", self.help("decisions"))
        rows = [line for line in self.help().splitlines() if line.strip().startswith(("decide ", "decisions "))]
        self.assertEqual(len(rows), 2)
        for line in rows:
            self.assertNotIn("card", line)


if __name__ == "__main__":
    unittest.main()
