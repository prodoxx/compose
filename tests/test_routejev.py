import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, sys, unittest
from pathlib import Path
from unittest import mock
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_routejev  # noqa: E402  (bin is on sys.path now; compose.py does not import it yet)

STATE = {"task": "Greet by name", "criteria": ["works"], "files": [{"path": "bin/x.py", "top": "bin", "exists": True}], "pin": None}
PLAIN = [{"name": "Secrets", "statement": "API keys are checked end to end.", "globs": [], "checks": ["t"]}, {"name": "Docs", "statement": "Docs match the CLI.", "globs": [], "checks": []}]
CALLERS = {"callers": {"bin/x.py": ["bin/y.py"]}}
def ans(choice, conf): return {"choice": choice, "probabilities": {choice: 0.8}, "confidence": conf}
def reply(**answers): return {"model": "jev-test", "answers": answers}
def pick(route, *keys): return tuple(route[k] for k in keys)


class Ask(unittest.TestCase):
    def decide(self, side_effect, state=STATE, cfg=None, plain=()):
        more = mock.Mock(return_value=CALLERS)
        with mock.patch.object(fc.fc_models, "jev_ask", side_effect=side_effect) as ask:
            route = fc_routejev.decide({}, cfg or {}, state, [], list(plain), more)
        return route, ask, more

    def test_questions_follow_pin_and_plain_policies(self):
        q, yn = fc_routejev.route_questions(STATE, PLAIN), {f"policy{i}": ("choice", {"APPLIES", "DOES_NOT_APPLY"}) for i in (0, 1)}
        self.assertEqual({k: (v["type"], set(v["criteria"])) for k, v in q.items()},
                         {"workflow": ("choice", {"TEST_FIRST", "IMPLEMENT_THEN_TEST", "FIX_THEN_REGRESSION_TEST"}), "depth": ("choice", {"FAST", "STANDARD", "DEEP"}), **yn})
        self.assertIn(PLAIN[1]["statement"], json.dumps(q["policy1"]["instructions"]))
        self.assertEqual(set(fc_routejev.route_questions({**STATE, "pin": "implement-then-test"}, [])), {"depth"})

    def test_confident_answers_route_without_retry(self):
        good = reply(depth=ans("STANDARD", 0.65), workflow=ans("FIX_THEN_REGRESSION_TEST", 0.6), policy0=ans("APPLIES", 0.9))
        route, ask, more = self.decide([good], plain=PLAIN[:1])
        self.assertEqual(route, {"workflow": "fix-then-regression-test", "depth": "DEEP", "decided_by": "jev", "confidence": 0.6,
                                 "answers": good["answers"], "retry": None, "fallback": None, "fell_back": [], "floor": ["Secrets"], "checks": ["t"]})
        self.assertEqual((ask.call_args.args[2:], more.call_count), ((STATE, fc_routejev.route_questions(STATE, PLAIN[:1]), "Jev route"), 0))
        route, ask, _ = self.decide([reply(depth=ans("FAST", 0.9))], state={**STATE, "pin": "fix-then-regression-test"})
        self.assertEqual((ask.call_count, *pick(route, "workflow", "depth", "decided_by", "confidence")), (1, "fix-then-regression-test", "FAST", "pin + jev", 0.9))

    def test_missing_answer_retries_once_with_dependencies(self):
        second = reply(depth=ans("DEEP", 0.9), workflow=ans("IMPLEMENT_THEN_TEST", 0.8))
        route, ask, more = self.decide([reply(depth=ans("FAST", 0.9)), second])
        self.assertEqual(ask.call_args_list[1].args[2:], ({**STATE, "dependencies": CALLERS}, ask.call_args_list[0].args[3], "Jev route retry"))
        self.assertEqual((more.call_count, *pick(route, "workflow", "depth", "decided_by", "retry", "answers")), (1, "implement-then-test", "DEEP", "jev after retry", CALLERS, second["answers"]))

    def test_each_unsure_answer_falls_back_on_its_own_with_the_threshold(self):
        last = reply(depth=ans("DEEP", 0.75), workflow={"choice": "IMPLEMENT_THEN_TEST", "probabilities": {}})
        route, ask, _ = self.decide([reply(depth=ans("DEEP", 0.7), workflow=ans("TEST_FIRST", 0.9)), last], cfg={"route_threshold": 0.8})
        self.assertEqual((ask.call_count, *pick(route, "depth", "workflow", "decided_by", "fallback", "fell_back", "confidence", "answers")),
                         (2, "STANDARD", "test-first", "fallback", "confidence below 0.8", ["workflow", "depth"], 0.75, last["answers"]))
        sure = reply(depth=ans("DEEP", 0.9), workflow=ans("IMPLEMENT_THEN_TEST", 0.2))
        route, ask, _ = self.decide([sure, sure])
        self.assertEqual((ask.call_count, *pick(route, "depth", "workflow", "decided_by", "fell_back")), (2, "DEEP", "test-first", "jev after retry + fallback", ["workflow"]))
        sure = reply(depth=ans("FAST", 0.3), workflow=ans("IMPLEMENT_THEN_TEST", 0.9))
        route, _, _ = self.decide([sure, sure])
        self.assertEqual(pick(route, "depth", "workflow", "decided_by", "fell_back"), ("STANDARD", "implement-then-test", "jev after retry + fallback", ["depth"]))
        route, _, _ = self.decide([reply(depth=ans("DEEP", 0.5))] * 2, state={**STATE, "pin": "implement-then-test"})
        self.assertEqual(pick(route, "workflow", "depth", "decided_by", "fallback"), ("implement-then-test", "STANDARD", "pin + fallback", "confidence below 0.6"))

    def test_a_protected_test_decides_test_first_without_a_workflow_question_unless_bug_or_pin(self):
        test = {"protected": ["tests/test_x.py"], "command": "t"}
        got = {"depth": ans("DEEP", 0.9), "policy0": ans("DOES_NOT_APPLY", 0.9)}
        state = {**STATE, "test": test, "bug": {"label": False}}
        route, ask, more = self.decide([reply(**got)], state=state, plain=PLAIN[:1])
        self.assertEqual((set(ask.call_args.args[3]), more.call_count, *pick(route, "workflow", "depth", "decided_by", "fell_back")), ({"depth", "policy0"}, 0, "test-first", "DEEP", "rule + jev", []))
        route, _, _ = self.decide([reply(depth=ans("FAST", 0.2))] * 2, state=state)
        self.assertEqual(pick(route, "workflow", "depth", "decided_by", "fell_back"), ("test-first", "STANDARD", "rule + fallback", ["depth"]))
        self.assertIn("workflow", fc_routejev.route_questions({**state, "pin": None, "test": None}, []))
        self.assertNotIn("workflow", fc_routejev.route_questions({**state, "pin": "implement-then-test"}, []))

    def test_a_bug_label_decides_fix_then_regression_test_by_rule_and_a_pin_wins(self):
        state = {**STATE, "bug": {"label": True}}
        self.assertNotIn("workflow", fc_routejev.route_questions(state, []))
        route, ask, _ = self.decide([reply(depth=ans("STANDARD", 0.9))], state=state)
        self.assertEqual((set(ask.call_args.args[3]), *pick(route, "workflow", "decided_by")), ({"depth"}, "fix-then-regression-test", "rule + jev"))
        route, _, _ = self.decide([reply(depth=ans("STANDARD", 0.9))], state={**state, "pin": "implement-then-test", "test": {"protected": ["t"]}})
        self.assertEqual(pick(route, "workflow", "decided_by"), ("implement-then-test", "pin + jev"))

    def test_jev_raising_falls_back_at_once_and_payment_propagates(self):
        route, ask, more = self.decide(RuntimeError("no key"))
        self.assertEqual((route, ask.call_count, more.call_count), ({"workflow": "test-first", "depth": "STANDARD", "decided_by": "fallback", "confidence": None, "answers": {},
                                 "retry": None, "fallback": "Jev unavailable: no key", "fell_back": ["workflow", "depth"], "floor": [], "checks": []}, 1, 0))
        self.assertRaises(fc.fc_models.PaymentRequired, self.decide, fc.fc_models.PaymentRequired("typesafe", "top up"))


