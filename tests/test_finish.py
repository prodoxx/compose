import hermetic  # first: no test sees the real home
import inspect, json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
import os
os.environ["HERDR_ENV"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
import fc_finish as F
import fc_herdrconf, fc_links
ALL = {"workspaces": {"default": {"openrouter": "sk-or-test", "linear": "lin-test"}}, "typesafe": "ts-test"}
READY, NOT_DONE = ["compose is ready", "next: run compose to open herdr"], "setup is not finished"
SKIPS = lambda lines: [l for l in lines if l.startswith("skipped")]
CLAUDE = "Claude Code is missing: install it with curl -fsSL https://claude.ai/install.sh | bash, log in, then run compose setup again"

class Finish(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp()); self.cfg, self.auth = tmp / "config.json", tmp / "auth.json"
        self.cfg.write_text(json.dumps({"workspaces": {"w": {"target": "x"}}, "models": {"implementer": "pi:mine"}}))
        self.probe, self.claude = mock.Mock(return_value=(True, ["probe passed"])), True
        self.herdr, self.path, self.tty = mock.Mock(return_value=("ok", [])), None, True  # the terminal path unless a case says no-TTY
        for p in (mock.patch.object(F.fc_config, "CONFIG_PATH", self.cfg), mock.patch.object(F.fc_config, "AUTH_PATH", self.auth),
                  mock.patch.object(F.fc_config, "ENV_FILE", tmp / ".env"), mock.patch.object(F.fc_steps, "FRAMELINK_ENV", tmp / "figma.env"),
                  mock.patch.object(F.fc_defaults, "probe_implementer", self.probe),
                  mock.patch.object(fc_herdrconf, "apply_herdr_config", self.herdr), mock.patch.object(fc_links, "path_line", lambda: self.path),
                  mock.patch.object(F.fc_keys, "skipped_line", lambda kind, ws: f"skipped {kind}"),
                  mock.patch.object(F.shutil, "which", lambda n, *a, **k: None if n == "claude" and not self.claude else f"/bin/{n}"),
                  mock.patch("sys.stdin.isatty", lambda: self.tty)):
            p.start(); self.addCleanup(p.stop)

    def connector(self):
        """`claude mcp list` shows the claude.ai Linear connector Connected; the returned path exists once claude ran."""
        tmp = Path(tempfile.mkdtemp()); ran, claude = tmp / "ran", tmp / "claude"; (Path.home() / ".claude.json").write_text("{}")
        claude.write_text(f"#!/bin/sh\ntouch {ran}\necho 'claude.ai Linear: https://mcp.linear.app/mcp - ✔ Connected'\n"); claude.chmod(0o755)
        p = mock.patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ['PATH']}"}); p.start(); self.addCleanup(p.stop)
        return ran

    def run_finish(self, auth, yes=False):
        self.auth.write_text(json.dumps(auth))
        lines, code = F.finish([("pi", "ok"), ("OpenRouter key", "skipped")], yes)
        self.assertNotIn("sk-or-test", "\n".join(lines)); return lines, code

    def test_ready_merges_models_once_keeps_values_and_probes(self):
        before = json.loads(self.cfg.read_text())
        lines, code = self.run_finish(ALL)
        merged, added = F.fc_defaults.merge_models(before)
        self.assertEqual((code, lines[-2:]), (0, READY))
        self.assertTrue(added and len([l for l in lines if l.startswith(ADDED)]) == 1 and set(F.fc_defaults.PRESET_STEPS) <= set(lines))
        self.assertIn("probe passed", lines); self.assertEqual(SKIPS(lines), [])
        self.assertEqual(json.loads(self.cfg.read_text()), merged); self.assertEqual(merged["models"]["implementer"], "pi:mine")
        again = self.run_finish(ALL)
        self.assertEqual(again, self.run_finish(ALL)); self.assertFalse([l for l in again[0] if l.startswith(ADDED)])
        self.assertEqual(json.loads(self.cfg.read_text()), merged)

    def test_missing_keys_name_linear_without_its_key_even_with_the_connector(self):
        bare, ran = {"workspaces": {"default": {}}}, self.connector()
        lines, code = self.run_finish(bare)
        skipped = ["skipped openrouter", "skipped typesafe", "skipped linear"]
        self.assertEqual((code, lines[-1], SKIPS(lines)), (1, f"{NOT_DONE}: missing OpenRouter, TypeSafe, Linear", skipped)); self.assertNotIn(READY[0], lines)
        lines, code = self.run_finish(bare, yes=True)
        fix = next(r[2] for r in F.fc_steps.STEPS if r[0] == "OpenRouter key")
        self.assertEqual((code, lines[-1], SKIPS(lines)), (0, FOOT, [])); self.assertIn(HEAD, lines); self.assertIn(f"  OpenRouter key: {fix}", lines)
        self.probe.assert_not_called()  # no OpenRouter key, or yes
        lines, code = self.run_finish({"workspaces": {"default": {"openrouter": "sk-or-test"}}, "typesafe": "ts-test"})
        self.assertEqual((code, lines[-1], SKIPS(lines)), (1, NOT_DONE + ": missing Linear", ["skipped linear"]))
        self.assertEqual(self.run_finish(ALL)[0][-2:], READY)
        self.assertFalse(ran.exists(), "finish ran claude mcp list")

    def test_probe_failure_and_missing_claude(self):
        self.probe.return_value = (False, ["provider: no endpoints", "make the preset"])
        lines, code = self.run_finish(ALL)
        self.assertEqual(code, 1); self.assertIn("provider: no endpoints", lines); self.assertNotIn(READY[0], lines)
        self.claude = False
        lines, code = self.run_finish(ALL, yes=True)
        self.assertEqual((code, lines[-1], self.probe.call_count), (1, CLAUDE, 1))  # yes skips the probe


