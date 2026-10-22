import hermetic  # first: no test sees the real home
import contextlib, io, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_e2e import fc, FAKE_AGENT  # the loaded compose and jev's stand-in Agent
os.environ["HERDR_ENV"] = ""


def case(name, url, **kw):
    return {"name": name, "url": url, "goal": "Open the page", **kw}


class FlagPathLint(unittest.TestCase):
    """A page whose every e2e case forces its variant in the URL never tests the assignment a real user gets."""
    lint = staticmethod(lambda *cases: fc.fc_prove.flag_path_lint({"e2e": list(cases)}))

    def test_a_page_reached_only_through_url_overrides_is_flagged(self):
        msgs = self.lint(case("a", "http://x/pricing?variant=b"), case("b", "http://x/pricing?experiment=new-pricing&u=1"))
        self.assertEqual(len(msgs), 1)
        self.assertIn("page /pricing", msgs[0]); self.assertIn("'a'", msgs[0]); self.assertIn("'b'", msgs[0]); self.assertIn("url_override", msgs[0])

    def test_one_case_through_the_real_path_covers_the_page(self):
        self.assertEqual(self.lint(case("forced", "http://x/pricing?variant=b"), case("real", "http://x/pricing")), [])
        self.assertEqual(self.lint(case("forced", "http://x/pricing?variant=b"), case("real", "http://x/pricing/?utm=1")), [])

    def test_the_fragment_counts_and_other_keys_do_not(self):
        self.assertEqual(len(self.lint(case("a", "http://x/p#flag=on"))), 1)
        self.assertEqual(self.lint(case("a", "http://x/p?plan=pro&page=2&tab=billing"), case("b", "http://x/q?id=7")), [])
        self.assertEqual(self.lint(case("a", "http://x/p?abc=1&shuffle=2")), [])  # "ab" and "ff" only as whole words

    def test_pages_are_judged_apart(self):
        msgs = self.lint(case("a", "http://x/one?variant=b"), case("b", "http://x/two"))
        self.assertEqual(len(msgs), 1); self.assertIn("page /one", msgs[0])

    def test_a_reason_opts_a_case_out_and_an_empty_one_does_not(self):
        ok = case("a", "http://x/p?variant=b", url_override="the variant cannot be assigned in the test env")
        self.assertEqual(self.lint(ok), [])
        for empty in ("", "  ", None):
            msgs = self.lint(case("a", "http://x/p?variant=b", url_override=empty))
            self.assertTrue(any("needs a reason" in m for m in msgs), (empty, msgs))

    def test_driver_cases_and_projects_without_cases_are_left_alone(self):
        self.assertEqual(self.lint({"name": "d", "driver": "true"}), [])
        self.assertEqual(fc.fc_prove.flag_path_lint({}), [])


class EnvFailure(unittest.TestCase):
    """fc_jev.env_failure: the output alone shows the environment broke the case, never the missing feature."""
    def kind(self, r, url="http://app/dash"):
        found = fc.fc_jev.env_failure({"url": url}, {"exit": 1, "stdout": "", "stderr": "", **r})
        return found and found[0]

    def jrun(self, **kw):  # a jev run summary
        return {"run": {"status": "blocked", "stop_reason": "The last 3 actions did not change the page", "url": "http://app/dash", "steps": 4, "history": [], **kw}}

    def test_a_blocked_run_on_the_right_page_is_not_decided_here(self):
        self.assertIsNone(self.kind(self.jrun()))

    def test_page_load_failures(self):
        self.assertEqual(self.kind(self.jrun(stop_reason="net::ERR_CONNECTION_REFUSED at http://app/dash")), "page did not load")
        self.assertEqual(self.kind(self.jrun(url="chrome-error://chromewebdata/")), "page did not load")
        self.assertEqual(self.kind(self.jrun(url="about:blank", steps=0)), "page did not load")

    def test_login_failures(self):
        self.assertEqual(self.kind(self.jrun(stop_reason="skipped the password field")), "login")
        self.assertEqual(self.kind(self.jrun(url="http://app/login?next=/dash")), "login")
        self.assertIsNone(self.kind(self.jrun(url="http://app/login"), url="http://app/login"))  # the case starts there

    def test_a_crashed_assert_or_harness(self):
        self.assertEqual(self.kind({"assert_exit": 1, "stderr": "Traceback (most recent call last):\n  File"}), "assert")
        self.assertEqual(self.kind({"assert_exit": 1, "stdout": "no button"}), None)  # an assert that merely says no
        self.assertEqual(self.kind(self.jrun(status="error", stop_reason="ValueError: boom")), "harness")
        self.assertEqual(self.kind({"stderr": "Chromium not found"}), "harness")


