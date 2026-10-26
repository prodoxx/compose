import hermetic  # first: no test sees the real home
import contextlib, http.server, importlib.machinery, importlib.util, io, json, os, sys, tempfile, threading, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_planner_brief


QUESTION = "- Q2: Which auth? - Recommendation: Clerk - every screen signs in\n"
OLD_CARD = "Q2. Which auth?\nWhy: every screen signs in.\nA. Clerk - affects: billing, vendor\nB. Own JWT - affects: two tasks of work\nRecommendation: A - less to build\n"


class Page(http.server.BaseHTTPRequestHandler):
    """Answers like lavish-axi's server: its real session answers, a made-up one is a 404."""
    def do_GET(self):
        self.send_response(200 if self.path == "/session/4433ea54b6a63d29" else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


class Ask(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.good = f"{cls.base}/session/4433ea54b6a63d29"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        (self.proj / "results" / "crew").mkdir(parents=True)

    def report(self, msg, state="needs-user", who="shop-architect"):
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), contextlib.redirect_stdout(out):
            fc.fc_crew.cmd_report(type("A", (), {"as_": who, "state": state, "message": msg.split()}))
        return out.getvalue()

    def sent(self):
        return [r["message"] for r in fc.fc_crew.crew_reports(self.proj, "shop-architect")]

    def test_question_ids_expand_ranges_and_lists(self):
        ids = fc.fc_ask.question_ids
        self.assertEqual(ids("NEB-1: 7 questions W1-W7; recommendation accept"), [f"W{n}" for n in range(1, 8)])
        self.assertEqual(ids("NEB-1: 4 questions Q1-Q2, Q5 and Q7; rec"), ["Q1", "Q2", "Q5", "Q7"])
        self.assertEqual(ids("NEB-1: 3 questions Q1 to Q3"), ["Q1", "Q2", "Q3"])
        self.assertEqual(ids("NEB-1: 1 question Q4; rec"), ["Q4"])
        self.assertEqual(ids("NEB-1: 2 questions Q1..Q2 and Q4"), ["Q1", "Q2", "Q4"])
        self.assertEqual(ids("which fixture? I recommend A"), [])
        self.assertEqual(ids("NEB-1: 2 questions"), [])

    def test_the_old_cards_wording_still_names_ids(self):
        self.assertEqual(fc.fc_ask.question_ids("NEB-1: 2 questions Q1-Q2; rec"), ["Q1", "Q2"])
        self.assertEqual(fc.fc_ask.question_ids("NEB-1: 1 card Q4; rec"), ["Q4"])

    def test_a_hyphenated_question_id_is_one_id_and_ranges_over_it_expand(self):
        ids = fc.fc_ask.question_ids
        self.assertEqual(ids("NEB-1: 2 questions C1-8, C1-9; rec"), ["C1-8", "C1-9"])
        self.assertEqual(ids("NEB-1: 3 questions C1-2..C1-4; rec"), ["C1-2", "C1-3", "C1-4"])
        self.assertEqual(ids("NEB-1: 3 questions C1-2 to C1-4 and Q5; rec"), ["C1-2", "C1-3", "C1-4", "Q5"])

    def test_an_id_with_letters_a_hyphen_and_digits_is_one_id(self):
        ids = fc.fc_ask.question_ids
        self.assertEqual(ids("NEB-1: 2 questions REC-19, REC-20; rec"), ["REC-19", "REC-20"])
        self.assertEqual(ids("NEB-1: 3 questions REC-19..REC-21; rec"), ["REC-19", "REC-20", "REC-21"])
        self.assertEqual(ids("NEB-1: 3 questions REC-19 to REC-21 and Q5; rec"), ["REC-19", "REC-20", "REC-21", "Q5"])
        self.assertEqual(ids("NEB-1: 3 questions UX22-UX24; rec"), ["UX22", "UX23", "UX24"])
        for cid in ("REC-19", "Q1", "UX24", "C1-8"):
            self.assertEqual(fc.fc_ask.questions_in(f"- {cid}: Which? - Recommendation: a - b\n").keys(), {cid})

    def test_a_question_line_reads_its_question_recommendation_why_and_pointer(self):
        qs = fc.fc_ask.questions_in(
            "- Q3: Which auth? - Recommendation: Clerk - every screen signs in\n"
            "- C1-2: Move to Paddle? (replaces D4) - **Recommendation:** Paddle - fees are 30 cents a sale\n"
            "- V1: Which layout? - Recommendation: the list - it scans faster; open in Lavish: http://127.0.0.1:1/session/ab12\n"
            "- Q4: Colors?\n"
            "- shipping rates are not settled yet\n")
        self.assertEqual(sorted(qs), ["C1-2", "Q3", "Q4", "V1"])
        self.assertEqual((qs["Q3"]["question"], qs["Q3"]["recommendation"], qs["Q3"]["why"], qs["Q3"]["span"]),
                         ("Which auth?", "Clerk", "every screen signs in", (0, 1)))
        self.assertEqual((qs["C1-2"]["question"], qs["C1-2"]["recommendation"], qs["C1-2"]["replaces"]), ("Move to Paddle? (replaces D4)", "Paddle", "D4"))
        self.assertIn("session/ab12", qs["V1"]["why"])
        self.assertEqual((qs["Q4"]["recommendation"], qs["Q4"]["span"]), ("", (3, 5)), "a question's span runs to the next question head")

    def test_a_recommendation_with_a_dash_in_it_splits_on_the_last_dash(self):
        q = fc.fc_ask.questions_in("- Q5: Which auth? - Recommendation: Clerk - the hosted one - every screen signs in\n")["Q5"]
        self.assertEqual((q["recommendation"], q["why"]), ("Clerk - the hosted one", "every screen signs in"))

    def test_a_recommendation_keeps_its_inline_code_and_emphasis_whole(self):
        qs = fc.fc_ask.questions_in("- Q6: Where? - Recommendation: move it to `src/server/stripe-webhook.test.ts` - it follows the webhook\n"
                                    "- Q7: Which? - Recommendation: **Clerk** - hosted\n- Q8: Which? - Recommendation: `vi.mock`* - same as today\n")
        self.assertEqual([qs[i]["recommendation"] for i in ("Q6", "Q7", "Q8")],
                         ["move it to `src/server/stripe-webhook.test.ts`", "**Clerk**", "`vi.mock`"])

    def test_a_question_span_takes_its_continuation_lines_in_both_formats(self):
        qs = fc.fc_ask.questions_in("- Q1: Which auth? - Recommendation: Clerk - every screen\n  signs in\nsee NEB-9\n- Q2: Colors? - Recommendation: warm - brand\n"
                                    "Q3. Database?\nA. Postgres - affects: hosting\nRecommendation: A\nmore notes on hosting\n")
        self.assertEqual([qs[i]["span"] for i in ("Q1", "Q2", "Q3")], [(0, 3), (3, 4), (4, 8)])

    def test_an_old_card_block_reads_as_its_question_and_recommended_option(self):
        qs = fc.fc_ask.questions_in("1. **Q1**: Which db?\n   - Why: hosting hangs on it\n   - **A.** Postgres (affects: schema)\n   - B) SQLite - affects deploy only\n"
                                    "   - Recommended: B, the smaller one\n- Q2. Colors?\n  A. warm, affects: tokens\n  B. cool, affects: tokens\n  Recommendation: A\n"
                                    "\nSupersedes text is just a note\n")
        self.assertEqual(sorted(qs), ["Q1", "Q2"])
        self.assertEqual((qs["Q1"]["question"], qs["Q1"]["why"], qs["Q1"]["recommendation"], qs["Q1"]["span"]), ("Which db?", "hosting hangs on it", "B. SQLite", (0, 5)))
        self.assertEqual(qs["Q1"]["options"], {"A": ("Postgres", "schema"), "B": ("SQLite", "deploy only")})
        self.assertEqual((qs["Q2"]["recommendation"], qs["Q2"]["span"]), ("A. warm", (5, 11)))

    def test_a_working_link_goes_out_with_the_text_fallback(self):
        self.report(f"NEB-1: 2 questions V1-V2; recommendation accept; visual questions open in Lavish: {self.good}")
        (msg,) = self.sent()
        self.assertIn(self.good, msg)
        self.assertTrue(msg.endswith(fc.fc_ask.ASK_FALLBACK))

    def test_a_link_that_does_not_answer_is_refused_and_nothing_is_sent(self):
        for url in (f"{self.base}/session/0123456789abcdef", "http://127.0.0.1:9/session/4433ea54b6a63d29"):
            with self.assertRaises(SystemExit) as e:
                self.report(f"NEB-1: 1 questions V1; recommendation a; visual questions open in Lavish: {url}")
            self.assertIn("does not answer", str(e.exception))
        self.assertEqual(self.sent(), [])

    def test_one_page_in_two_host_forms_is_refused(self):
        other = self.good.replace("127.0.0.1", "localhost")
        with self.assertRaises(SystemExit) as e:
            self.report(f"NEB-1: 1 questions V1; recommendation a; visual questions open in Lavish: {self.good} or {other}")
        self.assertIn("2 forms", str(e.exception))
        self.assertEqual(self.sent(), [])

    def test_a_visual_page_without_its_link_is_refused(self):
        with self.assertRaises(SystemExit) as e:
            self.report("NEB-1: 1 questions V1; recommendation a; visual questions open in Lavish: /tmp/x/lavish/architect.html")
        self.assertIn("lavish-axi <file>", str(e.exception))
        self.assertEqual(self.sent(), [])

    def test_other_needs_user_lines_and_states_are_untouched(self):
        self.report("Which payment provider? I recommend Stripe")
        self.report("Frame0 asks to allow the MCP server: click Allow")
        self.report(f"working {self.base}/session/0123456789abcdef", state="working")
        self.assertEqual(len(self.sent()), 3)

    def test_the_same_open_questions_are_asked_once(self):
        ask = "NEB-1: 3 questions Q1-Q3; recommendation accept-all"
        self.report(ask)
        out = self.report(ask)
        self.assertIn("already asked", out)
        self.assertEqual(self.sent(), [ask])
        self.assertEqual(sorted(json.loads((self.proj / "results" / "asked.json").read_text())), ["/NEB-1/Q1", "/NEB-1/Q2", "/NEB-1/Q3"])

    def test_a_teammates_ask_to_the_team_lead_does_not_use_up_the_forward_to_the_manager(self):
        for who, up in (("shop-planner", "orchestrator"), ("shop-orchestrator", "manager")):
            (self.proj / "results" / "crew" / f"{who}.json").write_text(json.dumps({"name": who, "reports_to": up, "pane": "p"}))
        ask = "NEB-1: 2 questions Q1-Q2; recommendation accept-all"
        self.report(ask, who="shop-planner")
        self.assertIn("already asked", self.report(ask, who="shop-planner"))
        self.assertNotIn("already asked", self.report(ask, who="shop-orchestrator"))
        self.assertIn("already asked", self.report(ask, who="shop-orchestrator"))
        self.assertEqual([r["message"] for r in fc.fc_crew.crew_reports(self.proj, "shop-orchestrator")], [ask])
        self.assertEqual([r["message"] for r in fc.fc_crew.crew_reports(self.proj, "shop-planner")], [ask])

    def test_one_reminder_is_allowed_then_no_more(self):
        ask = "NEB-1: 2 questions Q1-Q2; recommendation accept-all"
        self.report(ask)
        self.report("reminder: " + ask)
        out = self.report("reminder: " + ask)
        self.assertIn("reminder is spent", out)
        self.assertEqual(self.sent(), [ask, "reminder: " + ask])
        self.assertNotIn("already asked", self.report("NEB-1: 1 questions Q9; recommendation a"))

    def test_new_questions_go_out_with_the_old_ones_in_one_ask(self):
        self.report("NEB-1: 2 questions Q1-Q2; recommendation a")
        self.report("NEB-1: 3 questions Q1-Q3; recommendation a")
        self.assertEqual(len(self.sent()), 2)
        self.assertIn("already asked", self.report("NEB-1: 1 questions Q3; recommendation a"))

    def test_the_same_ids_on_another_issue_or_change_are_new_questions(self):
        self.report("NEB-1: 1 questions Q1; recommendation a")
        self.report("NEB-2: 1 questions Q1; recommendation a")
        self.report("change C1: NEB-1: 1 questions Q1; recommendation a")
        self.assertEqual(len(self.sent()), 3)

    def test_questions_already_decided_on_the_map_are_never_asked(self):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-1"}))
        desc = ("## Decisions so far\n- Q1: Stripe (product owner, 2026-10-20)\n- ~~Q2: old~~\n- Q3: no (yolo recommendation, unconfirmed, 2026-10-20)\n\n"
                "## Not yet specified\n" + QUESTION)
        gql = mock.Mock(return_value={"issue": {"description": desc}})
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", "k"), mock.patch.object(fc.fc_linear, "linear_gql", gql):
            out = self.report("NEB-1: 2 questions Q1, Q3; recommendation a")
            self.assertIn("already in the map's Decisions so far", out)
            self.assertEqual(self.sent(), [])
            self.report("NEB-1: 3 questions Q1-Q3; recommendation a")
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(sorted(json.loads((self.proj / "results" / "asked.json").read_text())), ["/NEB-1/Q2"])

    def ask_on_map(self, body, msg="NEB-1: 1 question Q2; recommendation Clerk"):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "NEB-1"}))
        gql = mock.Mock(return_value={"issue": {"description": body}})
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", "k"), mock.patch.object(fc.fc_linear, "linear_gql", gql):
            return self.report(msg)

    def test_an_open_question_with_a_recommendation_goes_out(self):
        self.ask_on_map("## Not yet specified\n" + QUESTION)
        self.assertEqual(len(self.sent()), 1)
        self.assertFalse((self.proj / "results" / "crew" / "shop-architect.refused").exists())

    def test_an_old_card_block_with_a_recommendation_goes_out_under_the_old_wording(self):
        self.ask_on_map("## Not yet specified\n" + OLD_CARD, msg="NEB-1: 1 cards Q2; recommendation A")
        self.assertEqual(self.sent(), ["NEB-1: 1 cards Q2; recommendation A"])

    def test_a_question_not_written_on_the_map_is_refused(self):
        out = self.ask_on_map("## Not yet specified\n- Q9: something else - Recommendation: x - y\n")
        self.assertIn("Q2 is not written under Not yet specified on NEB-1", out)
        self.assertIn("`- Q1: <question> - Recommendation: <recommendation> - <why it matters>`", out)
        self.assertIn("compose decisions", out)
        self.assertEqual(self.sent(), [])
        self.assertTrue((self.proj / "results" / "crew" / "shop-architect.refused").exists())

    def test_a_question_without_a_recommendation_is_refused(self):
        out = self.ask_on_map("## Not yet specified\n- Q2: Which auth?\n")
        self.assertIn("Q2 has no recommendation", out)
        out = self.ask_on_map("## Not yet specified\nQ2. Which auth?\nA. Clerk - affects: billing\nB. Own JWT - affects: work\n")
        self.assertIn("Q2 has no recommendation", out)
        self.assertEqual(self.sent(), [])
        self.assertFalse((self.proj / "results" / "asked.json").exists(), "a refused question is not marked asked")

    def test_lettered_options_are_no_longer_required(self):
        out = self.ask_on_map("## Not yet specified\n- Q2: Which auth? - Recommendation: Clerk - less to build\n")
        self.assertNotIn("not reported", out)
        self.assertEqual(len(self.sent()), 1)

    def test_a_question_closed_by_a_decision_line_is_never_asked(self):
        desc = ("## Decisions so far\n- D4: Clerk (closes Q1, Q3) (product owner, 2026-10-20)\n- ~~D5: old (closes Q2)~~ (C1)\n\n"
                "## Not yet specified\n" + QUESTION)
        out = self.ask_on_map(desc, msg="NEB-1: 2 questions Q1, Q3; recommendation a")
        self.assertIn("already in the map's Decisions so far", out)
        self.assertEqual(self.sent(), [])

    def test_an_id_open_on_the_issue_is_open_and_a_reused_one_must_be_renumbered(self):
        desc = ("## Decisions so far\n- D4: Clerk (closes Q2) (product owner, 2026-10-20)\n\n"
                "## Not yet specified\n- Q2: Which database? - Recommendation: SQLite - hosting\n")
        out = self.ask_on_map(desc)
        self.assertIn("renumber: Q2 was already used", out)
        self.assertNotIn("already in the map's Decisions so far", out)
        self.assertEqual(self.sent(), [])
        self.assertTrue((self.proj / "results" / "crew" / "shop-architect.refused").exists())

    def test_a_linear_outage_does_not_stop_an_ask(self):
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", "k"), mock.patch.object(fc.fc_linear, "linear_gql", side_effect=OSError("down")):
            self.report("NEB-1: 1 questions Q1; recommendation a")
        self.assertEqual(len(self.sent()), 1)


