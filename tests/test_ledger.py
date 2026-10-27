import hermetic  # first: no test sees the real home
import contextlib, hashlib, importlib.machinery, importlib.util, io, json, os, subprocess, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_ledger, fc_evidence  # noqa: E402


class Events(unittest.TestCase):
    ARGS = ("a1b2c3d4e5f6", "abc", ["003"], ["003.1:12345678"], "attempt", "run", "fail", None, "boom")

    def test_event_has_the_keys_in_order_and_a_stable_id(self):
        self.assertEqual(fc_ledger.KEYS, ("event_id", "run_id", "commit_sha", "task_ids", "criterion_id", "phase", "source", "result", "fails_on_base", "detail"))
        e = fc_ledger.event(*self.ARGS)
        self.assertEqual(list(e), list(fc_ledger.KEYS))
        rest = {k: v for k, v in e.items() if k != "event_id"}
        self.assertEqual(e["event_id"], hashlib.sha256(json.dumps(rest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12])
        self.assertEqual(fc_ledger.event(*self.ARGS), e)
        for i, other in enumerate(("r", "sha", ["004"], [], "final", "prove", "pass", True, "bang")):
            self.assertNotEqual(fc_ledger.event(*(self.ARGS[:i] + (other,) + self.ARGS[i + 1:]))["event_id"], e["event_id"], i)

    def test_detail_is_scrubbed_one_line_and_cut(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-secret-value-123"}):
            d = fc_ledger.event(*self.ARGS[:8], "  FAIL\n\tsk-secret-value-123 \r\n x  ")["detail"]
        self.assertEqual(d, "FAIL <redacted> x")
        long = fc_ledger.event(*self.ARGS[:8], "a\n\n b" * 200)["detail"]
        self.assertEqual((len(long), "\n" in long, "  " in long), (160, False, False))

    def test_criterion_ids_number_and_hash_each_criterion(self):
        sha = lambda t: hashlib.sha256(t.encode()).hexdigest()[:8]
        body = lambda c: "## Goal\nx\n\n## Acceptance criteria\n* works\n  * sub\n* " + c + "\n\n## Test plan\ny"
        ids = fc_ledger.criterion_ids({"id": "003", "body": body("fast")})
        self.assertEqual(ids, [f"003.1:{sha('works; sub')}", f"003.2:{sha('fast')}"])
        edited = fc_ledger.criterion_ids({"id": "003", "body": body("faster")})
        self.assertEqual((edited[0], edited[1] != ids[1], edited[1][:6]), (ids[0], True, "003.2:"))

    def test_run_id_is_kept_minted_and_replaced_on_fresh(self):
        proj = Path(tempfile.mkdtemp()) / "feat"
        proj.mkdir()
        a = fc_ledger.run_id(proj, "003")
        self.assertRegex(a, r"^[0-9a-f]{12}$")
        self.assertEqual(fc_ledger.run_id(proj, "003"), a)
        other = fc_ledger.run_id(proj, "004")
        fresh = fc_ledger.run_id(proj, "003", fresh=True)
        self.assertRegex(fresh, r"^[0-9a-f]{12}$")
        self.assertNotEqual(fresh, a)
        self.assertEqual(json.loads((proj / "results" / "runs.json").read_text()), {"003": fresh, "004": other})
        self.assertEqual(fc_ledger.run_id(proj, "003"), fresh)


def row(n, phase="attempt", source="run"):
    return {"event_id": f"{n:012x}", "run_id": "r", "commit_sha": "", "task_ids": ["003"], "criterion_id": [], "phase": phase,
            "source": source, "result": "fail", "fails_on_base": None, "detail": f"d{n}"}


class AddRow(unittest.TestCase):
    def setUp(self):
        self.calls, self.comments, self.fail, self.err, self.said, self.tries, self.lookup = [], [], 0, "boom", [], 0, False

        def gql(query, variables):
            self.calls.append(variables)
            if "commentCreate" not in query:
                if self.lookup:
                    raise RuntimeError("no issue")
                return {"issue": {"id": "uuid-" + variables["id"], "identifier": variables["id"]}}
            self.tries += 1
            if self.fail:
                self.fail -= 1
                raise RuntimeError(self.err)
            self.comments.append(variables["b"])
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", "lin_api_x"), (fc.fc_linear, "linear_gql", gql), (fc_ledger, "UNSAVED", []),
                                 (fc_ledger, "SAVED", set()), (fc.fc_core, "say", self.said.append)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        p = mock.patch("time.sleep"); self.sleep = p.start(); self.addCleanup(p.stop)

    def test_one_comment_holds_every_row_in_the_exact_text(self):
        rows = [row(1, "red"), row(2), row(3), row(4, "final", "prove")]
        self.assertTrue(fc_ledger.add_row("NEB-1", rows))
        self.assertEqual([c.splitlines() for c in self.comments], [["Evidence: run red, attempt, final", "", "```evidence", *map(json.dumps, rows), "```"]])

    def test_three_failed_tries_say_the_line_and_keep_the_rows_for_the_next_call(self):
        self.fail, self.err = 3, "x" * 300
        a, b, c = row(1), row(2), row(3)
        self.assertFalse(fc_ledger.add_row("NEB-1", [a, b]))
        self.assertEqual((self.tries, [c.args for c in self.sleep.call_args_list], fc_ledger.unsaved()), (3, [(2,), (2,)], [a, b]))
        self.assertEqual(self.said[-1], f"ledger NEB-1: evidence not saved after 3 tries, kept in memory: {a['event_id']}, {b['event_id']} ({'x' * 200})")
        self.assertTrue(fc_ledger.add_row("NEB-2", [c]))
        self.assertEqual((fc_ledger.unsaved(), self.comments[-1]), ([a, b], fc_ledger.comment_text([c])))
        self.assertTrue(fc_ledger.add_row("NEB-1", [c]))
        self.assertEqual((fc_ledger.unsaved(), self.comments[-1]), ([], fc_ledger.comment_text([a, b, c])))

    def test_a_late_success_saves_and_a_saved_row_is_never_written_again(self):
        self.fail = 2
        a, b = row(1), row(2)
        self.assertTrue(fc_ledger.add_row("NEB-1", [a, a]))
        self.assertEqual((self.comments, fc_ledger.unsaved(), self.said), ([fc_ledger.comment_text([a])], [], []))
        self.assertTrue(fc_ledger.add_row("NEB-1", [a, b]))
        self.assertTrue(fc_ledger.add_row("NEB-1", [a, b]))
        self.assertEqual(self.comments[1:], [fc_ledger.comment_text([b])])

    def test_a_failing_issue_lookup_is_a_failed_try_and_never_raises(self):
        self.lookup, a = True, row(1)
        self.assertFalse(fc_ledger.add_row("NEB-1", [a]))
        self.assertEqual((fc_ledger.unsaved(), self.comments, self.said[-1]),
                         ([a], [], f"ledger NEB-1: evidence not saved after 3 tries, kept in memory: {a['event_id']} (no issue)"))

    def test_no_key_or_no_issue_writes_nothing_and_keeps_nothing(self):
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", None):
            self.assertTrue(fc_ledger.add_row("NEB-1", [row(1)]))
        self.assertTrue(fc_ledger.add_row("", [row(1)]))
        self.assertEqual((self.calls, fc_ledger.unsaved()), ([], []))

    def test_record_refreshes_only_when_the_write_succeeded(self):
        with mock.patch.object(fc_evidence, "refresh") as refresh:
            self.fail = 3
            self.assertFalse(fc_ledger.record("NEB-1", [row(1)]))
            refresh.assert_not_called()
            self.assertTrue(fc_ledger.record("NEB-1", [row(2)]))
            refresh.assert_called_once_with("NEB-1")


class FakeLinear:
    """Comments per issue, as Linear keeps them. A write named in `drops` fails; it lands first unless its name is in `lost`,
    as a write that timed out may."""
    def __init__(self):
        self.comments, self.drops, self.lost, self.unreadable, self.writes = {}, [], [], False, 0

    def __call__(self, query, variables):
        if "commentCreate" in query:
            self.writes += 1
            err = self.drops.pop(0) if self.drops else None
            if err not in self.lost:
                self.comments.setdefault(variables["id"], []).append(variables["b"])
            if err:
                raise RuntimeError(err)
            return {"commentCreate": {"success": True}}
        if "comments(" in query:
            if self.unreadable:
                raise RuntimeError("read failed")
            return {"issue": {"comments": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                           "nodes": [{"body": b, "createdAt": f"2026-01-01T00:00:{i:02d}Z"} for i, b in enumerate(self.comments.get(variables["id"], []))]}}}
        return {"issue": {"id": variables["id"], "identifier": variables["id"]}}

    def rows(self, issue):
        return [r["event_id"] for b in self.comments.get(issue, []) for r in fc_evidence.parse_comment(b)]


class LateLanding(unittest.TestCase):
    """A Linear call that timed out may have written anyway: a retry must not add the same rows a second time."""
    def setUp(self):
        self.linear, self.said = FakeLinear(), []
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", "lin_api_x"), (fc.fc_linear, "linear_gql", self.linear), (fc_ledger, "UNSAVED", []),
                                 (fc_ledger, "SAVED", set()), (fc.fc_core, "say", self.said.append)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        p = mock.patch("time.sleep"); p.start(); self.addCleanup(p.stop)

    def test_a_write_that_timed_out_and_landed_is_not_written_again_by_the_retry(self):
        self.linear.drops = ["Linear gave no answer within 120s"]
        a, b = row(1), row(2)
        self.assertTrue(fc_ledger.add_row("NEB-1", [a, b]))
        self.assertEqual((self.linear.writes, self.linear.rows("NEB-1"), fc_ledger.unsaved(), self.said), (1, [a["event_id"], b["event_id"]], [], []))
        self.assertTrue(fc_ledger.add_row("NEB-1", [a, b]))  # and a saved row is still never written again
        self.assertEqual(self.linear.writes, 1)

    def test_a_write_that_never_landed_is_written_by_the_retry(self):
        self.linear.drops, self.linear.lost = ["boom"], ["boom"]
        a = row(1)
        self.assertTrue(fc_ledger.add_row("NEB-1", [a]))
        self.assertEqual((self.linear.writes, self.linear.rows("NEB-1")), (2, [a["event_id"]]))

    def test_only_the_rows_that_did_not_land_are_retried(self):
        a, b, c = row(1), row(2), row(3)
        self.assertTrue(fc_ledger.add_row("NEB-1", [a]))
        self.linear.comments["NEB-1"].append(fc_ledger.comment_text([b]))  # b landed through a timed-out write of an earlier call
        fc_ledger.UNSAVED.append(("NEB-1", b))
        self.assertTrue(fc_ledger.add_row("NEB-1", [c]))
        self.assertEqual(self.linear.rows("NEB-1"), [a["event_id"], b["event_id"], c["event_id"]])
        self.assertEqual(self.linear.comments["NEB-1"][-1], fc_ledger.comment_text([c]))
        self.assertEqual(fc_ledger.unsaved(), [])

    def test_rows_kept_after_three_tries_that_landed_late_are_not_written_again_by_the_next_call(self):
        self.linear.drops = ["t1", "t2", "t3"]
        a = row(1)
        with mock.patch.object(self.linear, "unreadable", True):
            self.assertFalse(fc_ledger.add_row("NEB-1", [a]))
        self.assertEqual((self.linear.writes, fc_ledger.unsaved()), (3, [a]))  # three blind writes: the read was down too
        self.assertEqual(len(self.linear.comments["NEB-1"]), 3)
        self.assertTrue(fc_ledger.add_row("NEB-1", []))
        self.assertEqual((self.linear.writes, fc_ledger.unsaved()), (3, []))
        self.assertEqual(fc_evidence.rows("NEB-1"), [{**a, "at": "2026-01-01T00:00:00Z"}])  # the reading side shows it once

    def test_a_read_that_fails_does_not_stop_the_retry_from_writing(self):
        self.linear.unreadable, self.linear.drops = True, ["boom"]
        self.assertTrue(fc_ledger.add_row("NEB-1", [row(1)]))
        self.assertEqual(self.linear.writes, 2)

    def test_another_issues_rows_are_not_mistaken_for_this_ones(self):
        a = row(1)
        self.assertTrue(fc_ledger.add_row("NEB-2", [a]))
        self.linear.drops, self.linear.lost = ["boom"], ["boom"]
        self.assertTrue(fc_ledger.add_row("NEB-1", [a]))
        self.assertEqual((self.linear.rows("NEB-1"), self.linear.rows("NEB-2")), ([a["event_id"]], [a["event_id"]]))


class RunEvidence(unittest.TestCase):
    BODY = "## Acceptance criteria\n* works\n* fast\n"

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"
        self.proj.mkdir()
        self.repo = Path(tempfile.mkdtemp())
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "one")
        self.base = git("rev-parse", "HEAD")
        git("branch", "task", "HEAD")
        git("commit", "-q", "--allow-empty", "-m", "two")
        self.head, self.task = git("rev-parse", "HEAD"), git("rev-parse", "task")
        self.meta = {"id": "003", "body": self.BODY}

    def events(self, **result):
        return fc_ledger.run_events(self.proj, self.repo, self.meta, {"id": "003", "title": "t", "attempts": 0, **result})

    def ev(self, sha, phase, result, base, detail):
        return fc_ledger.event(fc_ledger.run_id(self.proj, "003"), sha, ["003"], fc_ledger.criterion_ids(self.meta), phase, "run", result, base, detail)

    def test_held_has_no_events_and_already_green_has_one_red_pass(self):
        self.assertEqual(self.events(status="held", log=[{"attempt": 1, "test_exit": 1}]), [])
        self.assertEqual(self.events(status="already_green", base=self.base, log=[]),
                         [self.ev(self.head, "red", "pass", False, "test already passes before the change")])

    def test_a_task_that_ran_gives_red_each_attempt_and_the_final_in_order(self):
        log = [{"attempt": 1, "test_exit": 1, "tampered": [], "test_output": "ok a\nFAIL x\n\n"},
               {"attempt": 2, "rate_limited": True, "error": "429; giving up after 4 waits"},
               {"attempt": 2, "error": "implementer timed out"},
               {"attempt": 2, "test_exit": 0, "tampered": ["a.py", "b.py"], "test_output": ""},
               {"attempt": 3, "test_exit": 0, "tampered": [], "test_output": "OK"}]
        self.assertEqual(self.events(status="passed", base=self.base, branch="task", attempts=3, log=log), [
            self.ev(self.base, "red", "fail_expected", True, "test fails before the change"),
            self.ev("", "attempt", "fail", None, "attempt 1: test exit 1: FAIL x"),
            self.ev("", "attempt", "error", None, "attempt 2: implementer timed out"),
            self.ev("", "attempt", "fail", None, "attempt 2: test exit 0, tampered a.py, b.py"),
            self.ev("", "attempt", "pass", None, "attempt 3: test exit 0: OK"),
            self.ev(self.task, "final", "pass", None, "passed after 3 attempts")])

    def test_final_result_by_status_and_no_red_without_a_base(self):
        for status, result in (("failed", "fail"), ("rejected_tampered", "fail"), ("blocked_by_001", "error"), ("unchained", "error")):
            self.assertEqual(self.events(status=status, attempts=2, log=[], branch="task"), [self.ev("", "final", result, None, f"{status} after 2 attempts")], status)

    def test_run_tasks_records_each_finished_tasks_events_on_its_issue(self):
        (self.proj / "results").mkdir()
        passed = lambda repo, proj, meta, attempts: {"id": meta["id"], "title": "t", "status": "passed", "attempts": 1, "seconds": 0.0, "cost": 0.0, "branch": "-"}
        task = {"id": "004", "model": "pi:x/glm", "linear": "ENG-4", "body": self.BODY}
        with mock.patch.object(fc.fc_implement, "run_task", passed), mock.patch.object(fc.fc_run, "preflight", lambda m: None), \
                mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None), mock.patch.object(fc.fc_run, "triage", lambda *x: None), \
                mock.patch.object(fc_ledger, "record") as record, mock.patch.object(fc_ledger, "record_task") as record_task, \
                mock.patch("sys.stdout", io.StringIO()), self.assertRaises(SystemExit):
            fc.fc_run.run_tasks(self.repo, self.proj, [task], SimpleNamespace(trunk="main", parallel=1, attempts=1, no_stack=True))
        (issue, events), = [c.args for c in record.call_args_list] + [(c.args[2].get("linear"), c.args[3]) for c in record_task.call_args_list]
        self.assertEqual((issue, [(e["phase"], e["result"], e["task_ids"], e["criterion_id"]) for e in events]),
                         ("ENG-4", [("final", "pass", ["004"], fc_ledger.criterion_ids(task)), ("cost", "pass", ["004"], fc_ledger.criterion_ids(task))]))