class ProveCase(unittest.TestCase):
    def test_a_passing_case_proves_nothing(self):
        row = fc.fc_prove.prove_case({}, {"name": "c"}, {"exit": 0})
        self.assertEqual((row["verdict"], row["kind"]), ("passes", "passes"))

    def test_an_environment_rule_wins_without_asking_a_model(self):
        r = {"exit": 1, "run": {"status": "blocked", "stop_reason": "skipped the password field", "url": "http://a/", "steps": 1, "history": []}}
        with mock.patch.object(fc.fc_models, "fast_object", side_effect=AssertionError("asked a model")):
            row = fc.fc_prove.prove_case({}, {"name": "c", "url": "http://a/"}, r)
        self.assertEqual((row["verdict"], row["kind"]), ("environment", "login"))

    def test_the_judge_decides_the_rest(self):
        r = {"exit": 1, "run": {"status": "blocked", "stop_reason": "no change", "url": "http://a/", "steps": 2,
                                "history": [{"step": 1, "kind": "click", "label": "Settings", "page_changed": True}]}}
        for reply, want in (({"verdict": "feature_missing", "kind": "feature", "why": "no Export button"}, ("feature_missing", "feature")),
                            ({"verdict": "environment", "kind": "overlay", "why": "cookie banner"}, ("environment", "overlay")),
                            ({"verdict": "environment", "why": ""}, ("environment", "unproven"))):
            with mock.patch.object(fc.fc_models, "fast_object", return_value=reply) as m:
                row = fc.fc_prove.prove_case({}, {"name": "c", "url": "http://a/", "goal": "Export"}, r)
            self.assertEqual((row["verdict"], row["kind"]), want, reply)
            self.assertIn("Settings", m.call_args[0][2])  # the judge reads the steps

    def test_a_judge_that_cannot_be_asked_leaves_the_case_unproven(self):
        with mock.patch.object(fc.fc_models, "fast_object", side_effect=RuntimeError("down")):
            row = fc.fc_prove.prove_case({}, {"name": "c"}, {"exit": 1})
        self.assertEqual((row["verdict"], row["kind"]), ("environment", "unjudged"))


class Repo(unittest.TestCase):
    """A temp git repo with a project and verify.json, and jev's stand-in Agent under the real `python -m jev_ultrafast`."""
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        for args in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        jev = Path(tempfile.mkdtemp())
        (jev / "jev_ultrafast").mkdir()
        (jev / "jev_ultrafast" / "__init__.py").write_text(FAKE_AGENT)
        shutil.copy(fc.fc_config.JEV_DIR / "jev_ultrafast" / "__main__.py", jev / "jev_ultrafast" / "__main__.py")
        for p in (mock.patch.object(fc.fc_config, "JEV_DIR", jev), mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ts-test"}),
                  mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj)):
            p.start(); self.addCleanup(p.stop)

    def config(self, **cfg):
        (self.proj / "verify.json").write_text(json.dumps({"review": False, "jev_acceptance": False, "e2e_env": {"BU_CDP_URL": "http://127.0.0.1:1"}, **cfg}))

    def call(self, fn, **flags):
        a = SimpleNamespace(**{"repo": str(self.repo), "project": "p", "skip_e2e": False, "skip_review": False, "fix": 2, **flags})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            fn(a)
        return cm.exception.code, out.getvalue()

    def results(self, name):
        return json.loads((self.proj / "results" / name).read_text())


