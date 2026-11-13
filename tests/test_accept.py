import hermetic  # first: no test sees the real home
import hashlib, importlib.machinery, importlib.util, json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_accept, fc_ledger, fc_evidence  # noqa: E402

VERDICTS = ("proven", "unproven", "contradicted")
NEEDS = ("test", "prove", "check", "e2e", "review", "acceptance", "manual", "none")
META = {"id": "031", "title": "t", "body": "## Acceptance criteria\n* alpha\n* beta\n"}
C1, C2 = (f"031.{n}:{hashlib.sha256(t.encode()).hexdigest()[:8]}" for n, t in ((1, "alpha"), (2, "beta")))


class SufficiencyQuestions(unittest.TestCase):
    def test_questions_per_criterion_and_row(self):
        rows = [{"event_id": "x1", "criterion_id": [C1]}, {"event_id": "x2", "criterion_id": [C2]}, {"event_id": "x3", "criterion_id": []},
                {"event_id": "x4", "criterion_id": [C2, C1]}]
        qs = fc_accept.sufficiency_questions(META, rows)
        self.assertEqual(list(qs), ["v1", "n1", "e1_x1", "e1_x3", "e1_x4", "v2", "n2", "e2_x2", "e2_x3", "e2_x4"])
        for n, text in ((1, "alpha"), (2, "beta")):
            self.assertEqual(qs[f"v{n}"]["type"], "choice")
            self.assertEqual(list(qs[f"v{n}"]["criteria"]), list(VERDICTS))
            self.assertEqual(qs[f"n{n}"]["type"], "choice")
            self.assertEqual(list(qs[f"n{n}"]["criteria"]), list(NEEDS))
            self.assertLessEqual({"031", text}, set(qs[f"v{n}"]["instructions"].values()))
        for name, event_id in (("e1_x1", "x1"), ("e2_x4", "x4")):
            self.assertEqual(qs[name]["type"], "noul")
            self.assertTrue({"031", event_id} <= set(qs[name]["instructions"].values()))
        self.assertIn("alpha", qs["e1_x3"]["instructions"].values())
        self.assertIn("beta", qs["e2_x3"]["instructions"].values())

    def test_no_rows_asks_only_verdict_and_needs(self):
        self.assertEqual(list(fc_accept.sufficiency_questions(META, [])), ["v1", "n1", "v2", "n2"])

    def test_needs_adds_likely_values_in_order(self):
        needs = fc_accept.sufficiency_needs
        self.assertEqual(needs({"choice": "test", "probabilities": {"test": 0.9, "e2e": 0.25, "review": 0.24}}), ["test", "e2e"])
        self.assertEqual(needs({"choice": "review", "probabilities": {"test": 0.5, "check": 0.3, "prove": 0.1}}), ["test", "check", "review"])

    def test_needs_drops_none_beside_another_value(self):
        needs = fc_accept.sufficiency_needs
        self.assertEqual(needs({"choice": "none", "probabilities": {"none": 0.6, "test": 0.3}}), ["test"])
        self.assertEqual(needs({"choice": "test", "probabilities": {"none": 0.9}}), ["test"])
        self.assertEqual(needs({"choice": "none", "probabilities": {"none": 0.9}}), ["none"])

    def test_needs_reads_a_missing_or_unknown_choice_as_manual(self):
        needs = fc_accept.sufficiency_needs
        self.assertEqual(needs(None), ["manual"])
        self.assertEqual(needs({"choice": "bogus", "probabilities": {}}), ["manual"])
        self.assertEqual(needs({"choice": "bogus", "probabilities": {"test": 0.5}}), ["test", "manual"])


KEYS = ["event_id", "run_id", "commit_sha", "task_ids", "criterion_id", "phase", "source", "result", "fails_on_base", "detail", "cites", "needs"]
N1 = {"choice": "test", "probabilities": {"test": 1.0}}


def row(eid, phase, result, cids=()):
    return {"event_id": eid, "criterion_id": list(cids), "phase": phase, "result": result}


