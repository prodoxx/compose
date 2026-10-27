import hermetic  # first: no test sees the real home
import contextlib, importlib, io, importlib.machinery, importlib.util, json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_depth  # noqa: E402


class StackDepth(unittest.TestCase):
    """The stack's depth is its deepest code task's route depth."""
    def stack(self, *tasks):
        """Sync cache for (id, description or None, kind) tasks; a None description leaves the issue out."""
        proj = Path(tempfile.mkdtemp()) / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        metas = [{"id": i, "title": "t", "files": [], "protected": [], "test": "true", "body": "", "model": "m", "after": "", "linear": "ENG-" + i, "kind": k} for i, d, k in tasks]
        issues = {"ENG-" + i: {"description": d} for i, d, k in tasks if d is not None}
        (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": metas, "issues": issues}))
        return proj, metas

    def depth(self, *routes):
        """stack_depth for tasks 061, 062, ... whose route depths are `routes`; "-" is no Route section, None no issue."""
        tasks = [("%03d" % (61 + n), None if r is None else "## Goal\nx\n" if r == "-" else f"## Goal\nx\n\n## Route\n\n* workflow: test-first\n* depth: {r}\n", "code") for n, r in enumerate(routes)]
        proj, metas = self.stack(*tasks)
        with mock.patch.object(fc.fc_linear, "linear_gql", side_effect=AssertionError("no Linear call")):
            return fc_depth.stack_depth(proj, metas)

    def test_the_deepest_depth_and_the_first_task_with_it(self):
        self.assertEqual(self.depth("FAST", "DEEP", "DEEP"), ("DEEP", "062"))
        self.assertEqual(self.depth("DEEP", "STANDARD", "FAST"), ("DEEP", "061"))
        self.assertEqual(self.depth("FAST", "FAST"), ("FAST", "061"))

    def test_no_route_section_no_description_or_no_issue_counts_as_standard(self):
        self.assertEqual(self.depth("FAST", "-", "FAST"), ("STANDARD", "062"))
        self.assertEqual(self.depth("FAST", None), ("STANDARD", "062"))
        self.assertEqual(self.depth("FAST", "DEEP", "-"), ("DEEP", "062"))

    def test_a_depth_outside_the_known_depths_counts_as_standard(self):
        self.assertEqual(self.depth("FAST", "DEEPEST"), ("STANDARD", "062"))
        self.assertEqual(self.depth("FAST", "fast"), ("STANDARD", "062"))

    def test_design_tasks_are_ignored_and_no_code_task_is_standard_with_no_id(self):
        proj, metas = self.stack(("061", "## Route\n\n* depth: DEEP\n", "design"), ("062", "## Route\n\n* depth: FAST\n", "code"))
        self.assertEqual(fc_depth.stack_depth(proj, metas), ("FAST", "062"))
        self.assertEqual(fc_depth.stack_depth(proj, metas[:1]), ("STANDARD", None))
        self.assertEqual(fc_depth.stack_depth(proj, []), ("STANDARD", None))
        self.assertEqual(fc_depth.stack_depth(proj, [dict(metas[1], kind="hand")]), ("STANDARD", None))  # a hand task has no route to count


class DepthConfig(unittest.TestCase):
    """The verify settings at the stack's depth."""
    CFG = {"checks": ["c1"], "review": True, "jev_acceptance": True, "e2e": [{"name": "a", "cmd": "x"}, {"name": "b"}, {"name": "c"}]}

    def config(self, *tasks, cfg=None):
        """depth_config for (depth, test, requirements, body, kind) tasks numbered 061, 062, ..."""
        proj = Path(tempfile.mkdtemp()) / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        metas = [{"id": "%03d" % (61 + n), "title": "t", "files": [], "protected": [], "test": t, "body": b, "model": "m", "after": "", "linear": f"ENG-{n}", "kind": k} for n, (d, t, r, b, k) in enumerate(tasks)]
        issues = {f"ENG-{n}": {"description": f"## Route\n\n* workflow: test-first\n* depth: {d}\n* verification requirements: {r}\n"} for n, (d, t, r, b, k) in enumerate(tasks)}
        (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": metas, "issues": issues}))
        return fc_depth.depth_config(self.CFG if cfg is None else cfg, proj, metas)

    def test_fast_checks_are_cfg_then_task_tests_then_requirements_without_duplicates_and_review_is_off(self):
        got, depth, tid = self.config(("FAST", "t1", "r1, c1,, none , r2", "", "code"), ("FAST", "c1", "none", "", "code"), ("FAST", "t3", "dz", "", "design"), ("FAST", "t2", "r2, r3", "", "code"))
        self.assertEqual((depth, tid, got["depth"], got["review"]), ("FAST", "061", "FAST", False))
        self.assertEqual(got["checks"], ["c1", "t1", "t2", "r1", "r2", "r3"])
        self.assertNotIn("acceptance_gate", got)

    def test_fast_keeps_the_e2e_cases_named_by_case_lines_in_cfg_order(self):
        body = "## Goal\nx\n  case:  c  \nsee case: a\ncase: nope"
        got, _, _ = self.config(("FAST", "t", "none", body, "code"), ("FAST", "t", "none", "case: b", "code"))
        self.assertEqual(got["e2e"], [{"name": "b"}, {"name": "c"}])
        self.assertEqual(self.config(("FAST", "t", "none", "case: nope", "code"))[0]["e2e"], [])

    def test_standard_with_no_requirements_is_the_input_plus_its_depth(self):
        got, depth, tid = self.config(("STANDARD", "t1", "none", "case: a", "code"))
        self.assertEqual((got, depth, tid), ({**self.CFG, "depth": "STANDARD"}, "STANDARD", "061"))
        got, _, _ = self.config(("STANDARD", "t1", "r1", "", "code"))
        self.assertEqual(got, {**self.CFG, "depth": "STANDARD", "checks": ["c1", "r1"]})

    def test_deep_is_standard_with_the_acceptance_gate_and_every_e2e_case(self):
        got, depth, tid = self.config(("FAST", "t1", "r0", "case: a", "code"), ("DEEP", "t2", "r1", "case: b", "code"))
        self.assertEqual((got, depth, tid), ({**self.CFG, "depth": "DEEP", "checks": ["c1", "r0", "r1"], "acceptance_gate": True}, "DEEP", "062"))

CFG = {"review": True, "jev_acceptance": True, "e2e": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
SHA = "0123456789abcdef0123456789abcdef01234567"


class DepthEvents(unittest.TestCase):
    """The stages the depth skips go on the feature's ledger."""
    def setUp(self):
        self.ledger = importlib.import_module("fc_ledger")  # the foundation's module, not on every branch
        self.repo = Path(tempfile.mkdtemp()).resolve(); self.proj = self.repo / ".compose" / "p"; self.proj.mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo, check=True, capture_output=True, text=True)
        git("init", "-q", "-b", "main"); (self.repo / "a.txt").write_text("x"); git("add", "-A"); git("commit", "-qm", "init")
        self.head = git("rev-parse", "HEAD").stdout.strip()

    def events(self, cfg, run_cfg, depth="FAST", sha=SHA):
        return fc_depth.depth_events(cfg, run_cfg, depth, "061", sha)

    def expect(self, got, stages, sha=SHA, depth="FAST"):
        self.assertEqual(len(got), len(stages))
        for e, stage in zip(got, stages):
            self.assertEqual(e, self.ledger.event("", sha, [], [], stage.split()[0], "verify", "skipped", None, f"{stage} skipped at depth {depth} (deepest task 061)"))

    def test_fast_skips_review_acceptance_and_the_left_out_e2e_cases_in_order(self):
        self.expect(self.events(CFG, {"review": False, "e2e": [{"name": "b"}]}), ["review", "acceptance", "e2e a", "e2e c"])

    def test_only_the_stages_cfg_runs_and_run_cfg_does_not_are_skipped(self):
        self.expect(self.events({**CFG, "review": False, "jev_acceptance": False}, {"review": False, "e2e": []}), ["e2e a", "e2e b", "e2e c"])
        self.expect(self.events(CFG, {"review": True, "e2e": CFG["e2e"]}), ["acceptance"])
        self.expect(self.events({"e2e": [{"name": "a"}]}, {"review": False, "e2e": []}), ["review", "acceptance", "e2e a"])  # review and acceptance default on

    def test_record_depth_writes_the_events_on_the_feature_once_at_head(self):
        run_cfg = {"review": False, "e2e": [{"name": "a"}]}
        with mock.patch.object(self.ledger, "add_row", return_value=False) as add_row:
            self.assertIs(fc_depth.record_depth(self.proj, self.repo, CFG, run_cfg, "FAST", "061"), False)
        add_row.assert_called_once_with("ENG-1", self.events(CFG, run_cfg, sha=self.head))

    def test_record_depth_with_no_events_returns_true_without_add_row(self):
        with mock.patch.object(self.ledger, "add_row") as add_row:
            self.assertIs(fc_depth.record_depth(self.proj, self.repo, CFG, {"review": True, "e2e": CFG["e2e"]}, "DEEP", "061"), True)
        add_row.assert_not_called()


class VerifyDepth(unittest.TestCase):
    """verify runs the stages of the stack's deepest task."""

    def verify(self, *tasks):
        """cmd_verify on a stack of (id, depth, kind) tasks; a None depth has no Route. (said, record_depth calls, verify_rounds cfgs, repo)."""
        repo = Path(tempfile.mkdtemp()).resolve(); proj = repo / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        self.cfg = {"checks": ["c1"], "review": True, "jev_acceptance": True}; (proj / "verify.json").write_text(json.dumps(self.cfg))
        metas = [{"id": i, "title": "t", "files": [], "protected": [], "test": "t" + i, "body": "", "model": "m", "after": "", "linear": "ENG-" + i, "kind": k} for i, d, k in tasks]
        issues = {"ENG-" + i: {"description": "## Goal\nx\n" + ("" if d is None else f"\n## Route\n\n* workflow: test-first\n* depth: {d}\n* verification requirements: none\n")} for i, d, k in tasks}
        (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": metas, "issues": issues}))
        calls, said, a = [], [], mock.Mock(repo=str(repo), project="p", skip_e2e=False, skip_review=False, fix=0, review_only=False)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_core, "say", said.append), \
                mock.patch.object(fc.fc_verify, "verify_root", return_value=(repo, "main", None)), mock.patch.object(fc_depth, "record_depth", side_effect=lambda *x: calls.append(("record", x))), \
                mock.patch.object(fc.fc_verify, "verify_rounds", side_effect=lambda *x: calls.append(("rounds", x[2]))):
            fc.fc_verify.cmd_verify(a)
        return said, [c for c in calls if c[0] == "record"], [c[1] for c in calls if c[0] == "rounds"], repo, calls[0][0]

    def test_fast_prints_its_depth_and_task_and_runs_the_tasks_tests_without_review(self):
        said, _, rounds, _, _ = self.verify(("061", "FAST", "code"), ("062", "FAST", "code"))
        self.assertIn("verify depth FAST (deepest task 061)", said)
        self.assertEqual((rounds[0]["checks"], rounds[0]["review"], rounds[0]["depth"]), (["c1", "t061", "t062"], False, "FAST"))

    def test_record_depth_runs_once_before_the_first_round_with_the_root_and_both_cfgs(self):
        _, rec, rounds, repo, first = self.verify(("061", "FAST", "code"))
        self.assertEqual((first, len(rec), len(rounds)), ("record", 1, 1))
        proj, root, base_cfg, cfg, depth, tid = rec[0][1]
        self.assertEqual((proj.name, root, base_cfg, depth, tid), ("p", repo, self.cfg, "FAST", "061"))
        self.assertEqual((cfg["depth"], cfg["review"], cfg["checks"]), ("FAST", False, ["c1", "t061"]))

    def test_standard_with_no_code_task_or_no_route_keeps_the_verify_cfg(self):
        for tasks in ([("061", "DEEP", "design")], [("061", None, "code")], []):
            said, _, rounds, _, _ = self.verify(*tasks)
            self.assertIn("verify depth STANDARD", said)
            self.assertEqual(({k: rounds[0][k] for k in self.cfg}, rounds[0]["depth"]), (self.cfg, "STANDARD"))

    def stages(self, cfg):
        """run_stages with the review and Jev calls patched; the Jev acceptance mock."""
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        with mock.patch.object(fc.fc_accept, "jev_acceptance", return_value={"pass": True, "criteria": []}) as jev, mock.patch.object(fc.fc_verify, "review_inputs", return_value=("d", "p", "", [])), \
                mock.patch.object(fc.fc_verify.fc_review, "llm_review", return_value={"pass": True, "confirmed": [], "issues": []}) as rv, mock.patch.object(fc.fc_core, "say"):
            ok, report, _ = fc.fc_verify.run_stages(proj, proj, cfg, type("A", (), {"skip_e2e": True, "skip_review": False}))
        return jev, rv, report, ok

    def test_a_gated_criterion_proven_by_its_test_is_a_note_and_does_not_fail_verify(self):
        row = {"task": "061", "criterion": "c", "choice": "UNCLEAR", "p_met": 0.2, "p_not_met": 0.3, "proven_by": "f1"}
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        out = io.StringIO()
        with mock.patch.object(fc.fc_accept, "jev_acceptance", return_value={"pass": True, "criteria": [row], "unclear": [], "failed": []}), mock.patch.object(fc.fc_verify, "review_inputs", return_value=("d", "p", "", [])), \
                mock.patch.object(fc.fc_verify.fc_review, "llm_review", return_value={"pass": True, "confirmed": [], "issues": []}), mock.patch.object(fc.fc_core, "say") as say, contextlib.redirect_stdout(out):
            ok, _, findings = fc.fc_verify.run_stages(proj, proj, {"depth": "DEEP", "acceptance_gate": True, "jev_acceptance": True}, type("A", (), {"skip_e2e": True, "skip_review": False}))
        self.assertEqual((ok, findings), (True, ""))
        self.assertIn("0 unproven, 1 proven by their task's test", say.call_args.args[0])
        self.assertIn("unclear  p_met=0.20 p_not=0.30 [061] c  (note: the task's test passed, ledger event f1)", out.getvalue())

    def test_fast_leaves_the_jev_acceptance_out_and_other_depths_run_it(self):
        jev, rv, report, ok = self.stages({"depth": "FAST", "review": False, "jev_acceptance": True})
        self.assertEqual((jev.call_count, rv.call_count, report["acceptance"], ok), (0, 0, None, True))
        for depth in ("STANDARD", "DEEP"):
            jev, rv, report, _ = self.stages({"depth": depth, "jev_acceptance": True})
            self.assertEqual((jev.call_count, rv.call_count, report["acceptance"] is not None), (1, 1, True))
