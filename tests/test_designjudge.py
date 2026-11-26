import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_designjudge as dj, fc_designmatch as dm, fc_review, fc_ledger, fc_verify  # noqa: E402

NODE = {"nodes": [{"id": "7:21", "name": "Resume card", "type": "FRAME", "fills": ["#FFFFFF"], "borderRadius": "12px", "layout": {"mode": "column", "padding": "24px"},
                   "children": [{"id": "7:22", "name": "Title", "type": "TEXT", "text": "Upload your resume", "textStyle": {"fontSize": 18, "fontWeight": 600}},
                                {"id": "7:24", "name": "Button", "type": "INSTANCE", "componentProperties": {"variant": "primary"}, "fills": ["#2563EB"],
                                 "children": [{"id": "7:25", "name": "Label", "type": "TEXT", "text": "Upload resume"}]}]}]}
BARE = "export function ResumeCard() {\n  return (\n    <div>\n      <h2>Upload your resume</h2>\n      <Button variant=\"outline\">Upload resume</Button>\n    </div>\n  );\n}\n"
FILL = {"id": "7:21", "property": "fill", "design": "#FFFFFF", "code": "<div>", "why": "no class or style"}
VARIANT = {"id": "7:24", "property": "variant", "design": "variant=primary", "code": '<Button variant="outline">Upload resume</Button>', "why": "outline instead of primary"}
META = {"id": "012", "title": "Resume card", "kind": "code", "files": ["src/ResumeCard.tsx"], "protected": [], "figma_nodes": ["7-21"], "figma": "FileKey123456"}


