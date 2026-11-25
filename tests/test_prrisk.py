import hermetic  # first: no test sees the real home
import json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
from layers_kit import fc
os.environ["HERDR_ENV"] = ""

prrisk = fc.fc_stack.fc_prrisk
ROUTE = ("Build it.\n\n## Route\n\n* workflow: test-first\n* depth: DEEP\n* decided by: jev\n* confidence: 0.90\n"
         "* reason: 3 files under src; floor DEEP from Auth\n* verification requirements: login e2e\n* context hash: h\n")
DIFFS = {"main...compose/p-layer-01": "diff --git a/auth.py b/auth.py\n--- a/auth.py\n+++ b/auth.py\n@@\n+token = read()\n",
         "compose/p-layer-01...compose/p-layer-02": "diff --git a/menu.py b/menu.py\n--- a/menu.py\n+++ b/menu.py\n@@\n+open()\n"}
LAYERS = [{"branch": "compose/p-layer-01", "tasks": ["groundwork", "001"]}, {"branch": "compose/p-layer-02", "tasks": ["002"]}]
TASKS = [{"id": "001", "title": "Read the token", "linear": "NEB-1"}, {"id": "002", "title": "Open the menu", "linear": "NEB-2"}]
FINDINGS = [{"file": "./auth.py", "line": 4, "severity": "medium", "note": "the token is read twice"},
            {"file": "other.py", "line": None, "severity": "low", "note": "unused import"}]