class VerifyShared(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"
        self.proj.mkdir()
        self.repo = Path(tempfile.mkdtemp())
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "one")
        self.sha = git("rev-parse", "HEAD")
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')

    def ev(self, phase, result, detail):
        return fc_ledger.event("", self.sha, [], [], phase, "verify", result, None, detail)

    def record(self, report, saved=(True, True, True, True)):
        with mock.patch.object(fc_ledger, "record", side_effect=list(saved)) as record:
            ok = fc_ledger.verify_record(self.proj, self.repo, report)
        return ok, [c.args for c in record.call_args_list]

    def test_each_stage_is_one_record_on_the_feature_issue_and_gave_up_is_an_error(self):
        e = lambda **kw: {"tool_error": False, "stdout": "", "stderr": "", "output": "", **kw}
        ok, calls = self.record({"checks": [e(cmd="a", exit=0), e(cmd="b", exit=2), e(cmd="c", exit=0, output="JEV_GAVE_UP"), e(cmd="d", exit=127, tool_error=True)],
                                 "e2e": [e(name="w", exit=0), e(name="x", exit=1), e(name="y", exit=0, stdout="JEV_GAVE_UP"), e(name="z", exit=0, stderr="JEV_GAVE_UP")],
                                 "review": {"pass": False, "issues": [{}, {}], "confirmed": [{}]}})
        self.assertEqual(calls[:3], [("ENG-1", [self.ev("checks", "pass", "a: exit 0"), self.ev("checks", "fail", "b: exit 2"), self.ev("checks", "error", "c: exit 0"), self.ev("checks", "error", "d: exit 127")]),
                                 ("ENG-1", [self.ev("e2e", "pass", "w: exit 0"), self.ev("e2e", "fail", "x: exit 1"), self.ev("e2e", "error", "y: exit 0"), self.ev("e2e", "error", "z: exit 0")]),
                                 ("ENG-1", [self.ev("review", "fail", "1 confirmed of 2 raised")])])
        self.assertTrue(ok)

    def test_a_stage_that_did_not_run_is_one_skipped_event(self):
        _, calls = self.record({"checks": [], "e2e": [], "review": None})
        self.assertEqual([c[1] for c in calls], [[self.ev("checks", "skipped", "no checks ran")], [self.ev("e2e", "skipped", "no e2e cases ran")], [self.ev("review", "skipped", "review did not run")],
                                                    [self.ev("acceptance", "skipped", "acceptance did not run")]])
        _, calls = self.record({"checks": [], "e2e": [], "review": {"pass": True, "issues": [], "confirmed": []}})
        self.assertEqual(calls[2][1], [self.ev("review", "pass", "0 confirmed of 0 raised")])

    def test_false_when_any_record_fails_and_every_stage_is_still_tried(self):
        report = {"checks": [], "e2e": [], "review": None}
        for saved in ((False, True, True, True), (True, False, True, True), (True, True, False, True), (True, True, True, False)):
            ok, calls = self.record(report, saved)
            self.assertEqual((ok, len(calls)), (False, 4), saved)

    def test_verify_rounds_records_its_round_with_the_report(self):
        report = {"checks": [], "e2e": [], "review": None}
        with mock.patch.object(fc.fc_verify, "run_stages", return_value=(True, report, "")), mock.patch.object(fc.fc_verify, "review_inputs", return_value=(0, 0, 0, [])), \
                mock.patch.object(fc_ledger, "verify_record") as vr, mock.patch.object(fc.fc_core, "say"), self.assertRaises(SystemExit):
            fc.fc_verify.verify_rounds(self.repo, self.proj, {"review_base": "main"}, SimpleNamespace(fix=0, skip_review=True), [], 0)
        self.assertEqual([c.args for c in vr.call_args_list], [(self.proj, self.repo, report)])


