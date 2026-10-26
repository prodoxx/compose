import hermetic  # first: no test sees the real home
import unittest
import importlib.machinery, importlib.util, os, sys
from pathlib import Path
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_core, fc_linear, fc_linear_cmd, fc_readiness  # noqa: E402
import tempfile, time  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

TODAY = time.strftime("%Y-%m-%d")
FEATURE = '## Intent\n\nA shop.\n\n## Decisions so far\n\n- D1: the design on ENG-1 is approved (product owner, 2026-10-20; said: "yes")\n'
STORY = "## Intent\n\nPay by card.\n\n## Acceptance criteria\n\n- a card payment succeeds\n"


def labels(names):
    return {"nodes": [{"name": n} for n in names]}


def tree(story=STORY, labels_=(), feature=FEATURE, feature_labels=("Feature",), epic=None, epic_labels=("Epic",), docs=(), state="Todo", tasks=(), blockers=()):
    epic_node = None if epic is None else {"id": "e", "identifier": "ENG-0", "title": "Payments", "description": epic, "labels": labels(epic_labels)}
    return {"id": "s", "identifier": "ENG-7", "title": "Pay", "description": story, "state": {"name": state}, "labels": labels(labels_),
            "children": {"nodes": list(tasks)}, "inverseRelations": {"nodes": list(blockers)},
            "project": {"documents": {"nodes": list(docs)}},
            "parent": {"id": "f", "identifier": "ENG-1", "title": "Shop", "description": feature, "labels": labels(feature_labels), "parent": epic_node}}


class Gates(unittest.TestCase):
    def test_a_specced_story_passes_every_gate(self):
        self.assertEqual(fc_readiness.gates(fc_readiness.story_chain(tree())), [])

    def test_open_questions_on_the_map_and_under_open_questions_block_but_readiness_lines_do_not(self):
        mine = STORY + "\n## Open questions\n\n* Where does ENG-7 stop? (readiness check, 2026-10-25)\n"
        self.assertEqual(fc_readiness.gates(fc_readiness.story_chain(tree(story=mine))), [])
        theirs = mine + "* Which card brands?\n"
        self.assertEqual([x["question"] for x in fc_readiness.gates(fc_readiness.story_chain(tree(story=theirs)))],
                         ["ENG-7 has open questions waiting for the product owner (1)."])
        held = STORY + "\n## Not yet specified\n\n- Q1: Which processor? - Recommendation: Stripe - fees\n"
        self.assertEqual([(x["item"], x["question"]) for x in fc_readiness.gates(fc_readiness.story_chain(tree(story=held)))],
                         [("questions", "Answer the open questions under Not yet specified on ENG-7 with compose decide (0b).")])

    def test_deep_dive_on_the_epic_needs_its_tech_doc_linked_and_approved(self):
        epic = "## Intent\n\nPayments.\n"
        g = fc_readiness.gates(fc_readiness.story_chain(tree(epic=epic, epic_labels=("Epic", "Deep-Dive"))))
        self.assertEqual([(x["item"], x["issue"]) for x in g], [("tech doc", "ENG-0")])
        doc = {"title": "ENG-0: tech doc", "content": "Use Stripe intents.", "url": "https://linear.app/d/1"}
        linked = epic + "\n## Tech doc\n\nhttps://linear.app/d/1\n"
        self.assertIn("not approved yet", fc_readiness.gates(fc_readiness.story_chain(tree(epic=linked, epic_labels=("Epic", "deep-dive"), docs=[doc])))[0]["question"])
        approved = linked + '\n## Decisions so far\n\n- D1: the tech doc on ENG-0 is approved (product owner, 2026-10-24; said: "ok")\n'
        self.assertEqual(fc_readiness.gates(fc_readiness.story_chain(tree(epic=approved, epic_labels=("Epic", "deep-dive"), docs=[doc]))), [])

    def test_an_unlinked_tech_doc_is_reported(self):
        epic = '## Decisions so far\n\n- D1: the tech doc on ENG-0 is approved (po, 2026-10-24; said: "ok")\n'
        doc = {"title": "ENG-0: tech doc", "content": "x", "url": "https://linear.app/d/1"}
        g = fc_readiness.gates(fc_readiness.story_chain(tree(epic=epic, epic_labels=("Epic", "deep-dive"), docs=[doc])))
        self.assertEqual([x["question"] for x in g], ["ENG-0's tech doc is not linked under ## Tech doc."])

    def test_a_task_or_a_feature_is_refused(self):
        with self.assertRaisesRegex(fc_readiness.NotAStory, "ENG-7 is a task"):
            fc_readiness.story_chain(tree(story=STORY + "\n```compose\nid: 07\ntest: t\n```\n"))
        with self.assertRaisesRegex(fc_readiness.NotAStory, "is not a story"):
            fc_readiness.story_chain(tree(feature_labels=()))

    def test_no_or_too_many_criteria_is_a_criteria_gap_on_the_story(self):
        g = fc_readiness.gates(fc_readiness.story_chain(tree(story="## Intent\n\nPay.\n")))
        self.assertEqual([(x["item"], x["issue"]) for x in g], [("criteria", "ENG-7")])
        self.assertIn("no acceptance criteria", g[0]["question"])
        many = "## Acceptance criteria\n\n" + "".join(f"- c{i}\n" for i in range(6))
        g = fc_readiness.gates(fc_readiness.story_chain(tree(story=many)))
        self.assertIn("6 acceptance criteria (at most 5)", g[0]["question"])

    def test_an_unapproved_or_struck_design_blocks(self):
        for feature in ("## Intent\n\nA shop.\n",
                        "## Decisions so far\n\n- ~~D1: the design on ENG-1 is approved (po, 2026-10-20; said: \"yes\")~~ (C1)\n"):
            g = fc_readiness.gates(fc_readiness.story_chain(tree(feature=feature)))
            self.assertEqual([(x["item"], x["issue"]) for x in g], [("design", "ENG-1")])