class SufficiencyEvents(unittest.TestCase):
    def judge(self, rows, answers):
        events = fc_accept.sufficiency_events(META, rows, answers, 0.6, "run1", "sha1")
        self.assertEqual(len(events), 2)
        for e in events:
            d = {k: v for k, v in e.items() if k != "event_id"}
            self.assertEqual(list(e), KEYS)
            self.assertEqual(e["event_id"], hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12])
        return events

    def test_event_shape_and_cites_at_threshold_in_order(self):
        rows = [row("r1", "review", "pass"), row("r2", "acceptance", "pass", [C2]), row("r3", "e2e", "pass", [C1])]
        e1, e2 = self.judge(rows, {"v1": {"choice": "proven"}, "n1": N1, "e1_r1": {"noul": 0.7}, "e1_r3": {"noul": 0.5},
                                    "v2": {"choice": "contradicted"}, "e2_r1": {"noul": 0.6}, "e2_r2": {"noul": 0.9}})
        base = {"run_id": "run1", "commit_sha": "sha1", "task_ids": ["031"], "phase": "sufficiency", "source": "verify", "fails_on_base": None}
        self.assertEqual({k: v for k, v in e1.items() if k != "event_id"}, {**base, "criterion_id": [C1], "result": "proven", "detail": "alpha",
                                                                              "cites": ["r1"], "needs": fc_accept.sufficiency_needs(N1)})
        self.assertEqual({k: v for k, v in e2.items() if k != "event_id"}, {**base, "criterion_id": [C2], "result": "contradicted", "detail": "beta",
                                                                              "cites": ["r1", "r2"], "needs": fc_accept.sufficiency_needs(None)})

    def test_missing_or_unknown_verdict_is_unproven(self):
        rows = [row("r1", "review", "pass")]
        e1, e2 = self.judge(rows, {"v2": {"choice": "bogus"}, "e1_r1": {"noul": 1}, "e2_r1": {"noul": 1}})
        self.assertEqual((e1["result"], e2["result"], e1["cites"]), ("unproven", "unproven", ["r1"]))

    def test_deterministic_fail_unproves_and_joins_cites_once(self):
        rows = [row("f1", "attempt", "fail", [C1]), row("f2", "final", "fail", [C1]), row("f3", "review", "fail"), row("f4", "sufficiency", "fail", [C2]),
                row("f5", "acceptance", "fail"), row("p1", "e2e", "pass")]
        e1, e2 = self.judge(rows, {"v1": {"choice": "proven"}, "e1_f1": {"noul": 1}, "e1_f3": {"noul": 1}, "e1_p1": {"noul": 1},
                                    "v2": {"choice": "proven"}, "e2_p1": {"noul": 1}})
        self.assertEqual((e1["result"], e1["cites"]), ("unproven", ["f1", "f3", "p1", "f2"]))
        self.assertEqual((e2["result"], e2["cites"]), ("proven", ["p1"]))

    def test_deterministic_fail_does_not_change_other_verdicts(self):
        e1, e2 = self.judge([row("f1", "checks", "fail")], {"v1": {"choice": "contradicted"}, "v2": {"choice": "unproven"}})
        self.assertEqual((e1["result"], e1["cites"], e2["result"]), ("contradicted", [], "unproven"))

    def test_proven_needs_a_cited_row_other_than_error(self):
        rows = [row("x1", "checks", "error"), row("p1", "e2e", "pass", [C1])]
        e1, e2 = self.judge(rows, {"v1": {"choice": "proven"}, "e1_x1": {"noul": 1}, "e1_p1": {"noul": 1}, "v2": {"choice": "proven"}, "e2_x1": {"noul": 1}})
        self.assertEqual((e1["result"], e2["result"], e2["cites"]), ("proven", "unproven", ["x1"]))
        self.assertEqual(self.judge([], {"v1": {"choice": "proven"}, "v2": {"choice": "contradicted"}})[0]["result"], "unproven")


TASK = {"title": "t", "linear": "ENG-4", "kind": "code", "body": "## Acceptance criteria\n* alpha\n* beta\n"}