EXPORT = 'export PATH="$HOME/.local/bin:$PATH"'
PATH_LINES = ["~/.local/bin is not on your PATH; add this line to your shell profile:", EXPORT]
ROWS = [(n, lambda c: (True, ""), "true", "required", False, m) for n, m in
        [("apple", "run"), ("berry", "manual"), ("cherry", "run"), ("date", "run"), ("elder", "run"), ("fig", "run"), ("grape", "run"), ("kiwi", "key"), ("lime", "key")]]
NAMES = [r[0] for r in ROWS]

class Outcome(unittest.TestCase):
    def setUp(self):
        Finish.setUp(self)
        p = mock.patch.object(F.fc_steps, "STEPS", tuple(ROWS)); p.start(); self.addCleanup(p.stop)
        self.auth.write_text(json.dumps(ALL))

    def named(self, line, prefix):
        self.assertTrue(line.startswith(prefix), line)
        return sorted((n for n in NAMES if n in line), key=line.find)

    def test_groups_in_steps_order_then_herdr_then_path_then_the_old_lines(self):
        F.finish([("elder", "ok")], False); tail, code = F.finish([("elder", "ok")], False)  # second call: models already merged
        self.assertEqual(tail[-2:], READY)
        self.assertFalse(any(l.startswith(("failed", "waiting", "run by hand", "skipped:", "~/")) for l in tail))
        self.herdr.reset_mock(); self.herdr.return_value, self.path = ("written", ["herdr: wrote config.toml"]), EXPORT
        results = [("fig", "failed"), ("lime", "failed"), ("cherry", "claude"), ("grape", "done"), ("apple", "failed"),
                   ("kiwi", "skipped"), ("berry", "manual"), ("date", "skipped"), ("elder", "ok")]
        lines, code2 = F.finish(results, False)
        self.assertEqual([self.named(l, p) for l, p in zip(lines, ["failed: ", "waiting on Claude Code: ", "run by hand: ", "skipped: "])],
                         [["apple", "fig"], ["cherry"], ["berry"], ["date"]])
        self.assertEqual((lines[4:], code2), (["herdr: wrote config.toml", *PATH_LINES, *tail], code))
        home, checkout = Path(os.environ["HOME"]), Path(F.__file__).resolve().parents[1]
        self.herdr.assert_called_once()
        self.assertEqual([str(a) for a in self.herdr.call_args.args], [str(home / ".config/herdr/config.toml"), str(checkout / "bin/herdr.toml")])

    def test_symlinked_config_gets_manual_lines_and_nothing_else_is_added(self):
        F.finish([], False); quiet, _ = F.finish([], False)
        self.herdr.return_value = ("manual", ["set by hand in config.toml: a = 1", "b = 2"])
        lines, _ = F.finish([("apple", "done"), ("kiwi", "failed"), ("lime", "skipped")], False)
        self.assertEqual(lines, ["set by hand in config.toml: a = 1", "b = 2", *quiet])
        self.assertEqual(self.herdr.call_count, 3)  # once per call
        self.path = EXPORT; lines, code = F.finish([("berry", "manual")], True)
        self.assertEqual((self.named(lines[0], "run by hand: "), lines[1:5], code),
                         (["berry"], ["set by hand in config.toml: a = 1", "b = 2", *PATH_LINES], 0))