import json  # noqa: E402
import fc_claude, fc_models, fc_tasks  # noqa: E402

STATE = {"story": {"id": "ENG-7", "title": "Pay", "description": STORY}, "feature": FEATURE, "epic": "", "tech_docs": []}


class Judge(unittest.TestCase):
    def setUp(self):
        self.jev, self.asked = {}, []

        def ask(env, cfg, state, questions, label):
            self.asked = list(questions)
            return {"answers": {k: {"choice": c, "probabilities": {c: p}} for k, (c, p) in self.jev.items() if k in questions}}
        for p in (mock.patch.object(fc_models, "jev_ask", ask), mock.patch.object(fc_models, "fast_env", return_value={"TYPESAFE_API_KEY": "k"})):
            p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(fc_claude, "claude_json"); self.strong = p.start(); self.addCleanup(p.stop)

    def test_jev_judges_a_product_story_and_a_weak_ready_counts_as_missing(self):
        self.jev = {"intent": ("READY", 0.9), "scope": ("READY", 0.55), "testable": ("UNCLEAR", 0.7), "context": ("READY", 0.8), "kind": ("PRODUCT", 0.9)}
        out = fc_readiness.judge(STATE, "ENG-7", deep_dive=False)
        self.assertEqual(out["judge"], "jev")
        self.assertFalse(out["technical"])
        self.assertEqual([m["question"] for m in out["missing"]],
                         ["Where does ENG-7 stop: what is in it and what is left out?", "How would we check each acceptance criterion on ENG-7?"])
        self.assertEqual(sorted(self.asked), ["context", "intent", "kind", "scope", "testable"])
        self.strong.assert_not_called()

    def test_a_technical_story_is_judged_again_by_the_stronger_model_whose_verdict_wins(self):
        self.jev = {**{k: ("READY", 0.9) for k in ("intent", "scope", "testable", "context")}, "kind": ("TECHNICAL", 0.7)}
        self.strong.return_value = json.dumps({"items": {"intent": {"ready": True, "missing": ""}, "scope": {"ready": True, "missing": ""},
                                                         "testable": {"ready": True, "missing": ""}, "context": {"ready": False, "missing": "Which queue carries the retries?"}}})
        out = fc_readiness.judge(STATE, "ENG-7", deep_dive=False)
        self.assertEqual((out["technical"], out["judge"]), (True, "claude:claude-opus-5-5"))
        self.assertEqual(out["missing"], [{"item": "context", "issue": "ENG-7", "question": "Which queue carries the retries?"}])
        self.assertEqual(self.strong.call_args.kwargs["effort"], "high")

    def test_deep_dive_asks_grounding_and_goes_to_the_stronger_model_whatever_jev_says(self):
        self.jev = {"kind": ("PRODUCT", 0.95)}
        items = {k: {"ready": True, "missing": ""} for k in ("intent", "scope", "testable", "context", "grounding")}
        self.strong.return_value = json.dumps({"items": items})
        out = fc_readiness.judge(STATE, "ENG-7", deep_dive=True)
        self.assertIn("grounding", self.asked)
        self.assertIn("grounding", json.loads(self.strong.call_args.args[1])["items"])
        self.assertTrue(out["technical"])
        self.assertEqual(out["missing"], [])

    def test_a_judge_that_cannot_answer_raises(self):
        with mock.patch.object(fc_models, "jev_ask", side_effect=RuntimeError("HTTP 503")), self.assertRaisesRegex(RuntimeError, "503"):
            fc_readiness.judge(STATE, "ENG-7", deep_dive=False)
        self.jev = {"kind": ("TECHNICAL", 0.9)}
        self.strong.return_value = "not json"
        with self.assertRaises(RuntimeError):
            fc_readiness.judge(STATE, "ENG-7", deep_dive=False)
        self.strong.return_value = json.dumps({"items": {k: {"ready": True, "missing": ""} for k in ("intent", "testable", "context")}})
        with self.assertRaisesRegex(RuntimeError, "scope"):
            fc_readiness.judge(STATE, "ENG-7", deep_dive=False)

    def test_the_judges_read_the_storys_tasks_and_a_long_features_tech_doc(self):
        # NEB-2182: a 30k feature pushed its 11k tech doc out of the state and the 14 tasks were never in it, so the judge
        # asked for what the tasks settle and to "attach" the linked tech doc
        task = lambda n: {"identifier": f"ENG-7{n}", "title": f"Task {n}", "state": {"name": "Todo", "type": "unstarted"},
                          "description": f"```compose\nid: t{n}\n```\nVitest runs with jsdom ({n}). " + "y" * 1500}
        doc = {"title": "ENG-1: tech doc", "content": "The start script is node .output/server/index.mjs. " + "z" * 11000, "url": "u"}
        chain = fc_readiness.story_chain(tree(feature=FEATURE + "w" * 30000, feature_labels=("Feature", "deep-dive"), docs=[doc],
                                              tasks=[task(n) for n in range(14)]))
        state = fc_readiness.judge_state(chain)
        self.assertEqual([t["id"] for t in state["tasks"]], [f"ENG-7{n}" for n in range(14)])
        self.assertEqual(state["tasks"][3], {"id": "ENG-73", "title": "Task 3", "description": chain["tasks"][3]["description"]})
        self.assertEqual(state["tech_docs"], [doc["content"]])
        self.assertEqual(state["feature"], FEATURE + "w" * 30000)

    def test_over_the_cap_the_epic_then_the_feature_go_before_the_tech_doc_and_the_tasks(self):
        task = {"identifier": "ENG-71", "title": "t", "state": {"name": "Todo"}, "description": "```compose\nid: t\n```\n" + "y" * 20000}
        doc = {"title": "ENG-7: tech doc", "content": "x" * 40000, "url": "u"}
        big = lambda: fc_readiness.story_chain(tree(labels_=("deep-dive",), docs=[doc], tasks=[task], epic="e" * 10000, feature=FEATURE + "w" * 60000))
        state = fc_readiness.judge_state(big())
        self.assertLessEqual(len(json.dumps(state)), fc_readiness.STATE_CAP)
        self.assertIn("Pay by card.", state["story"]["description"])
        self.assertEqual((state["epic"], state["tech_docs"], state["tasks"][0]["description"]), ("", [doc["content"]], task["description"]))
        self.assertTrue(state["feature"].startswith("## Intent\n\nA shop."))
        with mock.patch.object(fc_readiness, "STATE_CAP", 30000):
            state = fc_readiness.judge_state(big())
        self.assertLessEqual(len(json.dumps(state)), 30000)
        self.assertEqual((state["epic"], state["feature"], state["tasks"][0]["description"]), ("", "", task["description"]))
        self.assertTrue(state["tech_docs"][0].startswith("xxx"))
        asked = STORY + "\n## Open questions\n\n* Which brands?\n"
        self.assertNotIn("Open questions", fc_readiness.judge_state(fc_readiness.story_chain(tree(story=asked)))["story"]["description"])

    def test_the_judges_are_told_the_tasks_are_part_of_the_story(self):
        self.jev = {"kind": ("TECHNICAL", 0.9)}
        self.strong.return_value = json.dumps({"items": {k: {"ready": True, "missing": ""} for k in ("intent", "scope", "testable", "context")}})
        with mock.patch.object(fc_models, "jev_ask", wraps=fc_models.jev_ask) as ask:
            fc_readiness.judge({**STATE, "tasks": []}, "ENG-7", deep_dive=False)
        self.assertIn("what a task says, the story says", ask.call_args.args[3]["intent"]["instructions"]["rules"])
        self.assertIn("its tasks", self.strong.call_args.args[0])
        self.assertIn("its tasks", fc_readiness.READINESS_ITEMS["context"][0])

    def test_a_secret_in_the_story_text_does_not_reach_the_stronger_model(self):
        self.jev = {"kind": ("TECHNICAL", 0.9)}
        self.strong.return_value = json.dumps({"items": {k: {"ready": True, "missing": ""} for k in ("intent", "scope", "testable", "context")}})
        state = {**STATE, "story": {**STATE["story"], "description": "use key sk-live-0123456789abcdef"}}
        with mock.patch.dict(fc_core.os.environ, {"OPENROUTER_API_KEY": "sk-live-0123456789abcdef"}):
            fc_readiness.judge(state, "ENG-7", deep_dive=False)
        sent = " ".join(str(a) for a in self.strong.call_args.args)
        self.assertNotIn("sk-live-0123456789abcdef", sent)
        self.assertIn("<redacted>", sent)


    def test_any_transport_failure_of_either_model_is_a_runtime_error(self):
        import socket, subprocess, urllib.error
        for err in (urllib.error.URLError("down"), socket.timeout("slow"), FileNotFoundError("claude"), OSError("io")):
            with mock.patch.object(fc_models, "jev_ask", side_effect=err), self.assertRaises(RuntimeError, msg=repr(err)):
                fc_readiness.judge(STATE, "ENG-7", deep_dive=False)
        self.jev = {"kind": ("TECHNICAL", 0.9)}
        for err in (subprocess.TimeoutExpired("claude", 600), FileNotFoundError("claude"), urllib.error.URLError("down")):
            self.strong.side_effect = err
            with self.assertRaises(RuntimeError, msg=repr(err)):
                fc_readiness.judge(STATE, "ENG-7", deep_dive=False)


