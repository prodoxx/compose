import hermetic  # first: no test sees the real home
import io, os, sys, threading, time, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import fc_prompt

class Out(io.StringIO): isatty = lambda self: self.tty

class Frame(unittest.TestCase):
    def shown(self, fn, *args, tty=True, no_color=""):
        out = Out(); out.tty = tty
        with mock.patch("sys.stdout", out), mock.patch.dict(os.environ, {"NO_COLOR": no_color}):
            fn(*args)
        return out.getvalue()

    def test_paint_colours_only_a_terminal_without_no_color(self):
        out = Out(); out.tty = True
        with mock.patch("sys.stdout", out), mock.patch.dict(os.environ, {"NO_COLOR": ""}):
            self.assertEqual(fc_prompt.paint("hi", "32"), "\x1b[32mhi\x1b[0m")
            del os.environ["NO_COLOR"]
            self.assertEqual(fc_prompt.paint("hi", "1"), "\x1b[1mhi\x1b[0m")
            os.environ["NO_COLOR"] = "1"
            self.assertEqual(fc_prompt.paint("hi", "32"), "hi")
            out.tty = False; os.environ["NO_COLOR"] = ""
            self.assertEqual(fc_prompt.paint("hi", "32"), "hi")
        self.assertEqual(out.getvalue(), "")

    def test_step_splits_entries_into_bar_lines_and_drops_empty_parts(self):
        lines = ["first\n  indented\n\nlast", "", "\nsecond entry\n"]
        self.assertEqual(self.shown(fc_prompt.step, "pi", "done", lines, no_color="1"),
                         "◇  pi: done\n│  first\n│    indented\n│  last\n│  second entry\n")
        self.assertEqual(self.shown(fc_prompt.step, "pi", "done", ("a",), no_color="1"), "◇  pi: done\n│  a\n")
        self.assertEqual(self.shown(fc_prompt.note, "Empty", [], no_color="1"), "│\n◇  Empty\n")

class NoteBars(unittest.TestCase):
    shown = Frame.shown

    def test_a_multi_line_note_entry_keeps_the_bar_on_every_line(self):
        first = "No TypeSafe key: without it verify and change cannot check your code. Add one later:"
        then = "  pbpaste | compose workspace key default --typesafe"
        for kw in ({"no_color": "1"}, {"tty": False}):
            with self.subTest(**kw):
                self.assertEqual(self.shown(fc_prompt.note, "Summary", [f"{first}\n{then}"], **kw),
                                 f"│\n◇  Summary\n│  {first}\n│  {then}\n")
        self.assertEqual(self.shown(fc_prompt.note, "Keys", ["one", "a\n\n  b\n", "", "two"], no_color="1"),
                         "│\n◇  Keys\n│  one\n│  a\n│    b\n│  two\n")