class Stage(unittest.TestCase):
    """#496: verify passed a task whose build had none of the styling of the Figma node it names, and said nothing."""
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.repo, self.proj = root / "repo", root / "repo" / ".compose" / "p"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "ResumeCard.tsx").write_text(BARE)
        path = dm.node_path(self.proj, "FileKey123456", "7-21")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(NODE))
        self.tasks, self.raised, self.confirms, self.asked = [dict(META)], [FILL, VARIANT], {}, []

    def stage(self, cfg=None, **flags):
        def raised(env, system, user, shape, key, **kw):
            self.asked.append(user)
            if isinstance(self.raised, Exception):
                raise self.raised
            return {"mismatches": self.raised}
        def confirm(env, system, user, key="confirmed", **kw):
            self.assertEqual(key, "violated")
            prop = user.split("PROPERTY: ")[1].split("\n")[0]
            return self.confirms.get(prop, True), f"the code sets nothing for {prop}"
        report, findings, out = {}, [], io.StringIO()
        a = SimpleNamespace(**{"skip_review": False, "fresh": False, **flags})
        with mock.patch.object(dj.fc_tasks, "load_tasks", return_value=self.tasks), mock.patch.object(dj.fc_models, "fast_object", side_effect=raised), \
                mock.patch.object(dj.fc_models, "confirm_call", side_effect=confirm), contextlib.redirect_stdout(out):
            ok = dj.stage(self.repo, self.proj, cfg or {}, a, report, findings)
        return ok, report.get("design"), findings, out.getvalue()

    def test_a_bare_build_of_a_styled_node_fails_with_each_value_the_code_lacks(self):
        ok, report, findings, out = self.stage()
        self.assertFalse(ok)
        self.assertIn("Resume card [FRAME 7:21]: fill #FFFFFF; radius 12px; padding 24px", self.asked[0])  # the judge reads the node, not only the criteria
        self.assertIn("<div>", self.asked[0])
        self.assertRegex(out, r"design FAIL .*1 task\(s\) with figma_nodes, 2 mismatch\(es\) with the node confirmed of 2 raised")
        self.assertRegex(out, r"mismatch  \[012\] Resume card \[FRAME 7:21\] fill: node 7-21 has #FFFFFF; code: <div> \(no class or style\)  id [0-9a-f]{8}")
        self.assertIn('compose review --project p --dismiss <id> --reason', out)
        self.assertEqual(len(findings), 1)
        self.assertIn("task 012 does not build its Figma node 7-21 as designed (its files: src/ResumeCard.tsx)", findings[0])
        self.assertIn("- Button [INSTANCE 7:24]: variant is variant=primary; the code `<Button variant=\"outline\">Upload resume</Button>` does not carry it", findings[0])
        self.assertEqual([t["result"] for t in report["tasks"]], ["fail"])
        self.assertIn("2 mismatch(es) with node 7-21: Resume card [FRAME 7:21] fill; Button [INSTANCE 7:24] variant; rendered build not measured", report["tasks"][0]["detail"])

    def test_a_build_the_judge_finds_no_mismatch_in_passes_and_says_the_render_was_not_measured(self):
        self.raised = []
        ok, report, findings, out = self.stage()
        self.assertTrue(ok)
        self.assertEqual(findings, [])
        self.assertRegex(out, r"design ok  .*0 mismatch\(es\)")
        self.assertIn("unmeasured [012] the rendered build was not measured against node 7-21, only its code was read", out)
        self.assertIn('"assert": "compose figma match --project p --only 012"', out)
        self.assertEqual(report["unmeasured"], ["012"])

    def test_a_task_an_e2e_case_measures_is_not_called_unmeasured(self):
        self.raised = []
        cfg = {"e2e": [{"name": "card", "url": "u", "goal": "g", "assert": "compose figma match --project p --only 012"}, {"name": "other", "assert": "python check.py --only 013"}]}
        self.assertEqual(dj.measured(cfg), {"012"})
        ok, report, _, out = self.stage(cfg)
        self.assertNotIn("unmeasured", out)
        self.assertEqual(report["tasks"][0]["detail"], "code matches node 7-21")

    def test_a_mismatch_the_second_call_refutes_or_the_team_lead_dismissed_does_not_fail(self):
        self.confirms = {"fill": False}
        ok, report, findings, out = self.stage()
        self.assertFalse(ok)
        self.assertEqual([r["property"] for r in report["failed"]], ["variant"])
        self.assertNotIn("fill: node", out)
        fc_review.keep_dismissal(self.proj, fc_review.design_finding(report["failed"][0]), "the outline is the approved variant")
        ok, report, findings, out = self.stage()
        self.assertTrue(ok)
        self.assertEqual(findings, [])
        self.assertRegex(out, r"dismissed \[012\] Button \[INSTANCE 7:24\] variant.*\(dismissed by the team lead: the outline is the approved variant\)")

    def test_a_row_about_an_element_a_value_or_a_line_nobody_showed_is_dropped(self):
        self.raised = [{**FILL, "id": "9:99"}, {**FILL, "design": "#000000"}, {**FILL, "code": '<div className="card">'}, {**FILL, "code": ""}, FILL, FILL,
                       {"id": "7:25", "property": "element", "design": "", "code": "", "why": "no label"}]
        ok, report, _, out = self.stage()
        self.assertEqual([(r["element_id"], r["property"]) for r in report["mismatches"]], [("7:21", "fill"), ("7:25", "element")])
        self.assertEqual(report["tasks"][0]["dropped"], 5)
        self.assertIn("2 mismatch(es) with the node confirmed of 7 raised", out)

    def test_the_same_node_and_code_keep_their_verdict_and_a_changed_file_is_judged_again(self):
        self.stage()
        ok, report, _, out = self.stage()
        self.assertEqual(len(self.asked), 1)
        self.assertFalse(ok)
        self.assertIn("1 task(s) on a kept verdict", out)
        self.assertTrue(report["tasks"][0]["reused"])
        (self.repo / "src" / "ResumeCard.tsx").write_text(BARE.replace("<div>", '<div className="rounded-xl bg-white p-6">'))
        self.raised = []
        self.assertTrue(self.stage()[0])
        self.assertEqual(len(self.asked), 2)
        self.stage(fresh=True)
        self.assertEqual(len(self.asked), 3)

    def test_skip_review_says_the_build_was_not_compared_instead_of_passing_in_silence(self):
        ok, report, _, out = self.stage(skip_review=True)
        self.assertTrue(ok)
        self.assertIsNone(report)
        self.assertIn("design warn  figma_nodes on task 012: build not compared to the design", out)
        self.assertEqual(self.asked, [])

    def test_a_model_or_a_node_that_cannot_be_read_is_a_tool_error_never_a_pass(self):
        self.raised = RuntimeError("HTTP 503")
        ok, report, findings, out = self.stage()
        self.assertFalse(ok)
        self.assertEqual(findings, [])  # nothing for a fixer to change
        self.assertRegex(out, r"design TOOL ERROR")
        self.assertIn("error     task 012: the model could not be asked: HTTP 503", out)
        self.assertEqual(report["tasks"][0]["result"], "error")
        dm.node_path(self.proj, "FileKey123456", "7-21").unlink()
        with mock.patch.object(dm.fc_framelink, "framelink_fetch", side_effect=RuntimeError("Figma refused the token")):
            ok, report, _, out = self.stage()
        self.assertFalse(ok)
        self.assertIn("its nodes could not be read", out)

    def test_tasks_without_nodes_or_files_cost_nothing(self):
        self.tasks = [{**META, "figma_nodes": []}, {**META, "id": "013", "kind": "design"}]
        self.assertEqual(self.stage()[:2], (True, None))
        self.tasks = [{**META, "files": ["src/Gone.tsx"]}]
        ok, report, _, out = self.stage()
        self.assertTrue(ok)
        self.assertIn("skipped   [012] none of its files is in the repo: no code to compare", out)
        self.assertEqual(self.asked, [])