GATE = {"name": "Gate", "statement": "Verify decides what ships.", "globs": ["bin/x.py"], "checks": ["v"]}
TRIO = [{"name": "Secrets", "statement": "Keys.", "globs": [], "checks": ["t", "v"]}, {"name": "Docs", "statement": "Docs.", "globs": [], "checks": ["d"]},
        {"name": "Style", "statement": "Style.", "globs": [], "checks": ["t"]}]
SURE = {"depth": ans("FAST", 0.9), "workflow": ans("TEST_FIRST", 0.9)}


class Floor(unittest.TestCase):
    def decide(self, side_effect, matched=(), plain=()):
        with mock.patch.object(fc.fc_models, "jev_ask", side_effect=side_effect) as ask:
            return fc_routejev.decide({}, {}, STATE, list(matched), list(plain), mock.Mock(return_value=CALLERS)), ask.call_count

    def test_glob_match_and_applying_plain_policies_floor_deep_and_change_nothing_else(self):
        good = reply(**SURE, policy0=ans("DOES_NOT_APPLY", 0.6), policy1=ans("APPLIES", 0.9), policy2=ans("DOES_NOT_APPLY", 0.95))
        bare, _ = self.decide([good])
        self.assertEqual(pick(bare, "depth", "floor", "checks"), ("FAST", [], []))
        route, calls = self.decide([good], [GATE], TRIO)
        self.assertEqual((route, calls), ({**bare, "depth": "DEEP", "floor": ["Gate", "Docs"], "checks": ["v", "d"]}, 1))

    def test_floor_reads_the_last_reply_and_an_unsure_policy_floors_without_a_fallback(self):
        first = reply(**SURE, policy0=ans("APPLIES", 0.9), policy1=ans("DOES_NOT_APPLY", 0.4), policy2=ans("APPLIES", 0.9))
        last = reply(**SURE, policy0=ans("DOES_NOT_APPLY", 0.3), policy1=ans("DOES_NOT_APPLY", 0.9))
        route, calls = self.decide([first, last], [GATE], TRIO)
        self.assertEqual((calls, *pick(route, "fallback", "depth", "floor", "checks")), (2, None, "DEEP", ["Gate", "Secrets", "Style"], ["v", "t"]))

    def test_jev_unavailable_plain_policies_keep_checks_but_set_no_floor(self):
        route, _ = self.decide(RuntimeError("no key"), plain=TRIO)
        self.assertEqual(pick(route, "depth", "floor", "checks", "fallback"), ("STANDARD", [], ["t", "v", "d"], "Jev unavailable: no key"))
        route, _ = self.decide(RuntimeError("no key"), [GATE], TRIO)
        self.assertEqual(pick(route, "depth", "floor", "checks"), ("DEEP", ["Gate"], ["v", "t", "d"]))