def jev_case(name, spec, **kw):
    return {"name": name, "url": "http://127.0.0.1:$E2E_PORT/", "goal": "Export the report", "assert": "true", "env": {"FAKE_JEV": json.dumps(spec)}, **kw}


MISSING = {"statuses": ["ready", "blocked"], "reason": "No Export button on the page", "url": "http://app/reports"}
JUDGE_MISSING = {"verdict": "feature_missing", "kind": "feature", "why": "no Export button"}


class ProveCommand(Repo):
    def prove(self, judge=JUDGE_MISSING, **cfg):
        self.config(**cfg)
        with mock.patch.object(fc.fc_models, "fast_object", return_value=judge):
            return self.call(fc.fc_prove.cmd_prove)

    def test_every_case_failing_for_the_missing_feature_passes_and_is_recorded(self):
        code, out = self.prove(e2e=[jev_case("export", MISSING), jev_case("export again", MISSING)])
        self.assertEqual(code, 0, out)
        self.assertIn("prove  ok    export: fails, feature missing", out)
        self.assertIn("prove  PASS  2/2 case(s) fail because the feature is missing", out)
        rec = self.results("prove.json")
        self.assertTrue(rec["passed"]); self.assertEqual([c["verdict"] for c in rec["cases"]], ["feature_missing"] * 2)
        self.assertFalse((self.proj / "results" / "verify.json").exists())  # prove is not a verify

    def test_a_case_that_fails_for_the_environment_fails_prove_with_its_reason(self):
        env = {"statuses": ["blocked"], "reason": "skipped the password field", "url": "http://app/login"}
        code, out = self.prove(e2e=[jev_case("export", MISSING), jev_case("login", env)])
        self.assertEqual(code, 1, out)
        self.assertIn("prove  ENV   login: environment (login)", out)
        self.assertIn("prove  FAIL  1/2", out)
        self.assertEqual([c["verdict"] for c in self.results("prove.json")["cases"]], ["feature_missing", "environment"])

    def test_a_judge_that_sees_an_overlay_fails_prove(self):
        code, out = self.prove(judge={"verdict": "environment", "kind": "overlay", "why": "cookie banner"}, e2e=[jev_case("export", MISSING)])
        self.assertEqual(code, 1)
        self.assertIn("environment (overlay) - cookie banner", out)

    def test_a_case_that_passes_on_the_base_fails_prove(self):
        code, out = self.prove(e2e=[jev_case("export", {"statuses": ["done"]})])
        self.assertEqual(code, 1)
        self.assertIn("prove  PASS  export: passes on the base", out)
        self.assertEqual(self.results("prove.json")["cases"][0]["verdict"], "passes")

    def test_url_overrides_alone_fail_prove_with_the_lint(self):
        code, out = self.prove(e2e=[jev_case("export", MISSING, url="http://127.0.0.1:$E2E_PORT/reports?variant=b")])
        self.assertEqual(code, 1)
        self.assertIn("prove  LINT  page /reports", out)
        self.assertEqual(len(self.results("prove.json")["lint"]), 1)

    def test_it_never_retries_and_runs_headless(self):
        with mock.patch.object(fc.fc_verify, "e2e_stage", wraps=fc.fc_verify.e2e_stage) as st:
            self.prove(e2e=[jev_case("export", MISSING)], e2e_headed=True)
        self.assertIs(st.call_args.kwargs["retry"], False)
        self.assertIs(st.call_args[0][1]["e2e_headed"], False)

    def test_no_cases_is_nothing_to_prove(self):
        code, out = self.prove(e2e=[])
        self.assertEqual(code, 0); self.assertIn("nothing to prove", out)

    def test_it_refuses_once_the_stack_holds_the_feature(self):
        self.config(e2e=[jev_case("export", MISSING)])
        with mock.patch.object(fc.fc_worktree, "stack_order", return_value=["001"]), mock.patch.object(fc.fc_verify, "e2e_stage") as st:
            with self.assertRaises(SystemExit) as cm:
                fc.fc_prove.cmd_prove(SimpleNamespace(repo=str(self.repo), project="p"))
        self.assertIn("before `run`", str(cm.exception.code)); st.assert_not_called()

    def test_it_is_a_verify_flag(self):
        with mock.patch.object(sys, "argv", ["compose", "verify", "--project", "p", "--prove"]), \
                mock.patch.object(fc.fc_prove, "cmd_prove", side_effect=SystemExit(0)) as cp, mock.patch.object(fc.fc_config, "apply_config", return_value="default"), \
                mock.patch.object(fc, "detach", side_effect=lambda a: a.fn(a)), self.assertRaises(SystemExit):
            fc.main()
        cp.assert_called_once()