class ClaudeJson(unittest.TestCase):
    def test_claude_json_passes_effort(self):
        for effort, want in (("high", True), (None, False)):
            with mock.patch.object(fc_claude.fc_proc, "run", return_value=mock.Mock(stdout='{"result": "{}"}', stderr="", returncode=0)) as run:
                fc_claude.claude_json("s", "u", "claude:claude-opus-5-5", effort=effort)
            cmd = run.call_args.args[0]
            self.assertEqual("--effort" in cmd, want)
            if want:
                self.assertEqual(cmd[cmd.index("--effort") + 1], "high")


class StoryReadiness(unittest.TestCase):
    def setUp(self):
        self.tree, self.updates = tree(), []
        self.verdict = {"technical": False, "judge": "jev", "missing": []}
        self.repo = Path(tempfile.mkdtemp())

        def gql(query, variables):
            if "issueUpdate" in query:
                self.updates.append(variables)
                return {"issueUpdate": {"success": True}}
            return {"issue": self.tree}
        self.judge = mock.Mock(side_effect=lambda *a, **k: dict(self.verdict))
        for p in (mock.patch.object(fc_linear, "linear_gql", gql), mock.patch.object(fc_linear_cmd, "await_write"),
                  mock.patch.object(fc_linear_cmd, "drop_get_cache"), mock.patch.object(fc_core, "repo_link", return_value=self.repo / ".compose" / "linear.json"),
                  mock.patch.object(fc_readiness, "judge", self.judge)):
            p.start(); self.addCleanup(p.stop)

    def test_a_specced_unblocked_todo_story_is_ready_without_tasks(self):
        r = fc_readiness.story_readiness(self.repo, "ENG-7")
        self.assertEqual({k: r[k] for k in ("ready", "specced", "unblocked", "has_tasks", "judge", "missing", "cached", "error")},
                         {"ready": True, "specced": True, "unblocked": True, "has_tasks": False, "judge": "jev", "missing": [], "cached": False, "error": None})
        self.assertEqual(self.updates, [])

    def test_missing_items_become_tagged_open_questions_and_their_own_lines_stay(self):
        self.tree = tree(story=STORY + "\n## Open questions\n\n* Which card brands?\n* Old one? (readiness check, 2026-10-01)\n")
        self.verdict["missing"] = [{"item": "scope", "issue": "ENG-7", "question": "Where does ENG-7 stop: what is in it and what is left out?"}]
        with mock.patch.object(fc_readiness, "gates", return_value=[]):  # the product owner's line alone would stop it at the gates
            r = fc_readiness.story_readiness(self.repo, "ENG-7")
        body = self.updates[0]["in"]["description"]
        self.assertIn(f"## Open questions\n\n* Which card brands?\n- Where does ENG-7 stop: what is in it and what is left out? (readiness check, {TODAY})", body)
        self.assertNotIn("Old one?", body)
        self.assertFalse(r["ready"])

    def test_the_second_check_is_cached_and_a_human_edit_judges_again(self):
        self.verdict["missing"] = [{"item": "intent", "issue": "ENG-7", "question": "What is ENG-7 for, and who is it for?"}]
        fc_readiness.story_readiness(self.repo, "ENG-7")
        self.tree = tree(story=self.updates[-1]["in"]["description"].replace("- ", "* "))  # Linear serves it back with * bullets
        again = fc_readiness.story_readiness(self.repo, "ENG-7")
        self.assertTrue(again["cached"])
        self.assertEqual((self.judge.call_count, len(self.updates)), (1, 1))
        self.tree = tree(story=STORY.replace("Pay by card.", "Pay by card, for returning buyers."))
        fc_readiness.story_readiness(self.repo, "ENG-7")
        self.assertEqual(self.judge.call_count, 2)

    def test_a_judge_error_is_not_ready_writes_nothing_and_caches_nothing(self):
        self.judge.side_effect = RuntimeError("typesafe (Jev readiness): HTTP 503")
        r = fc_readiness.story_readiness(self.repo, "ENG-7")
        self.assertEqual((r["ready"], r["specced"], r["error"]), (False, None, "typesafe (Jev readiness): HTTP 503"))
        self.assertEqual(self.updates, [])
        self.assertFalse(fc_readiness.cache_path(self.repo).exists())

    def test_a_closed_story_is_not_judged(self):
        for state in ("Done", "In Progress", "Canceled"):
            self.tree = tree(state=state)
            self.assertEqual(tuple(fc_readiness.story_readiness(self.repo, "ENG-7")[k] for k in ("ready", "specced", "state")), (False, None, state))
        self.judge.assert_not_called()

    def test_a_story_labeled_needs_shaping_is_not_ready_for_that_reason_and_is_never_judged(self):
        self.tree = tree(labels_=("Story", "Needs-Shaping"), story=STORY + "\n## Open questions\n\n- Old? (readiness check, 2026-10-01)\n")
        r = fc_readiness.story_readiness(self.repo, "ENG-7")
        self.assertEqual((r["ready"], r["needs_shaping"], r["missing"]), (False, True, []))
        self.assertEqual(fc_readiness.readiness_line(r), "ENG-7 not ready: needs shaping")
        self.judge.assert_not_called()
        self.assertEqual(self.updates, [])
        self.assertFalse(fc_readiness.cache_path(self.repo).exists())

    def test_a_blocked_story_is_specced_but_not_ready(self):
        blocker = {"type": "blocks", "issue": {"identifier": "ENG-3", "state": {"name": "In Progress"}, "labels": labels([])}}
        self.tree = tree(blockers=[blocker])
        r = fc_readiness.story_readiness(self.repo, "ENG-7")
        self.assertEqual((r["ready"], r["specced"], r["unblocked"], r["blocked_by"]), (False, True, False, ["ENG-3"]))

    def test_a_story_whose_task_waits_on_another_storys_task_is_not_ready(self):
        blocker = {"type": "blocks", "issue": {"identifier": "ENG-31", "state": {"name": "Todo"}, "labels": labels([]), "parent": {"identifier": "ENG-3"}}}
        task = {"identifier": "ENG-71", "state": {"name": "Todo"}, "description": "```compose\nid: t\n```", "inverseRelations": {"nodes": [blocker]}}
        self.tree = tree(tasks=[task])
        r = fc_readiness.story_readiness(self.repo, "ENG-7")
        self.assertEqual((r["ready"], r["specced"], r["unblocked"], r["blocked_by"]), (False, True, False, ["ENG-3"]))

    def test_gate_gaps_skip_the_model_and_only_the_storys_own_are_written(self):
        self.tree = tree(story="## Intent\n\nPay.\n", feature="## Intent\n\nA shop.\n")
        r = fc_readiness.story_readiness(self.repo, "ENG-7")
        self.judge.assert_not_called()
        self.assertEqual([m["item"] for m in r["missing"]], ["design", "criteria"])
        body = self.updates[0]["in"]["description"]
        self.assertIn("no acceptance criteria (readiness check,", body)
        self.assertNotIn("design is not approved", body)

    def test_homed_gaps_are_reported_but_never_written(self):
        story = STORY + "\n## Not yet specified\n\n- Which flow?\n\n## Open questions\n\n- Which card brands?\n"
        self.tree = tree(story=story, feature="## Intent\n\nA shop.\n")
        r = fc_readiness.story_readiness(self.repo, "ENG-7")
        self.assertEqual({m["item"] for m in r["missing"]}, {"design", "questions", "open questions"})
        self.assertEqual(self.updates, [])

    def test_a_multi_line_question_is_one_tagged_line(self):
        body = fc_readiness.with_questions(STORY, [{"item": "x", "issue": "ENG-7", "question": "## Which queue?\nAnd which retry policy?"}], TODAY)
        self.assertIn(f"- Which queue? And which retry policy? (readiness check, {TODAY})", body)
        self.assertNotIn("\n## Which", body)
        self.assertEqual(fc_linear.open_questions(body), [f"Which queue? And which retry policy? (readiness check, {TODAY})"])

    def test_a_heading_variant_is_replaced_in_place_and_none_is_dropped(self):
        miss = [{"item": "x", "issue": "ENG-7", "question": "Where?"}]
        body = fc_readiness.with_questions(STORY + "\n## Open questions:\n\nNone.\n\n## Other\n\nx\n", miss, TODAY)
        self.assertEqual(body.count("Open questions"), 1)
        self.assertNotIn("None", body)
        self.assertIn(f"- Where? (readiness check, {TODAY})", body)
        self.assertIn("## Other", body)

    def test_an_h3_open_questions_is_never_the_section_and_nothing_else_changes(self):
        body = STORY + "\n## Notes\n\n### Open questions\n\n- mine\n\n### Links\n\n- a\n"
        out = fc_readiness.with_questions(body, [{"item": "x", "issue": "ENG-7", "question": "Where?"}], TODAY)
        self.assertEqual(out, body + f"\n## Open questions\n\n- Where? (readiness check, {TODAY})\n")

    def test_an_h3_child_under_the_section_is_kept(self):
        body = STORY + "\n## Open questions\n\n- mine\n- old (readiness check, 2026-10-01)\n\n### Detail\n\ntext\n\n## After\n\nz\n"
        out = fc_readiness.with_questions(body, [{"item": "x", "issue": "ENG-7", "question": "Where?"}], TODAY)
        self.assertIn(f"## Open questions\n\n- mine\n- Where? (readiness check, {TODAY})\n\n### Detail\n\ntext\n\n## After\n\nz", out)
        self.assertNotIn("old", out)

    def test_a_heading_with_a_colon_keeps_its_colon(self):
        out = fc_readiness.with_questions(STORY + "\n## Open questions:\n\n- mine\n", [{"item": "x", "issue": "ENG-7", "question": "Where?"}], TODAY)
        self.assertIn(f"## Open questions:\n\n- mine\n- Where? (readiness check, {TODAY})", out)
        self.assertEqual(out.count("Open questions"), 1)

    def test_the_cache_is_keyed_by_the_identifier_not_the_id_as_typed(self):
        fc_readiness.story_readiness(self.repo, "eng-7")
        again = fc_readiness.story_readiness(self.repo, "ENG-7")
        self.assertTrue(again["cached"])
        self.assertEqual(self.judge.call_count, 1)

    def test_canceled_tasks_do_not_count_as_tasks(self):
        block = "```compose\nid: t\n```"
        self.assertTrue(fc_tasks.TASK_BLOCK.search(block), "fixture must be a task")
        self.tree = tree(tasks=[{"identifier": "ENG-71", "state": {"name": "Canceled"}, "description": block}])
        self.assertFalse(fc_readiness.story_readiness(self.repo, "ENG-7")["has_tasks"])
        self.tree = tree(tasks=[{"identifier": "ENG-71", "state": {"name": "Todo"}, "description": block}])
        self.assertTrue(fc_readiness.story_readiness(self.repo, "ENG-7")["has_tasks"])

    def test_write_false_never_writes(self):
        self.verdict["missing"] = [{"item": "intent", "issue": "ENG-7", "question": "What is ENG-7 for, and who is it for?"}]
        fc_readiness.story_readiness(self.repo, "ENG-7", write=False)
        self.assertEqual(self.updates, [])

    def test_refresh_judges_again_although_cached(self):
        fc_readiness.story_readiness(self.repo, "ENG-7")
        fc_readiness.story_readiness(self.repo, "ENG-7", refresh=True)
        self.assertEqual(self.judge.call_count, 2)

    def test_with_questions_removes_the_section_when_nothing_is_left(self):
        body = STORY + "\n## Open questions\n\n- Old? (readiness check, 2026-10-01)\n"
        self.assertNotIn("Open questions", fc_readiness.with_questions(body, [], TODAY))