class Briefs(unittest.TestCase):
    def test_the_change_brief_carries_the_ask_rules(self):
        brief = fc.fc_briefs.CHANGE_BRIEF
        flat = " ".join(brief.split())
        for want in ("visual questions open in Lavish: <url>", "`url:`", "never poll it for them",
                     "`yolo:`", "(yolo recommendation, unconfirmed, <date>)"):
            self.assertIn(want, flat)
        self.assertNotIn("\u2014", brief)


class BriefsNameBothPostures(unittest.TestCase):
    AWAY, YOLO = "(away recommendation, unconfirmed, <date>)", "(yolo recommendation, unconfirmed, <date>)"
    PLANNER = f"A Decisions line ending `{AWAY}` or `{YOLO}` is a recommendation the product owner has not confirmed:"
    CHANGE = ("An answer that starts `away:` or `yolo:` is the engineering manager's recommendation, never the product owner's: "
              f"`compose decide` ends its line `{AWAY}` or `{YOLO}`, the one its prefix names.")

    def flat(self, name):
        return " ".join(getattr(fc_planner_brief if name == "PLANNER_BRIEF" else fc.fc_briefs, name).split())

    def test_each_brief_carries_its_sentence(self):
        for name, want in (("PLANNER_BRIEF", self.PLANNER), ("CHANGE_BRIEF", self.CHANGE)):
            self.assertIn(want, self.flat(name), name)

    def test_planner_sentence_still_leads_into_the_built_on_rule(self):
        flat = self.flat("PLANNER_BRIEF")
        self.assertLess(flat.index(self.PLANNER), flat.index("Built on unconfirmed: <decision ids>"))

    def test_no_brief_writes_decisions_by_hand(self):
        for name in ("PLANNER_BRIEF", "CHANGE_BRIEF"):
            flat = self.flat(name)
            self.assertNotIn('--section "## Decisions so far"', flat, name)
            self.assertIn("never write a Decisions so far line" if name == "PLANNER_BRIEF" else "You never write a Decisions so far line", flat, name)
            self.assertIn("compose decisions", flat, name)
        self.assertIn("`compose decide --change {cid}`", self.flat("CHANGE_BRIEF"))
        self.assertIn("`compose report` refuses", self.flat("PLANNER_BRIEF"))

    def test_each_brief_writes_one_line_questions_without_options(self):
        line = "- Q1: <question> - Recommendation: <recommendation> - <why it matters>"
        self.assertIn(f"`{line}`", self.flat("PLANNER_BRIEF"))
        self.assertIn("`- {cid}-1: <question> (replaces <the D id it would replace>) - Recommendation: <recommendation> - <the evidence, quoted verbatim, with its source>`",
                      self.flat("CHANGE_BRIEF"))
        for name in ("PLANNER_BRIEF", "CHANGE_BRIEF"):
            flat = self.flat(name)
            for gone in ("affects:", "Recommendation: <letter>", "card", "Supersedes:"):
                self.assertNotIn(gone, flat, name)
            self.assertIn("--closes", flat, name)
            self.assertIn("one question at a time", flat, name)

    def test_each_brief_says_never_reuse_an_id(self):
        for name in ("PLANNER_BRIEF", "CHANGE_BRIEF"):
            self.assertIn("never reuse an id", self.flat(name), name)

    def test_the_change_page_shows_pictures_only_and_answers_come_through_the_manager(self):
        flat = self.flat("CHANGE_BRIEF")
        self.assertNotIn("accept-all button", flat)
        self.assertNotIn("same reads, forms", flat)
        self.assertIn("The page shows pictures only, with no answer controls; answers come through the engineering manager.", flat)

    def test_no_brief_keeps_a_yolo_only_rule_or_an_em_dash(self):
        for name in ("PLANNER_BRIEF", "CHANGE_BRIEF"):
            flat = self.flat(name)
            self.assertNotIn("\u2014", flat, name)
            self.assertNotIn("starts `yolo:` is", flat, name)
            self.assertNotIn("ending `(yolo recommendation, unconfirmed, <date>)` is", flat, name)


if __name__ == "__main__":
    unittest.main()
