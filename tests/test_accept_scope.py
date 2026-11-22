import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_accept, fc_ledger, fc_evidence, fc_review, fc_taskreview  # noqa: E402

BUTTON = 'const base = "inline-flex rounded-md border border-transparent bg-clip-padding";\nconst cta = "bg-origin-border";\nconst x = 1;\n'
STYLES = ":root {\n  --a: 1;\n}\n.dark {\n  --a: 2;\n}\n"
SHADOW = "    --shadow-tooltip: 0 4px 12px 0 rgba(13, 27, 41, 0.18);"
GREP = "`button.tsx` base classes contain `border-transparent` and its `cta` variant contains `bg-origin-border`."
PER_DIFF = "This task adds that one line and changes no other line of `styles.css`; `.dark` is untouched."
OK_SH = lambda argv, cwd, check=True: subprocess.CompletedProcess(argv, 0, "a" * 40 + "\n", "")


def task(tid, files, crit, **kw):
    return {"id": tid, "title": f"task {tid}", "linear": f"ENG-{tid}", "kind": "code", "files": files, "protected": [], "test": "t",
            "body": f"## Goal\ng\n\n## Acceptance criteria\n{crit}", **kw}


class Stack(unittest.TestCase):
    """#492: the judge read a task's hunks of the cumulative diff (every later task's change to the same file too), and never the lines
    a criterion is about when the task did not change them."""
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.repo, True)
        self.proj = self.repo / ".compose" / "feat"; (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        # the stack top: task 288 added SHADOW to styles.css, a later task added --later to it, and task 290 changed a line of button.tsx
        (self.repo / "styles.css").write_text(STYLES.replace("  --a: 1;\n", "  --a: 1;\n" + SHADOW + "\n").replace(".dark {\n", ".dark {\n  --later: 3;\n"))
        (self.repo / "button.tsx").write_text(BUTTON.replace("const x = 1;", "const x = 2;"))
        self.own = {"288": f"diff --git a/styles.css b/styles.css\n  --a: 1;\n+{SHADOW}\n }}\n", "290": "diff --git a/button.tsx b/button.tsx\n-const x = 1;\n+const x = 2;\n"}
        self.diff = ("diff --git a/styles.css b/styles.css\n  --a: 1;\n" + f"+{SHADOW}\n }}\n .dark {{\n+  --later: 3;\n"
                     + "diff --git a/button.tsx b/button.tsx\n-const x = 1;\n+const x = 2;\n")
        self.tasks([task("288", ["styles.css"], f"* {PER_DIFF}\n"), task("290", ["button.tsx"], f"* {GREP}\n")])
        self.codes, self.said = {}, []
        def ask(env, cfg, state, questions, label):
            if label == "Jev acceptance":
                self.codes[next(iter(questions.values()))["instructions"]["task"]] = state["code"]
            return {"model": "m", "answers": {k: {"choice": "MET", "probabilities": {"MET": 0.9}} for k in questions}}
        for mod, name, kw in ((fc.fc_config, "ENV_FILE", {"new": self.proj / "none.env"}), (fc.fc_models, "jev_ask", {"side_effect": ask}),
                              (fc_evidence, "task_rows", {"return_value": []}), (fc_ledger, "counted", {"side_effect": lambda *a: a[-1]}), (fc.fc_core, "say", {"new": self.said.append}),
                              (fc_accept, "own_changes", {"side_effect": lambda *a, **k: self.own}), (fc.fc_core, "sh", {"side_effect": OK_SH})):
            p = mock.patch.object(mod, name, **kw); p.start(); self.addCleanup(p.stop)

    def tasks(self, tasks):
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": tasks}))

    def judge(self, **kw):
        return fc_accept.jev_acceptance({}, self.proj, self.diff, repo=self.repo, **kw)

    def test_a_task_is_judged_on_its_own_change_and_not_on_what_a_later_task_did_to_the_same_file(self):
        self.judge()
        self.assertIn(SHADOW, self.codes["288"])
        self.assertNotIn("--later", self.codes["288"].split("==== styles.css as it is")[0])

    def test_the_whole_file_is_shown_when_a_criterion_names_what_the_change_does_not_hold(self):
        """The grep criterion of task 290: `border-transparent` is in a line of button.tsx the task did not change."""
        self.judge()
        self.assertIn("-const x = 1;", self.codes["290"])
        self.assertIn("==== button.tsx as it is at the verified tip", self.codes["290"])
        self.assertIn("rounded-md border border-transparent bg-clip-padding", self.codes["290"])

    def test_a_criterion_about_the_change_alone_is_judged_on_the_change_alone(self):
        self.tasks([task("288", ["styles.css"], "* The task adds `--shadow-tooltip` to `styles.css`.\n"), task("290", ["button.tsx"], "* It changes `const x`.\n")])
        self.judge()
        self.assertNotIn("as it is at the verified tip", self.codes["288"] + self.codes["290"])

    def test_a_stack_without_the_tasks_branches_falls_back_to_the_hunks_of_the_diff(self):
        self.own = {}
        self.judge()
        self.assertIn(SHADOW, self.codes["288"])
        self.assertIn("--later", self.codes["288"].split("==== styles.css as it is")[0])


class Rows(unittest.TestCase):
    """Reused verdicts, and every unsettled row carrying an id the team lead can dismiss."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"; (self.proj / "results").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.proj.parent, True)
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        self.repo = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.repo, True)
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [task("264", ["a.go"], "* alpha\n* beta\n")]}))
        self.choice, self.asked, self.said, self.proof = {"alpha": "MET", "beta": "UNCLEAR"}, [], [], {}
        def ask(env, cfg, state, questions, label):
            self.asked.append(label)
            pick = lambda q: self.choice.get(q["instructions"]["criterion"], "MET")  # a choice per criterion text, the re-ask of one included
            return {"model": "m", "answers": {k: {"choice": pick(q), "probabilities": {"MET": 0.9 if pick(q) == "MET" else 0.1, "NOT_MET": 0.9 if pick(q) == "NOT_MET" else 0.0}}
                                              for k, q in questions.items()}}
        def prove(repo, proj, rows):
            for r in rows:
                if r["criterion"] in self.proof:
                    r["proven_by"] = self.proof[r["criterion"]]
        self.confirm = mock.Mock(return_value=(True, "beta"))
        for mod, name, kw in ((fc.fc_config, "ENV_FILE", {"new": self.proj / "none.env"}), (fc.fc_models, "jev_ask", {"side_effect": ask}), (fc.fc_models, "confirm_call", {"new": self.confirm}),
                              (fc_accept, "proven_by_test", {"side_effect": prove}), (fc_accept, "own_changes", {"return_value": {}}), (fc.fc_core, "say", {"new": self.said.append}),
                              (fc.fc_core, "sh", {"side_effect": OK_SH})):
            p = mock.patch.object(mod, name, **kw); p.start(); self.addCleanup(p.stop)

    def judge(self, **kw):
        self.asked.clear()
        return fc_accept.jev_acceptance({}, self.proj, "diff --git a/a.go b/a.go\n+alpha\n+beta\n", repo=self.repo, **kw)

    def test_a_kept_row_that_is_neither_met_nor_proven_is_judged_again_at_the_next_verify(self):
        first = self.judge()
        self.assertEqual([r["choice"] for r in first["criteria"]], ["MET", "UNCLEAR"])
        self.judge()
        self.assertIn("Jev acceptance", self.asked)  # #482: it was reused whole, for ever

    def test_a_kept_unproven_row_whose_test_now_covers_it_is_reused_with_its_proof(self):
        self.judge()
        self.proof = {"beta": "abc123abc123"}
        again = self.judge()
        self.assertEqual(self.asked, [])
        self.assertEqual([(r["choice"], r.get("proven_by"), r.get("reused")) for r in again["criteria"]], [("MET", None, True), ("UNCLEAR", "abc123abc123", True)])
        self.assertTrue(again["pass"])

    def test_a_kept_verdict_whose_every_row_is_met_or_proven_is_reused(self):
        self.proof = {"beta": "abc123abc123"}
        self.judge()
        self.assertEqual(self.judge()["pass"], True)
        self.assertEqual(self.asked, [])

    def test_every_row_that_is_not_met_carries_an_id_a_not_met_included(self):
        self.choice = {"alpha": "NOT_MET", "beta": "UNCLEAR"}
        acc = self.judge()
        self.assertEqual([r["choice"] for r in acc["failed"]], ["NOT_MET"])
        ids = [r["id"] for r in acc["criteria"]]
        self.assertTrue(all(i and len(i) == 8 for i in ids) and len(set(ids)) == 2)
        self.assertEqual(ids[0], fc_review.finding_id(fc_review.acceptance_finding(acc["criteria"][0])))

    def test_a_dismissed_row_passes_costs_no_confirm_call_and_is_not_judged_again_for_ever(self):
        self.choice = {"alpha": "NOT_MET", "beta": "UNCLEAR"}
        for row in self.judge()["criteria"]:
            fc_review.keep_dismissal(self.proj, fc_review.acceptance_finding(row), "the grep matches")
        self.confirm.reset_mock()
        again = self.judge(fresh=True)
        self.assertEqual((again["pass"], again["failed"], again["unclear"]), (True, [], []))
        self.assertEqual(self.confirm.call_count, 0)
        self.assertEqual([r["dismissed"] for r in again["criteria"]], ["the grep matches"] * 2)
        self.assertEqual(self.judge()["pass"], True)
        self.assertEqual(self.asked, [])  # the kept verdict is reused: a dismissed row is settled

    def test_a_reworded_criterion_is_a_new_row_and_the_old_dismissal_does_not_cover_it(self):
        acc = self.judge()
        fc_review.keep_dismissal(self.proj, fc_review.acceptance_finding(acc["criteria"][1]), "r")
        self.assertTrue(self.judge()["pass"])
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [task("264", ["a.go"], "* alpha\n* beta, reworded\n")]}))
        self.choice["beta, reworded"] = "UNCLEAR"
        again = self.judge()
        self.assertEqual((again["pass"], [r.get("dismissed") for r in again["criteria"]]), (False, [None, None]))

    def test_the_dismiss_command_takes_an_acceptance_id_of_the_last_verify(self):
        self.choice = {"alpha": "NOT_MET", "beta": "UNCLEAR"}
        acc = self.judge()
        (self.proj / "results" / "verify.json").write_text(json.dumps({"rounds": [{"acceptance": acc}]}))
        known = fc_review.last_confirmed(self.proj)
        self.assertEqual(sorted(known), sorted(r["id"] for r in acc["criteria"]))
        target = acc["criteria"][1]
        a = SimpleNamespace(repo=str(self.repo), project="feat", only=None, range=None, dismiss=target["id"], reason="built output, a test covers it")
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc_ledger, "record", return_value=True) as record, contextlib.redirect_stdout(io.StringIO()):
            fc_taskreview.cmd_review(a)
        [d] = fc_review.read_dismissed(self.proj)
        self.assertEqual((d["id"], d["reason"], d["note"]), (target["id"], "built output, a test covers it", "beta"))
        [(feature, [event])] = [c.args for c in record.call_args_list]
        self.assertEqual((feature, event["phase"], event["source"], event["result"], event["task_ids"]), ("ENG-1", "acceptance", "dismiss", "dismissed", ["264"]))

    def test_the_report_says_how_to_clear_each_blocking_row(self):
        self.choice = {"alpha": "NOT_MET", "beta": "UNCLEAR"}
        acc = self.judge()
        text = "\n".join(fc_accept.report_lines(acc, "feat"))
        for r in acc["criteria"]:
            self.assertIn(f"[264] {r['criterion']}  id {r['id']}", text)
        self.assertIn("NOT MET ", text)
        self.assertIn("compose review --project feat --dismiss <id> --reason", text)
        self.assertIn("is judged again at every verify", text)
        self.assertEqual(fc_accept.report_lines({"criteria": [], "unclear": [], "failed": []}, "feat"), [])
        self.assertIn("compose review --project feat --dismiss <id>", fc_accept.no_change_note(acc, "feat"))
        self.assertEqual(fc_accept.no_change_note({"criteria": [], "unclear": [], "failed": []}, "feat"), "")
        for r in acc["criteria"]:
            fc_review.keep_dismissal(self.proj, fc_review.acceptance_finding(r), "met")
        done = self.judge(fresh=True)
        self.assertIn("dismissed p_met=", "\n".join(fc_accept.report_lines(done, "feat")))
        self.assertNotIn("clear a row", "\n".join(fc_accept.report_lines(done, "feat")))


class ProofNote(unittest.TestCase):
    """#482: a criterion reworded after the task's test ran has no covering event; the row says why instead of staying unexplained."""
    def test_a_row_the_ledger_cannot_match_says_that_a_passing_event_names_other_criteria(self):
        proj = Path(tempfile.mkdtemp()) / "feat"; (proj / "results").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, proj.parent, True)
        (proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [task("264", ["a.go"], "* alpha\n* beta, reworded\n")]}))
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        ids = fc_ledger.criterion_ids(task("264", ["a.go"], "* alpha\n* beta, reworded\n"))
        event = lambda eid, crit: {"event_id": eid, "phase": "final", "result": "pass", "criterion_id": crit}
        rows = [{"task": "264", "criterion": "alpha", "choice": "UNCLEAR"}, {"task": "264", "criterion": "beta, reworded", "choice": "UNCLEAR"}]
        with mock.patch.object(fc_evidence, "task_rows", return_value=[]), mock.patch.object(fc_ledger, "counted", return_value=[event("e1", [ids[0]])]):
            fc_accept.proven_by_test(Path("."), proj, rows)
        self.assertEqual(rows[0].get("proven_by"), "e1")
        self.assertNotIn("proven_by", rows[1])
        self.assertIn("a passing test event (e1) names other criteria of the task, not this one", rows[1]["note"])


if __name__ == "__main__":
    unittest.main()