class SufficiencyAsk(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"; (self.proj / "results").mkdir(parents=True)
        self.repo, self.sha = Path(tempfile.mkdtemp()), "a" * 40
        self.said, self.asks = [], []
        answer = lambda name, q: {"choice": "proven", "probabilities": {"proven": 1}} if q["type"] == "choice" else {"noul": 1}
        def ask(env, cfg, state, questions, label):
            self.asks.append((state, list(questions), label))
            return {"model": "m", "answers": {n: answer(n, q) for n, q in questions.items()}}
        self.rows = [{"event_id": f"r{i}", "criterion_id": [], "phase": "review", "result": "pass"} for i in range(35)]
        self.rows[3:3] = [{"event_id": "s1", "criterion_id": [], "phase": "sufficiency", "result": "proven"}, {"event_id": "c1", "criterion_id": [], "phase": "cost", "result": "pass"}]
        for mod, name, kw in ((fc.fc_config, "ENV_FILE", {"new": self.proj / "none.env"}), (fc.fc_models, "jev_ask", {"side_effect": ask}), (fc_evidence, "task_rows", {"return_value": self.rows}),
                              (fc_ledger, "counted", {"side_effect": lambda *a: a[-1]}), (fc_ledger, "record_task", {"return_value": True}), (fc_ledger, "run_id", {"return_value": "r1"}),
                              (fc.fc_core, "say", {"new": self.said.append}),
                              (fc.fc_core, "sh", {"return_value": subprocess.CompletedProcess([], 0, self.sha + "\n", "")})):
            p = mock.patch.object(mod, name, **kw); setattr(self, name, p.start()); self.addCleanup(p.stop)

    def tasks(self, *metas):
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [{"id": f"03{n}", **m} for n, m in enumerate(metas, 1)]}))

    def test_judges_counted_rows_newest_30_in_batches_and_records(self):
        self.tasks(TASK); (self.proj / "linear.json").write_text('{"feature": "ENG-9"}')
        events = fc_accept.jev_sufficiency({}, self.proj, self.repo)
        shown = [r for r in self.rows if r["phase"] not in ("sufficiency", "cost")][-30:]
        self.task_rows.assert_called_once_with("ENG-4", "ENG-9", "031")
        self.assertEqual([len(qs) for _, qs, _ in self.asks], [40, 24])
        self.assertTrue(all(label == "Jev sufficiency" and state is self.asks[0][0] for state, _, label in self.asks))
        self.assertEqual(self.asks[0][0], {"task": "t", "criteria": ["alpha", "beta"], "events": shown})
        self.assertEqual([e["result"] for e in events], ["proven", "proven"])
        self.assertEqual({e["commit_sha"] for e in events}, {self.sha})
        self.assertEqual((self.record_task.call_args.args[2]["id"], self.record_task.call_args.args[-1]), ("031", events))
        self.assertEqual(self.said, ["jev    sufficiency 031: 2 proven, 0 unproven, 0 contradicted"])

    def test_no_criteria_or_no_issue_asks_nothing(self):
        self.tasks({**TASK, "body": "## Goal\nx\n"}, {**TASK, "linear": None}, {**TASK, "kind": "design"})
        self.assertEqual(fc_accept.jev_sufficiency({}, self.proj, self.repo), [])
        self.assertEqual((self.asks, self.record_task.call_count, self.said), ([], 0, []))

    def test_a_failing_task_is_not_judged_and_the_next_still_is(self):
        self.tasks(TASK, TASK); err = "boom " + "x" * 300
        self.task_rows.side_effect = [RuntimeError(err), self.rows]
        events = fc_accept.jev_sufficiency({}, self.proj, self.repo)
        self.assertEqual(self.said, [f"jev    sufficiency 031: not judged ({err[:200]})", "jev    sufficiency 032: 2 proven, 0 unproven, 0 contradicted"])
        self.assertEqual(([e["task_ids"] for e in events], self.record_task.call_count), ([["032"], ["032"]], 1))


