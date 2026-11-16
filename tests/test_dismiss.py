import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, shutil, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_review, fc_taskreview, fc_ledger  # noqa: E402

RING = {"severity": "high", "perspective": "spec", "file": "src/Ring.tsx", "line": 46, "note": "Plan step 11 orders the `pending` prop removal last; this diff removes it now",
        "confirmed": True, "evidence": "pending?: boolean", "fix": ""}
OTHER = {"severity": "high", "perspective": "correctness", "file": "src/util.ts", "line": 3, "note": "add subtracts", "confirmed": True, "evidence": "return a - b", "fix": "return a + b"}
TASKS = [{"id": "258", "files": ["src/Ring.tsx"], "protected": []}, {"id": "262", "files": ["src/Ring.tsx"], "protected": []}, {"id": "264", "files": ["src/util.ts"], "protected": []}]


class FindingId(unittest.TestCase):
    def test_the_id_follows_the_file_and_the_words_and_nothing_else(self):
        a = fc_review.finding_id(RING)
        self.assertRegex(a, r"^[0-9a-f]{8}$")
        self.assertEqual(fc_review.finding_id({**RING, "line": 99, "severity": "medium", "confirmed": False, "evidence": "x"}), a)
        self.assertEqual(fc_review.finding_id({**RING, "note": "  " + RING["note"].upper().replace(" ", "\n ") + " "}), a)
        self.assertEqual(fc_review.finding_id({**RING, "file": "./src/Ring.tsx"}), a)
        self.assertNotEqual(fc_review.finding_id({**RING, "file": "src/Other.tsx"}), a)
        self.assertNotEqual(fc_review.finding_id({**RING, "note": "another claim"}), a)


