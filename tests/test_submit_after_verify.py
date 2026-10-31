import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_notify

ORCH = "hw-orchestrator"


class Setting(unittest.TestCase):
    """`submit_after_verify` in the project's verify.json is the product owner's standing go for a plain submit: off unless it is exactly true."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)

    def cfg(self, text):
        (self.proj / "verify.json").write_text(text)

    def test_it_is_off_unless_verify_json_says_true(self):
        self.assertFalse(fc.fc_tasks.submit_after_verify(self.proj))  # no verify.json
        for text in ("{}", '{"submit_after_verify": false}', '{"submit_after_verify": "true"}', '{"submit_after_verify": 1}', "not json", "[]"):
            self.cfg(text)
            self.assertFalse(fc.fc_tasks.submit_after_verify(self.proj), text)
        self.cfg('{"submit_after_verify": true}')
        self.assertTrue(fc.fc_tasks.submit_after_verify(self.proj))

    def test_the_push_stance_is_the_setting_then_the_posture_then_ask(self):
        self.assertEqual(fc.fc_tasks.push_stance(self.proj), "ask")
        (self.proj / "results" / "yolo.json").write_text(json.dumps({"mode": "away", "push": False}))
        self.assertEqual(fc.fc_tasks.push_stance(self.proj), "ask")
        (self.proj / "results" / "yolo.json").write_text(json.dumps({"mode": "away", "push": True}))
        self.assertEqual(fc.fc_tasks.push_stance(self.proj), "posture")
        self.cfg('{"submit_after_verify": true}')
        self.assertEqual(fc.fc_tasks.push_stance(self.proj), "auto")


class AfterVerify(unittest.TestCase):
    """`compose verify` submits the stack with a plain push when the setting is on and the verify passed, and otherwise never touches GitHub."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "results" / "crew").mkdir()
        self.stacks, self.out = [], io.StringIO()
        fc.fc_layers.STACK_STOP = ""
        self.addCleanup(setattr, fc.fc_layers, "STACK_STOP", "")

    def verify(self, setting=True, exit_code=0, passed=True, writes=True, stop="", lead=True):
        """Run fc_ship.cmd_verify with a verify that exits `exit_code` after writing results/verify.json (when `writes`) and a stack submit that stops with `stop`."""
        if setting:
            (self.proj / "verify.json").write_text('{"submit_after_verify": true}')
        def fake_verify(a):
            if writes:
                (self.proj / "results" / "verify.json").write_text(json.dumps({"passed": passed}))
            sys.exit(exit_code)
        def fake_stack(a):
            self.stacks.append(a)
            if stop:
                fc.fc_layers.STACK_STOP = stop
                sys.exit(1)
        record = {"name": ORCH, "tab": "t", "pane": "p"} if lead else None
        a = SimpleNamespace(repo=str(self.proj), project="hw", fix=0)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_verify, "cmd_verify", fake_verify), \
                mock.patch.object(fc.fc_ship, "cmd_stack", fake_stack), mock.patch.object(fc.fc_crew, "crew_record", return_value=record), \
                mock.patch.object(fc.fc_crew, "crew_attention"), contextlib.redirect_stdout(self.out), self.assertRaises(SystemExit) as end:
            fc.fc_ship.cmd_verify(a)
        return end.exception.code

    def reports(self):
        f = self.proj / "results" / "crew" / f"{ORCH}.status"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def test_a_passed_verify_with_the_setting_on_submits_once_with_a_plain_push(self):
        self.assertEqual(self.verify(), 0)
        self.assertEqual(len(self.stacks), 1)
        s = self.stacks[0]
        self.assertTrue(s.submit)
        self.assertFalse(s.update)  # never --force-with-lease
        self.assertEqual(self.reports(), [])
        self.assertIn("submit_after_verify", self.out.getvalue())

    def test_nothing_is_submitted_with_the_setting_off(self):
        self.assertEqual(self.verify(setting=False), 0)
        self.assertEqual(self.stacks, [])

    def test_nothing_is_submitted_when_verify_failed(self):
        self.assertEqual(self.verify(exit_code=1, passed=False), 1)
        self.assertEqual(self.stacks, [])

    def test_a_run_that_wrote_no_verify_result_submits_nothing(self):
        (self.proj / "results" / "verify.json").write_text(json.dumps({"passed": True}))  # an older verify's: this one was a review-only or smoke run
        os.utime(self.proj / "results" / "verify.json", (time.time() - 3600,) * 2)
        self.assertEqual(self.verify(writes=False), 0)
        self.assertEqual(self.stacks, [])

    def test_a_layer_that_needs_a_force_push_stops_and_asks_the_product_owner(self):
        why = "layer 1 (b) changed since PR #4. Nothing was pushed. `compose stack --submit --update` pushes it with --force-with-lease"
        self.assertEqual(self.verify(stop=why), 0)  # the verify itself passed
        self.assertEqual(len(self.stacks), 1)  # tried once, without --update, and did not retry
        self.assertFalse(self.stacks[0].update)
        [r] = self.reports()
        self.assertEqual(r["state"], "needs-user")
        self.assertIn("force push", r["message"])
        self.assertIn("--update", r["message"])
        self.assertIn("needs-user", self.out.getvalue())

    def test_a_base_ahead_of_its_remote_is_the_product_owners_to_push(self):
        self.verify(stop="main is 2 commits ahead of origin/main, so the PRs would carry them; nothing was built or pushed. Push main, or wait until origin/main has them, then re-run")
        [r] = self.reports()
        self.assertEqual(r["state"], "needs-user")

    def test_any_other_stop_blocks_the_team_lead_with_the_reason(self):
        self.verify(stop="layer 2 (b) fails its check: pytest")
        [r] = self.reports()
        self.assertEqual(r["state"], "blocked")
        self.assertIn("layer 2 (b) fails its check: pytest", r["message"])

    def test_a_stop_with_no_team_lead_is_only_printed(self):
        self.verify(stop="layer 1 changed. `compose stack --submit --update` pushes it with --force-with-lease", lead=False)
        self.assertEqual(self.reports(), [])
        self.assertIn("needs-user", self.out.getvalue())