class RunGate(ProveCommand):
    """`run` starts only on e2e cases a passing prove ran: never proven, a failed prove, or cases changed since stop it first."""
    CASES = [jev_case("export", MISSING)]

    def start(self, **flags):
        """cmd_run's exit message, or "preflight" when the gate let it through to the doctor preflight."""
        a = SimpleNamespace(repo=str(self.repo), project="p", only=None, no_stack=False, **flags)
        with mock.patch.object(fc.fc_run, "require_committed", side_effect=SystemExit("preflight")), self.assertRaises(SystemExit) as cm:
            fc.fc_run.cmd_run(a)
        return str(cm.exception.code)

    def test_never_proven_cases_stop_run_before_the_preflight_and_name_the_prove(self):
        self.config(e2e=self.CASES)
        out = self.start()
        self.assertIn("its e2e cases were never proven", out)
        self.assertIn("`compose verify --project p --prove`", out)
        self.assertIn("`--unproven` runs anyway", out)

    def test_a_passing_prove_opens_run_until_the_cases_or_their_setup_change(self):
        self.assertEqual(self.prove(e2e=self.CASES)[0], 0)
        self.assertEqual(self.start(), "preflight")
        self.config(e2e=self.CASES, e2e_setup="npm run dev")
        self.assertIn("its e2e cases or their setup changed since the last prove", self.start())
        self.config(e2e=[*self.CASES, jev_case("export again", MISSING)])
        self.assertIn("changed since the last prove", self.start())

    def test_a_failed_prove_stops_run(self):
        self.assertEqual(self.prove(e2e=[jev_case("export", {"statuses": ["done"]})])[0], 1)
        self.assertIn("the last prove failed", self.start())

    def test_unproven_no_cases_or_a_built_stack_skip_the_gate(self):
        self.config(e2e=self.CASES)
        self.assertEqual(self.start(unproven=True), "preflight")
        with mock.patch.object(fc.fc_worktree, "stack_order", return_value=["001"]):
            self.assertEqual(self.start(), "preflight")  # a later run of the same feature: the cases already pass on its layers
        self.config(e2e=[])
        self.assertEqual(self.start(), "preflight")

    def test_it_is_a_run_flag(self):
        with mock.patch.object(sys, "argv", ["compose", "run", "--project", "p", "--unproven"]), \
                mock.patch.object(fc.fc_run, "cmd_run", side_effect=SystemExit(0)) as cr, mock.patch.object(fc.fc_config, "apply_config", return_value="default"), \
                mock.patch.object(fc, "detach", side_effect=lambda a: a.fn(a)), self.assertRaises(SystemExit):
            fc.main()
        self.assertIs(cr.call_args[0][0].unproven, True)


