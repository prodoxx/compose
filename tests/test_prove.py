import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, sys, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


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

    def test_a_reason_opts_a_case_out_and_an_empty_one_does_not(self):
        ok = case("a", "http://x/p?variant=b", url_override="the variant cannot be assigned in the test env")
        self.assertEqual(self.lint(ok), [])
        for empty in ("", "  ", None):
            msgs = self.lint(case("a", "http://x/p?variant=b", url_override=empty))
            self.assertTrue(any("needs a reason" in m for m in msgs), (empty, msgs))



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
    def test_an_environment_rule_wins_without_asking_a_model(self):
        r = {"exit": 1, "run": {"status": "blocked", "stop_reason": "skipped the password field", "url": "http://a/", "steps": 1, "history": []}}
        with mock.patch.object(fc.fc_models, "fast_object", side_effect=AssertionError("asked a model")):
            row = fc.fc_prove.prove_case({}, {"name": "c", "url": "http://a/"}, r)
        self.assertEqual((row["verdict"], row["kind"]), ("environment", "login"))

    def test_a_case_whose_assert_says_the_feature_is_absent_is_missing_without_asking_a_model(self):
        # The same assert output was environment (path) in one prove and feature missing in the next, because jev wandered differently.
        for history in ([], [{"step": 1, "kind": "scroll", "label": "Scroll down", "page_changed": True}] * 4):
            r = {"exit": 1, "assert_exit": 1, "assert_failed": True, "stdout": "- no ask call reached the model\n- the planner should still run on a crisis message\n",  # the header is cut off
                 "run": {"status": "blocked", "stop_reason": "no change", "url": "http://a/features/x", "steps": len(history), "history": history}}
            with mock.patch.object(fc.fc_models, "fast_object", return_value={"verdict": "environment", "kind": "path", "why": "seed"}) as m:
                row = fc.fc_prove.prove_case({}, {"name": "c", "url": "http://a/"}, r)
            self.assertEqual((row["verdict"], row["kind"]), ("feature_missing", "feature"))
            m.assert_not_called()

    def test_a_crashed_assert_or_a_login_page_is_the_environment_whatever_the_assert_printed(self):
        r = {"exit": 1, "assert_exit": 1, "assert_failed": True, "stdout": "ASSERT FAILED (c):\nTraceback (most recent call last):",
             "run": {"status": "done", "stop_reason": "", "url": "http://a/", "steps": 1, "history": []}}
        self.assertEqual(fc.fc_prove.prove_case({}, {"name": "c", "url": "http://a/"}, r)["kind"], "assert")
        r = {**r, "stdout": "ASSERT FAILED (c):\n- no button", "run": {**r["run"], "url": "http://a/login"}}
        self.assertEqual(fc.fc_prove.prove_case({}, {"name": "c", "url": "http://a/"}, r)["kind"], "login")

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


if __name__ == "__main__":
    unittest.main()
