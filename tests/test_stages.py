import hermetic  # first: no test sees the real home
import base64, contextlib, io, json, os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from tests.test_trail_setup import fc
import fc_stages, fc_update, fc_core  # noqa: E402
os.environ["HERDR_ENV"] = ""


class Lines(unittest.TestCase):
    def test_one_line_per_change_and_nothing_for_what_was_shown(self):
        shown = {}
        states = {"checks": ("working", "", 1000.0), "review": ("idle", "pending", 1000.0)}
        first = fc_stages.lines(states, shown)
        self.assertEqual(len(first), 2)
        self.assertTrue(first[0].endswith("checks                   working") and first[1].endswith("review                   idle     pending"), first)
        self.assertEqual(fc_stages.lines(states, shown), [])
        states["checks"] = ("blocked", "FAIL", 1001.0)
        self.assertEqual(len(fc_stages.lines(states, shown)), 1)
        self.assertEqual(shown["checks"], ("blocked", "FAIL"))

    def test_the_status_report_is_the_osc_7501_sequence(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            fc_stages.report("blocked", "checks failed")
            fc_stages.report("done")
        msg = base64.b64encode(b"checks failed").decode()
        self.assertEqual(out.getvalue(), f"\x1b]7501;state=blocked:app=compose:kind=question:msg={msg}\x1b\\" + "\x1b]7501;state=done:app=compose\x1b\\")


class Follow(unittest.TestCase):
    def follow(self, holds):
        """cmd_stages over a verify whose hold file reads as each of `holds` in turn, then is gone. (stdout, exit code)."""
        it = iter(holds)
        proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        with mock.patch.object(fc_core, "project_dir", return_value=proj), mock.patch.object(fc_update, "live_on", lambda p, r, k: ([h] if (h := next(it, None)) else [])), \
                mock.patch.object(fc_stages.time, "sleep"), contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                fc_stages.cmd_stages(SimpleNamespace(repo=".", project="shop"))
            except SystemExit as e:
                return out.getvalue(), e.code
        return out.getvalue(), 0

    def test_a_passing_verify_prints_each_stage_once_and_ends_done(self):
        out, code = self.follow([{"stage_states": {"checks": ["working", "", 1.0]}},
                                 {"stage_states": {"checks": ["idle", "ok", 2.0], "review": ["working", "", 2.0]}},
                                 {"stage_states": {"checks": ["idle", "ok", 2.0], "review": ["idle", "0 confirmed", 3.0]}}])
        text = out.replace("\x1b]7501;", "\n<").replace("\x1b\\", ">")
        self.assertEqual(code, 0)
        self.assertEqual([l for l in text.splitlines() if l.startswith("stages")], ["stages  following the verify of shop", "stages  verify ended: every stage passed"])
        self.assertEqual(text.count("checks"), 2 + 0)  # working, then ok
        self.assertIn("<state=working:app=compose>", text)
        self.assertTrue(text.rstrip().endswith("<state=done:app=compose>"), text)

    def test_a_failed_stage_blocks_the_pane_names_it_and_exits_1(self):
        out, code = self.follow([{"stage_states": {"checks": ["blocked", "FAIL", 1.0]}}])
        self.assertEqual(code, 1)
        self.assertIn("stages  verify ended: checks failed", out)
        self.assertIn("state=blocked:app=compose:kind=question:msg=" + base64.b64encode(b"checks failed").decode(), out)
        self.assertIn("state=error:app=compose", out)

    def test_no_verify_within_the_wait_ends_quietly(self):
        clock = iter([0, 0, 1000, 1000, 1000])
        with mock.patch.object(fc_stages, "WAIT_FOR_START", 10), mock.patch.object(fc_stages.time, "time", lambda: next(clock, 1000)):
            out, code = self.follow([])
        self.assertEqual(code, 0)
        self.assertIn("no verify of shop started within 10 s", out)


class HoldFile(unittest.TestCase):
    def test_hold_stage_keeps_every_stages_last_state_and_message(self):
        d = Path(tempfile.mkdtemp())
        f = d / f"{os.getpid()}.json"
        f.write_text(json.dumps({"pid": os.getpid(), "kind": "verify"}))
        with mock.patch.object(fc_update, "live_dir", return_value=d):
            fc_update._stages.clear(); fc_update._stage_states.clear()
            fc_update.hold_stage("checks", "working")
            fc_update.hold_stage("checks", "blocked", "FAIL")
            fc_update.hold_stage("review", "working", "")
        held = json.loads(f.read_text())
        self.assertEqual(sorted(held["stages"]), ["review"])
        self.assertEqual({k: v[:2] for k, v in held["stage_states"].items()}, {"checks": ["blocked", "FAIL"], "review": ["working", ""]})


if __name__ == "__main__":
    unittest.main()
