import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

BINDING = "Each value becomes the drawn value, bound to a variable, text style or effect style; use the ones the earlier tasks made"
OWN = "The page has 20 colour primitives named color/<ramp>/<step>"


def task(crits, body="Add the colour primitives as variables. Binding the existing nodes to them is a later task of the story.\n\n"):
    desc = body + "## Acceptance criteria\n" + "".join(f"- {c}\n" for c in crits) + "\n```compose\nid: 001\nkind: design\nnodes: Tokens/Colour\ntest: t\n```\n"
    return {"id": "ENG-3", "title": "Colour primitives", "description": desc, "status": "Todo", "blockedBy": []}


def big_tree(parts=3):
    """A tree of `parts` chunks of variables, with no binding stated anywhere: Framelink is read-only data."""
    kids = [{"id": f"1:{i}", "name": f"color/{i}", "type": "RECTANGLE", "fills": "#FF0000", "padding": "x" * 9000} for i in range(parts * 3)]
    return "NODE 9:1\n" + json.dumps({"metadata": {}, "nodes": [{"id": "9:1", "name": "Ramps", "type": "FRAME", "children": kids}], "globalVars": {}}, indent=2) + "\n"


class Judge(unittest.TestCase):
    """#487: a token-only design task was marked `failed` on a criterion about binding, which a later task of the story does."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "figma": "AbCdEf123456"}))
        (self.proj / "verify.json").write_text(json.dumps({"acceptance_threshold": 0.6}))
        for mod, name, value in ((fc.fc_config, "ENV_FILE", self.proj / "none.env"), (fc.fc_core, "say", lambda m: None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def judge(self, crits, tree, chunk, scope=None, confirm=None, settle=None, force=False):
        """chunk(criterion, part_text) -> (choice, p_not_met); scope(criterion) -> (OWN or OTHER, p) or an exception; confirm and settle are
        the fast model's replies to a NOT_MET confirm and to the whole-tree check."""
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [task(crits)]})
        (self.proj / "results" / "001.tree.txt").write_text(tree)
        self.asked, self.scope_asks = [], []

        def jev(env, cfg, state, q, label):
            if label == "figma judge scope":
                self.scope_asks.append((state, q))
                got = {k: scope(v["instructions"]["criterion"]) if scope else ("OWN", 0.95) for k, v in q.items()}
                if isinstance(next(iter(got.values())), Exception):
                    raise next(iter(got.values()))
                return {"answers": {k: {"choice": c, "probabilities": {c: p}} for k, (c, p) in got.items()}}
            if label.endswith("re-ask"):
                return {"answers": {"c0": {"choice": "UNCLEAR", "probabilities": {"UNCLEAR": 0.9}}}}
            self.asked.append([v["instructions"]["criterion"] for v in q.values()])
            ans = {k: chunk(v["instructions"]["criterion"], state["tree"]) for k, v in q.items()}
            return {"model": "jev-test", "answers": {k: {"choice": c, "probabilities": {c: 0.9, "NOT_MET": p}} for k, (c, p) in ans.items()}}

        def fast(env, system, user, **kw):
            return settle if "SATISFIES" in system else confirm
        self.out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_models, "jev_ask", side_effect=jev), \
                mock.patch.object(fc.fc_models, "fast_json", side_effect=fast), mock.patch.object(fc.fc_linear, "linear_event"), mock.patch("sys.stdout", self.out):
            try:
                fc.fc_figma.cmd_figma(SimpleNamespace(repo=".", project="p", action="judge", figma=None, only="001", force=force))
            except SystemExit as e:
                self.assertEqual(e.code, 1)
        return json.loads((self.proj / "results" / "001.json").read_text())

    def test_status_failed_needs_a_confirmed_not_met(self):
        """The shape of the issue: a binding criterion that parts called NOT_MET, a confirm that refuted them, a whole-tree check that said no.
        Nothing is violated and nothing is shown met: that is not `failed`, and the list of failures is empty."""
        no = {"violated": False, "evidence": "no node violates it; the tree states no binding either way"}
        res = self.judge([BINDING], big_tree(), lambda c, t: ("NOT_MET", 0.59), scope=lambda c: ("OWN", 0.95), confirm=no, settle={"confirmed": False, "evidence": "no binding is stated"})
        self.assertGreater(res["chunks"], 1)
        row = res["criteria"][0]
        self.assertEqual((row["choice"], row["confirmed"], row["whole_tree"]["met"], res["failed"]), ("UNCLEAR", False, False, []))
        self.assertEqual(res["status"], "unclear")
        self.assertIn("001 UNCLEAR: no criterion is violated", self.out.getvalue())
        self.assertIn("compose done --project p --only 001", self.out.getvalue())

    def test_a_confirmed_not_met_still_fails_and_all_met_still_passes(self):
        yes = {"violated": True, "evidence": "color/3 has no name"}
        res = self.judge([OWN], big_tree(), lambda c, t: ("NOT_MET", 0.9), confirm=yes)
        self.assertEqual((res["status"], len(res["failed"])), ("failed", 1))
        res = self.judge([OWN], big_tree(), lambda c, t: ("MET", 0.0))
        self.assertEqual((res["status"], res["failed"], res["skipped"]), ("passed", [], []))

    def test_a_criterion_a_later_task_owns_is_not_judged_and_does_not_fail_the_task(self):
        scope = lambda c: ("OTHER", 0.95) if c == BINDING else ("OWN", 0.95)
        res = self.judge([OWN, BINDING], big_tree(), lambda c, t: ("MET", 0.0), scope=scope)
        self.assertEqual(res["status"], "passed")
        self.assertEqual([r["criterion"] for r in res["criteria"]], [OWN])
        self.assertEqual([(r["criterion"], r["choice"]) for r in res["skipped"]], [(BINDING, "NOT_APPLICABLE")])
        self.assertIn("p_other", res["skipped"][0]["scope"])
        self.assertNotIn(BINDING, [c for ask in self.asked for c in ask])  # the judge was never asked it
        self.assertIn(f"001 SKIPPED, not this task's work: {BINDING}", self.out.getvalue())
        state, q = self.scope_asks[0]
        self.assertIn("later task of the story", state["brief"])
        self.assertEqual(state["task"], "Colour primitives")
        self.assertEqual(sorted(q["c0"]["criteria"]), ["OTHER", "OWN"])

    def test_only_a_sure_other_skips_a_criterion_and_a_task_never_skips_all_of_its_own(self):
        res = self.judge([OWN, BINDING], big_tree(), lambda c, t: ("MET", 0.0), scope=lambda c: ("OTHER", 0.7))
        self.assertEqual((len(res["criteria"]), res["skipped"]), (2, []))
        res = self.judge([BINDING], big_tree(), lambda c, t: ("MET", 0.0), scope=lambda c: ("OTHER", 0.99))
        self.assertEqual((len(res["criteria"]), res["skipped"], res["status"]), (1, [], "passed"))  # nothing is left to judge: judge it all

    def test_a_scope_call_that_fails_or_answers_oddly_judges_every_criterion(self):
        for scope in (lambda c: RuntimeError("typesafe: HTTP 500"), lambda c: ("MET", 0.9)):
            res = self.judge([OWN, BINDING], big_tree(), lambda c, t: ("MET", 0.0), scope=scope)
            self.assertEqual((len(res["criteria"]), res["skipped"], res["status"]), (2, [], "passed"))
        res = self.judge([OWN, BINDING], big_tree(), lambda c, t: ("MET", 0.0), scope=lambda c: RuntimeError("typesafe: HTTP 500"))
        self.assertIn("001: could not tell which criteria are this task's own, so it judges every one: typesafe: HTTP 500", self.out.getvalue())

    def test_a_node_without_a_screen_is_said_plainly_and_recorded(self):
        shots = self.proj / "results" / "001.screens"
        shots.mkdir()
        (shots / "screens.json").write_text(json.dumps({"screens": [["1:1 (1 of 2, top to bottom)", "a.png"], ["1:1 (2 of 2, top to bottom)", "b.png"]], "missing": ["1:2", "1:3"]}))
        with mock.patch.object(fc.fc_figma.fc_figmashots, "judge_screens", return_value=[None]):
            res = self.judge([OWN], "FRAME\n", lambda c, t: ("MET", 0.0))
        self.assertEqual(res["screens_missing"], ["1:2", "1:3"])
        self.assertIn("001: screens cover 1 of 3 nodes; 1:2, 1:3 have no screen and are judged from the tree alone", self.out.getvalue())

    def test_a_tree_too_big_to_read_whole_says_so_on_the_criterion_it_could_not_settle(self):
        huge = big_tree(40)
        self.assertGreater(len(huge), fc.fc_figma.SETTLE_TREE_CHARS)
        res = self.judge([OWN], huge, lambda c, t: ("UNCLEAR", 0.0), settle={"confirmed": True, "evidence": "x"})
        row = res["criteria"][0]
        self.assertIn("not read whole", row["whole_tree_skipped"])
        self.assertIn(str(res["tree_chars"]), row["whole_tree_skipped"])
        self.assertEqual(res["status"], "unclear")
        self.assertIn("not read whole", self.out.getvalue())

    def test_the_confirm_evidence_keeps_the_reason_past_300_characters(self):
        long = "NODE 1:2 is a sample rectangle whose radius reads as a raw value of 8 and not a variable. " * 8
        res = self.judge([OWN], "FRAME\n", lambda c, t: ("NOT_MET", 0.9), confirm={"violated": True, "evidence": long})
        self.assertGreater(len(long), 300)
        self.assertEqual(res["criteria"][0]["evidence"], long)


if __name__ == "__main__":
    unittest.main()