class AcceptanceConfirm(unittest.TestCase):
    """The confirm step of the acceptance judge (#457): a NOT_MET was confirmed on evidence that said the criterion was met, because it
    asked for `{"confirmed": bool}` (read as "the criterion is confirmed met", #348 in the figma judge), and it read the task's files
    whole from the working tree, so code a later task added to the same file counted against an earlier task's criterion."""
    CRITERION = "A failed transcribe saves nothing."
    DIFF = "diff --git a/svc.ts b/svc.ts\n+  if (!text) return;\n+  await transcribe(file);\n"

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        body = f"## Goal\ng\n\n## Acceptance criteria\n* {self.CRITERION}\n"
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [{"id": "031", "title": "one", "linear": "ENG-4", "kind": "code", "files": ["svc.ts"], "protected": [], "test": "t", "body": body}]}))
        self.repo = Path(tempfile.mkdtemp())  # a later task has grown the file since: its function is in the working tree, not in task 031's hunk
        (self.repo / "svc.ts").write_text("export const lastAgentState = () => select({ agentState: true });\n")
        self.sent = []
        reply = {"model": "m", "answers": {"c0": {"choice": "NOT_MET", "probabilities": {"MET": 0.1, "NOT_MET": 0.9}}}}
        for mod, name, kw in ((fc.fc_config, "ENV_FILE", {"new": self.proj / "none.env"}), (fc.fc_models, "jev_ask", {"return_value": reply}), (fc.fc_models, "reask_criterion", {}),
                              (fc_evidence, "task_rows", {"return_value": []}), (fc_ledger, "counted", {"side_effect": lambda *a: a[-1]}),
                              (fc.fc_core, "sh", {"side_effect": lambda argv, cwd, check=True: subprocess.CompletedProcess(argv, 0, "a" * 40 + "\n", "")})):
            p = mock.patch.object(mod, name, **kw); p.start(); self.addCleanup(p.stop)

    def judge(self, answer):
        """jev_acceptance with the confirm call answering `answer` (its (violated, evidence) pair)."""
        def confirm(env, system, user, **kw):
            self.sent.append((system, user, kw))
            return answer
        with mock.patch.object(fc.fc_models, "confirm_call", side_effect=confirm):
            return fc_accept.jev_acceptance({}, self.proj, self.DIFF, repo=self.repo)

    def test_the_confirm_asks_for_violated_with_the_evidence_first_never_for_confirmed(self):
        self.judge((False, ""))
        [(system, user, kw)] = self.sent
        self.assertEqual(kw, {"key": "violated"})
        self.assertIn('Return JSON {"evidence": str, "violated": bool}.', system)
        self.assertNotIn("confirmed", system)
        self.assertIn("including a criterion that says something is absent and it is absent", system)

    def test_a_met_criterion_whose_evidence_quotes_no_violation_is_not_failed(self):
        """The row of #457: confirmed on "tests confirm no call is made", which quotes no line."""
        for answer in ((True, "Tests confirm no R2 calls occur when files are absent or when an earlier gate fails."), (True, ""), (True, "the code never calls saveTurn")):
            with self.subTest(answer=answer):
                res = self.judge(answer)
                self.assertEqual((res["failed"], [r["confirmed"] for r in res["criteria"]], len(res["unclear"])), ([], [False], 1))
                self.assertIn("no line of the code is quoted as the violation", res["criteria"][0]["evidence"])

    def test_a_violation_that_quotes_a_line_of_the_code_fails_the_criterion_whatever_the_quote_is_marked_with(self):
        for evidence in ("await transcribe(file);", "+  await transcribe(file);", "The call `await transcribe(file)` is not guarded", 'line: "if (!text) return;"'):
            with self.subTest(evidence=evidence):
                res = self.judge((True, evidence))
                self.assertEqual(([r["task"] for r in res["failed"]], res["unclear"]), (["031"], []))

    def test_the_confirm_reads_the_tasks_own_hunks_the_judge_read_and_not_the_file_as_a_later_task_grew_it(self):
        self.judge((False, ""))
        [(_, user, _)] = self.sent
        self.assertIn("await transcribe(file);", user)
        self.assertNotIn("lastAgentState", user)
        self.assertTrue(user.startswith(f"CRITERION: {self.CRITERION}\n\nCODE:\n"))

    def test_a_verdict_kept_from_another_confirm_question_is_judged_again(self):
        """A confirmed row is kept until the task's code or criteria change, so a wrong one came back on every rerun: the key holds the question."""
        meta, crits = {"id": "031", "title": "one"}, [self.CRITERION]
        key = fc_accept.verdict_key(meta, crits, self.DIFF)
        with mock.patch.object(fc_accept, "CONFIRM", fc_accept.CONFIRM + " "):
            self.assertNotEqual(fc_accept.verdict_key(meta, crits, self.DIFF), key)

    def test_quotes_code(self):
        code = "diff --git a/f b/f\n+  const a = 1;\n-  const b = 2;\n   keep();\n"
        for evidence, ok in (("const a = 1;", True), ("+const a = 1;", True), ("-  const b = 2;", True), ("keep();", True), ("`keep()` runs twice", True), ("const a  =\n 1;", True),
                             ("it never keeps", False), ("", False), ("a", False), ("`x`", False)):
            self.assertEqual(fc_accept.quotes_code(evidence, code), ok, evidence)