class VerifyTasks(unittest.TestCase):
    T1 = "python3 -m unittest x"

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"
        (self.proj / "results").mkdir(parents=True)
        self.repo = Path(tempfile.mkdtemp())
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "one")
        self.sha = git("rev-parse", "HEAD")
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        body = lambda *c: "## Goal\ng\n\n## Acceptance criteria\n" + "".join(f"* {x}\n" for x in c)
        self.tasks = [{"id": "021", "linear": "ENG-4", "test": self.T1, "body": body("a", "b", "c", "d", "e")}, {"id": "022", "linear": "ENG-5", "test": "t2", "body": body("x")},
                      {"id": "023", "linear": "ENG-6", "test": "t3", "body": body("y"), "kind": "design"}, {"id": "024", "linear": "ENG-7", "test": "t4", "body": body("z")}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": self.tasks}))

    def record(self, scoped=(), acceptance=None, saved=lambda issue, events: True):
        report = {"checks": [], "e2e": [], "review": None, "scoped": list(scoped), "acceptance": acceptance}
        writes = []  # (issue, events) in call order, whichever writer made it
        def write(issue, events):
            writes.append((issue, events))
            return saved(issue, events)
        with mock.patch.object(fc_ledger, "record", side_effect=write), \
                mock.patch.object(fc_ledger, "record_task", side_effect=lambda repo, proj, meta, events: write(meta.get("linear"), events)):
            ok = fc_ledger.verify_record(self.proj, self.repo, report)
        return ok, writes

    def ev(self, t, ids, phase, result, detail):
        return fc_ledger.event(fc_ledger.run_id(self.proj, t["id"]), self.sha, [t["id"]], ids, phase, "verify", result, None, detail)

    def test_each_code_task_gets_scoped_then_acceptance_events_even_when_empty(self):
        t1, t2 = self.tasks[:2]
        ids, x = fc_ledger.criterion_ids(t1), fc_ledger.criterion_ids(t2)
        e = lambda **kw: {"tool_error": False, "output": "", **kw}
        row = lambda t, c, choice, **kw: {"task": t, "criterion": c, "choice": choice, "p_met": 0.9, "p_not_met": 0.1, **kw}
        rows = [row("021", "b", "NOT_MET", confirmed=True), row("021", "a", "MET"), row("021", "c", "NOT_MET"), row("021", "d", "NOT_MET", judge_error="no answer"),
                row("021", "e", None, p_met=None, p_not_met=None), row("022", "x", "MET")]
        rows[2]["proven_by"] = "f1"  # criterion c: unclear for Jev, proven by the task's test
        scoped = [e(cmd=self.T1, exit=1), e(cmd="other", exit=0), e(cmd=self.T1, exit=0, output="JEV_GAVE_UP"), e(cmd=self.T1, exit=0)]
        _, calls = self.record(scoped, {"criteria": rows})
        miss = "NOT_MET: p_met 0.9, p_not_met 0.1"
        self.assertEqual([c for c in calls if c[0] != "ENG-1"], [
            ("ENG-4", [self.ev(t1, ids, "scoped", r, f"{self.T1}: exit {n}") for r, n in (("fail", 1), ("error", 0), ("pass", 0))]),
            ("ENG-4", [self.ev(t1, [ids[1]], "acceptance", "fail", miss), self.ev(t1, [ids[0]], "acceptance", "pass", "MET: p_met 0.9, p_not_met 0.1"),
                       self.ev(t1, [ids[2]], "acceptance", "pass", miss + ", proven by test event f1"), self.ev(t1, [ids[3]], "acceptance", "error", miss),
                       self.ev(t1, [ids[4]], "acceptance", "error", "None: p_met None, p_not_met None")]),
            ("ENG-5", []), ("ENG-5", [self.ev(t2, x, "acceptance", "pass", "MET: p_met 0.9, p_not_met 0.1")]), ("ENG-7", []), ("ENG-7", [])])

    def test_acceptance_that_did_not_judge_is_one_feature_event(self):
        for acceptance, result, detail in ((None, "skipped", "acceptance did not run"), ({"error": "boom"}, "error", "boom"), ({"criteria": [], "note": "no criteria"}, "skipped", "no criteria")):
            ok, calls = self.record(acceptance=acceptance)
            events = [e for issue, es in calls if issue == "ENG-1" for e in es if e["phase"] == "acceptance"]
            self.assertEqual(events, [fc_ledger.event("", self.sha, [], [], "acceptance", "verify", result, None, detail)], acceptance)
            self.assertEqual((ok, len([c for c in calls if c[0] != "ENG-1"])), (True, 6))

    def test_false_when_a_task_record_fails(self):
        ok, _ = self.record(saved=lambda issue, events: issue != "ENG-5")
        self.assertFalse(ok)

class VerifyUnsaved(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"
        (self.proj / "results").mkdir(parents=True)
        self.repo = Path(tempfile.mkdtemp())
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo, check=True, capture_output=True, text=True)
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "one")
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')

    def rounds(self, passed, rows=(), saves=False):
        """verify_rounds with the stages mocked; the rows are (issue, event id) still unsaved, and `saves` lets add_row clear them"""
        said, out, unsaved = [], io.StringIO(), [(i, {"event_id": e}) for i, e in rows]
        add = mock.Mock(return_value=saves)
        lost = lambda: [] if saves and add.called else [r for _, r in unsaved]
        with mock.patch.object(fc.fc_verify, "run_stages", return_value=(passed, {"checks": [], "e2e": [], "review": None}, "" if passed else "finding")), \
                mock.patch.object(fc_ledger, "verify_record"), mock.patch.object(fc_ledger, "UNSAVED", unsaved), mock.patch.object(fc_ledger, "add_row", add), \
                mock.patch.object(fc_ledger, "unsaved", lost), mock.patch.object(fc.fc_verify, "verify_failed") as failed, mock.patch.object(fc.fc_linear, "linear_reconcile"), \
                mock.patch.object(fc.fc_core, "say", said.append), contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            fc.fc_verify.verify_rounds(self.repo, self.proj, {"review_base": "main"}, SimpleNamespace(fix=0, skip_review=True), [], 0)
        return cm.exception.code, said, out.getvalue(), json.loads((self.proj / "results" / "verify.json").read_text()), add, failed

    def batches(self, add):
        """each add_row call as (issue, its events' phases): verify's cost batch on the feature comes before the unsaved retries"""
        return [(c.args[0], [e["phase"] for e in c.args[1]]) for c in add.call_args_list]

    def test_rows_still_unsaved_fail_even_a_passing_verify_and_skip_the_failure_screen(self):
        for passed in (True, False):
            code, said, out, final, add, failed = self.rounds(passed, [("ENG-4", "e1"), ("ENG-4", "e2"), ("ENG-5", "e3")])
            self.assertEqual(self.batches(add), [("ENG-1", ["cost"]), ("ENG-4", []), ("ENG-5", [])], passed)
            self.assertIn("verify FAIL  evidence not saved on Linear: e1, e2, e3", said)
            self.assertEqual((code, final["passed"], failed.called, "Unresolved" in out), (1, False, False, False), passed)

    def test_rows_saved_on_the_retry_leave_the_verdict(self):
        code, said, _, final, add, _ = self.rounds(True, [("ENG-4", "e1")], saves=True)
        self.assertEqual((code, final["passed"], self.batches(add)), (0, True, [("ENG-1", ["cost"]), ("ENG-4", [])]))
        self.assertFalse([s for s in said if "not saved" in s])

    def test_nothing_unsaved_keeps_the_verdict_and_exit(self):
        code, said, out, final, add, failed = self.rounds(True)
        self.assertEqual((code, final["passed"], self.batches(add), failed.called), (0, True, [("ENG-1", ["cost"])], False))
        code, said, out, final, add, failed = self.rounds(False)
        self.assertEqual((code, final["passed"], self.batches(add), failed.call_count, "Unresolved" in out), (1, False, [("ENG-1", ["cost"])], 1, True))
        self.assertFalse([s for s in said if "not saved" in s])

    def test_verify_record_writes_each_tasks_scoped_then_acceptance_with_record_task(self):
        task = {"id": "021", "linear": "ENG-4", "test": "t1", "body": "## Acceptance criteria\n* a\n"}
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [task]}))
        report = {"checks": [], "e2e": [], "review": None, "scoped": [{"cmd": "t1", "exit": 0, "tool_error": False, "output": ""}],
                  "acceptance": {"criteria": [{"task": "021", "criterion": "a", "choice": "MET", "p_met": 0.9, "p_not_met": 0.1}]}}
        with mock.patch.object(fc_ledger, "record", return_value=True) as record, mock.patch.object(fc_ledger, "record_task", return_value=True) as record_task:
            fc_ledger.verify_record(self.proj, self.repo, report)
        self.assertEqual([c.args[:3] for c in record_task.call_args_list], [(self.repo, self.proj, task)] * 2)
        self.assertEqual([[e["phase"] for e in c.args[3]] for c in record_task.call_args_list], [["scoped"], ["acceptance"]])
        self.assertEqual({c.args[0] for c in record.call_args_list}, {"ENG-1"})