class ProveEvidence(Repo):
    """cmd_prove records one event per case on the feature issue; event is faked as its arguments, so only cmd_prove's calls are tested."""
    ROWS = [{"name": "a", "verdict": "feature_missing", "why": "no section"}, {"name": "b", "verdict": "passes", "why": "x"},
            {"name": "c", "verdict": "environment", "why": "y"}]
    FIELDS = ("run_id", "commit_sha", "task_ids", "criterion_id", "phase", "source", "result", "fails_on_base", "detail")

    def prove(self, rows, ok, feature="ENG-1", saved=True):
        self.config(e2e=[{"name": r["name"]} for r in rows])
        (self.proj / "linear.json").unlink(missing_ok=True)
        if feature:
            (self.proj / "linear.json").write_text(json.dumps({"feature": feature}))
        ledger = fc.fc_prove.fc_ledger
        with mock.patch.object(fc.fc_prove, "prove", return_value=(rows, [], ok)), mock.patch.object(fc.fc_worktree, "stack_order", return_value=[]), \
                mock.patch.object(ledger, "event", side_effect=lambda *a: dict(zip(self.FIELDS, a))), \
                mock.patch.object(ledger, "record", return_value=saved) as self.record:
            return self.call(fc.fc_prove.cmd_prove)

    def test_one_event_per_case_in_order_on_the_feature_issue(self):
        code, out = self.prove(self.ROWS, False)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(code, 1, out)
        self.record.assert_called_once()
        issue, events = self.record.call_args.args
        self.assertEqual(issue, "ENG-1")
        self.assertEqual(events, [dict(zip(self.FIELDS, ("", sha, [], [], "case", "prove", res, fob, detail)))
                                  for res, fob, detail in (("fail_expected", True, "a: no section"), ("pass", False, "b: x"), ("error", None, "c: y"))])

    def test_a_passing_prove_records_and_exits_as_before(self):
        code, out = self.prove(self.ROWS[:1], True)
        self.assertEqual(code, 0, out)
        self.assertEqual([e["result"] for e in self.record.call_args.args[1]], ["fail_expected"])
        self.assertTrue(self.results("prove.json")["passed"])

    def test_nothing_changes_when_the_events_are_not_saved_or_there_is_no_feature_issue(self):
        for kw in ({"saved": False}, {"feature": None}):
            code, out = self.prove(self.ROWS[:2], False, **kw)
            self.assertEqual(code, 1, out)
            self.assertEqual([c["verdict"] for c in self.results("prove.json")["cases"]], ["feature_missing", "passes"])
            self.assertEqual(self.record.call_args.args[0], None if kw.get("feature", 1) is None else "ENG-1")
            self.assertEqual(len(self.record.call_args.args[1]), 2)


class PrototypeCommand(Repo):
    SCREEN = {"name": "settings", "url": "http://127.0.0.1:$E2E_PORT/settings", "goal": "Open the settings page", "reference": "frame0 page Settings",
              "must_match": ["icons", "empty state copy"]}

    def capture(self, screens, **flags):
        self.config(prototype=screens)
        with mock.patch.object(fc.fc_verify, "verify_root", return_value=(self.repo, "main", None)):
            return self.call(fc.fc_prove.cmd_prototype, **flags)

    def test_it_captures_each_screen_and_writes_one_checklist(self):
        code, out = self.capture([{**self.SCREEN, "env": {"FAKE_JEV": json.dumps({"statuses": ["done"]})}}])
        self.assertEqual(code, 0, out)
        self.assertIn("prototype PASS  1/1 screen(s) captured", out)
        md = (self.proj / "results" / "prototype.md").read_text()
        self.assertIn("## settings", md); self.assertIn("- reference: frame0 page Settings", md)
        self.assertIn("- [ ] icons", md); self.assertIn("- [ ] empty state copy", md)
        shot = next(l for l in md.splitlines() if l.startswith("- capture: ")).split("`")[1]
        self.assertTrue(Path(shot).exists() and shot.endswith(".jpg"), md)  # a pass keeps its screenshots here
        self.assertFalse((self.proj / "results" / "verify.json").exists())

    def test_a_screen_it_could_not_reach_fails_and_says_why(self):
        code, out = self.capture([{**self.SCREEN, "env": {"FAKE_JEV": json.dumps(MISSING)}}])
        self.assertEqual(code, 1)
        md = (self.proj / "results" / "prototype.md").read_text()
        self.assertIn("- capture: not captured: No Export button on the page", md)

    def test_an_entry_without_a_must_match_list_is_refused(self):
        self.config(prototype=[{k: v for k, v in self.SCREEN.items() if k != "must_match"}])
        with self.assertRaises(SystemExit) as cm:
            fc.fc_prove.prototype_cases(json.loads((self.proj / "verify.json").read_text()))
        self.assertIn("must_match", str(cm.exception.code))

    def test_no_entries_is_an_error_naming_the_config(self):
        self.config()
        with self.assertRaises(SystemExit) as cm:
            fc.fc_prove.cmd_prototype(SimpleNamespace(repo=str(self.repo), project="p"))
        self.assertIn("prototype", str(cm.exception.code))

    def test_it_is_a_verify_flag(self):
        with mock.patch.object(sys, "argv", ["compose", "verify", "--project", "p", "--prototype"]), \
                mock.patch.object(fc.fc_prove, "cmd_prototype", side_effect=SystemExit(0)) as cp, mock.patch.object(fc.fc_config, "apply_config", return_value="default"), \
                mock.patch.object(fc, "detach", side_effect=lambda a: a.fn(a)), self.assertRaises(SystemExit):
            fc.main()
        cp.assert_called_once()