ADDED, WHY = "added default models to ~/.compose/config.json: ", "provider: no endpoints for glm-5-3-flash-fast"
NOT_DONE_LINE = lambda lines: [l for l in lines if l.startswith(NOT_DONE)]
REAL_PROBE = F.fc_defaults.probe_implementer

class Tidy(unittest.TestCase):
    run_finish = Finish.run_finish

    def setUp(self):
        Finish.setUp(self); self.why = (True, "ok")
        for p in (mock.patch.object(F.fc_defaults, "probe_implementer", REAL_PROBE),
                  mock.patch.object(F.fc_defaults.fc_models, "model_available", lambda m: self.why)):
            p.start(); self.addCleanup(p.stop)

    def test_preset_steps_once_in_every_result(self):
        self.assertTrue(F.fc_defaults.PRESET_STEPS)
        for self.why, auth, yes in (((True, "ok"), ALL, False), ((False, WHY), ALL, False), ((False, WHY), ALL, True),
                                    ((False, WHY), {"workspaces": {"default": {}}}, False)):
            lines, _ = self.run_finish(auth, yes)
            self.assertEqual([lines.count(l) for l in F.fc_defaults.PRESET_STEPS], [1] * len(F.fc_defaults.PRESET_STEPS), lines)
            self.assertEqual(WHY in lines, not self.why[0] and auth is ALL and not yes)

    def test_models_in_one_line_and_no_write_when_nothing_to_add(self):
        crew = F.fc_defaults.DEFAULT_MODELS["crew"]
        self.cfg.write_text(json.dumps({"models": {"implementer": "pi:mine", "crew": {}}}))
        lines, code = self.run_finish(ALL)
        added = [l for l in lines if "models" in l and l not in F.fc_defaults.PRESET_STEPS]
        self.assertEqual((len(added), code), (1, 0))
        self.assertEqual(set(added[0][len(ADDED):].split(", ")), {"fallback", "tiers", f"crew ({len(crew)} roles)"})
        self.cfg.write_text(json.dumps({"models": {"implementer": "pi:mine", "crew": crew}}))
        self.assertEqual([sorted(l[len(ADDED):].split(", ")) for l in self.run_finish(ALL)[0] if l.startswith(ADDED)], [["fallback", "tiers"]])
        self.cfg.unlink(); lines, _ = self.run_finish(ALL)  # fresh: no config.json yet
        added = [l for l in lines if l.startswith(ADDED)]
        self.assertEqual(len(added), 1); self.assertIn("implementer", added[0]); self.assertTrue(added[0].endswith(f"crew ({len(crew)} roles)"))
        self.assertFalse([l for l in lines if "{" in l or l.startswith("models.")])
        os.utime(self.cfg, ns=(10**18, 10**18)); before = (self.cfg.read_bytes(), self.cfg.stat().st_mtime_ns, self.cfg.stat().st_ino)
        lines, code = self.run_finish(ALL)
        self.assertEqual((self.cfg.read_bytes(), self.cfg.stat().st_mtime_ns, self.cfg.stat().st_ino, code), (*before, 0))
        self.assertFalse([l for l in lines if l.startswith("added default models")]); self.assertEqual(lines[-2:], READY)

    def test_not_finished_says_why(self):
        self.why = (False, WHY)
        lines, code = self.run_finish(ALL)
        self.assertEqual((lines[-1], code), (NOT_DONE + ": the model probe failed", 1))
        self.why = (True, "ok")
        for auth, keys in (({"workspaces": {"default": {}}}, ["openrouter", "typesafe", "linear"]),
                           ({"workspaces": {"default": {"openrouter": "sk-or-test"}}}, ["typesafe", "linear"])):
            lines, code = self.run_finish(auth); (line,) = NOT_DONE_LINE(lines)
            self.assertEqual((lines[-1], code), (line, 1))
            self.assertTrue(line.startswith(NOT_DONE + ": missing ") and ";" not in line, line)
            self.assertEqual([k for k in ("openrouter", "typesafe", "linear") if k in line.lower()], keys)
        self.why = (False, WHY)
        (line,) = NOT_DONE_LINE(self.run_finish({"workspaces": {"default": {"openrouter": "sk-or-test"}}})[0])
        self.assertTrue(line.startswith(NOT_DONE + ": missing ") and line.endswith("; the model probe failed"), line)
        self.assertNotIn("openrouter", line.lower())