class Relevance(unittest.TestCase):
    def setUp(self):
        self.repo, self.proj = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "feat"
        (self.proj / "results").mkdir(parents=True)
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main"); (self.repo / "a.py").write_text("1\n"); git("add", "-A"); git("commit", "-q", "-m", "a")
        self.old = git("rev-parse", "HEAD")
        git("checkout", "-q", "-b", "task-021"); (self.repo / "b.py").write_text("x\n"); git("add", "-A"); git("commit", "-q", "-m", "b")
        self.tip = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")
        (self.proj / "results" / "runs.json").write_text('{"021": "r1"}')
        (self.proj / "results" / "021.json").write_text('{"branch": "task-021"}')
        h = lambda t: hashlib.sha256(t.encode()).hexdigest()[:8]
        self.a, self.b = f"021.1:{h('a')}", f"021.2:{h('b')}"

    def row(self, n, **kw):
        return {"event_id": n, "run_id": "r1", "commit_sha": "", "task_ids": [], "criterion_id": [], "phase": "final", "source": "run",
                "result": "pass", "fails_on_base": None, "detail": "", "at": "2026-10-21T10:00:00Z", "shared": False, **kw}

    def counted(self, rows, files=(), protected=()):
        meta = {"id": "021", "body": "## Acceptance criteria\n* a\n* b\n", "files": list(files), "protected": list(protected)}
        return fc_ledger.counted(self.repo, self.proj, meta, rows)

    def ids(self, *args, **kw):
        return [r["event_id"] for r in self.counted(*args, **kw)]

    def test_task_and_run_decide_and_a_row_with_no_commit_counts_by_them_alone(self):
        rows = [self.row("all"), self.row("mine", task_ids=["021"]), self.row("other", task_ids=["022", "023"]), self.row("old-run", run_id="r0"),
                self.row("shared", run_id="r0", shared=True), self.row("base", fails_on_base=True), self.row("other-shared", shared=True, task_ids=["022"])]
        self.assertEqual(self.ids(rows), ["all", "mine", "shared", "base"])

    def test_the_tip_counts_and_an_older_commit_only_while_the_files_are_unchanged(self):
        rows = [self.row("tip", commit_sha=self.tip), self.row("old", commit_sha=self.old), self.row("unknown", commit_sha="0" * 40)]
        self.assertEqual([self.ids(rows), self.ids(rows, files=["a.py"]), self.ids(rows, files=["b.py"])], [["tip"], ["tip", "old"], ["tip"]])

    def test_a_fails_on_base_row_follows_the_protected_files_not_the_tip_or_the_files(self):
        rows = [self.row("tip", commit_sha=self.tip, fails_on_base=True), self.row("old", commit_sha=self.old, fails_on_base=True)]
        self.assertEqual([self.ids(rows, files=["b.py"], protected=["a.py"]), self.ids(rows, files=["a.py"], protected=["b.py"]), self.ids(rows, files=["a.py"])],
                         [["tip", "old"], ["tip"], []])

    def test_the_tip_is_the_result_branch_or_else_head(self):
        rows = [self.row("tip", commit_sha=self.tip), self.row("head", commit_sha=self.old)]
        self.assertEqual(self.ids(rows), ["tip"])
        for text in (None, "{}", '{"branch": "gone"}'):
            (self.proj / "results" / "021.json").unlink(missing_ok=True)
            if text: (self.proj / "results" / "021.json").write_text(text)
            self.assertEqual(self.ids(rows), ["head"], text)

    def test_a_row_keeps_only_the_current_criteria_and_is_dropped_when_none_remain(self):
        rows = [self.row("none"), self.row("both", criterion_id=[self.a, self.b]), self.row("mixed", criterion_id=["021.1:00000000", self.b]),
                self.row("stale", criterion_id=["021.1:00000000", "021.3:" + self.a[6:]])]
        self.assertEqual([(r["event_id"], r["criterion_id"]) for r in self.counted(rows)], [("none", []), ("both", [self.a, self.b]), ("mixed", [self.b])])