class Around(unittest.TestCase):
    ROW = {"task": "012", "node": "7-21", "element_id": "7:21", "element": "Resume card [FRAME 7:21]", "property": "fill", "design": "#FFFFFF", "code": "<div>", "confirmed": True}

    def test_a_confirmed_mismatch_of_the_last_verify_can_be_dismissed_by_its_id(self):
        proj = Path(tempfile.mkdtemp())
        (proj / "results").mkdir()
        (proj / "results" / "verify.json").write_text(json.dumps({"rounds": [{"review": None, "acceptance": None, "design": {"failed": [self.ROW]}}]}))
        known = fc_review.last_confirmed(proj)
        self.assertEqual(list(known), [fc_review.design_id(self.ROW)])
        self.assertEqual(known[fc_review.design_id(self.ROW)], {"file": "design of task 012", "line": None, "severity": "high", "note": "Resume card [FRAME 7:21] fill",
                                                               "task": "012", "phase": "design"})
        self.assertEqual(fc_review.design_id({**self.ROW, "code": "<section>", "why": "other words"}), fc_review.design_id(self.ROW))  # the id follows the element and property

    def test_a_fix_that_rewrites_the_line_a_mismatch_quotes_answers_it(self):
        diff = "--- a/src/ResumeCard.tsx\n+++ b/src/ResumeCard.tsx\n-    <div>\n+    <div className=\"rounded-xl bg-white p-6\">\n"
        with mock.patch.object(fc_verify.fc_core, "sh", return_value=SimpleNamespace(stdout=diff, returncode=0)):
            self.assertTrue(fc_verify.fix_answers(Path("."), ["src/ResumeCard.tsx"], {"design": {"failed": [self.ROW]}}))
            self.assertFalse(fc_verify.fix_answers(Path("."), ["src/ResumeCard.tsx"], {"design": {"failed": [{**self.ROW, "code": "<h2>Title</h2>"}]}}))

    def test_the_ledger_keeps_a_design_event_on_the_task_issue(self):
        proj = Path(tempfile.mkdtemp())
        task = {"id": "012", "linear": "ENG-12", "body": "## Acceptance criteria\n* alpha\n", "test": "t", "kind": "code"}
        report = {"checks": [], "e2e": [], "review": None, "acceptance": None,
                  "design": {"tasks": [{"task": "012", "result": "fail", "detail": "1 mismatch(es) with node 7-21: Resume card [FRAME 7:21] fill"},
                                       {"task": "013", "result": "pass", "detail": "other task"}]}}
        with mock.patch.object(fc_ledger.fc_tasks, "load_tasks", return_value=[task]), mock.patch.object(fc_ledger, "record", return_value=True), \
                mock.patch.object(fc_ledger.fc_core, "sh", return_value=SimpleNamespace(stdout="abc123\n", returncode=0)), \
                mock.patch.object(fc_ledger, "record_task", return_value=True) as kept:
            self.assertTrue(fc_ledger.verify_record(proj, Path("."), report))
        events = kept.call_args.args[3]
        self.assertEqual([(e["phase"], e["result"], e["task_ids"], e["detail"]) for e in events],
                         [("design", "fail", ["012"], "1 mismatch(es) with node 7-21: Resume card [FRAME 7:21] fill")])
        report["design"]["tasks"][0]["reused"] = True  # its row is on the issue already
        with mock.patch.object(fc_ledger.fc_tasks, "load_tasks", return_value=[task]), mock.patch.object(fc_ledger, "record", return_value=True), \
                mock.patch.object(fc_ledger.fc_core, "sh", return_value=SimpleNamespace(stdout="abc123\n", returncode=0)), \
                mock.patch.object(fc_ledger, "record_task", return_value=True) as kept:
            fc_ledger.verify_record(proj, Path("."), report)
        self.assertEqual(kept.call_args.args[3], [])


if __name__ == "__main__":
    unittest.main()