NR_ROWS = [(n, lambda c: (False, ""), f"fix-{n}", lvl, False, m) for n, lvl, m in
           [("Homebrew", "default", "interactive"), ("git", "required", "manual"), ("gh", "required", "run"),
            ("gh auth", "required", "interactive"), ("pi", "required", "run"), ("~/.local/bin on PATH", "required", "manual"),
            ("OpenRouter key", "required", "key"), ("uv", "default", "run"), ("framelink-figma token", "designer", "key")]]
OK = {r[0]: "ok" for r in NR_ROWS} | {"git": "done"}
HEAD, FOOT = "compose is installed but not ready:", "run compose setup in a terminal to finish"
ENDING = lambda *names: [HEAD, *[f"  {n}: fix-{n}" for n in names], FOOT]

class NotReady(unittest.TestCase):
    def setUp(self):
        Finish.setUp(self)
        p = mock.patch.object(F.fc_steps, "STEPS", tuple(NR_ROWS)); p.start(); self.addCleanup(p.stop)
        F.finish([], False)  # models merged once, so later endings carry no added line

    def end(self, changes, yes, auth=ALL):
        self.auth.write_text(json.dumps(auth)); self.probe.reset_mock()
        lines, code = F.finish(list((OK | changes).items())[::-1], yes)  # reversed: the ending follows STEPS order
        self.assertNotIn("sk-or-test", "\n".join(lines)); return lines, code

    def assert_not_ready(self, lines, code, *names):
        self.assertEqual((lines[-len(names) - 2:], code), (ENDING(*names), 0))
        self.assertNotIn(READY[0], lines); self.assertFalse([l for l in lines if l.startswith("next:")])

    def test_yes_lists_every_missing_required_row_in_steps_order(self):
        changes = {"gh": "failed", "gh auth": "skipped", "~/.local/bin on PATH": "manual", "OpenRouter key": "skipped",
                   "pi": "claude", "Homebrew": "failed", "uv": "skipped", "framelink-figma token": "skipped"}
        self.assert_not_ready(*self.end(changes, True), "gh", "gh auth", "pi", "~/.local/bin on PATH", "OpenRouter key")

    def test_each_kind_of_missing_required_row_counts_alone(self):
        for name, state in (("gh", "failed"), ("gh auth", "skipped"), ("~/.local/bin on PATH", "manual"),
                            ("OpenRouter key", "skipped"), ("git", "skipped")):
            with self.subTest(name):
                self.assert_not_ready(*self.end({name: state}, True), name)

    def test_yes_with_required_rows_in_place_is_ready_even_if_default_or_designer_rows_fail(self):
        lines, code = self.end({"Homebrew": "failed", "uv": "skipped", "framelink-figma token": "skipped"}, True)
        self.assertEqual((lines[-2:], code), (READY, 0)); self.assertNotIn(HEAD, lines)

    def test_stdin_not_a_terminal_is_handled_as_yes(self):
        self.tty = False
        self.assert_not_ready(*self.end({"gh auth": "skipped", "OpenRouter key": "skipped"}, False), "gh auth", "OpenRouter key")
        lines, code = self.end({}, False)  # every required row in place: ready, and no probe as under yes
        self.assertEqual((lines[-2:], code), (READY, 0)); self.probe.assert_not_called()