class RecordTask(unittest.TestCase):
    def setUp(self):
        self.repo, self.proj = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "feat"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "results" / "runs.json").write_text('{"021": "r1"}')
        for args in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "a"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        self.meta = {"id": "021", "linear": "ENG-4", "body": "## Acceptance criteria\n* a\n", "files": [], "protected": []}
        self.issues = {"ENG-4": [self.row("mine"), self.row("old-run", run_id="r0")], "ENG-1": [self.row("shared", run_id="r0"), self.row("other", task_ids=["022"])]}
        self.writes, self.description, self.events = [], "## Goal\nx\n", [{"event_id": "new"}]

    def row(self, n, **kw):
        return {"event_id": n, "run_id": "r1", "commit_sha": "", "task_ids": [], "criterion_id": [], "phase": "final", "source": "run",
                "result": "pass", "fails_on_base": None, "detail": "", "at": "2026-10-21T10:00:00.000Z", **kw}

    def gql(self, query, variables):
        self.writes.append(query)
        if "issueUpdate" in query:
            self.description = variables["d"]
        return {"issue": {"id": "uuid-" + variables["id"], "description": self.description}}

    def record(self, saved=True, linear=True):
        if linear: (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        with mock.patch.object(fc_ledger, "add_row", return_value=saved) as add_row, mock.patch.object(fc.fc_linear, "linear_gql", self.gql), \
                mock.patch.object(fc.fc_config, "LINEAR_KEY", "lin_api_x"), mock.patch.object(fc_evidence, "rows", lambda issue, **kw: list(self.issues[issue])):
            result = fc_ledger.record_task(self.repo, self.proj, self.meta, self.events)
        add_row.assert_called_once_with("ENG-4", self.events)
        return result

    def test_saved_events_refresh_the_table_from_the_counted_rows_of_both_issues(self):
        self.assertIs(self.record(), True)
        self.assertEqual([k in self.description for k in ("mine", "shared", "old-run", "other")], [True, True, False, False])

    def test_unsaved_events_refresh_nothing_and_return_false(self):
        self.assertIs(self.record(saved=False), False)
        self.assertEqual((self.writes, self.description), ([], "## Goal\nx\n"))

    def test_without_linear_json_or_a_feature_the_table_holds_the_task_issue_rows_only(self):
        self.issues["ENG-1"] = None  # reading the feature issue would fail
        self.assertIs(self.record(linear=False), True)
        self.assertEqual([k in self.description for k in ("mine", "old-run")], [True, False])
        (self.proj / "linear.json").write_text("{}")
        self.assertIs(self.record(linear=False), True)


class RedoRunId(unittest.TestCase):
    def setUp(self):
        self.repo, self.proj = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "feat"
        (self.proj / "results").mkdir(parents=True)
        self.tasks = [{"id": "001", "model": "pi:x/glm", "linear": "ENG-1", "body": "", "protected": []},
                      {"id": "002", "model": "pi:x/glm", "linear": "ENG-2", "body": "", "protected": [], "linear_state": "Done"},
                      {"id": "003", "model": "pi:x/glm", "linear": "ENG-3", "body": "", "protected": []}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": self.tasks}))

    def run_cmd(self, redo, only=None):
        a = SimpleNamespace(repo=str(self.repo), project="p", only=only, redo=redo, no_stack=False, unproven=True)
        parent = mock.Mock()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_run, "require_committed"), \
                mock.patch.object(fc.fc_briefs, "test_open", return_value=False), mock.patch.object(fc.fc_briefs, "waiting_writers", return_value={}), \
                mock.patch.object(fc.fc_run, "run_tasks", parent.run_tasks), mock.patch.object(fc_ledger, "run_id", parent.run_id), \
                mock.patch.object(fc.fc_route, "route_tasks", return_value=({}, {})), mock.patch.dict(os.environ), mock.patch("sys.stdout", io.StringIO()):
            try:
                fc.fc_run.cmd_run(a)
            except SystemExit:
                pass
        return [(c[0], c[1][1], c[2]) if c[0] == "run_id" else (c[0],) for c in parent.mock_calls]

    def test_redo_mints_a_new_run_id_for_each_dispatched_task_before_dispatch(self):
        self.assertEqual(self.run_cmd(redo=True), [("run_id", "001", {"fresh": True}), ("run_id", "003", {"fresh": True}), ("run_tasks",)])
        self.assertEqual(self.run_cmd(redo=True, only="003"), [("run_id", "003", {"fresh": True}), ("run_tasks",)])

    def test_a_run_that_stops_before_dispatch_mints_no_run_id(self):
        self.tasks[0]["protected"] = ["tests/test_missing.py"]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": self.tasks}))
        self.assertEqual(self.run_cmd(redo=True), [])
        self.assertEqual(self.run_cmd(redo=True, only="002"), [])  # its only task is Done: nothing to run

    def test_run_tasks_records_each_finished_task_with_record_task(self):
        (self.proj / "results").mkdir(exist_ok=True)
        passed = lambda repo, proj, meta, attempts: {"id": meta["id"], "title": "t", "status": "passed", "attempts": 1, "seconds": 0.0, "cost": 0.0, "branch": "-"}
        t1, t3 = self.tasks[0], self.tasks[2]
        with mock.patch.object(fc.fc_implement, "run_task", passed), mock.patch.object(fc.fc_run, "preflight", lambda m: None), \
                mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None), mock.patch.object(fc.fc_run, "triage", lambda *x: None), \
                mock.patch.object(fc_ledger, "run_events", side_effect=lambda proj, repo, meta, r: [meta["id"], r["status"]]), \
                mock.patch.object(fc.fc_run.fc_costevent, "run_event", side_effect=lambda proj, meta, r, final: f"cost of {final}"), \
                mock.patch.object(fc_ledger, "record_task") as record_task, mock.patch("sys.stdout", io.StringIO()), self.assertRaises(SystemExit):
            fc.fc_run.run_tasks(self.repo, self.proj, [t1, t3], SimpleNamespace(trunk="main", parallel=1, attempts=1, no_stack=True))
        self.assertEqual(sorted((c.args for c in record_task.call_args_list), key=lambda a: a[2]["id"]), [(self.repo, self.proj, t1, ["001", "passed", "cost of passed"]), (self.repo, self.proj, t3, ["003", "passed", "cost of passed"])])