class ReviewOnly(Repo):
    """`compose verify --review-only`, right after `run`: the review and the acceptance, with no checks, no e2e and no fix."""
    def verify(self, **flags):
        self.config(checks=[f"touch {self.repo}/checked"], e2e=[{"name": "probe", "driver": f"touch {self.repo}/driven"}], **flags.pop("cfg", {}))
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": "001", "title": "a", "files": ["x.py"], "protected": ["t.py"], "test": "true", "body": "## Goal\nx"}]}))
        with mock.patch.object(fc.fc_linear, "linear_reconcile") as self.lr, mock.patch.object(fc.fc_verify, "verify_failed"):
            return self.call(fc.cmd_verify if hasattr(fc, "cmd_verify") else fc.fc_verify.cmd_verify, review_only=True, **flags)

    def test_it_skips_checks_and_e2e_and_records_apart_from_the_full_verify(self):
        code, out = self.verify(skip_review=True)
        self.assertEqual(code, 0, out)
        self.assertFalse((self.repo / "checked").exists()); self.assertFalse((self.repo / "driven").exists())
        self.assertIn("review PASS (review and acceptance only", out)
        self.assertEqual(self.results("review.json")["left_out"], ["review", "checks", "e2e"])
        for name in ("verify.json", "smoke.json"):
            self.assertFalse((self.proj / "results" / name).exists(), name)
        self.assertEqual(self.lr.call_count, 0)  # no Linear write for a partial look

    def test_it_runs_the_review_and_never_a_fix_round(self):
        bad = {"pass": False, "issues": [], "confirmed": []}
        calls = []
        with mock.patch.object(fc.fc_verify, "review_inputs", return_value=("diff", "plan", "", [])), \
                mock.patch.object(fc.fc_verify.fc_review, "llm_review", side_effect=lambda *a: calls.append(a) or bad), \
                mock.patch.object(fc.fc_verify, "fix_tests", side_effect=AssertionError("a fix round ran")):
            code, out = self.verify(fix=2, cfg={"review": True})
        self.assertEqual(code, 1, out)
        self.assertEqual(len(calls), 1); self.assertIs(calls[0][-1], False)  # nobody tells the reviewers the tests pass
        self.assertIn("review FAIL", out)
        self.assertEqual(len(self.results("review.json")["rounds"]), 1)