IN_HERDR = "next: run claude in a pane and type /compose"

class InsideHerdr(unittest.TestCase):
    end = NotReady.end

    def setUp(self):
        NotReady.setUp(self)

    def under(self, extra, changes={}, yes=False, auth=ALL):
        """The ending with HERDR_ENV and HERDR_SOCKET_PATH dropped from the environment, then extra added."""
        env = {k: v for k, v in os.environ.items() if k not in ("HERDR_ENV", "HERDR_SOCKET_PATH")} | extra
        with mock.patch.dict(os.environ, env, clear=True):
            return self.end(changes, yes, auth)

    def test_ready_inside_herdr_says_run_claude_and_nothing_else_changes(self):
        for yes, self.tty in ((False, True), (True, True), (False, False)):
            out = self.under({}, yes=yes)
            self.assertEqual((out[0][-2:], out[1]), (READY, 0))
            for value in ("1", "0", "anything"):  # any non-empty value, no socket path
                with self.subTest(yes=yes, tty=self.tty, value=value):
                    lines, code = self.under({"HERDR_ENV": value}, yes=yes)
                    self.assertEqual((lines[:-1], lines[-1], code), (out[0][:-1], IN_HERDR, 0))
                    self.assertNotIn(READY[1], lines)

    def test_endings_that_are_not_ready_print_no_next_line_inside_or_outside(self):
        def same(changes={}, yes=False, auth=ALL):
            out = self.under({}, changes, yes, auth)
            for value in ("1", ""):
                lines, code = self.under({"HERDR_ENV": value}, changes, yes, auth)
                self.assertEqual((lines, code), out)
                self.assertNotIn(READY[0], lines); self.assertFalse([l for l in lines if l.startswith("next:")], lines)
            return out
        self.herdr.return_value = ("manual", ["set by hand in config.toml: a = 1"])
        self.probe.return_value = (False, ["provider: no endpoints"])
        for changes, auth in (({}, ALL), ({"OpenRouter key": "skipped"}, {"workspaces": {"default": {}}})):
            lines, code = same(changes, auth=auth)
            self.assertEqual((lines[-1].startswith(NOT_DONE), code), (True, 1))
        self.probe.return_value = (True, ["probe passed"])
        self.assertEqual(same({"gh": "failed"}, True)[0][-1], FOOT)
        self.tty = False
        self.assertEqual(same({"gh auth": "skipped"})[0][-1], FOOT)
        self.tty, self.claude = True, False
        self.assertEqual(same()[0][-1], CLAUDE); self.assertEqual(same({}, True)[0][-1], CLAUDE)



KEY_ROWS = [(n, lambda c: (False, ""), f"fix-{n}", "required", False, m) for n, m in
            [("gh", "run"), ("OpenRouter key", "key"), ("TypeSafe key", "key"), ("Linear key", "key")]]
KEYS_OK = {"workspaces": {"default": {"openrouter": "sk-or-test", "linear": "lin-test"}}, "typesafe": "ts-test"}