class Assess(unittest.TestCase):
    """A passing verify asks the fast model how risky each layer's own diff is to merge, and keeps the answer for the stack to read."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [], "issues": {"NEB-1": {"description": ROUTE}}}))
        self.trees = {"main": "t0", "compose/p-layer-01": "t1", "compose/p-layer-02": "t2"}
        self.asked, self.said, self.replies = [], [], []
        def sh(cmd, *a, **k):
            if cmd[:2] == ["git", "rev-parse"]:
                tree = self.trees.get(cmd[-1].removesuffix("^{tree}"))
                return subprocess.CompletedProcess(cmd, 0 if tree else 1, f"{tree}\n" if tree else "", "")
            return subprocess.CompletedProcess(cmd, 0, DIFFS.get(cmd[-1], ""), "")
        def fast_object(env, system, user, shape, key, **kw):
            self.asked.append((system, user, shape, key))
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        for p in (mock.patch.object(fc.fc_core, "sh", sh), mock.patch.object(fc.fc_core, "say", self.said.append),
                  mock.patch.object(fc.fc_models, "fast_object", fast_object), mock.patch.object(fc.fc_models, "fast_env", return_value={})):
            p.start(); self.addCleanup(p.stop)

    def assess(self, **kw):
        return prrisk.assess("/repo", self.proj, LAYERS, ["groundwork-001", "002"], "main", TASKS, **kw)

    def kept(self):
        return json.loads((self.proj / "results" / "risk.json").read_text())

    def test_each_layer_is_judged_on_its_own_diff_and_the_answer_is_kept_with_its_trees(self):
        self.replies = [{"risk_level": "High", "risk_rationale": " It changes how the token\n is read. "}, {"risk_level": "low", "risk_rationale": "One new menu."}]
        self.assess(findings=FINDINGS)
        self.assertEqual(self.kept(), {"groundwork-001": {"level": "high", "rationale": "It changes how the token is read.", "below": "t0", "tree": "t1"},
                                       "002": {"level": "low", "rationale": "One new menu.", "below": "t1", "tree": "t2"}})
        (system, first, shape, key), (_, second, _, _) = self.asked
        self.assertEqual((shape, key), ('{"risk_level": "low|medium|high", "risk_rationale": str}', "risk_level"))
        for words in ("well-bounded, mostly cosmetic, or straightforward with little ambiguity", "safe to merge first with concerns addressed as follow-ups",
                      "should not be merged without explicit human approval", "one short sentence"):
            self.assertIn(words, system)
        self.assertIn("+token = read()", first)
        self.assertNotIn("+open()", first)
        self.assertIn("- Read the token (built at depth DEEP: 3 files under src; floor DEEP from Auth; checks required: login e2e)", first)
        self.assertIn("- [medium] auth.py:4: the token is read twice", first)
        self.assertNotIn("unused import", first)  # a finding on a file this layer does not change is not this layer's
        self.assertIn("+open()", second)
        self.assertIn("- Open the menu\n", second)  # no route: its title alone
        self.assertIn("REVIEW FINDINGS STILL OPEN ON ITS FILES:\nnone", second)
        self.assertEqual(self.said, ["risk   high    layer 1 of 2 (compose/p-layer-01)  It changes how the token is read.",
                                     "risk   low     layer 2 of 2 (compose/p-layer-02)  One new menu."])

    def test_a_layer_whose_trees_are_unchanged_is_not_asked_about_again_unless_fresh(self):
        self.replies = [{"risk_level": "low", "risk_rationale": "A."}, {"risk_level": "low", "risk_rationale": "B."}]
        self.assess()
        self.trees["compose/p-layer-02"] = "t2b"
        self.replies = [{"risk_level": "medium", "risk_rationale": "C."}]
        self.assess()
        self.assertEqual(len(self.asked), 3)
        self.assertEqual([self.kept()[n]["rationale"] for n in ("groundwork-001", "002")], ["A.", "C."])
        self.replies = [{"risk_level": "low", "risk_rationale": "D."}, {"risk_level": "low", "risk_rationale": "E."}]
        self.assess(fresh=True)
        self.assertEqual([self.kept()[n]["rationale"] for n in ("groundwork-001", "002")], ["D.", "E."])

    def test_a_reply_that_is_no_assessment_or_a_call_that_fails_leaves_that_layer_without_one(self):
        self.replies = [{"risk_level": "severe", "risk_rationale": "x"}, RuntimeError("rate limited")]
        self.assess()
        self.assertEqual(self.kept(), {})
        self.assertEqual(self.said, ["risk   not assessed  layer 1 of 2 (compose/p-layer-01): the reply names no level and reason",
                                     "risk   not assessed  layer 2 of 2 (compose/p-layer-02): RuntimeError: rate limited"])
        self.replies = [{"risk_level": "low", "risk_rationale": ""}, {"risk_level": "low", "risk_rationale": "x" * 400}]
        self.assess()
        self.assertEqual(list(self.kept()), ["002"])
        self.assertEqual(len(self.kept()["002"]["rationale"]), prrisk.RATIONALE_CHARS)
        self.replies = [{"risk_level": "low", "risk_rationale": "word " * 80}, {"risk_level": "low", "risk_rationale": "B."}]
        self.assess(fresh=True)
        self.assertEqual(self.kept()["groundwork-001"]["rationale"], ("word " * 59).strip() + "...")  # cut at a word

    def test_a_layer_with_no_diff_is_not_asked_about_and_a_regrouped_layers_old_answer_is_dropped(self):
        (self.proj / "results" / "risk.json").write_text(json.dumps({"001-002": {"level": "low", "rationale": "Old.", "below": "t0", "tree": "t9"}}))
        self.replies = [{"risk_level": "low", "risk_rationale": "A."}]
        with mock.patch.dict(DIFFS, {"compose/p-layer-01...compose/p-layer-02": ""}):
            self.assess()
        self.assertEqual(list(self.kept()), ["groundwork-001"])
        self.assertEqual(len(self.asked), 1)

    def test_the_review_model_judges_it_when_one_is_set(self):
        seen = []
        self.replies = [{"risk_level": "low", "risk_rationale": "A."}, {"risk_level": "low", "risk_rationale": "B."}]
        with mock.patch.object(fc.fc_config, "REVIEW_MODEL", "claude:opus"), \
                mock.patch.object(fc.fc_models, "fast_object", lambda env, *a, **k: seen.append(env.get("TEXT_MODEL")) or self.replies.pop(0)):
            self.assess()
        self.assertEqual(seen, ["claude:opus", "claude:opus"])


class RiskLines(unittest.TestCase):
    """The stack reads the kept assessment of a layer, and only while the layer's tree is the one that was judged."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / "p"
        (self.proj / "results").mkdir(parents=True)
        self.tree = "t1"
        sh = lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 0, self.tree + "\n", "")
        p = mock.patch.object(fc.fc_core, "sh", sh); p.start(); self.addCleanup(p.stop)

    def keep(self, **entry):
        (self.proj / "results" / "risk.json").write_text(json.dumps({"001": {"level": "low", "rationale": "One new menu.", "below": "t0", "tree": "t1", **entry}}))

    def lines(self, name="001"):
        return prrisk.risk_lines("/repo", self.proj, name, "compose/p-layer-01")

    def test_the_line_is_the_mark_the_level_and_the_reason(self):
        for level, line in (("low", "✅ Low: One new menu."), ("medium", "⚠️ Medium: One new menu."), ("high", "🚨 High: One new menu.")):
            self.keep(level=level)
            self.assertEqual(self.lines(), [line])

    def test_no_line_without_an_assessment_of_this_layer_as_it_stands(self):
        self.assertEqual(self.lines(), [])  # no file
        self.keep()
        self.assertEqual(self.lines("002"), [])  # another layer's
        self.tree = "t1b"
        self.assertEqual(self.lines(), [])  # the layer changed since it was judged
        self.tree = "t1"
        for bad in ({"level": "severe"}, {"rationale": ""}):
            self.keep(**bad)
            self.assertEqual(self.lines(), [])
        (self.proj / "results" / "risk.json").write_text("{not json")
        self.assertEqual(self.lines(), [])


if __name__ == "__main__":
    unittest.main()
