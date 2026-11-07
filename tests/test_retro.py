import hermetic  # first: no test sees the real home
import contextlib, io, json, os, shutil, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from tests.test_trail_setup import fc
import fc_history  # noqa: E402
os.environ["HERDR_ENV"] = ""

RED = {"attempt": 1, "test_exit": 1, "lines": 12, "stray": [], "new_docs": [], "refused": 2, "summary": "DID: added f\nWHY: t.py::test_x expected 2, got 1; a.py:3\nNEXT: double it"}
GREEN_OVER = {"attempt": 2, "test_exit": 0, "lines": 410, "stray": ["b.py"], "new_docs": ["NOTES.md"], "broke": ["t.py::test_y"], "tampered": ["tests/t.py"], "refused": 0}


def result(status, log, **kw):
    return {"id": "003", "title": "Parse the node", "status": status, "model": "pi:glm", "attempts": len(log), "cost": 0.0123, "log": log, **kw}


class Kind(unittest.TestCase):
    def test_the_status_names_the_kind_for_everything_but_failed(self):
        for status, kind in (("contradiction", "brief"), ("rejected_size", "scope"), ("rejected_tampered", "scope"), ("payment_required", "harness"),
                             ("interrupted", "harness"), ("broken_base", "environment"), ("unchained", "environment")):
            self.assertEqual(fc.fc_retro.kind_of(result(status, [])), kind, status)

    def test_a_failed_task_is_the_codes_when_a_test_ran_and_the_harnesss_when_none_did(self):
        self.assertEqual(fc.fc_retro.kind_of(result("failed", [RED])), "code")
        self.assertEqual(fc.fc_retro.kind_of(result("failed", [{"attempt": 1, "error": "implementer timed out"}, {"attempt": 2, "rate_limited": True}])), "harness")
        self.assertEqual(fc.fc_retro.kind_of(result("failed", [])), "harness")


class Text(unittest.TestCase):
    def test_each_attempt_is_one_line_with_what_the_cli_saw_and_the_readers_why(self):
        lines = fc.fc_retro.attempt_lines(result("failed", [RED, GREEN_OVER, {"attempt": 3, "rate_limited": True}, {"attempt": 3, "error": "implementer timed out"},
                                                            {"attempt": 4, "payment_required": {"provider": "openrouter"}}, {"attempt": 5, "contradiction": "brief says x / code shows y"},
                                                            {"leak_reverted": ["a.py"]}]))
        self.assertEqual(lines, ["attempt 1: RED (exit 1), 12 lines, 2 refused · WHY: t.py::test_x expected 2, got 1; a.py:3",
                                 "attempt 2: GREEN, 410 lines, outside files: b.py, new docs: NOTES.md, broke: t.py::test_y, tampered: tests/t.py",
                                 "attempt 3: provider busy, no turn", "attempt 3: implementer timed out", "attempt 4: payment required (openrouter)",
                                 "attempt 5: contradiction: brief says x / code shows y"])

    def test_a_reflection_without_a_why_line_gives_its_first_line(self):
        self.assertEqual(fc.fc_retro.why_line({"summary": "\nt.py::test_x expected 2\nlook at a.py:3"}), "t.py::test_x expected 2")
        self.assertEqual(fc.fc_retro.why_line({"summary": ""}), "")

    def test_the_text_leads_with_the_outcome_and_ends_with_what_this_kind_of_miss_calls_for(self):
        text = fc.fc_retro.text(result("failed", [RED]))
        self.assertEqual(text.splitlines()[0], "Retrospective: failed after 1 attempt(s), a code miss, model `pi:glm`, $0.0123.")
        self.assertEqual(text.splitlines()[1], "- attempt 1: RED (exit 1), 12 lines, 2 refused · WHY: t.py::test_x expected 2, got 1; a.py:3")
        self.assertTrue(text.splitlines()[-1].startswith("Next: the implementer had the facts and still missed: re-split"))
        for status, word in (("contradiction", "fix the brief"), ("rejected_scope", "broke a limit"), ("interrupted", "tool bug"), ("broken_base", "fix it")):
            self.assertIn(word, fc.fc_retro.text(result(status, [])), status)
        self.assertIn("- no attempt ran", fc.fc_retro.text(result("interrupted", [])))