CHECK = {**STATE, "check": "python3 -m unittest tests/test_a.py"}
RECORDED = {"recorded": {"workflow": "check-only", "depth": "FAST", "checks": []}, "pin": "check-only"}
ONLY = "Small work whose result shows by running it: state the expected behavior, do the work, run the same check again; no written test and no suite"


class CheckOnlyDecide(unittest.TestCase):
    def decide(self, side_effect, state=CHECK, matched=(), plain=()):
        with mock.patch.object(fc.fc_models, "jev_ask", side_effect=side_effect) as ask:
            return fc_routejev.decide({}, {}, state, list(matched), list(plain), mock.Mock(return_value=CALLERS)), ask

    def test_check_only_is_offered_only_with_a_check_and_no_pin(self):
        crit = lambda state: fc_routejev.route_questions(state, [])["workflow"]["criteria"]
        self.assertEqual((crit(CHECK)["CHECK_ONLY"], len(crit(CHECK))), (ONLY, 4))
        self.assertEqual(set(crit({**STATE, "check": None})), {"TEST_FIRST", "IMPLEMENT_THEN_TEST", "FIX_THEN_REGRESSION_TEST"})
        self.assertEqual(set(fc_routejev.route_questions({**CHECK, "pin": "test-first"}, [])), {"depth"})

    def test_a_check_only_pick_routes_at_fast_whatever_the_depth(self):
        route, _ = self.decide([reply(workflow=ans("CHECK_ONLY", 0.9), depth=ans("DEEP", 0.9))])
        self.assertEqual(pick(route, "workflow", "depth", "floor"), ("check-only", "FAST", []))

    def test_a_check_only_pin_asks_no_depth_and_with_no_question_left_makes_no_call(self):
        pinned = {**CHECK, "pin": "check-only"}
        self.assertEqual(fc_routejev.route_questions(pinned, []), {})
        route, ask = self.decide([], state=pinned)
        self.assertEqual((ask.call_count, *pick(route, "workflow", "depth", "decided_by", "answers", "confidence")), (0, "check-only", "FAST", "pin", {}, None))
        route, ask = self.decide([reply(policy0=ans("DOES_NOT_APPLY", 0.9))], state=pinned, plain=PLAIN[:1])
        self.assertEqual((ask.call_count, set(ask.call_args.args[3]), *pick(route, "workflow", "depth")), (1, {"policy0"}, "check-only", "FAST"))

    def test_a_floor_turns_check_only_into_test_first_deep_decided_by_floor(self):
        gate = {"name": "Gate", "statement": "Verify.", "globs": ["bin/x.py"], "checks": ["v"]}
        want = ("test-first", "DEEP", "floor", ["Gate"], ["v"])
        route, _ = self.decide([reply(workflow=ans("CHECK_ONLY", 0.9), depth=ans("FAST", 0.9))], matched=[gate])
        self.assertEqual(pick(route, "workflow", "depth", "decided_by", "floor", "checks"), want)
        route, ask = self.decide([], state={**CHECK, "pin": "check-only"}, matched=[gate])
        self.assertEqual((ask.call_count, *pick(route, "workflow", "depth", "decided_by", "floor", "checks")), (0, *want))
        route, _ = self.decide([reply(workflow=ans("CHECK_ONLY", 0.9), depth=ans("FAST", 0.9), policy0=ans("APPLIES", 0.9))], plain=PLAIN[:1])
        self.assertEqual(pick(route, "workflow", "depth", "decided_by", "floor"), ("test-first", "DEEP", "floor", ["Secrets"]))

    def test_a_reassessment_keeps_check_only_asks_the_depth_and_a_floor_only_deepens(self):
        state = {**CHECK, **RECORDED}
        self.assertEqual(set(fc_routejev.route_questions(state, [])), {"depth"})
        route, ask = self.decide([reply(depth=ans("STANDARD", 0.9))], state=state)
        self.assertEqual((ask.call_count, *pick(route, "workflow", "depth")), (1, "check-only", "STANDARD"))
        route, _ = self.decide([reply(depth=ans("FAST", 0.9))], state=state, matched=[{"name": "Gate", "statement": "V.", "globs": ["bin/x.py"], "checks": []}])
        self.assertEqual(pick(route, "workflow", "depth", "floor"), ("check-only", "DEEP", ["Gate"]))

    def test_the_fallback_keeps_a_check_only_pin_at_fast_and_no_pin_at_standard(self):
        route, _ = self.decide(RuntimeError("no key"), state={**CHECK, "pin": "check-only"}, plain=PLAIN[:1])
        self.assertEqual(pick(route, "workflow", "depth", "fallback"), ("check-only", "FAST", "Jev unavailable: no key"))
        route, _ = self.decide(RuntimeError("no key"))
        self.assertEqual(pick(route, "workflow", "depth"), ("test-first", "STANDARD"))