class KeepRecord(Repo):
    def test_a_stage_that_asks_keeps_the_screenshots_of_a_pass(self):
        for keep, kept in ((False, False), (True, True)):
            self.config(e2e=[jev_case("c", {"statuses": ["done"]})])
            report, rec = {"e2e": []}, self.proj / "results" / f"k{keep}"
            with contextlib.redirect_stdout(io.StringIO()):
                ok = fc.fc_verify.e2e_stage(self.repo, json.loads((self.proj / "verify.json").read_text()), {}, report, [], rec, keep=keep)
            self.assertTrue(ok)
            self.assertEqual("screenshots" in report["e2e"][0], kept)

    def run_case(self, spec, rec, keep=False, warning=""):
        """Run one jev case through e2e_stage; fc_shrink.shrink is a stand-in that writes a smaller copy, or passes the source through."""
        def fake(path, out_dir=None, *a, **kw):
            if warning:
                return fc.fc_shrink.Shrunk(path, fc.fc_shrink.Size(2400, 1600, 3), fc.fc_shrink.Size(2400, 1600, 3), warning)
            copy = (Path(out_dir.name) if isinstance(out_dir, tempfile.TemporaryDirectory) else Path(out_dir)) / f"shrunk-{path.name}"
            copy.write_bytes(b"small")
            return fc.fc_shrink.Shrunk(copy, fc.fc_shrink.Size(2400, 1600, 3), fc.fc_shrink.Size(1000, 667, 5), "")
        self.config(e2e=[jev_case("c", spec)])
        report, out = {"e2e": []}, io.StringIO()
        with mock.patch.object(fc.fc_shrink, "shrink", side_effect=fake) as shrink, contextlib.redirect_stdout(out):
            ok = fc.fc_verify.e2e_stage(self.repo, json.loads((self.proj / "verify.json").read_text()), {}, report, [], rec, keep=keep, retry=False)
        return ok, report["e2e"][0], shrink, out.getvalue()

    def test_a_failed_case_keeps_its_screenshots_shrunk_in_place(self):
        ok, row, shrink, out = self.run_case({"statuses": ["blocked"], "reason": "x"}, self.proj / "results" / "fail")
        rec = Path(row["screenshots"])
        self.assertFalse(ok)
        self.assertNotEqual(row["exit"], 0)
        self.assertEqual(shrink.call_count, 1)
        self.assertEqual(shrink.call_args.args[0].name, "000000.jpg")
        self.assertEqual(sorted(os.listdir(rec)), ["000000.jpg"])  # same name and count, and no temp dir left behind
        self.assertEqual((rec / "000000.jpg").read_bytes(), b"small")
        self.assertNotIn("shrunk", out)  # nothing printed per screenshot

    def test_a_keep_record_pass_is_shrunk_in_place(self):
        ok, row, shrink, _ = self.run_case({"statuses": ["done"]}, self.proj / "results" / "keep", keep=True)
        rec = Path(row["screenshots"])
        self.assertTrue(ok)
        self.assertEqual(shrink.call_count, 1)
        self.assertEqual(sorted(os.listdir(rec)), ["000000.jpg"])
        self.assertEqual((rec / "000000.jpg").read_bytes(), b"small")

    def test_a_pass_through_leaves_the_screenshot_as_jev_wrote_it(self):
        ok, row, shrink, _ = self.run_case({"statuses": ["blocked"], "reason": "x"}, self.proj / "results" / "pass",
                                           warning="compose shrink: sips could not shrink 000000.jpg, using the original")
        self.assertEqual(shrink.call_count, 1)
        self.assertEqual(sorted(os.listdir(Path(row["screenshots"]))), ["000000.jpg"])
        self.assertEqual((Path(row["screenshots"]) / "000000.jpg").read_bytes(), b"jpg")

    def test_a_passed_case_whose_record_is_deleted_is_not_shrunk(self):
        ok, row, shrink, _ = self.run_case({"statuses": ["done"]}, self.proj / "results" / "gone")
        self.assertTrue(ok)
        shrink.assert_not_called()
        self.assertNotIn("screenshots", row)


if __name__ == "__main__":
    unittest.main()