class Dismissals(unittest.TestCase):
    """#469: a false finding stayed confirmed and failed verify, and nothing let the team lead dismiss it."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"
        (self.proj / "results").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.proj.parent, True)

    def review(self, dismissed, raised, confirm=None):
        """llm_review over `raised` findings (the reviewer's reply), every confirm call answering `confirm` (confirmed or not); returns (result, confirm prompts)."""
        prompts = []
        def fj(env, system, user, **kw):
            if "strict code reviewer" in system:
                return {"issues": [{k: v for k, v in i.items() if k in ("file", "line", "severity", "note")} for i in raised]}
            prompts.append(user)
            return {"requirement": "r", "wrong_line": "pending?: boolean", "fixed_line": "", "proof": "", "confirmed": True if confirm is None else confirm}
        with mock.patch.object(fc.fc_models, "fast_json", fj), mock.patch.object(fc.fc_core, "load_env", return_value={}):
            return fc_review.llm_review({"perspectives": ["spec"]}, "-  pending?: boolean\n", "plan", "", False, dismissed), prompts

    def test_a_dismissed_finding_is_not_confirmed_again_and_fails_nothing_and_costs_no_confirm_call(self):
        reason = "no caller passes pending: git grep has 0 hits at the layer and at the top"
        dismissed = [{"id": fc_review.finding_id(RING), "file": RING["file"], "note": RING["note"], "reason": reason}]
        rv, prompts = self.review(dismissed, [RING])
        self.assertEqual((rv["pass"], rv["confirmed"], prompts), (True, [], []))
        self.assertEqual(rv["dismissed"], [{"id": dismissed[0]["id"], "file": RING["file"], "note": RING["note"], "reason": reason}])
        self.assertEqual((rv["issues"][0]["confirmed"], rv["issues"][0]["dismissed"]), (False, reason))

    def test_another_finding_still_fails_and_the_confirm_call_is_told_what_was_dismissed(self):
        dismissed = [{"id": fc_review.finding_id(RING), "file": RING["file"], "note": RING["note"], "reason": "no caller passes pending"}]
        rv, prompts = self.review(dismissed, [RING, OTHER])
        self.assertEqual(([i["file"] for i in rv["confirmed"]], rv["pass"]), (["src/util.ts"], False))
        [prompt] = prompts
        self.assertIn("DISMISSED BY THE TEAM LEAD", prompt)
        self.assertIn("src/Ring.tsx: Plan step 11 orders the `pending` prop removal last", prompt)
        self.assertIn("(reason: no caller passes pending)", prompt)

    def test_the_same_claim_in_other_words_is_left_to_the_confirm_call_which_is_told_it_was_dismissed(self):
        dismissed = [{"id": fc_review.finding_id(RING), "file": RING["file"], "note": RING["note"], "reason": "no caller passes pending"}]
        reworded = {**RING, "note": "Removing the pending prop breaks the typecheck of its callers"}
        rv, prompts = self.review(dismissed, [reworded], confirm=False)
        self.assertEqual((rv["pass"], rv["confirmed"], len(prompts), "dismissed" in rv), (True, [], 1, False))
        self.assertIn("no caller passes pending", prompts[0])

    def test_without_a_dismissal_the_prompt_is_as_it_was(self):
        rv, prompts = self.review([], [RING])
        self.assertNotIn("DISMISSED", prompts[0])
        self.assertEqual(([i["file"] for i in rv["confirmed"]], "dismissed" in rv), (["src/Ring.tsx"], False))

    def test_dismissals_are_kept_in_the_project_and_a_damaged_file_reads_as_none(self):
        rec = fc_review.keep_dismissal(self.proj, RING, "  no caller\npasses it ")
        self.assertEqual((rec["id"], rec["reason"], rec["file"]), (fc_review.finding_id(RING), "no caller passes it", "src/Ring.tsx"))
        fc_review.keep_dismissal(self.proj, RING, "second word")  # the same finding again replaces the first
        self.assertEqual([(d["id"], d["reason"]) for d in fc_review.read_dismissed(self.proj)], [(rec["id"], "second word")])
        for text in ("", "{bad", '{"a": 1}', '[{"id": "x"}, 3]'):
            fc_review.dismissed_file(self.proj).write_text(text)
            self.assertEqual(fc_review.read_dismissed(self.proj), [])


class DismissCommand(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.proj = self.repo / ".compose" / "feat"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
        self.addCleanup(shutil.rmtree, self.repo, True)
        (self.proj / "results" / "verify.json").write_text(json.dumps({"rounds": [{"review": {"confirmed": [RING, OTHER], "held": [RING]}}]}))
        self.said = []

    def run_dismiss(self, ids, reason="no caller passes pending"):
        a = SimpleNamespace(repo=str(self.repo), project="feat", only=None, range=None, dismiss=ids, reason=reason)
        out = io.StringIO()
        sha = SimpleNamespace(stdout="f" * 40 + "\n", returncode=0)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_core, "say", self.said.append), mock.patch.object(fc.fc_core, "sh", return_value=sha), \
                mock.patch.object(fc_ledger, "record", return_value=True) as record, mock.patch.object(fc_review, "llm_review") as llm, contextlib.redirect_stdout(out):
            try:
                fc_taskreview.cmd_review(a)
                code = None
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), record, llm

    def test_a_finding_of_the_last_verify_is_dismissed_with_its_reason_in_the_project_and_on_the_ledger_and_no_review_runs(self):
        fid = fc_review.finding_id(RING)
        code, out, record, llm = self.run_dismiss(fid)
        self.assertIsNone(code)
        llm.assert_not_called()
        [d] = fc_review.read_dismissed(self.proj)
        self.assertEqual((d["id"], d["file"], d["reason"]), (fid, "src/Ring.tsx", "no caller passes pending"))
        [(feature, events)] = [c.args for c in record.call_args_list]
        self.assertEqual((feature, [(e["phase"], e["source"], e["result"], e["detail"]) for e in events]), ("ENG-1", [("review", "dismiss", "dismissed", f"{fid} src/Ring.tsx: no caller passes pending")]))
        self.assertEqual(self.said, [f"review dismissed {fid} src/Ring.tsx:46 [high]: no caller passes pending"])
        self.assertIn("dismissed.json", out)

    def test_an_id_prefix_and_several_ids_work_and_an_unknown_or_missing_reason_stops_with_the_ids_to_use(self):
        a, b = fc_review.finding_id(RING), fc_review.finding_id(OTHER)
        self.assertIsNone(self.run_dismiss(f"{a[:5]},{b}")[0])
        self.assertEqual(sorted(d["id"] for d in fc_review.read_dismissed(self.proj)), sorted([a, b]))
        code, *_ = self.run_dismiss("deadbeef")
        self.assertIn("deadbeef names no one confirmed finding", code)
        self.assertIn(f"  {a} [high] src/Ring.tsx: Plan step 11", code)
        self.assertIn("review --dismiss needs --reason", self.run_dismiss(a, reason=" ")[0])

    def test_a_ledger_that_cannot_be_written_fails_the_command_but_the_dismissal_is_kept(self):
        a = SimpleNamespace(repo=str(self.repo), project="feat", only=None, range=None, dismiss=fc_review.finding_id(RING), reason="r")
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_core, "say") as say, \
                mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(stdout="f" * 40, returncode=0)), mock.patch.object(fc_ledger, "record", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            fc_taskreview.cmd_review(a)
        self.assertEqual(e.exception.code, 1)
        self.assertIn("kept in dismissed.json but not saved on Linear", say.call_args.args[0])
        self.assertEqual(len(fc_review.read_dismissed(self.proj)), 1)


class HeldFindings(unittest.TestCase):
    """The auto-fix round tried to land a fix on a lower layer's file, which wrong_layer reverts: the round cost a model call and ended with the same FAIL."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "p"
        (self.proj / "results").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.proj.parent, True)
        self.tasks = mock.patch.object(fc.fc_tasks, "load_tasks", return_value=TASKS); self.tasks.start(); self.addCleanup(self.tasks.stop)

    def held(self, confirmed, top="264"):
        with mock.patch.object(fc.fc_verify.fc_fixround, "top_task", return_value=top):
            return fc.fc_verify.fc_fixround.held_findings(self.proj, self.proj, confirmed)

    def test_a_finding_on_a_file_a_lower_layer_owns_and_the_top_does_not_is_held_with_its_owners(self):
        self.assertEqual(self.held([RING, OTHER]), [(RING, ["258", "262"], "264")])
        self.assertEqual(self.held([{**RING, "file": "./src/Ring.tsx"}])[0][1:], (["258", "262"], "264"))

    def test_nothing_is_held_for_the_top_layers_own_file_a_file_no_task_lists_or_a_stack_without_a_top(self):
        self.assertEqual(self.held([OTHER, {**OTHER, "file": "README.md"}]), [])
        self.assertEqual(self.held([RING], top="262"), [])  # the top owns it too
        self.assertEqual(self.held([RING], top=None), [])
        self.assertEqual(self.held([]), [])

    def stages(self, confirmed, top="264"):
        rv = {"pass": False, "issues": confirmed, "confirmed": confirmed, "malformed": 0}
        out = io.StringIO()
        with mock.patch.object(fc.fc_verify.fc_review, "llm_review", return_value=rv), mock.patch.object(fc.fc_verify, "review_inputs", return_value=("d", "p", "", [])), \
                mock.patch.object(fc.fc_verify.fc_fixround, "top_task", return_value=top), mock.patch.object(fc.fc_core, "say"), contextlib.redirect_stdout(out):
            ok, report, findings = fc.fc_verify.run_stages(self.proj, self.proj, {"jev_acceptance": False}, SimpleNamespace(skip_e2e=True, skip_review=False))
        return ok, report, findings, out.getvalue()

    def test_a_held_finding_still_fails_verify_names_what_to_do_and_is_not_handed_to_the_fixer(self):
        ok, report, findings, out = self.stages([RING])
        self.assertEqual((ok, findings, report["review"]["held"]), (False, "", [RING]))
        fid = fc_review.finding_id(RING)
        self.assertIn(f"held      {fid} src/Ring.tsx belongs to layer 258, 262, not the top layer 264", out)
        self.assertIn("If it is false, dismiss it; if it is true, cut a new top task (id above 264) whose files: lists src/Ring.tsx, run it, then verify again", out)
        self.assertIn(f"confirmed [high/spec] src/Ring.tsx:46: {RING['note']}\n             id {fid}\n", out)
        self.assertIn(f'compose review --project p --dismiss <id> --reason "<why it is false>"', out)

    def test_the_fixer_still_gets_the_findings_it_can_land(self):
        ok, report, findings, _ = self.stages([RING, OTHER])
        self.assertIn("src/util.ts: add subtracts", findings)
        self.assertNotIn("Ring.tsx", findings)
        self.assertEqual(report["review"]["held"], [RING])

    def test_a_dismissed_finding_is_listed_with_its_reason_and_is_no_failure(self):
        dismissed = {**RING, "confirmed": False, "dismissed": "no caller passes pending"}
        rv = {"pass": True, "issues": [dismissed], "confirmed": [], "malformed": 0, "dismissed": [{"id": fc_review.finding_id(RING), "file": "src/Ring.tsx", "note": "n", "reason": "no caller passes pending"}]}
        out = io.StringIO()
        with mock.patch.object(fc.fc_verify.fc_review, "llm_review", return_value=rv) as llm, mock.patch.object(fc.fc_verify, "review_inputs", return_value=("d", "p", "", [])), \
                mock.patch.object(fc.fc_verify.fc_fixround, "top_task", return_value="264"), mock.patch.object(fc.fc_core, "say"), contextlib.redirect_stdout(out):
            (self.proj / "dismissed.json").write_text(json.dumps([{"id": "abc12345", "file": "f", "note": "n", "reason": "r"}]))
            ok, report, findings = fc.fc_verify.run_stages(self.proj, self.proj, {"jev_acceptance": False}, SimpleNamespace(skip_e2e=True, skip_review=False))
        self.assertEqual((ok, findings), (True, ""))
        self.assertEqual(llm.call_args.args[5], [{"id": "abc12345", "file": "f", "note": "n", "reason": "r"}])  # the project's dismissals reach the review
        self.assertIn("dismissed [high/spec] src/Ring.tsx:46", out.getvalue())
        self.assertIn("dismissed by the team lead: no caller passes pending", out.getvalue())

    def test_a_fix_round_is_not_started_when_every_confirmed_finding_is_held(self):
        repo = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, repo, True)
        proj = repo / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        report = {"review": {"confirmed": [RING], "held": [RING], "issues": [RING], "pass": False}}
        fixer, said = mock.Mock(), []
        with mock.patch.object(fc.fc_verify, "run_stages", return_value=(False, report, "")), mock.patch.object(fc_ledger, "verify_record"), mock.patch.object(fc.fc_accept, "jev_sufficiency", return_value=None), \
                mock.patch.object(fc.fc_verify.fc_reroute, "after_round", return_value=([], {})), mock.patch.object(fc.fc_implement, "implementer", fixer), mock.patch.object(fc.fc_worktree, "review_base", return_value="BASE"), \
                mock.patch.object(fc.fc_worktree, "dirty_paths", return_value=[]), mock.patch.object(fc.fc_verify, "verify_failed"), mock.patch.object(fc.fc_linear, "linear_reconcile"), \
                mock.patch.object(fc.fc_core, "say", said.append), mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(stdout="", stderr="", returncode=0)), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            fc.fc_verify.verify_rounds(repo, proj, {}, SimpleNamespace(fix=2, skip_review=True), [], 0)
        self.assertEqual(e.exception.code, 1)
        fixer.assert_not_called()
        self.assertIn("fix    skip  every confirmed finding is in a lower layer's file (see held above): no fix round can land it; dismiss it or cut a top task", said)
        self.assertNotIn("round 1", " ".join(said))


class DismissedOnTheLedger(unittest.TestCase):
    def test_the_verify_review_row_counts_the_dismissed_and_each_gets_a_row_with_its_reason(self):
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, proj.parent, True)
        (proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
        dismissed = [{"id": "abc12345", "file": "src/Ring.tsx", "note": "n", "reason": "no caller passes pending"}]
        report = {"checks": [], "e2e": [], "review": {"pass": True, "confirmed": [], "issues": [{}, {}], "dismissed": dismissed}}
        with mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(stdout="f" * 40)), mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[]), \
                mock.patch.object(fc_ledger, "record", return_value=True) as record:
            fc_ledger.verify_record(proj, proj, report)
        review = next(c.args[1] for c in record.call_args_list if any(e["phase"] == "review" for e in c.args[1]))
        self.assertEqual([(e["result"], e["detail"]) for e in review], [("pass", "0 confirmed of 2 raised, 1 dismissed by the team lead"), ("dismissed", "abc12345 src/Ring.tsx: no caller passes pending")])


if __name__ == "__main__":
    unittest.main()