class KeyTextOnce(unittest.TestCase):
    """Under yes or without a terminal, a missing key is named once: its row in the not-ready list, no skipped_line text."""
    connector = Finish.connector
    def setUp(self):
        Finish.setUp(self)
        p = mock.patch.object(F.fc_steps, "STEPS", tuple(KEY_ROWS)); p.start(); self.addCleanup(p.stop)
        F.finish([], False)  # models merged once

    def end(self, auth, skipped, yes):
        self.auth.write_text(json.dumps(auth))
        lines, code = F.finish([("gh", "ok"), *[(n, "skipped") for n in skipped]], yes)
        self.assertNotIn("sk-or-test", "\n".join(lines)); return lines, code

    def assert_once(self, auth, skipped, yes):
        lines, code = self.end(auth, skipped, yes)
        self.assertEqual((SKIPS(lines), code), ([], 0))
        self.assertEqual(lines[lines.index(HEAD):], [HEAD, *[f"  {n}: fix-{n}" for n in skipped], FOOT])
        self.assertEqual([l for l in lines if any(n in l for n in skipped)], [f"  {n}: fix-{n}" for n in skipped])
        self.assertEqual((lines, code), self.end(KEYS_OK, skipped, yes))  # every other line as with the keys saved

    def test_yes_names_a_missing_openrouter_key_once(self):
        self.assert_once({"workspaces": {"default": {"linear": "lin-test"}}, "typesafe": "ts-test"}, ["OpenRouter key"], True)

    def test_linear_without_its_key_and_every_key_missing(self):
        self.connector()
        self.assert_once({"workspaces": {"default": {"openrouter": "sk-or-test"}}, "typesafe": "ts-test"}, ["Linear key"], True)
        self.assert_once({"workspaces": {"default": {}}}, ["OpenRouter key", "TypeSafe key", "Linear key"], True)

PI = """#!/bin/sh
printf '%s\\n%s' "$OPENROUTER_API_KEY" "$*" > "$REC"
echo '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"ok"}]}}'
"""
KEYS = ("OPENROUTER_API_KEY", "TEXT_MODEL_API_KEY", "TYPESAFE_API_KEY")

class SavedKey(unittest.TestCase):
    """The real probe path, pi faked on PATH: its env holds the key auth.json holds when finish runs, not only what setup exported at start."""
    def setUp(self):
        saved = dict(vars(F.fc_config)); self.addCleanup(lambda: vars(F.fc_config).update(saved))  # apply_config sets module globals
        Finish.setUp(self); self.start_cfg = json.loads(self.cfg.read_text())
        fakebin = Path(tempfile.mkdtemp()); self.rec = fakebin / "rec"
        (fakebin / "pi").write_text(PI); (fakebin / "pi").chmod(0o755)
        self.addCleanup(F.fc_defaults.fc_models._PROBED.clear)
        for p in (mock.patch.object(F.fc_defaults, "probe_implementer", REAL_PROBE),
                  mock.patch.object(F.fc_config, "PROBE_CACHE", self.cfg.parent / "model-probe.json"),
                  mock.patch.dict(os.environ, {"PATH": f"{fakebin}:/usr/bin:/bin", "REC": str(self.rec)})):
            p.start(); self.addCleanup(p.stop)

    def run_setup(self, start, saved, cfg={}, shell=None):
        """(key pi got, ending, code): setup starts on auth.json `start` (None: no file), then the keys screen writes `saved`."""
        self.cfg.write_text(json.dumps(self.start_cfg | cfg)); self.auth.unlink(missing_ok=True); self.rec.unlink(missing_ok=True)
        F.fc_config.PROBE_CACHE.unlink(missing_ok=True); F.fc_defaults.fc_models._PROBED.clear()
        for k in KEYS: os.environ.pop(k, None)
        if shell: os.environ["OPENROUTER_API_KEY"] = shell
        if start is not None: self.auth.write_text(json.dumps(start))
        F.fc_config.apply_config()
        self.auth.write_text(json.dumps(saved))
        lines, code = F.finish([("pi", "ok"), ("OpenRouter key", "done")], False)
        key, argv = self.rec.read_text().split("\n", 1) if self.rec.exists() else (None, "")
        for value in ("sk-or-test", "sk-or-w", "sk-or-stale", "ts-test", "lin-test"):
            self.assertNotIn(value, "\n".join(lines) + argv)
        return key, lines, code

    def test_first_run_and_rerun_probe_with_the_saved_key(self):
        key, first, code = self.run_setup(None, ALL)
        self.assertEqual((key, code, first[-2:]), ("sk-or-test", 0, READY)); self.assertIn("implementer pi:mine: answered", first)
        self.assertEqual(os.environ.get("TYPESAFE_API_KEY"), "ts-test")
        self.assertEqual(self.run_setup({"workspaces": {"default": {}}}, ALL)[0], "sk-or-test")  # an empty auth.json at start
        self.assertEqual(self.run_setup(ALL, ALL), ("sk-or-test", first, 0))  # rerun: exported at start, same ending

    def test_saved_key_wins_over_a_stale_shell_key_and_follows_the_active_workspace(self):
        self.assertEqual(self.run_setup(None, ALL, shell="sk-or-stale")[:3:2], ("sk-or-test", 0))
        other = {"workspaces": {**ALL["workspaces"], "w": {"openrouter": "sk-or-w"}}, "typesafe": "ts-test"}
        self.assertEqual(self.run_setup(None, other, {"workspace": "w"})[:3:2], ("sk-or-w", 0))
        self.assertEqual(self.run_setup(ALL, other, {"workspace": "w"})[:3:2], ("sk-or-w", 0))