class Ask(unittest.TestCase):
    def ask(self, fn, *args, answers=(), getpass=None):
        out = Out(); out.tty = False
        with mock.patch("builtins.input", side_effect=list(answers)) as ask, \
             mock.patch("getpass.getpass", side_effect=getpass) as gp, \
             mock.patch("sys.stdout", out), mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            got = fn(*args)
        return got, [c.args[0] for c in (gp if getpass else ask).call_args_list], out.getvalue()

    def test_select_numbers_options_and_returns_the_index(self):
        got, prompts, shown = self.ask(fc_prompt.select, "How?", ["a", "b", "c"], answers=["3"])
        self.assertEqual((got, prompts, shown), (2, ["│  number [1]: "], "◆  How?\n│  1) a\n│  2) b\n│  3) c\n"))
        for answer in ("", EOFError):
            with self.subTest(answer=answer):
                got, prompts, _ = self.ask(fc_prompt.select, "How?", ["a", "b", "c"], 1, answers=[answer])
                self.assertEqual((got, prompts), (1, ["│  number [2]: "]))

    def test_select_reads_again_until_a_number_in_range(self):
        bad = ["0", "4", "two", "-1", "1.5"]
        got, prompts, shown = self.ask(fc_prompt.select, "How?", ["a", "b", "c"], answers=[*bad, "2"])
        self.assertEqual((got, prompts), (1, ["│  number [1]: "] * 6))
        self.assertEqual(shown, "◆  How?\n│  1) a\n│  2) b\n│  3) c\n" + "│  type a number from 1 to 3\n" * 5)

    def test_confirm_reads_yes_or_no_in_any_case_and_spacing(self):
        for answer, want in (("y", True), (" YES ", True), ("Yes", True), ("n", False), (" No ", False), ("nO", False)):
            for default in (True, False):
                with self.subTest(answer=answer, default=default):
                    self.assertEqual(self.ask(fc_prompt.confirm, "Go?", default, answers=[answer])[0], want)

    def test_confirm_defaults_on_enter_and_end_of_input(self):
        for default, prompt in ((True, "◆  Go? (Y/n) "), (False, "◆  Go? (y/N) ")):
            for answer in ("", EOFError):
                with self.subTest(default=default, answer=answer):
                    self.assertEqual(self.ask(fc_prompt.confirm, "Go?", default, answers=[answer])[:2],
                                     (default, [prompt]))

    def test_confirm_reads_again_on_anything_else(self):
        got, prompts, _ = self.ask(fc_prompt.confirm, "Go?", False, answers=["maybe", "ye", "yess", "y"])
        self.assertEqual((got, prompts), (True, ["◆  Go? (y/N) "] * 4))

    def test_password_is_hidden_stripped_and_empty_at_end_of_input(self):
        got, prompts, shown = self.ask(fc_prompt.password, "OpenRouter key", getpass=[" sk-or-test-x \n"])
        self.assertEqual((got, prompts), ("sk-or-test-x", ["◆  OpenRouter key: "]))
        self.assertNotIn("sk-or-test-x", shown)
        self.assertEqual(self.ask(fc_prompt.password, "Linear key", getpass=EOFError)[:2], ("", ["◆  Linear key: "]))

class Spin(unittest.TestCase):
    CLEAR = "\r\x1b[2K"

    def spin(self, label, body, tty=True, no_color=""):
        class Tty(Out):
            flushes = 0
            def flush(self): self.flushes += 1
        self.out = out = Tty(); out.tty = tty
        before = set(threading.enumerate())
        with mock.patch("sys.stdout", out), mock.patch.dict(os.environ, {"NO_COLOR": no_color}):
            try:
                with fc_prompt.spin(label):
                    body(out, before)
            finally:
                self.assertEqual(set(threading.enumerate()), before, "the spinner thread is still running")
        return out.getvalue()

    def first_frame(self, out):
        end = time.monotonic() + 2
        while not out.getvalue() and time.monotonic() < end: time.sleep(0.01)
        return out.getvalue()

    def test_off_a_terminal_the_block_runs_and_nothing_is_written(self):
        ran = []
        self.assertEqual(self.spin("gh", lambda out, before: (time.sleep(0.25), ran.append(1)), tty=False), "")
        self.assertEqual(ran, [1])

    def test_on_a_terminal_the_first_frame_shows_while_the_block_runs_and_the_line_is_cleared_last(self):
        for no_color in ("", "1"):
            with self.subTest(no_color=no_color):
                seen = []
                body = lambda out, before: seen.extend([self.first_frame(out)[:11],
                                                        [t.daemon for t in set(threading.enumerate()) - before]])
                shown = self.spin("gh auth", body, no_color=no_color)
                self.assertEqual(seen, ["\r◒  gh auth", [True]])
                self.assertTrue(shown.endswith(self.CLEAR), repr(shown))

    def test_a_block_that_raises_still_stops_the_thread_and_the_error_propagates(self):
        boom = KeyError("boom")
        for tty, wait in ((True, True), (True, False), (False, False)):
            def body(out, before):
                if wait: self.first_frame(out)
                raise boom
            with self.subTest(tty=tty, wait=wait):
                with self.assertRaises(KeyError) as raised: self.spin("uv", body, tty=tty)
                self.assertIs(raised.exception, boom)
                shown = self.out.getvalue()
                self.assertTrue(shown.endswith(self.CLEAR) if tty else shown == "", repr(shown))


if __name__ == "__main__":
    unittest.main()
