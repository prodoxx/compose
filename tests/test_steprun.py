import hermetic  # first: no test sees the real home
import subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
import os
os.environ["HERDR_ENV"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import fc_checks, fc_config, fc_core, fc_keys, fc_links, fc_pi, fc_steps, fc_steprun


class RunStep(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.ran, self.has = self.tmp / "ran", lambda c: (self.ran.exists(), "")
        for p in (mock.patch.object(fc_steps, "check", lambda row, c: row[1](c)), mock.patch.object(fc_config, "WORKSPACE", "acme"),
                  mock.patch.object(fc_config, "apply_config", return_value="acme"), mock.patch.object(fc_config, "OPENROUTER_KEY", "sk-or-test")):
            p.start(); self.addCleanup(p.stop)

    def step(self, check, install=None, level="required", claude_row=False, mode="run", yes=False, claude=True, terminal=False, name="pi"):
        state, line = fc_steprun.run_step((name, check, install or f"touch {self.ran}", level, claude_row, mode), self.tmp, yes, claude, terminal)
        self.assertNotIn("sk-or-test", line)
        return state, line

    def test_present_tool_is_reused_and_never_run(self):
        self.assertEqual((self.step(lambda c: (True, ""), claude_row=True, claude=False)[0], self.ran.exists()), ("ok", False))

    def test_claude_missing_and_designer_under_yes(self):
        state, line = self.step(self.has, claude_row=True, claude=False)
        skipped = self.step(self.has, level="designer", yes=True)[0]
        self.assertEqual((state, line, skipped, self.ran.exists()), ("claude", "", "skipped", False))

    def test_manual_and_interactive_without_terminal_show_the_command(self):
        for mode in ("manual", "interactive"):
            state, line = self.step(self.has, mode=mode)
            self.assertEqual((state in ("manual", "skipped"), f"touch {self.ran}" in line), (True, True))
        self.assertFalse(self.ran.exists())

    def test_key_row_uses_the_skipped_line_for_the_active_workspace(self):
        with mock.patch.object(fc_keys, "skipped_line", return_value="no key; run compose setup") as skipped:
            state, line = self.step(lambda c: (False, ""), mode="key", name="OpenRouter key", yes=True)
        skipped.assert_called_once_with("openrouter", "acme")
        self.assertEqual((state in ("manual", "skipped"), "no key; run compose setup" in line, self.ran.exists()), (True, True, False))

    def test_run_and_interactive_with_terminal_install_then_recheck(self):
        self.assertEqual(self.step(self.has)[0], "done")
        self.ran.unlink()  # an interactive row with a terminal runs the same way, inheriting it
        self.assertEqual(self.step(self.has, mode="interactive", terminal=True)[0], "done")

    def test_failing_install_reports_its_last_error_line(self):
        state, line = self.step(self.has, install="echo first >&2; echo boom >&2; exit 3")
        self.assertEqual((state, "boom" in line), ("failed", True))
        self.assertEqual(self.step(self.has, install="true")[0], "failed")  # the install exits 0 but the check still fails

    def test_write_rows_call_their_writers_then_recheck(self):
        with mock.patch.dict(fc_steprun.WRITERS, {"compose CLI link": lambda c: self.ran.touch() or ["linked"]}):
            self.assertEqual(self.step(self.has, install="compose setup", mode="write", name="compose CLI link")[0], "done")
        with mock.patch.object(fc_links, "relink_owned", return_value=["linked"]) as relink, mock.patch.object(fc_pi, "declare_provider", return_value="declared") as declare, \
             mock.patch.object(fc_pi, "write_text_model", return_value="written") as text:
            for name in ("pi openrouter-fast provider", "TEXT_MODEL lines"):
                self.assertEqual(self.step(lambda c: (False, ""), install="compose setup", mode="write", name=name)[0], "failed")
        relink.assert_not_called()
        declare.assert_called_once_with(fc_pi.PI_MODELS)
        text.assert_called_once_with(fc_config.ENV_FILE)


class Dry(unittest.TestCase):
    setUp = RunStep.setUp

    def step(self, check, mode="run", name="pi", install=None, level="required", claude_row=False, yes=False, claude=True, terminal=False, dry=True):
        return fc_steprun.run_step((name, check, install or f"touch {self.ran}", level, claude_row, mode), self.tmp, yes, claude, terminal, dry=dry)

    def forbid(self):
        """No subprocess starts and no writer is called: each fails the test."""
        for target, name in ((subprocess, "run"), (subprocess, "Popen"), (fc_pi, "declare_provider"), (fc_pi, "write_text_model"),
                             (fc_links, "ensure_link"), (fc_links, "relink_owned")):
            p = mock.patch.object(target, name, side_effect=AssertionError(f"{name} called with dry")); p.start(); self.addCleanup(p.stop)

    def test_run_and_interactive_rows_say_what_would_run(self):
        self.forbid()
        self.assertEqual(self.step(self.has, install=f"{Path.home()}/bin/fix --all"), ("would", "would run: ~/bin/fix --all"))
        self.assertEqual(self.step(self.has, "interactive", "Homebrew", terminal=True), ("would", f"would run: touch {self.ran} (it takes the terminal)"))
        self.assertEqual(self.step(self.has, level="designer"), ("would", f"would run: touch {self.ran}"))  # a designer row without yes is past the guards
        self.assertFalse(self.ran.exists())

    def test_write_rows_say_what_they_would_write(self):
        home = Path.home()
        for p in (mock.patch.object(fc_pi, "PI_MODELS", home / "pi-dir/models.json"), mock.patch.object(fc_config, "ENV_FILE", home / "env-dir/.env")):
            p.start(); self.addCleanup(p.stop)
        self.forbid()
        want = {"pi openrouter-fast provider": "would declare openrouter-fast in ~/pi-dir/models.json",
                "TEXT_MODEL lines": "would append TEXT_MODEL, TEXT_MODEL_BASE_URL to ~/env-dir/.env",
                **{n: f"would link ~/{link.relative_to(home)} -> {fc_core.tilde(target)}" for n, link, target in fc_checks.owned_links(self.tmp)}}
        self.assertEqual(set(fc_steprun.PREVIEWS), set(fc_steprun.WRITERS))
        for name, line in want.items():
            self.assertEqual(self.step(lambda c: (False, ""), "write", name, "compose setup"), ("would", line))

    def test_guards_return_as_without_dry(self):
        self.forbid()
        rows = (dict(check=lambda c: (True, ""), mode="write", name="bp link"), dict(check=self.has, claude_row=True, claude=False),
                dict(check=self.has, mode="manual"), dict(check=self.has, mode="key", name="OpenRouter key"),
                dict(check=self.has, level="designer", yes=True), dict(check=self.has, mode="interactive"))
        for kw in rows:
            got = self.step(**kw)
            self.assertEqual((got[0] != "would", got), (True, self.step(**kw, dry=False)))
        self.assertFalse(self.ran.exists())

class PiPreview(unittest.TestCase):
    setUp, step, forbid = RunStep.setUp, Dry.step, Dry.forbid

class StepDetail(unittest.TestCase):
    """A step's line never repeats the step's name, nor only its state: the ◇ header above it shows both."""
    setUp, step = RunStep.setUp, RunStep.step

    def test_guard_rows_say_only_what_to_do(self):
        by_hand = f"run by hand: touch {self.ran}"
        self.assertEqual(self.step(self.has, claude_row=True, claude=False, name="superpowers"), ("claude", ""))
        self.assertEqual(self.step(self.has, mode="manual", name="git"), ("manual", by_hand))
        self.assertEqual(self.step(self.has, mode="interactive", name="Homebrew"), ("skipped", by_hand))
        self.assertEqual(self.step(self.has, level="designer", yes=True, name="figma"), ("skipped", "designer extra, skipped"))
        self.assertFalse(self.ran.exists())

    def test_failed_write_row_is_its_writers_line_else_the_checks_detail(self):
        for said, detail, want in (("linked bp", "stale", "linked bp"), ("", "points elsewhere", "points elsewhere"), ("", "", "")):
            with mock.patch.dict(fc_steprun.WRITERS, {"bp link": lambda c, said=said: said}):
                self.assertEqual(self.step(lambda c, d=detail: (False, d), "compose setup", mode="write", name="bp link"), ("failed", want))


class StepLines(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.tmp = Path(t.name).resolve()
        self.ran, self.has = self.tmp / "ran", lambda c: (self.ran.exists(), "")
        p = mock.patch.object(fc_steps, "check", lambda row, c: row[1](c)); p.start(); self.addCleanup(p.stop)
        for f in ("bin/compose", "bp/bin/bp.mjs"):
            (self.tmp / f).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / f).write_text("")
        home = Path.home()
        self.links = {"compose CLI link": (home / ".local/bin/compose", self.tmp / "bin/compose"),
                      "compose skill link": (home / ".claude/skills/compose", self.tmp), "bp link": (home / ".local/bin/bp", self.tmp / "bp/bin/bp.mjs")}

    def step(self, check, install=None, mode="run", terminal=False, name="pi"):
        return fc_steprun.run_step((name, check, install or f"touch {self.ran}", "required", False, mode), self.tmp, False, True, terminal)

    def test_failed_rows_name_the_command_to_run_by_hand(self):
        boom = "echo 'pi: first' >&2; echo 'pi: boom' >&2; exit 3"
        self.assertEqual(self.step(self.has, boom), ("failed", f"pi: boom; by hand: {boom}"))  # the output's own words stay
        self.assertEqual(self.step(self.has, "true", name="gh stack"), ("failed", "by hand: true"))
        self.assertEqual(self.step(self.has, "exit 1", "interactive", True, "gh auth"), ("failed", "by hand: exit 1"))

    def test_each_link_row_makes_only_its_own_link(self):
        for name, (link, target) in self.links.items():
            others = [l for n, (l, _) in self.links.items() if n != name]
            for l in [link] + others:
                l.unlink(missing_ok=True)
            elsewhere = self.tmp / "elsewhere"
            if name == "compose CLI link":  # a stale link is repointed, a dangling sibling is left alone
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(elsewhere)
                others[1].symlink_to(elsewhere)
            check = lambda c, link=link, target=target: (link.is_symlink() and link.resolve() == target, "")
            state, line = self.step(check, "compose setup", "write", name=name)
            tilde = lambda p: "~/" + str(p.relative_to(Path.home()))
            self.assertEqual((state, "\n" in line, ";" in line, tilde(link) in line, str(Path.home()) in line), ("done", False, False, True, False))
            self.assertFalse(any(tilde(l) in line for l in others))
            self.assertFalse(others[0].is_symlink())
            self.assertEqual(others[1].is_symlink() and os.readlink(others[1]), name == "compose CLI link" and str(elsewhere))


class HomePaths(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.tmp = Path(t.name).resolve()
        self.ran, self.has = self.tmp / "ran", lambda c: (self.ran.exists(), "")
        p = mock.patch.object(fc_steps, "check", lambda row, c: row[1](c)); p.start(); self.addCleanup(p.stop)

    def step(self, install, mode="run", name="pi", check=None):
        return fc_steprun.run_step((name, check or self.has, install, "required", False, mode), self.tmp, False, True, False)

    def test_write_row_line_shows_home_as_tilde(self):
        h = str(Path.home())
        line = f"linked {h}/.local/bin/compose -> {h}/.compose/src/bin/compose"
        with mock.patch.dict(fc_steprun.WRITERS, {"compose CLI link": lambda c: self.ran.touch() or line}):
            self.assertEqual(self.step("compose setup", "write", "compose CLI link"),
                             ("done", "linked ~/.local/bin/compose -> ~/.compose/src/bin/compose"))

    def test_failed_and_manual_lines_show_home_as_tilde(self):
        h = str(Path.home())
        boom = f"echo 'cannot write {h}/.npm/x' >&2; exit 3"
        self.assertEqual(self.step(boom), ("failed", f"cannot write ~/.npm/x; by hand: {boom.replace(h + '/', '~/')}"))
        state, line = self.step(f"{h}/bin/fix", "manual", "git")
        self.assertEqual((state in ("manual", "skipped"), "~/bin/fix" in line, h + "/" in line), (True, True, False))