class HomePaths(unittest.TestCase):
    """Every line finish returns shows ~/ for the home directory followed by /; nothing else changes."""
    def setUp(self):
        Finish.setUp(self); self.auth.write_text(json.dumps(ALL))
        tmp = Path(tempfile.mkdtemp()).resolve(); self.home, self.other = tmp / "al", tmp / "alex"  # alex only shares a prefix
        self.herdr.side_effect = lambda path, shipped, dry=False: ("edited", [f"copied {shipped} to {path}", f"edited {path} and {path}"])
        for p in (mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                  mock.patch.object(F, "__file__", str(self.home / "src/compose/bin/fc_finish.py")),
                  mock.patch.object(F.fc_steps, "STEPS", ())):
            p.start(); self.addCleanup(p.stop)
        F.finish([], False)  # models merged once, so later endings carry no added line

    def test_herdr_lines_and_the_path_line(self):
        self.path = EXPORT
        lines, code = F.finish([], False)
        self.assertEqual((lines[:4], lines[-2:], code), (["copied ~/src/compose/bin/herdr.toml to ~/.config/herdr/config.toml",
                                                          "edited ~/.config/herdr/config.toml and ~/.config/herdr/config.toml", *PATH_LINES], READY, 0))
        self.assertFalse([l for l in lines if str(self.home) + "/" in l], lines)

    def test_probe_message_with_a_home_path_and_a_prefix_sharing_path(self):
        self.probe.return_value = (False, [f"pi not found at {self.home}/.npm/bin/pi", f"see {self.other}/f", f"HOME is {self.home}"])
        lines, code = F.finish([], False)
        self.assertEqual(code, 1); self.assertEqual(lines[-1], NOT_DONE + ": the model probe failed")
        for line in ("pi not found at ~/.npm/bin/pi", f"see {self.other}/f", f"HOME is {self.home}"):
            self.assertIn(line, lines)


class ShippedHerdr(unittest.TestCase):
    """With no ~/.config/herdr/config.toml, finish copies the checkout's bin/herdr.toml there; every other line and the code stay."""
    def setUp(self):
        real = fc_herdrconf.apply_herdr_config; Finish.setUp(self); self.real = real
        tmp, cwd = tempfile.mkdtemp(), os.getcwd(); os.chdir(tmp); self.addCleanup(os.chdir, cwd)  # the shipped path is not cwd-relative

    def test_fresh_machine_gets_bin_herdr_toml_and_nothing_else_changes(self):
        shipped, toml = Path(F.__file__).resolve().parent / "herdr.toml", Path.home() / ".config/herdr/config.toml"
        bare = {"workspaces": {"default": {}}}
        for auth, yes in ((ALL, False), (bare, False), (bare, True)):
            self.herdr.side_effect = None; Finish.run_finish(self, auth, yes); base = Finish.run_finish(self, auth, yes)  # herdr says ok
            self.herdr.side_effect = self.real; toml.unlink(missing_ok=True)
            lines, code = Finish.run_finish(self, auth, yes)
            self.assertEqual((lines, code), ([f"copied {shipped} to ~/.config/herdr/config.toml", *base[0]], base[1]))
            self.assertEqual(toml.read_bytes(), shipped.read_bytes())
            self.assertEqual(Finish.run_finish(self, auth, yes), base)  # a rerun finds the copy quiet and adds nothing


