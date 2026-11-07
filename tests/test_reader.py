import hermetic  # first: no test sees the real home
import os, subprocess, unittest
from pathlib import Path
from unittest import mock
from tests.test_trail_setup import fc
os.environ["HERDR_ENV"] = ""


class Reflection(unittest.TestCase):
    """The test reader reflects on a RED attempt: it sees the attempt's diff and every earlier reflection, and answers in three
    labelled parts (DID, WHY, NEXT) the next turn reads as a hypothesis."""
    META = {"test": "pytest t.py", "files": ["a.py"]}

    def read(self, reply, diff="", earlier=()):
        calls = []
        def once(cwd, prompt, timeout=900, model=None, tools=None):
            calls.append((model, tools, prompt))
            return None, reply, 2, {"cost": 0.0, "reference_cost": 0.002}
        with mock.patch.object(fc.fc_claude, "claude_once", once):
            brief, usage = fc.fc_reader.read_failure(Path("/w"), self.META, "FAIL test_x", Path("/w/001.test.log"), diff=diff, earlier=earlier)
        return brief, usage, calls[0]

    def test_the_reader_sees_the_diff_and_every_earlier_reflection_and_answers_in_three_parts(self):
        brief, usage, (model, tools, prompt) = self.read("\nDID: added f\nWHY: t.py::test_x expected 2, got 1; a.py:3 returns 1\nNEXT: return n * 2\n",
                                                         diff="+def f(): return 1", earlier=["DID: x\nWHY: y\nNEXT: z", "DID: p\nWHY: q\nNEXT: r"])
        self.assertEqual((brief, usage["reference_cost"], model, tools),
                         ("DID: added f\nWHY: t.py::test_x expected 2, got 1; a.py:3 returns 1\nNEXT: return n * 2", 0.002, "claude-haiku-5-5", "Read,Grep,Glob"))
        for part in ("FAIL test_x", "pytest t.py", "/w/001.test.log", "a.py", "file:line", "+def f(): return 1", "DID:", "WHY:", "NEXT:", "hypothesis"):
            self.assertIn(part, prompt)
        self.assertIn("1. DID: x\n   WHY: y\n   NEXT: z\n2. DID: p\n   WHY: q\n   NEXT: r", prompt)
        self.assertIn("Never repeat a NEXT", prompt)

    def test_the_diff_is_cut_so_a_big_change_never_crowds_the_test_output(self):
        _, _, (_, _, prompt) = self.read("ok", diff="x" * 9000)
        self.assertIn("x" * fc.fc_reader.DIFF_CAP, prompt)
        self.assertNotIn("x" * (fc.fc_reader.DIFF_CAP + 1), prompt)
        self.assertIn("cut at", prompt)

    def test_without_a_diff_or_an_earlier_reflection_the_prompt_names_neither(self):
        _, _, (_, _, prompt) = self.read("ok")
        self.assertNotIn("Earlier attempts", prompt)
        self.assertNotIn("The attempt's change", prompt)
        self.assertNotIn("Never repeat", prompt)

    def test_a_timeout_or_no_reader_gives_nothing_as_before(self):
        with mock.patch.object(fc.fc_claude, "claude_once", side_effect=subprocess.TimeoutExpired("claude", 120)):
            self.assertEqual(fc.fc_reader.read_failure(Path("/w"), self.META, "FAIL", Path("/w/l")), ("", None))
        with mock.patch.object(fc.fc_config, "TEST_READER", None), mock.patch.object(fc.fc_claude, "claude_once", side_effect=AssertionError("no reader set")):
            self.assertEqual(fc.fc_reader.read_failure(Path("/w"), self.META, "FAIL", Path("/w/l")), ("", None))


class RetryNote(unittest.TestCase):
    LOG = [{"attempt": 1, "summary": "DID: a\nWHY: b\nNEXT: c"}, {"attempt": 2, "rate_limited": True}, {"attempt": 2, "summary": ""},
           {"attempt": 3, "summary": "DID: d\nWHY: e\nNEXT: f"}]

    def test_the_reflections_are_the_logs_non_empty_summaries_in_order(self):
        self.assertEqual(fc.fc_reader.reflections(self.LOG), ["DID: a\nWHY: b\nNEXT: c", "DID: d\nWHY: e\nNEXT: f"])
        self.assertEqual(fc.fc_reader.reflections([]), [])

    def test_the_retry_note_numbers_every_reflection_but_the_newest_which_stands_in_what_failed(self):
        self.assertEqual(fc.fc_reader.retry_note(self.LOG[:1]), "")
        self.assertEqual(fc.fc_reader.retry_note([]), "")
        note = fc.fc_reader.retry_note(self.LOG)
        self.assertTrue(note.startswith("Earlier attempts and why each missed"))
        self.assertIn("1. DID: a\n   WHY: b\n   NEXT: c", note)
        self.assertNotIn("DID: d", note)
        self.assertIn("the test decides", note)