class Told(unittest.TestCase):
    """The product owner is asked for the push once, in the line a passed verify sends, and the line says what happens next."""
    def snaps(self):
        base = {"tasks": {}, "agents": {}, "crew": {}, "run": False, "ended": None, "verify": None, "halt": None, "run_end": 0, "last": [0, ""]}
        return base, {**base, "verify": (2, True)}

    def tell(self, push):
        before, after = self.snaps()
        return fc.fc_watch.owner_lines("hw", before, after, push)

    def test_without_a_standing_go_the_one_line_asks_for_the_push(self):
        [line] = self.tell("ask")
        self.assertIn("verify passed - the feature is In Review", line)
        self.assertIn("compose stack --submit", line)
        self.assertIn("waits for your go", line)

    def test_with_the_setting_on_the_line_says_it_is_being_submitted(self):
        [line] = self.tell("auto")
        self.assertIn("submitting the stack", line)
        self.assertNotIn("waits for your go", line)

    def test_with_the_posture_push_the_line_says_the_team_lead_submits(self):
        [line] = self.tell("posture")
        self.assertNotIn("waits for your go", line)
        self.assertIn("push is on", line)

    def test_a_failed_verify_asks_nothing(self):
        before, after = self.snaps()
        after["verify"] = (2, False)
        self.assertEqual(fc.fc_watch.owner_lines("hw", before, after, "ask"), ["event tell='verify failed'"])


class Gate(unittest.TestCase):
    """The phone notification for a verify that passed while the push is off stays quiet when the project submits by itself."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "results" / "yolo.json").write_text(json.dumps({"mode": "away", "push": False}))

    def sent(self, setting):
        if setting:
            (self.proj / "verify.json").write_text('{"submit_after_verify": true}')
        with mock.patch.object(fc_notify, "send") as send:
            fc_notify.gates(self.proj, ["event verify=PASS"])
        return [c.args[1] for c in send.call_args_list]

    def test_the_gate_fires_only_when_nothing_will_submit_the_stack(self):
        self.assertEqual(self.sent(False), ["verify passed; the stack waits for your push approval"])
        self.assertEqual(self.sent(True), [])


class Shown(unittest.TestCase):
    """`compose yolo` and `compose status` say whether the verified stack is submitted without asking."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)

    def test_yolo_without_push_says_the_setting_still_submits(self):
        (self.proj / "verify.json").write_text('{"submit_after_verify": true}')
        a = SimpleNamespace(repo=".", project="hw", push=False, clear_words=False, words=[], cmd="yolo")
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), contextlib.redirect_stdout(out):
            fc.fc_yolo.cmd_yolo(a)
        self.assertIn("submit_after_verify", out.getvalue())
        self.assertNotIn("the stack waits for you", out.getvalue())

    def test_the_digest_names_the_setting_next_to_push(self):
        self.assertEqual(fc.fc_yolo.push_line({"push": False}, "ask"), "push: no")
        self.assertEqual(fc.fc_yolo.push_line({"push": True}, "posture"), "push: yes")
        self.assertIn("submit_after_verify", fc.fc_yolo.push_line({"push": False}, "auto"))

    def test_status_shows_the_setting_either_way(self):
        self.assertIn("off", fc.fc_status.submit_line(self.proj))
        (self.proj / "verify.json").write_text('{"submit_after_verify": true}')
        self.assertIn("on", fc.fc_status.submit_line(self.proj))
        self.assertIn("submit_after_verify", fc.fc_status.submit_line(self.proj))


if __name__ == "__main__":
    unittest.main()