class DoneUnsaved(unittest.TestCase):
    def setUp(self):
        self.repo, self.proj = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "feat"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "verify.json").write_text('{"task_prefix": "task-"}')
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [{"id": t, "title": f"t{t}", "linear": f"ENG-{t}", "body": "## Acceptance criteria\n* a\n"} for t in ("003", "004", "005")]}))
        self.old = json.dumps({"id": "003", "status": "failed", "branch": "task-003"})
        (self.proj / "results" / "003.json").write_text(self.old)

    def done(self, saved, only="003,004,005"):
        """cmd_done with record_task answering `saved` per id; returns (printed, record_task, linear_event, exit code, result files seen at each record_task call)."""
        seen, out = [], io.StringIO()
        def record_task(repo, proj, meta, events):
            seen.append((meta["id"], (proj / "results" / f"{meta['id']}.json").exists() and (proj / "results" / f"{meta['id']}.json").read_text()))
            return saved[meta["id"]]
        code = None
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc_ledger, "record_task", side_effect=record_task) as rt, \
                mock.patch.object(fc.fc_linear, "linear_event") as le, contextlib.redirect_stdout(out):
            try:
                fc.fc_status.cmd_done(SimpleNamespace(repo=str(self.repo), only=only, note="by hand"))
            except SystemExit as e:
                code = e.code
        return out.getvalue(), rt, le, code, seen

    def result(self, tid):
        f = self.proj / "results" / f"{tid}.json"
        return f.read_text() if f.exists() else None

    def test_an_unsaved_done_event_marks_nothing_and_exits_1_after_every_id(self):
        out, rt, le, code, _ = self.done({"003": False, "004": True, "005": False})
        self.assertEqual([c.args[2]["id"] for c in rt.call_args_list], ["003", "004", "005"])
        self.assertEqual(code, 1)
        self.assertEqual(self.result("003"), self.old)
        self.assertIsNone(self.result("005"))
        self.assertEqual(json.loads(self.result("004"))["status"], "hand_done")
        self.assertEqual([c.args[1]["id"] for c in le.call_args_list], ["004"])
        self.assertIn("003 not done: its done event is not saved on Linear; run compose done again\n", out)
        self.assertIn("005 not done: its done event is not saved on Linear; run compose done again\n", out)
        self.assertNotIn("004 not done", out)
        self.assertIn("004 hand_done  t004  (by hand)\n", out)
        self.assertNotIn("hand_done  t003", out)

    def test_each_task_records_its_done_event_before_its_result_is_written(self):
        out, rt, le, code, seen = self.done({"003": True, "004": True, "005": True})
        self.assertIn(code, (None, 0))
        self.assertEqual(seen, [("003", self.old), ("004", False), ("005", False)])
        for c, tid in zip(rt.call_args_list, ("003", "004", "005")):
            self.assertEqual(c.args[:2], (self.repo.resolve(), self.proj))
            self.assertEqual((c.args[2]["id"], len(c.args[3]), c.args[3][0]["phase"], c.args[3][0]["task_ids"]), (tid, 1, "done", [tid]))
        self.assertEqual([json.loads(self.result(t))["status"] for t in ("003", "004", "005")], ["hand_done"] * 3)
        self.assertEqual(le.call_count, 3)
        self.assertNotIn("not done", out)
        self.assertIn("003 hand_done  t003  (by hand)\n", out)

    def test_done_brings_the_task_branch_from_its_clone_into_the_repo(self):
        git = lambda cwd, *c: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *c], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
        clone = Path(tempfile.mkdtemp()) / "003"
        git(self.repo, "clone", "-q", "--shared", "--no-hardlinks", str(self.repo), str(clone))
        git(clone, "checkout", "-q", "-b", "task-003")
        git(clone, "commit", "-q", "--allow-empty", "-m", "finished by hand in the clone")
        (self.proj / "results" / "003.json").write_text(json.dumps({"id": "003", "status": "failed", "branch": "task-003", "worktree": str(clone)}))
        self.done({"003": True}, only="003")
        self.assertEqual(git(self.repo, "rev-parse", "task-003"), git(clone, "rev-parse", "HEAD"))