READY = {"id": "ENG-7", "ready": True, "specced": True, "unblocked": True, "blocked_by": [], "has_tasks": True, "missing": [], "error": None, "state": "Todo"}
NOT_READY = {**READY, "id": "ENG-8", "ready": False, "specced": False, "has_tasks": True,
             "missing": [{"item": "scope", "issue": "ENG-8", "question": "Where does ENG-8 stop: what is in it and what is left out?"}]}


class Cli(unittest.TestCase):
    def setUp(self):
        self.repo, self.out = Path(tempfile.mkdtemp()), ""
        p = mock.patch.object(fc.fc_config, "LINEAR_KEY", "k"); p.start(); self.addCleanup(p.stop)

    def cli(self, *stories, **flags):
        import io, contextlib
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                fc_readiness.cmd_readiness(SimpleNamespace(**{**dict(stories=list(stories), repo=str(self.repo), project=None, refresh=False, dry_run=False, json=False), **flags}))
        finally:
            self.out = buf.getvalue()

    def test_prints_one_line_per_story_and_exits_one_when_any_is_not_ready(self):
        rs = {"ENG-7": READY, "ENG-8": dict(NOT_READY, blocked_by=["ENG-3"], unblocked=False)}
        with mock.patch.object(fc_readiness, "story_readiness", side_effect=lambda repo, s, refresh, write: rs[s]) as sr, self.assertRaises(SystemExit) as e:
            self.cli("ENG-7", "ENG-8", dry_run=True)
        self.assertEqual(e.exception.code, 1)
        self.assertEqual(self.out.splitlines(), ["ENG-7 ready", "ENG-8 not ready (blocked by ENG-3): scope: Where does ENG-8 stop: what is in it and what is left out?"])
        self.assertEqual(sr.call_args.kwargs, {"refresh": False, "write": False})

    def test_all_ready_exits_zero(self):
        with mock.patch.object(fc_readiness, "story_readiness", side_effect=lambda repo, s, refresh, write: READY):
            self.cli("ENG-7")
        self.assertEqual(self.out.splitlines(), ["ENG-7 ready"])

    def test_without_a_linear_key_it_says_how_to_add_one(self):
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", None), self.assertRaises(SystemExit) as e:
            self.cli("ENG-7")
        self.assertIn("readiness", str(e.exception.code))

    def test_a_task_id_is_one_refusal_line_and_json_prints_the_results(self):
        def sr(repo, s, refresh, write):
            if s == "ENG-9":
                raise fc_readiness.NotAStory("ENG-9 is a task: the planner's task lint judges tasks")
            return READY
        with mock.patch.object(fc_readiness, "story_readiness", side_effect=sr), self.assertRaises(SystemExit) as e:
            self.cli("ENG-9")
        self.assertEqual(e.exception.code, 1)
        self.assertEqual(self.out.strip(), "ENG-9 refused: ENG-9 is a task: the planner's task lint judges tasks")
        with mock.patch.object(fc_readiness, "story_readiness", side_effect=sr):
            self.cli("ENG-7", json=True)
        self.assertEqual(json.loads(self.out)[0]["id"], "ENG-7")

    def test_json_lists_a_refused_story_and_an_errored_story_and_keeps_judging(self):
        def sr(repo, s, refresh, write):
            if s == "ENG-9":
                raise fc_readiness.NotAStory("ENG-9 is a task")
            if s == "ENG-6":
                raise RuntimeError("readiness: the judge failed")
            return READY
        with mock.patch.object(fc_readiness, "story_readiness", side_effect=sr), self.assertRaises(SystemExit) as e:
            self.cli("ENG-9", "ENG-6", "ENG-7", json=True)
        self.assertEqual(e.exception.code, 1)
        got = json.loads(self.out)
        self.assertEqual(got[0], {"id": "ENG-9", "ready": False, "refused": "ENG-9 is a task"})
        self.assertEqual(got[1], {"id": "ENG-6", "ready": False, "error": "readiness: the judge failed"})
        self.assertEqual(got[2]["id"], "ENG-7")

    def test_a_transport_error_is_an_error_result_and_json_still_prints_all(self):
        def sr(repo, s, refresh, write):
            if s == "ENG-6":
                raise OSError("connection reset")
            return READY
        with mock.patch.object(fc_readiness, "story_readiness", side_effect=sr), self.assertRaises(SystemExit) as e:
            self.cli("ENG-6", "ENG-7", json=True)
        self.assertEqual(e.exception.code, 1)
        got = json.loads(self.out)
        self.assertEqual((got[0]["id"], got[0]["error"], got[1]["id"]), ("ENG-6", "connection reset", "ENG-7"))

    def test_an_errored_story_is_a_line_and_the_next_story_is_still_judged(self):
        def sr(repo, s, refresh, write):
            if s == "ENG-6":
                raise RuntimeError("the judge failed")
            return READY
        with mock.patch.object(fc_readiness, "story_readiness", side_effect=sr), self.assertRaises(SystemExit) as e:
            self.cli("ENG-6", "ENG-7")
        self.assertEqual(e.exception.code, 1)
        self.assertEqual(self.out.splitlines(), ["ENG-6 not judged: the judge failed", "ENG-7 ready"])
        self.assertLessEqual(self.out.splitlines()[0].count("readiness:"), 1, "no doubled readiness: prefix")

    def test_line_shapes(self):
        L = fc_readiness.readiness_line
        self.assertEqual(L(dict(READY, has_tasks=False)), "ENG-7 ready (no tasks yet: the planner cuts them)")
        self.assertEqual(L(dict(READY, ready=False, error="boom")), "ENG-7 not judged: boom")
        self.assertEqual(L(dict(READY, ready=False, specced=None, state="Done")), "ENG-7 not open: Done")
        self.assertEqual(L(dict(READY, ready=False, specced=False, unblocked=False, needs_shaping=True, blocked_by=["ENG-3"])), "ENG-7 not ready (blocked by ENG-3): needs shaping")

    def test_the_verb_parses_its_flags(self):
        import subprocess
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "compose"), "readiness", "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        for f in ("--refresh", "--dry-run", "--json"):
            self.assertIn(f, r.stdout)


if __name__ == "__main__":
    unittest.main()