WOULD, OR_ONLY = "would add default models to ~/.compose/config.json: ", {"workspaces": {"default": {"openrouter": "sk-or-test"}}}
DRY = lambda c: inspect.signature(lambda path, shipped, dry=False: 0).bind(*c.args, **c.kwargs).arguments.get("dry")

class Dry(unittest.TestCase):
    """finish(results, yes, dry=True): would lines instead of writes, no probe, today's exit code, every file as it was."""
    def setUp(self):
        Finish.setUp(self); self.tmp, self.env = self.cfg.parent, self.cfg.parent / ".env"
        p = mock.patch.object(F.fc_config, "PROBE_CACHE", self.tmp / "model-probe.json"); p.start(); self.addCleanup(p.stop)

    def dry(self, auth, yes=False):
        if auth is not None: self.auth.write_text(json.dumps(auth))
        before = {p.name: p.read_bytes() for p in self.tmp.iterdir()}; self.herdr.reset_mock()
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            lines, code = F.finish([("pi", "ok"), ("OpenRouter key", "skipped")], yes, dry=True)
        self.assertEqual({p.name: p.read_bytes() for p in self.tmp.iterdir()}, before)  # same bytes, no new file, no probe cache
        self.probe.assert_not_called(); self.assertEqual([DRY(c) for c in self.herdr.call_args_list], [True])
        self.assertNotIn("sk-or-test", "\n".join(lines)); return lines, code

    def added(self, lines, prefix):
        return [l[len(prefix):] for l in lines if l.startswith(prefix)]

    def test_set_up_home_with_old_keys_keeps_them_and_names_the_real_additions(self):
        cfg = json.loads(self.cfg.read_text()); cfg["openrouter_api_key"] = "sk-or-test-old"; cfg["workspaces"]["w"]["linear_api_key"] = "lin-old"
        self.cfg.write_text(json.dumps(cfg)); self.env.write_text("TEXT_MODEL_API_KEY=sk-or-test-env\nOTHER=1\n")
        lines, code = self.dry(ALL)
        moves = [F.fc_core.tilde(f"would move {n} out of {p} into {self.auth}") for n, p in F.fc_auth.old_keys(self.cfg, self.env)]
        self.assertEqual((len(moves), [l for l in lines if l.startswith("would move")], code), (3, moves, 0))
        self.assertIn("would check implementer pi:mine", lines)
        real = self.added(Finish.run_finish(self, ALL)[0], ADDED)
        self.assertTrue(real); self.assertEqual(self.added(lines, WOULD), real)

    def test_fresh_home_checks_the_default_implementer(self):
        self.cfg.unlink()
        lines, code = self.dry(OR_ONLY)
        self.assertEqual(code, 1); self.assertFalse([l for l in lines if l.startswith("would move")])
        self.assertIn("would check implementer " + F.fc_defaults.DEFAULT_MODELS["implementer"], lines)
        real = self.added(Finish.run_finish(self, OR_ONLY)[0], ADDED)
        self.assertTrue(real); self.assertEqual(self.added(lines, WOULD), real)

    def test_exit_code_counts_the_probe_as_not_run(self):
        self.probe.return_value = (False, ["provider: no endpoints"])
        for auth, yes, tty, claude, want in ((ALL, False, True, True, 0), (OR_ONLY, False, True, True, 1), (ALL, False, True, False, 1),
                                             ({}, True, True, True, 0), (ALL, True, True, False, 1), ({}, False, False, True, 0)):
            self.tty, self.claude = tty, claude
            lines, code = self.dry(auth, yes)
            self.assertEqual(code, want, (auth, yes, tty, claude, lines))
        self.assertFalse([l for l in self.dry({}, False)[0] if l.startswith("would check")])  # no OpenRouter key, nothing to check