class AcceptanceReuse(unittest.TestCase):
    """A task whose criteria, judged hunks, and files are as they were at its last verify keeps that verdict: jev is not asked again,
    and neither is sufficiency once it has judged that verdict."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"; (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        self.repo, self.head, self.changed = Path(tempfile.mkdtemp()), "a" * 40, set()
        self.asked, self.said, self.codes = [], [], {}
        def ask(env, cfg, state, questions, label):
            self.asked.append((label, next(iter(questions.values()))["instructions"]["task"]))
            if label == "Jev acceptance":
                self.codes[self.asked[-1][1]] = state["code"]
            return {"model": "m", "answers": {k: {"choice": "MET", "probabilities": {"MET": 0.9}} if q["type"] == "choice" else {"noul": 1} for k, q in questions.items()}}
        def sh(argv, cwd, check=True):  # rev-parse answers HEAD; diff --quiet says whether a path of the task moved since the judged commit
            if argv[1] == "diff":
                return subprocess.CompletedProcess(argv, int(bool(set(argv[argv.index("--") + 1:]) & self.changed)), "", "")
            return subprocess.CompletedProcess(argv, 0, self.head + "\n", "")
        for mod, name, kw in ((fc.fc_config, "ENV_FILE", {"new": self.proj / "none.env"}), (fc.fc_models, "jev_ask", {"side_effect": ask}),
                              (fc.fc_models, "fast_json", {"return_value": {"violated": False, "evidence": ""}}), (fc_evidence, "task_rows", {"return_value": []}),
                              (fc_ledger, "counted", {"side_effect": lambda *a: a[-1]}), (fc_ledger, "record_task", {"return_value": True}),
                              (fc.fc_core, "say", {"new": self.said.append}), (fc.fc_core, "sh", {"side_effect": sh})):
            p = mock.patch.object(mod, name, **kw); p.start(); self.addCleanup(p.stop)
        self.tasks()

    def tasks(self, crit2="* beta\n"):
        body = lambda c: "## Goal\ng\n\n## Acceptance criteria\n" + c
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [
            {"id": "031", "title": "one", "linear": "ENG-4", "kind": "code", "files": ["a.go"], "protected": ["a_test.go"], "test": "t1", "body": body("* alpha\n")},
            {"id": "032", "title": "two", "linear": "ENG-5", "kind": "code", "files": ["b.go"], "protected": ["b_test.go"], "test": "t2", "body": body(crit2)}]}))

    @staticmethod
    def diff(a="+a", b="+b"):
        return f"diff --git a/a.go b/a.go\n{a}\ndiff --git a/b.go b/b.go\n{b}\n"

    def judge(self, diff=None, **kw):
        self.asked.clear(); self.said.clear()
        return fc_accept.jev_acceptance({}, self.proj, diff or self.diff(), repo=self.repo, **kw)

    def acceptance_asked(self):
        return [t for label, t in self.asked if label == "Jev acceptance"]

    def sufficiency(self, acc):
        self.asked.clear(); self.said.clear()
        out = fc_accept.jev_sufficiency({}, self.proj, self.repo, acc)
        return out, [t for label, t in self.asked if label == "Jev sufficiency"]

    def test_a_second_verify_of_the_same_code_and_criteria_asks_jev_for_no_task(self):
        first = self.judge()
        self.assertEqual(self.acceptance_asked(), ["031", "032"])
        self.assertFalse(any(r.get("reused") for r in first["criteria"]))
        again = self.judge()
        self.assertEqual(self.acceptance_asked(), [])
        self.assertEqual([(r["task"], r["criterion"], r["choice"], r["reused"]) for r in again["criteria"]], [("031", "alpha", "MET", True), ("032", "beta", "MET", True)])
        self.assertEqual((again["pass"], again["unclear"], again["failed"]), (True, [], []))
        self.assertEqual([s for s in self.said if "not rejudged" in s], ["jev    acceptance 2 of 2 tasks unchanged since their last verify, not rejudged"])

    def test_a_task_whose_hunks_criteria_or_files_changed_is_judged_again_and_the_other_is_not(self):
        self.judge()
        self.judge(self.diff(b="+b2"))
        self.assertEqual(self.acceptance_asked(), ["032"])
        self.tasks(crit2="* gamma\n")
        self.judge(self.diff(b="+b2"))
        self.assertEqual(self.acceptance_asked(), ["032"])
        self.changed = {"a_test.go"}  # a path of 031 differs between the judged commit and HEAD, although its hunks against the base read the same
        self.judge(self.diff(b="+b2"))
        self.assertEqual(self.acceptance_asked(), ["031"])
        self.changed = set()
        self.judge(self.diff(b="+b2"))
        self.assertEqual(self.acceptance_asked(), [])

    def test_fresh_judges_every_task_again(self):
        self.judge()
        self.judge(fresh=True)
        self.assertEqual(self.acceptance_asked(), ["031", "032"])
        self.assertEqual([s for s in self.said if "not rejudged" in s], [])

    def test_without_a_repo_nothing_is_kept_and_every_verify_judges(self):
        fc_accept.jev_acceptance({}, self.proj, self.diff())
        fc_accept.jev_acceptance({}, self.proj, self.diff())
        self.assertEqual(self.acceptance_asked(), ["031", "032"] * 2)
        self.assertFalse((self.proj / "results" / "acceptance").exists())

    def test_sufficiency_is_not_asked_again_for_a_reused_task_it_judged_before(self):
        out, asked = self.sufficiency(self.judge())
        self.assertEqual((asked, [e["task_ids"] for e in out]), (["031", "032"], [["031"], ["032"]]))
        out, asked = self.sufficiency(self.judge(self.diff(b="+b2")))  # 031 reused, 032 judged again
        self.assertEqual((asked, [e["task_ids"] for e in out]), (["032"], [["032"]]))
        self.assertEqual(len([s for s in self.said if "not rejudged" in s]), 1)
        out, asked = self.sufficiency(self.judge(self.diff(b="+b2")))
        self.assertEqual((asked, out), ([], []))

    def test_a_reused_task_whose_sufficiency_never_ran_is_judged(self):
        self.judge()
        acc = self.judge()
        self.assertEqual(acc["criteria"][0]["reused"], True)
        out, asked = self.sufficiency(acc)
        self.assertEqual((asked, [e["task_ids"] for e in out]), (["031", "032"], [["031"], ["032"]]))
        self.assertEqual([s for s in self.said if "not rejudged" in s], [])


class AcceptanceCode(unittest.TestCase):
    """#455: each task is judged on its own code - its hunks, else its files as they are - and never on another task's."""
    setUp, tasks, diff, judge, acceptance_asked = (AcceptanceReuse.setUp, AcceptanceReuse.tasks, staticmethod(AcceptanceReuse.diff),
                                                   AcceptanceReuse.judge, AcceptanceReuse.acceptance_asked)

    def test_a_task_that_changed_none_of_its_files_is_judged_on_them_as_they_are(self):
        (self.repo / "b.go").write_text("package b\nfunc B() {}\n")
        self.judge("diff --git a/a.go b/a.go\n+a\n")
        self.assertEqual(self.acceptance_asked(), ["031", "032"])
        self.assertEqual(self.codes["032"], "==== b.go (no change in this diff) ====\npackage b\nfunc B() {}\n")
        self.assertIn("jev    acceptance 1 task(s) changed none of their files, judged on those files as they are: 032", self.said)

    def test_a_task_with_no_code_is_not_asked_and_its_criteria_are_unclear_with_the_reason(self):
        acc = self.judge("diff --git a/a.go b/a.go\n+a\n")
        self.assertEqual(self.acceptance_asked(), ["031"])
        self.assertNotIn("+a", "".join(c for t, c in self.codes.items() if t != "031"))
        self.assertEqual([(r["task"], r["choice"], r.get("note")) for r in acc["criteria"]],
                         [("031", "MET", None), ("032", "UNCLEAR", fc_accept.NO_CODE)])
        self.assertIn("jev    acceptance 1 task(s) have no code to judge (none of their files in the diff or the repo): 032", self.said)
        self.assertFalse((self.proj / "results" / "acceptance" / "032.json").exists())  # nothing judged, so no verdict kept to reuse

    def test_a_lockfile_is_left_out_of_the_code(self):
        (self.repo / "b.go").write_text("package b\n")
        (self.repo / "go.sum").write_text("sum\n")
        body = "## Goal\ng\n\n## Acceptance criteria\n* beta\n"
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [
            {"id": "032", "title": "two", "linear": "ENG-5", "kind": "code", "files": ["go.sum", "b.go"], "protected": [], "test": "t2", "body": body}]}))
        self.judge("diff --git a/go.sum b/go.sum\n+sum2\n")
        self.assertEqual(self.codes["032"], "==== b.go (no change in this diff) ====\npackage b\n")

    def test_code_over_the_cap_is_cut_at_a_whole_line_and_marked_as_cut_by_the_tool(self):
        self.assertEqual(fc_accept.capped("a\nb"), "a\nb")
        out = fc_accept.capped(("l" * 99 + "\n") * 400)
        self.assertLessEqual(len(out), fc_accept.JEV_CODE_CAP)
        self.assertTrue(out.endswith(fc_accept.CUT_MARK))
        self.assertEqual({len(line) for line in out[:-len(fc_accept.CUT_MARK)].split("\n")}, {99})