class Record(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "shop"
        (self.proj / "results").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.proj.parent, True)
        self.meta = {"id": "003", "title": "Parse the node", "files": ["a.py", "c.py"], "model": "pi:glm", "linear": "ENG-3"}

    def test_record_appends_one_scrubbed_line_and_returns_the_text_the_issue_gets(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-secret-value-123"}):
            text = fc.fc_retro.record(self.proj, self.meta, result("failed", [{**RED, "summary": "WHY: key sk-secret-value-123 leaked"}]))
        self.assertEqual(text, fc.fc_retro.text(result("failed", [{**RED, "summary": "WHY: key sk-secret-value-123 leaked"}])))
        [line] = fc.fc_retro.read(self.proj)
        self.assertEqual((line["task"], line["title"], line["status"], line["kind"], line["attempts"], line["model"], line["route"], line["files"]),
                         ("003", "Parse the node", "failed", "code", 1, "pi:glm", "-", ["a.py", "c.py"]))
        self.assertEqual(line["lines"], ["attempt 1: RED (exit 1), 12 lines, 2 refused · WHY: key <redacted> leaked"])
        self.assertNotIn("sk-secret-value-123", (self.proj / "results" / "retro.jsonl").read_text())
        fc.fc_retro.record(self.proj, self.meta, result("interrupted", []))
        self.assertEqual([r["status"] for r in fc.fc_retro.read(self.proj)], ["failed", "interrupted"])

    def test_an_unwritable_file_never_fails_the_task_and_an_unreadable_one_reads_as_none(self):
        (self.proj / "results" / "retro.jsonl").mkdir()
        self.assertTrue(fc.fc_retro.record(self.proj, self.meta, result("failed", [RED])).startswith("Retrospective"))
        self.assertEqual(fc.fc_retro.read(self.proj), [])
        self.assertEqual(fc.fc_retro.read(self.proj.parent / "none"), [])

    def test_the_harness_lines_name_only_the_misses_that_point_at_compose(self):
        fc.fc_retro.record(self.proj, self.meta, result("failed", [RED]))
        fc.fc_retro.record(self.proj, {**self.meta, "id": "004", "title": "Save it"}, result("failed", [{"attempt": 1, "error": "implementer timed out"}, {"attempt": 2, "rate_limited": True}], id="004"))
        fc.fc_retro.record(self.proj, {**self.meta, "id": "005", "title": "Show it"}, result("payment_required", [], id="005"))
        self.assertEqual(fc.fc_retro.harness_lines(self.proj), ["004 Save it: failed after 2 attempt(s); attempt 1: implementer timed out; attempt 2: provider busy, no turn",
                                                                "005 Show it: payment_required after 0 attempt(s); no attempt ran"])

    def test_the_task_facts_for_the_history_line_are_the_route_and_the_models_tier(self):
        with mock.patch.object(fc.fc_retro.fc_routecost, "route", return_value=("test-first", "FAST")), mock.patch.object(fc.fc_retro.fc_tiers, "tier_of", return_value="cheap"):
            self.assertEqual(fc.fc_retro.task_facts(self.proj, self.meta), {"route": "test-first", "tier": "cheap"})
        self.assertEqual(fc.fc_retro.task_facts(self.proj, self.meta), {"route": "-", "tier": None})


class IssueComment(unittest.TestCase):
    """A missed task's retrospective is its failure comment on Linear; a result without one keeps the one-line comment."""
    def comment(self, res):
        issue = {"identifier": "ENG-3", "parent": None}
        with mock.patch.object(fc.fc_linear, "linear_ready", return_value=True), mock.patch.object(fc.fc_linear, "linear_issue", return_value=issue), \
             mock.patch.object(fc.fc_linear, "linear_move") as move, mock.patch.object(fc.fc_linear, "linear_comment") as comment:
            fc.fc_linear.linear_event(Path("proj"), {"id": "003", "linear": "ENG-3"}, "failed", res)
        self.assertEqual([c.args[1] for c in move.call_args_list], ["In Progress"])
        return [c.args[1] for c in comment.call_args_list]

    def test_the_retrospective_is_the_comment_and_without_one_the_old_line_stands(self):
        res = result("failed", [RED])
        self.assertEqual(self.comment({**res, "retrospective": fc.fc_retro.text(res)}), [fc.fc_retro.text(res)])
        self.assertEqual(self.comment({**res, "log": [{**RED, "test_output": "x\nAssertionError: expected 2, got 1\n"}]}),
                         ["failed after 1 attempt(s) ($0.0123, model `pi:glm`): AssertionError: expected 2, got 1"])


class Learnings(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "shop"
        (self.proj / "results").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.proj.parent, True)

    def test_the_draft_has_the_note_shape_and_ties_each_why_to_the_tasks_files(self):
        self.assertEqual(fc.fc_retro.learnings_draft(self.proj), "")
        fc.fc_retro.record(self.proj, {"id": "003", "title": "Parse the node", "files": ["a.py"]}, result("failed", [RED, {**RED, "attempt": 2, "summary": "WHY: a.py:3 returns a str, the test wants an int"}]))
        fc.fc_retro.record(self.proj, {"id": "005", "title": "Show it", "files": []}, result("interrupted", [], id="005"))
        draft = fc.fc_retro.learnings_draft(self.proj)
        self.assertTrue(draft.startswith("# What building shop taught about this repo (draft from 2 retrospective(s); keep only facts about the repo)\n\n## Decided\n"))
        self.assertIn("\n## Tried and ruled out\n- task 003 Parse the node ended failed (code, 2 attempt(s)): attempt 1: RED (exit 1), 12 lines, 2 refused · WHY: t.py::test_x expected 2, got 1; a.py:3; attempt 2: RED", draft)
        self.assertIn("- task 005 Show it ended interrupted (harness, 0 attempt(s)): no attempt ran", draft)
        self.assertIn("\n## Watch out\n- a.py: WHY: t.py::test_x expected 2, got 1; a.py:3 | WHY: a.py:3 returns a str, the test wants an int\n", draft)


class Command(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.proj = self.repo / ".compose" / "shop"
        (self.proj / "results").mkdir(parents=True)
        self.cfg = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.cfg, True)
        cp = mock.patch.object(fc_history.fc_config, "CONFIG_PATH", self.cfg / "config.json"); cp.start(); self.addCleanup(cp.stop)
        pd = mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj); pd.start(); self.addCleanup(pd.stop)

    def retro(self, **kw):
        a = SimpleNamespace(**{"repo": str(self.repo), "project": "shop", "task": None, "learnings": False, "history": False, **kw})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            fc.fc_retro.cmd_retro(a)
        return out.getvalue()

    def test_the_list_one_task_and_the_draft(self):
        self.assertEqual(self.retro(), "retro  none: no task of this project ended without passing\n")
        self.assertEqual(self.retro(learnings=True), "retro  none: no task of this project ended without passing\n")
        fc.fc_retro.record(self.proj, {"id": "003", "title": "Parse the node", "files": ["a.py"]}, result("failed", [RED]))
        out = self.retro().splitlines()
        self.assertTrue(out[0].endswith("  003  failed       code         x1  Parse the node"), out[0])
        self.assertEqual(out[1], "    attempt 1: RED (exit 1), 12 lines, 2 refused · WHY: t.py::test_x expected 2, got 1; a.py:3")
        self.assertEqual(self.retro(task="003"), fc.fc_retro.text(result("failed", [RED])) + "\n")
        with self.assertRaises(SystemExit) as e:
            self.retro(task="009")
        self.assertEqual(e.exception.code, "retro  none for task 009")
        self.assertTrue(self.retro(learnings=True).startswith("# What building shop taught"))

    def test_the_history_report_counts_first_try_and_final_passes_by_model_route_tier_and_outcome(self):
        self.assertEqual(self.retro(history=True), "retro  no task has run here yet\n")
        rows = [("001", "m1", 1, "passed", "test-first", "cheap", 0.1), ("002", "m1", 2, "passed", "test-first", "cheap", 0.2), ("003", "m1", 3, "failed", "check-only", "cheap", 0.3),
                ("004", "m2", 1, "already_green", None, None, 0.0), ("005", "m2", 2, "rejected_size", "check-only", "standard", 0.4)]
        for task, model, attempts, outcome, route, tier, cost in rows:
            fc_history.record(self.proj, self.repo, "task", task=task, model=model, attempts=attempts, outcome=outcome, route=route, tier=tier, cost=cost)
        fc_history.record(self.repo / ".compose" / "other", self.repo, "task", task="009", model="m3", attempts=1, outcome="passed", cost=1.0)
        fc_history.record(self.proj, self.repo, "verify-end", rounds=2, outcome="fail")
        fc_history.record(self.proj, self.repo, "verify-end", rounds=1, outcome="pass")
        out = self.retro(history=True).splitlines()
        self.assertEqual(out[0], "tasks 5 · passed 3 (60%) · first try 2 (40%) · $1.0000")
        self.assertEqual(out[1], "by model:")
        self.assertEqual(out[2].split(), ["m1", "tasks", "3", "passed", "2", "(66%)", "first", "try", "1", "(33%)", "$0.6000"])
        self.assertEqual(out[3].split(), ["m2", "tasks", "2", "passed", "1", "(50%)", "first", "try", "1", "(50%)", "$0.4000"])
        self.assertEqual(out[4], "by route:")
        self.assertEqual([l.split()[0] for l in out[5:8]], ["test-first", "check-only", "-"])
        self.assertEqual(out[8], "by tier:")
        self.assertEqual(out[-2], "by outcome: passed 2, failed 1, already_green 1, rejected_size 1")
        self.assertEqual(out[-1], "verify: 2 run(s) · pass 1 (50%) · rounds mean 1.5")
        self.assertIn("m3", self.retro(history=True, project=None))
