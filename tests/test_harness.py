import hermetic  # first: no test sees the real home
import contextlib, hashlib, importlib.machinery, importlib.util, io, json, os, stat, subprocess, sys, tempfile, time, tomllib, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
import fc_checks, fc_config, fc_harness, fc_init, fc_links, fc_models, fc_steprun, fc_steps
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(fc)

FC_MODULES = {n: m for n, m in sys.modules.items() if n.startswith("fc_")}

def reload_steps():  # another test file may have dropped or replaced the fc_* modules in sys.modules; reload against the ones imported here
    with mock.patch.dict(sys.modules, FC_MODULES):
        for m in (fc_steps, fc_steprun): importlib.reload(m)


class CodexIds(unittest.TestCase):
    def test_probe_command(self):
        head, prompt = ["codex", "exec", "--ephemeral", "--skip-git-repo-check"], "Reply with exactly: ok"
        self.assertEqual(fc_harness.probe_command("codex:gpt-6-sol"), head + ["-m", "gpt-6-sol", prompt])
        self.assertEqual(fc_harness.probe_command("codex:a:b"), head + ["-m", "a:b", prompt])
        for model in ("codex", "codex:"): self.assertEqual(fc_harness.probe_command(model), head + [prompt])

    def test_probe_result(self):
        def result(code, out, err, model="codex:gpt-6-sol"): return fc_harness.probe_result(model, subprocess.CompletedProcess([], code, out, err))
        refused = 'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The \'x\' model is not supported."}}'
        self.assertEqual(result(0, "", "ERROR: noise"), (True, "answered"))
        self.assertEqual(result(0, "", "", "codex"), (True, "answered"))
        self.assertEqual(result(1, "", "warn\n" + refused + "\n"), (False, "The 'x' model is not supported."))
        self.assertEqual(result(1, "", "ERROR: unexpected status 401 Unauthorized: no key\n", "codex"), (False, "unexpected status 401 Unauthorized: no key"))
        self.assertEqual(result(1, "", "ERROR: first\nERROR: second\n"), (False, "first"))
        self.assertEqual(result(1, "out\n", "x ERROR: not first\nlast\n\n"), (False, "last"))
        self.assertEqual(result(1, "a\nb\n\n", " \n"), (False, "b"))
        self.assertEqual(result(3, "", ""), (False, "exit 3"))

class CodexSkillLink(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.bin, self.home, self.co = tmp / "bin", tmp / "home", tmp / "co"
        for d in (self.bin, self.home, self.co): d.mkdir()
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)
        h = self.home
        self.three = [("compose CLI link", h / ".local/bin/compose", self.co / "bin/compose"),
                      ("compose skill link", h / ".claude/skills/compose", self.co), ("bp link", h / ".local/bin/bp", self.co / "bp/bin/bp.mjs")]

    def codex(self):
        (self.bin / "codex").write_text("#!/bin/sh\n"); (self.bin / "codex").chmod(stat.S_IRWXU)

    def second(self):
        (self.bin / "other").write_text("#!/bin/sh\n"); (self.bin / "other").chmod(stat.S_IRWXU)
        row = {**fc_harness.HARNESSES.get("codex", {}), "name": "Other", "exe": "other", "home_env": "OTHER_HOME", "home": "~/.other"}
        p = mock.patch.dict(fc_harness.HARNESSES, {"other": row}); p.start(); self.addCleanup(p.stop)

    def test_codex_on_path_adds_its_skill_link_after_the_three(self):
        self.codex()
        self.assertEqual(fc_checks.owned_links(self.co), self.three + [("Codex skill link", self.home / ".codex/skills/compose", self.co)])
        os.environ["CODEX_HOME"] = str(self.home / "elsewhere")
        self.assertEqual(fc_checks.owned_links(self.co)[3:], [("Codex skill link", self.home / "elsewhere/skills/compose", self.co)])

    def test_no_codex_is_exactly_the_three_links(self):
        self.assertEqual(fc_checks.owned_links(self.co), self.three)
        os.environ["CODEX_HOME"] = str(self.home / "elsewhere")
        self.assertEqual(fc_checks.owned_links(self.co), self.three)

    def test_a_second_installed_row_follows_codex_under_its_own_skills_dir(self):
        self.codex(); self.second()
        other = ("Other skill link", self.home / ".other/skills/compose", self.co)
        self.assertEqual(fc_checks.owned_links(self.co)[3:], [("Codex skill link", self.home / ".codex/skills/compose", self.co), other])
        (self.bin / "codex").unlink()
        self.assertEqual(fc_checks.owned_links(self.co)[3:], [other])

    def test_relink_links_the_skill_only_with_codex_on_path(self):
        os.environ["CODEX_HOME"] = str(self.home / "elsewhere")
        fc_links.relink_owned(self.co)
        self.assertFalse((self.home / ".codex").exists() or (self.home / "elsewhere").exists())
        self.codex(); fc_links.relink_owned(self.co)
        self.assertEqual((self.home / "elsewhere/skills/compose").resolve(), self.co.resolve())


class CodexSkillRow(unittest.TestCase):
    NAME = "Codex skill link"
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.bin, self.home, self.co = tmp / "bin", tmp / "home", tmp / "co"
        for d in (self.bin, self.home, self.co / "bin"): d.mkdir(parents=True)
        for f in ("bin/compose.py", "SKILL.md"): (self.co / f).write_text("")  # a compose checkout
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None); self.link = self.home / ".codex/skills/compose"
        names = [r[0] for r in fc_steps.STEPS]
        self.row = fc_steps.STEPS[names.index(self.NAME)]
        self.assertEqual(names[names.index(self.NAME) - 1], "compose skill link")

    def tool(self, name="codex"):
        (self.bin / name).write_text("#!/bin/sh\n"); (self.bin / name).chmod(stat.S_IRWXU)

    def run_row(self, row=None, dry=False): return fc_steprun.run_step(row or self.row, self.co, True, False, False, dry=dry)

    def test_without_codex_the_check_passes_and_run_step_does_nothing(self):
        self.assertEqual(self.row[2:], ("compose setup", "default", False, "write"))
        self.assertEqual(fc_steps.check(self.row, self.co), (True, ""))
        self.assertEqual(self.run_row(), ("ok", ""))

    def test_with_codex_the_check_reads_like_the_compose_skill_link(self):
        self.tool()
        self.assertEqual(fc_steps.check(self.row, self.co), (False, "~/.codex/skills/compose is missing"))
        self.link.parent.mkdir(parents=True); self.link.symlink_to(self.home / "gone")
        self.assertEqual(fc_steps.check(self.row, self.co), (False, f"dangling link {self.link} -> {self.home / 'gone'}"))
        self.link.unlink(); self.link.symlink_to(self.co)
        self.assertEqual(fc_steps.check(self.row, self.co), (True, ""))

    def test_run_step_links_the_checkout_under_the_skills_dir_then_finds_it_ok(self):
        self.tool(); os.environ["CODEX_HOME"] = str(self.home / "elsewhere")
        self.assertEqual(self.run_row()[0], "done")
        self.assertEqual((self.home / "elsewhere/skills/compose").resolve(), self.co.resolve())
        self.assertEqual(self.run_row(), ("ok", ""))

    def test_every_harness_row_gets_its_own_skill_link_row(self):
        self.tool(); self.tool("other")
        self.addCleanup(reload_steps)
        p = mock.patch.dict(fc_harness.HARNESSES, {"other": {**fc_harness.HARNESSES["codex"], "name": "Other", "exe": "other", "home_env": "OTHER_HOME", "home": "~/.other"}})
        p.start(); self.addCleanup(p.stop)
        reload_steps()
        names = [r[0] for r in fc_steps.STEPS]; i = names.index(self.NAME); j = names.index("Other skill link")
        self.assertEqual(names[i - 1:i + 2], ["compose skill link", self.NAME, "Codex commands"])
        self.assertGreater(j, i)
        self.assertEqual(self.run_row(fc_steps.STEPS[j])[0], "done")
        self.assertEqual((self.home / ".other/skills/compose").resolve(), self.co.resolve())
        self.assertFalse(self.link.exists())
        self.assertEqual(set(fc_steprun.WRITERS), set(fc_steprun.PREVIEWS))

    def test_doctor_lists_the_missing_link_as_optional(self):
        self.tool(); out = io.StringIO()
        with contextlib.redirect_stdout(out): fc_init.check_deps()
        self.assertIn("MISSING: Codex skill link: ~/.codex/skills/compose is missing (install: compose setup) - optional\n", out.getvalue())


class CodexCommands(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.bin, self.home, self.co = tmp / "bin", tmp / "home", tmp / "co"
        for d in (self.bin, self.home, self.co / "commands"): d.mkdir(parents=True)
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)
        for n in ("yolo", "planner"): (self.co / f"commands/{n}.md").write_bytes(f"# {n}\r\nRun $ARGUMENTS \xe9\n".encode())
        (self.co / "commands/notes.txt").write_text("not a command")
        s = self.home / ".codex/skills"
        self.planner, self.yolo = s / "compose-planner/SKILL.md", s / "compose-yolo/SKILL.md"

    def tool(self, name="codex"):
        (self.bin / name).write_text("#!/bin/sh\n"); (self.bin / name).chmod(stat.S_IRWXU)

    def test_without_the_exe_nothing_is_listed_checked_or_created(self):
        self.assertEqual(fc_checks.command_copies("codex", self.co), [])
        self.assertEqual(fc_checks.commands_ok("codex", self.co), (True, ""))
        fc_links.relink_owned(self.co)
        self.assertFalse((self.home / ".codex").exists())

    def test_one_copy_per_command_sorted_by_name(self):
        self.tool()
        self.assertEqual(fc_checks.command_copies("codex", self.co), [(self.planner, self.co / "commands/planner.md"), (self.yolo, self.co / "commands/yolo.md")])

    def test_check_names_the_first_missing_or_stale_copy(self):
        self.tool()
        self.assertEqual(fc_checks.commands_ok("codex", self.co), (False, "~/.codex/skills/compose-planner/SKILL.md is missing"))
        fc_links.write_commands("codex", self.co)
        self.assertEqual(fc_checks.commands_ok("codex", self.co), (True, ""))
        self.yolo.write_bytes(b"old")
        self.assertEqual(fc_checks.commands_ok("codex", self.co), (False, "~/.codex/skills/compose-yolo/SKILL.md is stale"))

    def test_write_copies_bytes_replaces_stale_then_a_second_write_changes_nothing(self):
        self.tool(); self.yolo.parent.mkdir(parents=True); self.yolo.write_bytes(b"old")
        self.assertEqual(fc_links.write_commands("codex", self.co), [f"wrote {self.planner}", f"wrote {self.yolo}"])
        for copy, name in ((self.planner, "planner"), (self.yolo, "yolo")): self.assertEqual(copy.read_bytes(), (self.co / f"commands/{name}.md").read_bytes())
        self.assertEqual(fc_links.write_commands("codex", self.co), [f"ok {self.planner}", f"ok {self.yolo}"])

    def test_a_symlink_or_directory_is_refused_untouched(self):
        self.tool(); other = self.home / "other.md"; other.write_bytes(b"mine")
        self.planner.parent.mkdir(parents=True); self.planner.symlink_to(other)
        self.yolo.mkdir(parents=True)
        lines = fc_links.write_commands("codex", self.co)
        self.assertTrue(lines[0].startswith(f"refused {self.planner}: ") and lines[1].startswith(f"refused {self.yolo}: "))
        self.assertEqual((os.readlink(self.planner), other.read_bytes()), (str(other), b"mine"))
        self.assertTrue(self.yolo.is_dir() and not any(self.yolo.iterdir()))

    def test_relink_ends_with_one_commands_line_per_copy(self):
        self.tool()
        self.assertEqual(fc_links.relink_owned(self.co)[-2:], [f"Codex commands: wrote {self.planner}", f"Codex commands: wrote {self.yolo}"])
        self.assertEqual(fc_links.relink_owned(self.co)[-2:], [f"Codex commands: ok {self.planner}", f"Codex commands: ok {self.yolo}"])

    def test_another_row_gets_its_own_copies_under_its_skills_dir(self):
        self.tool("other")
        row = {**fc_harness.HARNESSES.get("codex", {}), "name": "Other", "exe": "other", "home_env": "OTHER_HOME", "home": "~/.other"}
        with mock.patch.dict(fc_harness.HARNESSES, {"other": row}):
            copy = self.home / ".other/skills/compose-yolo/SKILL.md"
            self.assertEqual(fc_checks.command_copies("other", self.co)[1], (copy, self.co / "commands/yolo.md"))
            self.assertEqual(fc_links.write_commands("other", self.co)[1], f"wrote {copy}")
            self.assertIn(f"Other commands: ok {copy}", fc_links.relink_owned(self.co))
        self.assertFalse((self.home / ".codex").exists())


class CodexCommandsRow(unittest.TestCase):
    NAME = "Codex commands"
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.bin, self.home, self.co = tmp / "bin", tmp / "home", tmp / "co"
        for d in (self.bin, self.home, self.co / "commands"): d.mkdir(parents=True)
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)
        for n in ("yolo", "planner"): (self.co / f"commands/{n}.md").write_bytes(f"# {n}\nRun $ARGUMENTS\n".encode())
        s = self.home / ".codex/skills"
        self.planner, self.yolo = s / "compose-planner/SKILL.md", s / "compose-yolo/SKILL.md"
        names = [r[0] for r in fc_steps.STEPS]
        self.row = fc_steps.STEPS[names.index(self.NAME)]
        self.assertEqual(names[names.index(self.NAME) - 1], "Codex skill link")

    def codex(self):
        (self.bin / "codex").write_text("#!/bin/sh\n"); (self.bin / "codex").chmod(stat.S_IRWXU)

    def run_row(self, dry=False): return fc_steprun.run_step(self.row, self.co, True, False, False, dry=dry)

    def test_without_codex_the_check_passes_and_run_step_does_nothing(self):
        self.assertEqual(self.row[2:], ("compose setup", "default", False, "write"))
        self.assertEqual(fc_steps.check(self.row, self.co), (True, ""))
        self.assertEqual(self.run_row(), ("ok", ""))

    def test_a_missing_or_stale_copy_fails_the_check_and_run_step_writes_the_copies(self):
        self.codex()
        self.assertEqual(fc_steps.check(self.row, self.co), (False, "~/.codex/skills/compose-planner/SKILL.md is missing"))
        self.assertEqual(self.run_row()[0], "done")
        self.assertEqual(self.yolo.read_bytes(), (self.co / "commands/yolo.md").read_bytes())
        self.assertEqual(self.run_row(), ("ok", ""))
        self.yolo.write_bytes(b"old")
        self.assertEqual(fc_steps.check(self.row, self.co), (False, "~/.codex/skills/compose-yolo/SKILL.md is stale"))
        self.assertEqual(self.run_row()[0], "done")
        self.assertEqual(self.yolo.read_bytes(), (self.co / "commands/yolo.md").read_bytes())

    def test_dry_run_previews_one_line_per_copy_and_writes_nothing(self):
        self.codex()
        self.assertEqual(self.run_row(dry=True), ("would", "would write ~/.codex/skills/compose-planner/SKILL.md; would write ~/.codex/skills/compose-yolo/SKILL.md"))
        self.assertFalse((self.home / ".codex").exists())

    def test_every_harness_row_gets_its_own_commands_row_after_its_skill_link_row(self):
        (self.bin / "other").write_text("#!/bin/sh\n"); (self.bin / "other").chmod(stat.S_IRWXU)
        self.addCleanup(reload_steps)
        p = mock.patch.dict(fc_harness.HARNESSES, {"other": {**fc_harness.HARNESSES["codex"], "name": "Other", "exe": "other", "home_env": "OTHER_HOME", "home": "~/.other"}})
        p.start(); self.addCleanup(p.stop)
        reload_steps()
        names = [r[0] for r in fc_steps.STEPS]; i = names.index("Other commands")
        self.assertEqual(names[i - 1], "Other skill link")
        row = fc_steps.STEPS[i]; self.assertEqual(row[2:], ("compose setup", "default", False, "write"))
        self.assertEqual(fc_steprun.run_step(row, self.co, True, False, False)[0], "done")
        self.assertEqual((self.home / ".other/skills/compose-yolo/SKILL.md").read_bytes(), (self.co / "commands/yolo.md").read_bytes())
        self.assertFalse((self.home / ".codex").exists())
        self.assertEqual(set(fc_steprun.WRITERS), set(fc_steprun.PREVIEWS))

    def test_doctor_lists_the_first_missing_copy_as_optional(self):
        self.codex(); out = io.StringIO()
        with contextlib.redirect_stdout(out): fc_init.check_deps()
        self.assertIn("MISSING: Codex commands: ~/.codex/skills/compose-away/SKILL.md is missing (install: compose setup) - optional\n", out.getvalue())


class HarnessBlock(unittest.TestCase):
    CODEX = {"models": {"crew": {"default": "codex:gpt-6"}}}
    CFG = {"models": {"implementer": "pi:top", "crew": {"default": "claude:sonnet", "planner": "claude:opus"}},
           "workspace": "work", "workspaces": {"work": {"models": {"implementer": "pi:work"}}}, "harnesses": {"codex": CODEX}}

    def setUp(self):
        for k in ("HARNESS", "DEFAULT_MODEL", "CREW_MODELS", "CREW_EFFORT", "CREW_FALLBACK", "WORKSPACE"):
            p = mock.patch.object(fc_config, k, getattr(fc_config, k)); p.start(); self.addCleanup(p.stop)
        path = fc_config.CONFIG_PATH; before = path.read_bytes() if path.exists() else None
        self.addCleanup(lambda: path.write_bytes(before) if before is not None else path.unlink(missing_ok=True))

    def apply(self, cfg, harness=None, **env):
        fc_config.write_config(cfg)
        with mock.patch.dict(os.environ):
            for k in ("COMPOSE_HARNESS", "CLAUDECODE", "CODEX_THREAD_ID"): os.environ.pop(k, None)
            os.environ.update(env)
            return fc_config.apply_config(move=False, harness=harness)

    def exits(self, cfg, message, **kw):
        with self.assertRaises(SystemExit) as e: self.apply(cfg, **kw)
        self.assertEqual(str(e.exception), f"{fc_config.CONFIG_PATH}: {message}")

    def test_session_harness_precedence(self):
        with mock.patch.dict(os.environ, {"COMPOSE_HARNESS": "gemini", "CLAUDECODE": "1"}):
            os.environ.pop("CODEX_THREAD_ID", None)
            self.assertEqual(fc_config.session_harness("codex"), "codex")
            self.assertEqual(fc_config.session_harness(), "gemini")
            del os.environ["COMPOSE_HARNESS"]
            self.assertEqual(fc_config.session_harness(), "claude")
            os.environ["CLAUDECODE"] = "0"
            self.assertIsNone(fc_config.session_harness())

    def test_block_replaces_crew_whole_over_the_workspace_merge(self):
        self.apply(self.CFG, "codex")
        self.assertEqual((fc_config.CREW_MODELS, fc_config.DEFAULT_MODEL, fc_config.HARNESS), ({"default": "codex:gpt-6"}, "pi:work", "codex"))
        st = fc_config.settings(self.CFG, "codex")[1]
        self.assertNotIn("harnesses", st)
        self.assertEqual(st["models"]["implementer"], "pi:work")

    def test_block_from_the_workspace_and_a_top_level_key(self):
        cfg = {**self.CFG, "harnesses": {}, "workspaces": {"work": {"harnesses": {"codex": {**self.CODEX, "parallel": 4}}}}}
        self.assertEqual(fc_config.settings(cfg, "codex")[1]["parallel"], 4)

    def test_a_block_without_default_exits_active_or_not(self):
        bad = {**self.CFG, "harnesses": {"codex": self.CODEX, "gemini": {"models": {"crew": {"planner": "x"}}}}}
        for kw in ({"harness": "codex"}, {}):
            self.exits(bad, 'harnesses.gemini.models.crew has no "default"', **kw)

    def test_a_harness_argument_without_a_block_exits(self):
        self.exits(self.CFG, 'no "harnesses.gemini" block', harness="gemini")


class ClaudeTopLevel(unittest.TestCase):
    CFG, setUp, apply, exits = HarnessBlock.CFG, HarnessBlock.setUp, HarnessBlock.apply, HarnessBlock.exits
    NO_BLOCK = 'no "harnesses.gemini" block'

    def test_claude_code_needs_no_claude_block(self):
        self.apply(self.CFG, CLAUDECODE="1")
        self.assertEqual((fc_config.HARNESS, fc_config.CREW_MODELS, fc_config.DEFAULT_MODEL), ("claude", self.CFG["models"]["crew"], "pi:work"))
        self.assertEqual(fc_config.settings(self.CFG, "claude"), fc_config.settings(self.CFG))

    def test_a_harness_without_a_block_exits_with_or_without_a_map(self):
        for harnesses in ({}, {"codex": HarnessBlock.CODEX}, None):
            cfg = {**self.CFG, "harnesses": harnesses}
            if harnesses is None: del cfg["harnesses"]
            with self.subTest(harnesses=harnesses):
                self.exits(cfg, self.NO_BLOCK, COMPOSE_HARNESS="gemini")
                self.exits(cfg, self.NO_BLOCK, CLAUDECODE="1", COMPOSE_HARNESS="gemini")

    def test_an_explicit_claude_block_merges_as_codexs_does(self):
        claude = {"models": {"crew": {"default": "claude:haiku"}}}
        self.apply({**self.CFG, "harnesses": {"claude": claude}}, CLAUDECODE="1")
        self.assertEqual((fc_config.CREW_MODELS, fc_config.DEFAULT_MODEL), ({"default": "claude:haiku"}, "pi:work"))

    def test_a_block_without_default_exits_when_claude_has_no_block(self):
        bad = {**self.CFG, "harnesses": {"gemini": {"models": {"crew": {"planner": "x"}}}}}
        self.exits(bad, 'harnesses.gemini.models.crew has no "default"', CLAUDECODE="1")

class CodexDefaultBlock(unittest.TestCase):
    CFG = {"models": {"implementer": "claude:sonnet", "fallback": "claude:opus", "fix": "claude:opus",
                      "crew": {"orchestrator": {"model": "claude:opus", "effort": "high", "fallback": ["claude:opus", "pi:x/y"]},
                               "architect": "claude:opus", "planner": "pi:openai/gpt-6-astra", "tests": "claude:haiku"}}}
    setUp = HarnessBlock.setUp

    def test_every_claude_crew_role_becomes_codex_and_a_pi_role_keeps_its_value(self):
        crew = fc_config.settings(self.CFG, "codex")[1]["models"]["crew"]
        self.assertEqual({k: (v["model"] if isinstance(v, dict) else v) for k, v in crew.items()},
                         {"orchestrator": "codex", "architect": "codex", "tests": "codex", "planner": "pi:openai/gpt-6-astra", "default": "codex", "designer": "codex"})
        self.assertEqual((crew["orchestrator"]["effort"], crew["orchestrator"]["fallback"]), ("high", ["pi:x/y"]))  # the Claude fallback equals its own model, so it is dropped

    def test_claude_implementer_fallback_fix_and_test_reader_become_codex_and_none_stays(self):
        models = fc_config.settings(self.CFG, "codex")[1]["models"]
        self.assertEqual((models.get("implementer"), models.get("fallback"), models.get("fix"), models.get("test_reader")), ("codex",) * 4)
        models = fc_config.settings({**self.CFG, "models": {**self.CFG["models"], "fallback": None, "fix": None}}, "codex")[1]["models"]
        self.assertEqual((models["fallback"], models["fix"]), (None, None))

    def test_a_config_block_wins_whole_and_is_not_merged_under_the_derived_one(self):
        block = {"models": {"crew": {"default": "codex:gpt-6-sol"}}}
        models = fc_config.settings({**self.CFG, "harnesses": {"codex": block}}, "codex")[1]["models"]
        self.assertEqual((models["crew"], models["implementer"]), ({"default": "codex:gpt-6-sol"}, "claude:sonnet"))

    def test_gemini_grok_and_an_unknown_name_with_no_block_still_exit(self):
        cfg = {k: v for k, v in self.CFG.items() if k != "harnesses"}
        for name in ("gemini", "grok", "nope"):
            with self.subTest(name=name):
                with self.assertRaises(SystemExit) as e: fc_config.settings(cfg, name)
                self.assertEqual(str(e.exception), f'{fc_config.CONFIG_PATH}: no "harnesses.{name}" block')

class CodexDefaultTiers(unittest.TestCase):
    CFG = {"models": {"crew": {"default": "claude:sonnet"}}}
    apply = HarnessBlock.apply
    CODEX_TIERS = {"cheap": ["codex", "pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast"], "standard": ["codex"],
                   "frontier": [{"model": "codex", "effort": "high"}, {"model": "codex", "effort": "high"}, {"model": "pi:openai/gpt-6-astra", "effort": "high"}]}

    def setUp(self):
        HarnessBlock.setUp(self)
        p = mock.patch.object(fc_config, "TIERS", fc_config.TIERS); p.start(); self.addCleanup(p.stop)

    def tiers(self, cfg): return fc_config.settings(cfg, "codex")[1]["models"]["tiers"]

    def test_no_block_and_no_tiers_give_the_codex_defaults(self):
        self.assertEqual(self.tiers(self.CFG), self.CODEX_TIERS)

    def test_each_entry_maps_by_its_shape_and_keeps_its_effort(self):
        tiers = {"cheap": ["claude", "claude:claude-haiku-5-5", "pi:a/b", "openrouter/openai/gpt-oss-120b"],
                 "standard": [{"model": "claude:claude-sonnet-5-5", "effort": "xhigh"}, {"model": "pi:c/d", "effort": "max"}],
                 "huge": ["pi:x/y"]}
        self.assertEqual(self.tiers({**self.CFG, "models": {"tiers": tiers}}),
                         {"cheap": ["codex", "codex", "pi:a/b", "openrouter/openai/gpt-oss-120b"],
                          "standard": [{"model": "codex", "effort": "xhigh"}, {"model": "pi:c/d", "effort": "max"}],
                          "frontier": self.CODEX_TIERS["frontier"], "huge": ["pi:x/y"]})

    def test_escalate_never_picks_a_claude_model(self):
        self.apply(self.CFG, harness="codex")
        picks = [fc.fc_tiers.escalate(m) for t in fc_config.TIERS.values() for m, _ in t]
        self.assertEqual(picks[0], "codex")
        self.assertEqual([p for p in picks if p and p.startswith("claude")], [])

    def test_a_codex_block_wins_whole(self):
        block = {"models": {"crew": {"default": "codex:gpt-6-sol"}, "tiers": {"cheap": ["claude:claude-haiku-5-5"]}}}
        self.assertEqual(self.tiers({**self.CFG, "harnesses": {"codex": block}}), {"cheap": ["claude:claude-haiku-5-5"]})

    def test_claude_and_no_harness_keep_the_claude_tiers(self):
        for harness in ("claude", None):
            with self.subTest(harness=harness):
                self.apply(self.CFG, harness=harness)
                self.assertEqual((fc_config.TIERS["cheap"][0], fc.fc_tiers.escalate("claude:claude-haiku-5-5")), (("claude:claude-haiku-5-5", None), "claude:claude-sonnet-5-5"))

class HarnessPassDown(unittest.TestCase):
    BLOCK = lambda m, t: {"models": {"crew": {"default": m, "tests": t}}}
    CFG = {"models": {"crew": {"default": "claude:top", "tests": "claude:toptests"}},
           "harnesses": {"codex": BLOCK("claude:cdx", "claude:cdxtests"), "gemini": BLOCK("claude:gem", "claude:gemtests")}}

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"; self.proj.mkdir(parents=True)
        for k in ("HARNESS", "DEFAULT_MODEL", "CREW_MODELS", "CREW_EFFORT", "CREW_FALLBACK", "WORKSPACE"):
            p = mock.patch.object(fc_config, k, getattr(fc_config, k)); p.start(); self.addCleanup(p.stop)

    def start(self, *flags, cfg=CFG, **env):
        """(calls herdr got, the tab's env, the model the teammate record names) for `crew start --role tests-001`."""
        fc_config.write_config(cfg); calls = []
        def herdr(*args, **kw):
            calls.append(args)
            return {"agent": {"agent_status": "working"}} if args[:2] == ("agent", "get") else {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}} if args[:2] == ("tab", "create") else {}
        with mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}), mock.patch.object(sys, "argv", ["compose", "crew", "start", "--role", "tests-001", "--brief", "Write the test.", *flags]), \
                mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), mock.patch.object(fc.fc_models, "model_available", return_value=(True, "")), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(time, "sleep"), \
                mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), mock.patch("builtins.print"):
            for k in ("COMPOSE_HARNESS", "CLAUDECODE", "CODEX_THREAD_ID"): os.environ.pop(k, None)
            os.environ.update(env)
            try: fc.main()
            except SystemExit as e: self.exit = e
        tab = next((c for c in calls if c[:2] == ("tab", "create")), ())
        rec = next(fc.fc_crew.crew_dir(self.proj).glob("*.json"), None)
        return calls, [tab[i + 1] for i, v in enumerate(tab) if v == "--env"], rec and json.loads(rec.read_text())["model"]

    def test_the_flag_names_the_block_for_the_tab_and_the_model(self):
        _, envs, model = self.start("--harness", "codex")
        self.assertIn("COMPOSE_HARNESS=codex", envs)
        self.assertEqual(model, "claude:cdxtests")  # the role's own entry in the block, not just its default

    def test_the_env_does_the_same_and_the_flag_wins(self):
        _, envs, model = self.start(COMPOSE_HARNESS="codex")
        self.assertEqual((("COMPOSE_HARNESS=codex" in envs), model), (True, "claude:cdxtests"))
        _, envs, model = self.start("--harness", "codex", COMPOSE_HARNESS="gemini")
        self.assertEqual((("COMPOSE_HARNESS=codex" in envs), ("COMPOSE_HARNESS=gemini" in envs), model), (True, False, "claude:cdxtests"))

    def test_a_flag_without_its_block_exits_before_any_herdr_call(self):
        calls, _, _ = self.start("--harness", "gemini", cfg={k: v for k, v in self.CFG.items() if k != "harnesses"})
        self.assertEqual((calls, hasattr(self, "exit")), ([], True))


class CodexProbe(unittest.TestCase):
    ID, REFUSED = "gpt-6-sol", 'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","message":"m"}}\n'

    def setUp(self):
        self.calls = []
        for p in (mock.patch.object(fc_config, "PROBE_CACHE", Path(tempfile.mkdtemp()) / "probe.json"), mock.patch.object(fc_models.subprocess, "run", self.fake_run)):
            p.start(); self.addCleanup(p.stop)
        fc_models._PROBED.clear(); self.addCleanup(fc_models._PROBED.clear)

    def fake_run(self, argv, **kw):
        self.calls.append(argv)  # a model called "bad" is refused
        bad = "bad" in argv
        return subprocess.CompletedProcess(argv, int(bad), "", self.REFUSED if bad else "")

    def command(self, mid): return ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "-m", mid, "Reply with exactly: ok"]

    def test_bare_codex_is_available_without_a_process_or_a_cache_read(self):
        with mock.patch.object(fc_config, "PROBE_CACHE") as cache:
            for model in ("codex", "codex:"): self.assertEqual(fc_models.model_available(model), (True, "Codex's default model"))
        self.assertEqual((self.calls, cache.mock_calls), ([], []))

    def test_an_id_is_probed_once_and_a_pass_is_cached_for_the_day(self):
        self.assertEqual(fc_models.model_available(f"codex:{self.ID}"), (True, "answered"))
        fc_models._PROBED.clear()  # a new process still finds the day's cache file
        self.assertEqual(fc_models.model_available(f"codex:{self.ID}"), (True, "answered"))
        self.assertEqual(self.calls, [self.command(self.ID)])

    def test_a_refusal_is_returned_and_probed_again(self):
        for _ in range(2): self.assertEqual(fc_models.model_available("codex:bad"), (False, "m"))
        self.assertEqual(self.calls, [self.command("bad")] * 2)

    def test_models_probe_lists_codex_entries_with_their_results(self):
        out = []
        with mock.patch.object(fc_config, "CREW_MODELS", {"a": f"codex:{self.ID}", "b": "codex:bad", "c": "codex"}), mock.patch.object(fc_config, "CREW_FALLBACK", {}), \
                mock.patch("builtins.print", lambda *a: out.append(" ".join(map(str, a)))):
            fc_models.cmd_models(SimpleNamespace(probe=True))
        self.assertEqual(out, [f"codex:{self.ID}: ok (answered)", "codex:bad: unavailable (m)", "codex: ok (Codex's default model)"])

    def test_every_harness_row_is_probed_through_its_own_probe(self):
        row = {**fc_harness.HARNESSES.get("codex", {}), "name": "Other", "probe": mock.Mock(return_value=["other-probe"]), "result": mock.Mock(return_value=(True, "fine"))}
        p = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.dict(fc_harness.HARNESSES, {"other": row}), mock.patch.object(fc_config, "CREW_MODELS", {"a": "other:m"}), mock.patch.object(fc_config, "CREW_FALLBACK", {}):
            self.assertEqual((fc_models.probe_command("other:m"), fc_models.probe_result("other:m", p)), (["other-probe"], (True, "fine")))
            row["probe"].assert_called_once_with("m"); row["result"].assert_called_once_with(p)
            for model in ("other", "other:"): self.assertEqual(fc_models.model_available(model), (True, "Other's default model"))
            self.assertEqual((fc_models.model_available("other:m"), self.calls), ((True, "fine"), [["other-probe"]]))
            out = []
            with mock.patch("builtins.print", lambda *a: out.append(" ".join(map(str, a)))): fc_models.cmd_models(SimpleNamespace(probe=True))
        self.assertEqual(out, ["other:m: ok (fine)"])

class ClaudeOptional(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.bin, self.home, self.co, self.elsewhere = tmp / "bin", tmp / "home", tmp / "co", tmp / "elsewhere"
        for d in (self.bin, self.home, self.co / "bin", self.elsewhere): d.mkdir(parents=True)
        for f in ("bin/compose.py", "SKILL.md"): (self.co / f).write_text("")  # a compose checkout
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)

    def exe(self, name):
        (self.bin / name).write_text("#!/bin/sh\n"); (self.bin / name).chmod(stat.S_IRWXU)

    def link(self, hidden, target):
        link = self.home / hidden / "skills/compose"
        link.parent.mkdir(parents=True); link.symlink_to(target)

    def second(self):
        self.exe("other")
        row = {**fc_harness.HARNESSES.get("codex", {}), "name": "Other", "exe": "other", "home_env": "OTHER_HOME", "home": "~/.other"}
        p = mock.patch.dict(fc_harness.HARNESSES, {"other": row}); p.start(); self.addCleanup(p.stop)

    def test_claude_only_names_are_steps_rows(self):
        self.assertEqual(fc_steps.CLAUDE_ONLY, ("claude", "herdr skill", "i-have-adhd skill", "compose skill link"))
        self.assertLessEqual(set(fc_steps.CLAUDE_ONLY), {r[0] for r in fc_steps.STEPS})

    def test_other_harness_is_codex_only_with_the_binary_and_a_link_to_the_checkout(self):
        self.assertIsNone(fc_steps.other_harness(self.co))
        self.link(".codex", self.co)
        self.assertIsNone(fc_steps.other_harness(self.co))  # the link without codex on PATH
        self.exe("codex")
        self.assertEqual(fc_steps.other_harness(self.co), "codex")

    def test_other_harness_needs_the_link_to_point_at_the_checkout(self):
        self.exe("codex")
        self.assertIsNone(fc_steps.other_harness(self.co))  # codex on PATH, no link
        self.link(".codex", self.elsewhere)
        self.assertIsNone(fc_steps.other_harness(self.co))

    def test_other_harness_is_the_first_installed_row_whose_own_link_is_ok(self):
        self.exe("codex"); self.second(); self.link(".other", self.co)
        self.assertEqual(fc_steps.other_harness(self.co), "other")  # codex has no link
        self.link(".codex", self.co)
        self.assertEqual(fc_steps.other_harness(self.co), "codex")

    def test_required_drops_only_claude_only_rows_and_only_while_other_is_set(self):
        for row in fc_steps.STEPS:
            with self.subTest(row=row[0]):
                self.assertEqual(fc_steps.required(row, None), row[3] == "required")
                self.assertEqual(fc_steps.required(row, "codex"), row[3] == "required" and row[0] not in fc_steps.CLAUDE_ONLY)
        by = {r[0]: r for r in fc_steps.STEPS}
        for name in ("git", "herdr", "pi"): self.assertTrue(fc_steps.required(by[name], "codex"))
        for name in fc_steps.CLAUDE_ONLY: self.assertEqual((fc_steps.required(by[name], "codex"), fc_steps.required(by[name], None)), (False, True))
        for level in ("default", "designer"):
            row = next(r for r in fc_steps.STEPS if r[3] == level)
            self.assertEqual((fc_steps.required(row, None), fc_steps.required(row, "codex")), (False, False))


import fc_finish, fc_prompt, fc_setup
ONLY_CLAUDE, NOT_READY = ("claude", "herdr skill", "i-have-adhd skill", "compose skill link"), "compose is installed but not ready:"


class FinishWithoutClaude(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.bin, self.home, self.co = tmp / "bin", tmp / "home", Path(fc_finish.__file__).resolve().parents[1]
        self.bin.mkdir(); self.home.mkdir(); (tmp / "CONFIG_PATH").write_text("{}")
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home), "HERDR_ENV": "1"}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)
        for name in ("CONFIG_PATH", "AUTH_PATH", "ENV_FILE"):
            q = mock.patch.object(fc_config, name, tmp / name); q.start(); self.addCleanup(q.stop)

    def tools(self, claude=False, codex=False, link=None):
        for name in ("claude",) * claude + ("codex",) * codex:  # Codex is set up with codex and a skill link to this checkout; `link` alone overrides the link
            (self.bin / name).write_text("#!/bin/sh\n"); (self.bin / name).chmod(stat.S_IRWXU)
        if codex if link is None else link:
            (self.home / ".codex/skills").mkdir(parents=True); (self.home / ".codex/skills/compose").symlink_to(self.co)

    def finish(self, *failed):  # (lines, code, the rows listed as not ready)
        lines, code = fc_finish.finish([(n, "failed") for n in failed], True)
        return lines, code, lines[lines.index(NOT_READY) + 1:-1] if NOT_READY in lines else []

    def walk(self):  # the rows whose skip says compose stops working without them
        seen = {}
        with mock.patch.object(fc_prompt, "confirm", lambda *a: False), mock.patch.object(fc_prompt, "step", lambda name, word, lines=(): seen.update({name: list(lines)})):
            fc_setup.walk_steps(self.co, True, True)
        return {n for n, lines in seen.items() if any(l.startswith("compose stops working without it") for l in lines)}

    def test_codex_set_up_without_claude_is_ready_and_points_at_codex(self):
        self.tools(codex=True)
        lines, code, _ = self.finish(*ONLY_CLAUDE)
        self.assertEqual((code, lines[-2:]), (0, ["compose is ready", "next: run codex in a pane and type $compose"]))
        os.environ["HERDR_ENV"] = ""
        self.assertEqual(self.finish()[0][-1], "next: run compose to open herdr")

    def test_the_next_line_names_the_exe_of_the_harness_row_that_is_set_up(self):
        (self.bin / "other").write_text("#!/bin/sh\n"); (self.bin / "other").chmod(stat.S_IRWXU)
        (self.home / ".other/skills").mkdir(parents=True); (self.home / ".other/skills/compose").symlink_to(self.co)
        p = mock.patch.dict(fc_harness.HARNESSES, {"other": {**fc_harness.HARNESSES.get("codex", {}), "name": "Other", "exe": "other", "home_env": "OTHER_HOME", "home": "~/.other"}})
        p.start(); self.addCleanup(p.stop)
        lines, code, _ = self.finish(*ONLY_CLAUDE)
        self.assertEqual((code, lines[-2:]), (0, ["compose is ready", "next: run other in a pane and type $compose"]))

    def test_codex_set_up_without_claude_still_lists_git_and_herdr(self):
        self.tools(codex=True)
        _, code, rows = self.finish("git", "herdr", *ONLY_CLAUDE)
        self.assertEqual((code, [r.split(":")[0] for r in rows]), (0, ["  git", "  herdr"]))

    def test_without_claude_and_codex_not_set_up_finish_stops(self):
        for tools in ({}, {"codex": True, "link": False}, {"link": True}):  # the binary or the link alone is not set up
            self.setUp(); self.tools(**tools); lines, code, _ = self.finish()
            self.assertEqual((code, lines[-1]), (1, fc_finish.CLAUDE_MISSING), tools)

    def test_custom_flow_warns_about_skipping_only_what_compose_needs(self):
        required = {r[0] for r in fc_steps.STEPS if r[3] == "required" and r[5] != "key"}
        self.tools(codex=True)
        self.assertEqual(self.walk(), required - set(ONLY_CLAUDE))
        self.setUp(); self.tools()
        self.assertEqual(self.walk(), required)


CLAUDE_LINE = "MISSING: claude (install: curl -fsSL https://claude.ai/install.sh | bash)"
OPTIONAL = " - optional, Codex is set up"


class DoctorWithoutClaude(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.bin, self.home, self.co = tmp / "bin", tmp / "home", Path(fc_init.__file__).resolve().parents[1]
        self.bin.mkdir(); self.home.mkdir()
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)

    def tools(self, claude=False, codex=False, link=False):  # Codex is set up with the binary and a skill link to this checkout
        for name in ("claude",) * claude + ("codex",) * codex:
            (self.bin / name).write_text("#!/bin/sh\n"); (self.bin / name).chmod(stat.S_IRWXU)
        if link:
            (self.home / ".codex/skills").mkdir(parents=True); (self.home / ".codex/skills/compose").symlink_to(self.co)

    def deps(self):  # (the rows returned, the MISSING lines by row name, the waiting line)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bad = fc_init.check_deps()
        lines = out.getvalue().splitlines()
        return bad, {l[len("MISSING: "):].split(" (")[0].split(":")[0]: l for l in lines if l.startswith("MISSING: ")}, [l for l in lines if l.startswith("waiting on Claude Code:")]

    def test_codex_set_up_without_claude_makes_the_claude_rows_optional(self):
        self.tools(codex=True, link=True)
        bad, lines, waiting = self.deps()
        self.assertFalse(set(bad) & set(ONLY_CLAUDE))
        self.assertIn("git", bad)
        self.assertEqual(lines["claude"], CLAUDE_LINE + " - optional, Codex is set up")
        for name in ("herdr skill", "compose skill link"): self.assertTrue(lines[name].endswith(OPTIONAL), lines[name])
        self.assertEqual(len(waiting), 1)
        self.assertIn("i-have-adhd skill", waiting[0])

    def test_codex_set_up_with_claude_installed_still_makes_its_rows_optional(self):
        self.tools(claude=True, codex=True, link=True)
        bad, lines, waiting = self.deps()
        self.assertFalse(set(bad) & set(ONLY_CLAUDE))
        self.assertNotIn("claude", lines)
        for name in ("herdr skill", "i-have-adhd skill", "compose skill link"): self.assertTrue(lines[name].endswith(OPTIONAL), lines[name])

    def test_the_note_names_whichever_harness_row_is_set_up(self):
        row = {**fc_harness.HARNESSES.get("codex", {}), "name": "Other", "exe": "other", "home_env": "OTHER_HOME", "home": "~/.other"}
        p = mock.patch.dict(fc_harness.HARNESSES, {"other": row}); p.start(); self.addCleanup(p.stop)
        (self.bin / "other").write_text("#!/bin/sh\n"); (self.bin / "other").chmod(stat.S_IRWXU)
        (self.home / ".other/skills").mkdir(parents=True); (self.home / ".other/skills/compose").symlink_to(self.co)
        bad, lines, waiting = self.deps()
        self.assertFalse(set(bad) & set(ONLY_CLAUDE))
        self.assertEqual(lines["claude"], CLAUDE_LINE + " - optional, Other is set up")

class CodexArgv(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        self.tmp, self.results = Path(d.name), Path(d.name, "results")
        git = lambda *a, cwd: subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, text=True).stdout.strip()
        def tree(name):  # a worktree of its own repo, with the dirs it needs: the shared one and its own
            main, wt = self.tmp / f"{name}-main", self.tmp / name
            main.mkdir(); git("init", "-q", cwd=main); git("commit", "-q", "--allow-empty", "-m", "i", cwd=main); git("worktree", "add", "-q", "-b", name, str(wt), cwd=main)
            return wt, [str(Path(git(*a, cwd=wt)).resolve()) for a in (("rev-parse", "--path-format=absolute", "--git-common-dir"), ("rev-parse", "--absolute-git-dir"))]
        (self.cwd, self.dirs), (self.work, self.wdirs) = tree("cwd"), tree("work")
        self.clone = self.tmp / "clone"; self.clone.mkdir(); git("init", "-q", cwd=self.clone)

    def argv(self, model="codex:gpt-6.1-sol", effort="high", prompt="p", cwd=None, work=None):
        return [str(x) for x in fc_harness.crew_argv(model, effort, prompt, "/s/SKILL.md", cwd or self.cwd, self.results, work)]

    def adds(self, argv): return [v for f, v in zip(argv, argv[1:]) if f == "--add-dir"]

    def test_exact_list(self):
        trust = "projects={%s={trust_level=\"trusted\"}}" % json.dumps(os.path.realpath(self.cwd))
        text = json.dumps("p\n\nThe compose skill is the file /s/SKILL.md.")
        self.assertEqual(self.argv(), ["-c", "check_for_update_on_startup=false", "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"', "-s", "workspace-write", "-a", "never",
                                       "-c", "sandbox_workspace_write.network_access=true", "-c", trust, "-c", "developer_instructions=" + text,
                                       "--add-dir", str(self.results), "--add-dir", self.dirs[0], "--add-dir", self.dirs[1]])

    def test_bare_codex_and_no_effort(self):
        for model in ("codex", "codex:"):
            argv = self.argv(model, None)
            self.assertNotIn("-m", argv)
            self.assertFalse([a for a in argv if "model_reasoning_effort" in a])
            self.assertEqual(argv[:4], ["-c", "check_for_update_on_startup=false", "-s", "workspace-write"])

    def test_update_check_is_off_once_and_parses_as_false(self):
        for model, effort in (("codex:gpt-6.1-sol", "high"), ("codex", None), ("codex:x", None), ("codex", "high")):
            argv = self.argv(model, effort)
            self.assertEqual(argv[:2], ["-c", "check_for_update_on_startup=false"], model)
            self.assertEqual(sum(a.startswith("check_for_update_on_startup") for a in argv), 1, model)
            self.assertEqual(tomllib.loads(argv[1]), {"check_for_update_on_startup": False})

    def test_work_goes_last_and_repeats_or_missing_repos_are_left_out(self):
        self.assertEqual(self.adds(self.argv(work=self.work)), [str(self.results), *self.dirs, str(self.work), *self.wdirs])
        self.assertEqual(self.adds(self.argv(work=self.cwd)), [str(self.results), *self.dirs, str(self.cwd)])  # its git dirs are already there
        self.assertEqual(self.adds(self.argv(cwd=self.clone)), [str(self.results), os.path.realpath(self.clone / ".git")])  # one git dir, not two
        self.assertEqual(self.adds(self.argv(work=self.tmp / "gone")), [str(self.results), *self.dirs, str(self.tmp / "gone")])  # not a folder: no git dirs asked
        outside = self.tmp / "plain"; outside.mkdir()
        self.assertEqual(self.adds(self.argv(cwd=outside)), [str(self.results)])  # no git dir to add

    def test_values_parse_as_toml_through_a_symlink(self):
        import tomllib
        link, prompt = self.tmp / "link", 'say "hi"\nthen \\ stop'
        link.symlink_to(self.cwd)
        values = {a.split("=", 1)[0]: tomllib.loads(a) for a in self.argv(cwd=link, prompt=prompt) if a.startswith(("projects=", "developer_instructions="))}
        self.assertEqual(values["projects"], {"projects": {os.path.realpath(self.cwd): {"trust_level": "trusted"}}})
        self.assertEqual(values["developer_instructions"]["developer_instructions"], prompt + "\n\nThe compose skill is the file /s/SKILL.md.")

    def test_no_unfenced_sandbox(self):
        for a in self.argv(work=self.work): self.assertFalse(a == "danger-full-access" or a.startswith("--dangerously"), a)

class CodexStart(unittest.TestCase):
    def start(self, model=None, entry="codex:gpt-6.1-sol", trusted=True, role="tester", signed=True, screen=""):  # signed None: the real sign-in check runs
        proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"; proj.mkdir(parents=True)
        calls, ready = [], mock.Mock()
        skill = proj.parent / "skill"; skill.mkdir(); (skill / "SKILL.md").write_text("v1\n")
        def herdr(*args, **kw):
            calls.append(args)
            return {"agents": []} if args[:2] == ("agent", "list") else {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}} if args[:2] == ("tab", "create") else {"agent": {"agent_status": "working"}} if args[:2] == ("agent", "get") else {}
        a = type("A", (), {"action": "start", "role": role, "title": "t", "brief": "b", "model": model})
        self.calls, self.proj = calls, proj
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), mock.patch.object(fc.fc_herdr, "herdr_login_screen", return_value=screen), \
                mock.patch.object(fc_harness, "signed_in", return_value=signed) if signed is not None else contextlib.nullcontext(), \
                mock.patch.object(fc.fc_herdr, "herdr_ready", ready), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=trusted), mock.patch.object(fc.fc_models, "model_available", return_value=(True, "")), \
                mock.patch.object(fc.fc_mcp, "installed_servers", return_value=[]), mock.patch.object(fc.fc_config, "CREW_MODELS", {role: entry}), mock.patch.object(fc.fc_config, "CREW_EFFORT", {role: "high"}), \
                mock.patch.object(fc.fc_hooks, "SKILL_DIR", skill), mock.patch("builtins.print"):
            fc.fc_crew.cmd_crew(a)
        start = next(c for c in calls if c[:2] == ("agent", "start"))
        return start, ready, json.loads(next(fc.fc_crew.crew_dir(proj).glob("*.json")).read_text()), proj, calls

    def test_codex_role_starts_with_crew_argv_and_never_waits_for_ready(self):
        start, ready, rec, proj, calls = self.start(trusted=False)  # a folder Claude Code does not trust
        self.assertEqual((start[3:10], rec["effort"], "skill" in rec), (("--kind", "codex", "--pane", "w9:p7", "--timeout", "60000", "--"), "high", True))
        argv = fc_harness.crew_argv("codex:gpt-6.1-sol", "high", fc.fc_crew.TRUST_PROMPT, fc.fc_crew.CLI_REPO / "SKILL.md", rec["cwd"], fc.fc_crew.crew_dir(proj).parent, None)
        self.assertEqual([str(x) for x in start[10:]], [str(x) for x in argv])
        self.assertEqual(list(start[10:12]), ["-c", "check_for_update_on_startup=false"])
        ready.assert_not_called()
        self.assertFalse([c for c in calls if any("key" in str(x) for x in c[:2])])  # Codex's "Hooks need review" dialog is never answered

    def test_model_flag_codex_ids_start_as_codex(self):
        for m in ("codex", "codex:x"):
            start, ready = self.start(m)[:2]
            self.assertEqual((start[4], "-m" in start, ready.called), ("codex", m != "codex", False))

    def test_crew_pick_full_accepts_codex_and_refuses_the_rest_exactly(self):
        with mock.patch.object(fc.fc_models, "model_available", return_value=(True, "")):
            for m in ("codex", "codex:x"): self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick_full("tester", m)[0], m)
        def refusal(label, where): return (f"{label} is 'codex/x', but teammate sessions run in Claude Code, pi, or Codex: use claude:<model-id> (e.g. claude:claude-sonnet-5-5), "
                                           f"pi:<provider/model> (e.g. pi:openai/gpt-6-astra), or codex:<model> (e.g. codex:gpt-6.1-sol){where}; OpenCode ids are for implementers only")
        with self.assertRaises(SystemExit) as e: fc.fc_crew.fc_crewpick.crew_pick_full("tester", "codex/x")
        self.assertEqual(str(e.exception), refusal("--model", ""))
        with mock.patch.object(fc.fc_config, "CREW_MODELS", {"tester": "codex/x"}), self.assertRaises(SystemExit) as e: fc.fc_crew.fc_crewpick.crew_pick_full("tester")
        self.assertEqual(str(e.exception), refusal("models.crew.tester", f" in {fc.fc_config.CONFIG_PATH}"))
        with self.assertRaises(SystemExit): fc.fc_crew.fc_crewpick.crew_pick_full("tester", "grok:x")


class SignInCheck(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup); self.tmp = Path(d.name); self.log = self.tmp / "log"; (self.tmp / "bin").mkdir()
        p = mock.patch.dict(os.environ, {"PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}", "CODEX_HOME": str(self.tmp / "ch")}); p.start(); self.addCleanup(p.stop)

    def codex(self, code):
        (self.tmp / "bin" / "codex").write_text(f'#!/bin/sh\necho "$CODEX_HOME|$*" >> {self.log}\nexit {code}\n'); (self.tmp / "bin" / "codex").chmod(stat.S_IRWXU); self.log.write_text("")

    def made(self):  # what a start leaves behind: herdr calls that make or drive a tab, and the teammate's files
        return [c for c in self.calls if c[:2] in (("tab", "create"), ("agent", "start"), ("agent", "prompt"), ("agent", "send-keys"))], [p for p in fc.fc_crew.crew_dir(self.proj).glob("*") if p.suffix in (".json", ".md")]

    def test_signed_in_is_true_only_on_exit_0_after_the_read_only_check_in_the_home(self):
        os.environ.pop("CODEX_HOME")  # the home comes from home_dir, not from the caller's environment
        for code, want in ((0, True), (1, False), (2, False)):
            self.codex(code)
            self.assertIs(fc_harness.signed_in("codex"), want, code)
            self.assertEqual(self.log.read_text(), f"{fc_harness.home_dir('codex')}|login status\n")

    def test_no_codex_executable_is_not_signed_in(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp / "bin")}): self.assertIs(fc_harness.signed_in("codex"), False)  # an empty folder: no codex to run

    def test_the_message_names_the_home_and_the_login_command(self):
        h = fc_harness.home_dir("codex")
        self.assertEqual(fc_harness.login_message("codex"), f"Codex is not signed in for CODEX_HOME={h}. compose never signs in for you: run `CODEX_HOME={h} codex login` yourself, then the same crew start again.")

    def test_not_signed_in_exits_before_a_tab_or_record_for_any_role(self):
        self.codex(1)
        for role in ("tester", "orchestrator"):
            with self.assertRaises(SystemExit) as e: CodexStart.start(self, signed=None, role=role)
            self.assertEqual(str(e.exception), fc_harness.login_message("codex"))
            self.assertEqual(self.made(), ([], []), role)

    def test_signed_in_starts_as_today_and_claude_and_pi_never_ask(self):
        self.codex(0)
        self.assertEqual(CodexStart.start(self, signed=None)[0][4], "codex")
        self.codex(1)
        for entry in ("claude:claude-sonnet-5-5", "pi:x/y"): self.assertEqual(CodexStart.start(self, entry=entry, signed=None)[0][4], entry.split(":")[0])
        self.assertEqual(self.log.read_text(), "")

    def test_a_sign_in_screen_before_the_brief_closes_the_tab_and_registers_nothing(self):
        with self.assertRaises(SystemExit) as e: CodexStart.start(self, screen="Sign in with ChatGPT")
        self.assertEqual(str(e.exception), fc_harness.login_message("codex"))
        self.assertIn(("tab", "close", "w9:t3"), self.calls)
        self.assertEqual(self.made()[1], []); self.assertFalse([c for c in self.calls if c[:2] == ("agent", "prompt")])


class CodexLead(unittest.TestCase):
    def test_more_follows_work_and_leaves_out_none_and_repeats(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        cwd, res, a, b = (Path(d.name, n) for n in ("cwd", "res", "a", "b")); cwd.mkdir()  # a folder outside any repo: no git dirs
        for fn, m in ((fc_harness.crew_argv, "codex:x"), (fc_harness._codex_argv, "x")):
            argv = lambda **kw: [str(x) for x in fn(m, "high", "p", "/s/SKILL.md", cwd, res, **kw)]
            add = lambda *dirs: [x for d in dirs for x in ("--add-dir", str(d))]
            self.assertEqual(argv(more=()), argv())
            self.assertEqual(argv(more=(a, b))[:2], ["-c", "check_for_update_on_startup=false"])
            self.assertEqual(argv(more=(a, b)), argv() + add(a, b))
            self.assertEqual(argv(more=(a, None, a, b)), argv() + add(a, b))
            self.assertEqual(argv(work=b, more=(a,)), argv(work=b) + add(a))
            self.assertFalse([x for x in argv(more=(a, b)) if x == "danger-full-access" or x.startswith("--dangerously")])

    def test_only_a_codex_team_lead_gets_the_folders_its_commands_write_to(self):
        with mock.patch.object(fc.fc_worktree, "common_dir", return_value=Path("/r/.git")), mock.patch.object(fc.fc_worktree, "use_treehouse") as treehouse:
            def check(role, entry, *more, pool=True):
                treehouse.return_value = pool
                start, _, rec, proj, _ = CodexStart.start(self, entry, role=role)
                if not entry.startswith("codex"): return self.assertNotIn("--add-dir", start)
                want = fc_harness.crew_argv(entry, rec["effort"], fc.fc_crew.TRUST_PROMPT, fc.fc_crew.CLI_REPO / "SKILL.md", rec["cwd"], fc.fc_crew.crew_dir(proj).parent, None, more)
                self.assertEqual([str(x) for x in start[10:]], [str(x) for x in want], (role, pool))
            check("orchestrator", "claude:claude-sonnet-5-5")
            check("orchestrator", "codex:x", fc.fc_config.CONFIG_PATH.parent, Path("/r/.treehouse"))
            check("orchestrator", "codex:x", fc.fc_config.CONFIG_PATH.parent, pool=False)  # no treehouse, no slot pool
            for role in ("planner", "tester"): check(role, "codex:x")


class HarnessTable(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.bin, self.home = tmp / "bin", tmp / "home"
        for d in (self.bin, self.home): d.mkdir()
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)

    def exe(self, name, mode=stat.S_IRWXU):
        (self.bin / name).write_text("#!/bin/sh\n"); (self.bin / name).chmod(mode)

    def test_harness_of_and_model_id_read_only_a_key_that_has_fields(self):
        for m, want in (("codex", ("codex", None)), ("codex:", ("codex", None)), ("codex:gpt-6-sol", ("codex", "gpt-6-sol")), ("codex:a:b", ("codex", "a:b")),
                        ("gemini:x", (None, None)), ("grok", (None, None)), ("grok:x", (None, None)), ("codex/gpt-6-sol", (None, None)), ("codexx:y", (None, None)),
                        ("Codex:x", (None, None)), ("claude:opus", (None, None)), ("pi:x/y", (None, None)), ("", (None, None)), (None, (None, None))):
            self.assertEqual((fc_harness.harness_of(m), fc_harness.model_id(m)), want, m)

    def test_a_row_with_fields_is_named_by_its_key_and_listed_in_table_order(self):
        self.exe("other")
        with mock.patch.dict(fc_harness.HARNESSES, {"other": {"name": "Other", "exe": "other", "home_env": "OTHER_HOME", "home": "~/.other"}}):
            self.assertEqual((fc_harness.harness_of("other:m"), fc_harness.model_id("other:m"), fc_harness.rows(), fc_harness.installed()), ("other", "m", ["codex", "other"], ["other"]))
            self.exe("codex"); self.assertEqual(fc_harness.installed(), ["codex", "other"])
            os.environ["OTHER_HOME"] = str(self.home / "o"); self.assertEqual(fc_harness.skills_dir("other"), self.home / "o/skills")

    def test_rows_is_codex_and_installed_needs_an_executable_codex_on_path(self):
        self.assertEqual((fc_harness.rows(), fc_harness.installed()), (["codex"], []))
        self.exe("codex", stat.S_IRUSR | stat.S_IWUSR); self.assertEqual(fc_harness.installed(), [])
        self.exe("codex"); self.assertEqual(fc_harness.installed(), ["codex"])

    def test_skills_dir_prefers_a_non_empty_codex_home_and_reads_home_at_call_time(self):
        self.assertEqual(fc_harness.skills_dir("codex"), self.home / ".codex/skills")
        os.environ["CODEX_HOME"] = ""; self.assertEqual(fc_harness.skills_dir("codex"), self.home / ".codex/skills")
        os.environ["HOME"] = str(self.home / "moved"); self.assertEqual(fc_harness.skills_dir("codex"), self.home / "moved/.codex/skills")
        os.environ["CODEX_HOME"] = str(self.home / "elsewhere"); self.assertEqual(fc_harness.skills_dir("codex"), self.home / "elsewhere/skills")


class HookFields(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        p = mock.patch.dict(os.environ, {"HOME": str(self.home)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)

    def digest(self, event, matcher, hooks):
        o = {"event_name": event, "hooks": hooks}
        if matcher: o["matcher"] = matcher
        return "sha256:" + hashlib.sha256(json.dumps(o, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def test_hash_is_the_sha256_of_the_sorted_compact_json_with_defaults_filled_in(self):
        h = fc_harness._codex_hash
        c = {"type": "command", "command": '"/c/bin/compose" guard'}
        self.assertEqual(h("PreToolUse", {"matcher": "Bash", "hooks": [{**c, "timeout": 10}]}), self.digest("pre_tool_use", "Bash", [{**c, "timeout": 10, "async": False}]))
        self.assertEqual(h("UserPromptSubmit", {"hooks": [c]}), self.digest("user_prompt_submit", None, [{**c, "timeout": 600, "async": False}]))
        full = {**c, "timeout": 5, "async": True, "statusMessage": "checking"}
        self.assertEqual(h("SessionStart", {"matcher": "compact", "hooks": [full, c]}), self.digest("session_start", "compact", [full, {**c, "timeout": 600, "async": False}]))

    def test_home_dir_prefers_a_non_empty_codex_home_and_reads_the_environment_at_call_time(self):
        self.assertEqual(fc_harness.home_dir("codex"), self.home / ".codex")
        os.environ["CODEX_HOME"] = ""; self.assertEqual(fc_harness.home_dir("codex"), self.home / ".codex")
        os.environ["HOME"] = str(self.home / "moved"); self.assertEqual(fc_harness.home_dir("codex"), self.home / "moved/.codex")
        os.environ["CODEX_HOME"] = str(self.home / "elsewhere")
        self.assertEqual((fc_harness.home_dir("codex"), fc_harness.skills_dir("codex")), (self.home / "elsewhere", self.home / "elsewhere/skills"))

    def test_hook_groups_is_empty_for_a_row_without_hooks_and_follows_any_row_with_them(self):
        self.assertEqual((fc_harness.hook_groups("grok", "/c"), fc_harness.hook_groups("gemini", "/c")), ({}, {}))
        with mock.patch.dict(fc_harness.HARNESSES, {"other": {"name": "Other", "hooks": (("Ev", None, "a"), ("Ev", "m", "b"))}, "bare": {"name": "Bare"}}):
            self.assertEqual(fc_harness.hook_groups("bare", "/c"), {})
            self.assertEqual(fc_harness.hook_groups("other", "/c"), {"Ev": [{"hooks": [{"type": "command", "command": '"/c/bin/compose" a', "timeout": 10}]},
                                                                      {"matcher": "m", "hooks": [{"type": "command", "command": '"/c/bin/compose" b', "timeout": 10}]}]})


class HooksFile(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        p = mock.patch.dict(os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.home / "codex")}); p.start(); self.addCleanup(p.stop)
        (self.home / "codex").mkdir()
        self.file, self.want = self.home / "codex/hooks.json", self.groups("/c")

    def groups(self, co):
        def g(m, sub): return {**({"matcher": m} if m else {}), "hooks": [{"type": "command", "command": f'"{co}/bin/compose" {sub}', "timeout": 10}]}
        return {"PreToolUse": [g("Write|Edit|MultiEdit|NotebookEdit", "guard"), g("Bash", "guard")], "UserPromptSubmit": [g(None, "skillcheck")], "SessionStart": [g("compact", "reenter")]}

    def put(self, doc): self.file.write_text(doc if isinstance(doc, str) else json.dumps(doc))

    def test_a_missing_file_gets_the_four_groups_and_a_second_call_writes_nothing(self):
        self.assertEqual([str(p) for p in fc_links.write_hooks("codex", "/c")], [str(self.file)])
        self.assertEqual(json.loads(self.file.read_text()), {"hooks": self.want})
        self.assertIn('\n  "hooks": {', self.file.read_text())
        before = self.file.read_bytes()
        self.assertEqual((fc_links.write_hooks("codex", "/c"), self.file.read_bytes()), ([], before))
        self.assertEqual([p.name for p in (self.home / "codex").iterdir()], ["hooks.json"])

    def test_herdrs_group_keeps_its_index_and_compose_appends_after_it(self):
        herdr = {"hooks": [{"type": "command", "command": "herdr hook session-start"}]}
        self.put({"theme": "dark", "hooks": {"SessionStart": [herdr], "Stop": [herdr]}})
        self.assertEqual(len(fc_links.write_hooks("codex", "/c")), 1)
        doc = json.loads(self.file.read_text())
        self.assertEqual(doc["hooks"], {**self.want, "SessionStart": [herdr, *self.want["SessionStart"]], "Stop": [herdr]})
        self.assertEqual(doc["theme"], "dark")

    def test_a_compose_group_of_another_checkout_is_replaced_at_its_index_and_an_equal_one_is_kept(self):
        herdr = {"hooks": [{"type": "command", "command": "herdr hook"}]}
        old = self.groups("/old")
        self.put({"hooks": {"UserPromptSubmit": [herdr, old["UserPromptSubmit"][0]], "SessionStart": [self.want["SessionStart"][0], herdr], "PreToolUse": [old["PreToolUse"][1], old["PreToolUse"][0]]}})
        fc_links.write_hooks("codex", "/c")
        doc = json.loads(self.file.read_text())["hooks"]
        self.assertEqual(doc["UserPromptSubmit"], [herdr, *self.want["UserPromptSubmit"]])
        self.assertEqual(doc["SessionStart"], [*self.want["SessionStart"], herdr])
        self.assertEqual(doc["PreToolUse"], [self.want["PreToolUse"][1], self.want["PreToolUse"][0]])

    def test_invalid_json_exits_naming_the_file_and_writes_nothing(self):
        self.put("{not json")
        with self.assertRaises(SystemExit) as e: fc_links.write_hooks("codex", "/c")
        self.assertIn(str(self.file), str(e.exception))
        self.assertEqual((self.file.read_text(), sorted(p.name for p in (self.home / "codex").iterdir())), ("{not json", ["hooks.json"]))

    def test_a_row_without_hooks_writes_nothing(self):
        row = {"name": "Other", "home_env": "CODEX_HOME", "home": "~/.codex", "hooks_file": "hooks.json"}
        with mock.patch.dict(fc_harness.HARNESSES, {"other": row}): self.assertEqual(fc_links.write_hooks("other", "/c"), [])
        self.assertFalse(self.file.exists())


class HookTrust(unittest.TestCase):
    SNAKE = {"PreToolUse": "pre_tool_use", "UserPromptSubmit": "user_prompt_submit", "SessionStart": "session_start"}
    def setUp(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.home, self.bin = tmp / "home", tmp / "bin"
        (self.home / "codex").mkdir(parents=True); self.bin.mkdir()
        p = mock.patch.dict(os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.home / "codex"), "PATH": str(self.bin)}); p.start(); self.addCleanup(p.stop)
        self.file, self.trust = self.home / "codex/hooks.json", self.home / "codex/config.toml"
        g = lambda m, sub: {**({"matcher": m} if m else {}), "hooks": [{"type": "command", "command": f'"/c/bin/compose" {sub}', "timeout": 10}]}
        self.put({"PreToolUse": [g("Write|Edit|MultiEdit|NotebookEdit", "guard"), g("Bash", "guard")], "UserPromptSubmit": [g(None, "skillcheck")], "SessionStart": [g("compact", "reenter")]})

    def put(self, hooks): self.file.write_text(json.dumps({"hooks": hooks}))
    def hooks(self): return json.loads(self.file.read_text())["hooks"]
    def records(self):
        return {f"{self.file}:{self.SNAKE[ev]}:{i}:{j}": fc_harness._codex_hash(ev, g)
                for ev, gs in self.hooks().items() for i, g in enumerate(gs) for j in range(len(g["hooks"]))}

    def state(self): return {k: v["trusted_hash"] for k, v in tomllib.loads(self.trust.read_text())["hooks"]["state"].items()}

    def test_four_records_are_written_with_the_hashes_and_a_second_call_writes_nothing(self):
        self.assertEqual([str(p) for p in fc_links.trust_hooks("codex", "/c")], [str(self.trust)])
        self.assertEqual(self.state(), self.records())
        self.assertIn(f"[hooks.state.{json.dumps(f'{self.file}:pre_tool_use:1:0')}]", self.trust.read_text())
        before = self.trust.read_bytes(); self.assertEqual((fc_links.trust_hooks("codex", "/c"), self.trust.read_bytes()), ([], before))

    def test_other_content_and_herdrs_record_stay_and_herdrs_group_gets_none(self):
        herdr = {"hooks": [{"type": "command", "command": "herdr hook session-start"}]}; self.put({**self.hooks(), "SessionStart": [herdr, *self.hooks()["SessionStart"]]})
        hk = f"{self.file}:session_start:0:0"
        old = f'# mine\nmodel = "x"\n\n[projects."{self.home}"]\ntrust_level = "trusted"  # keep\n\n[hooks.state.{json.dumps(hk)}]\ntrusted_hash = "sha256:herdr"\n'
        self.trust.write_text(old)
        fc_links.trust_hooks("codex", "/c")
        self.assertTrue(self.trust.read_text().startswith(old))
        self.assertEqual(self.state(), {**{k: v for k, v in self.records().items() if k != hk}, hk: "sha256:herdr"})

    def test_a_wrong_hash_has_only_its_trusted_hash_line_replaced(self):
        key = f"{self.file}:pre_tool_use:0:0"
        old = f'# top\n[hooks.state.{json.dumps(key)}]\nenabled = true\ntrusted_hash = "sha256:bad"\n\n[other]\ntrusted_hash = "keep"\n'
        self.trust.write_text(old)
        fc_links.trust_hooks("codex", "/c")
        self.assertTrue(self.trust.read_text().startswith(old.replace('"sha256:bad"', json.dumps(self.records()[key]))))
        self.assertEqual((self.state(), tomllib.loads(self.trust.read_text())["other"]), (self.records(), {"trusted_hash": "keep"}))

    def test_invalid_toml_or_a_header_not_found_as_text_exits_naming_the_file_and_writes_nothing(self):
        for text in ("[unclosed", f'[hooks.state]\n{json.dumps(f"{self.file}:pre_tool_use:0:0")} = {{ trusted_hash = "sha256:bad" }}\n'):
            self.trust.write_text(text)
            with self.assertRaises(SystemExit) as e: fc_links.trust_hooks("codex", "/c")
            self.assertIn(str(self.trust), str(e.exception))
            self.assertEqual(self.trust.read_text(), text)

    def test_hooks_ok_names_the_first_missing_hook_or_record_and_passes_after_both_writes(self):
        self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (True, ""))
        (self.bin / "codex").write_text("#!/bin/sh\n"); (self.bin / "codex").chmod(stat.S_IRWXU)
        hooks = self.hooks(); self.file.unlink()
        ok, why = fc_checks.hooks_ok("codex", "/c"); self.assertFalse(ok); self.assertIn("hooks.json", why); self.assertTrue(why.endswith("lacks compose's guard hook"), why)
        self.put(hooks)
        ok, why = fc_checks.hooks_ok("codex", "/c"); self.assertFalse(ok); self.assertIn("config.toml", why); self.assertTrue(why.endswith("has no trust record for compose's guard hook"), why)
        fc_links.trust_hooks("codex", "/c"); self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (True, ""))
        self.trust.write_text(self.trust.read_text().replace(self.records()[f"{self.file}:session_start:0:0"], "sha256:bad"))
        self.assertTrue(fc_checks.hooks_ok("codex", "/c")[1].endswith("has no trust record for compose's reenter hook"))
        with mock.patch.dict(fc_harness.HARNESSES, {"codex": {k: v for k, v in fc_harness.HARNESSES["codex"].items() if k != "hooks"}}): self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (True, ""))


class CodexGuard(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        fc.fc_core.sh(["git", "init", "-q"], self.repo)
        d = Path(tempfile.mkdtemp())
        (d / "skill").mkdir(); (d / "skill" / "SKILL.md").write_text("v1\n")
        for p in (mock.patch.object(fc.fc_hooks, "SKILL_DIR", d / "skill"), mock.patch.object(fc.fc_hooks, "SKILL_SEEN", d / "seen")):
            p.start(); self.addCleanup(p.stop)

    def run_hook(self, cmd, ev, role):
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_ROLE"} | ({"COMPOSE_ROLE": role} if role else {})
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(ev))), mock.patch.object(sys, "stdout", out):
            cmd(None)
        return out.getvalue()

    def denied(self, tool, ti, role="orchestrator"):
        out = self.run_hook(fc.fc_hooks.cmd_guard, {"tool_name": tool, "cwd": str(self.repo), "tool_input": ti}, role)
        return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else None

    def patch(self, *lines):
        return {"command": "*** Begin Patch\n" + "\n".join(lines) + "\n*** End Patch"}

    def test_apply_patch_headers_are_denied_when_relative_paths_land_in_the_repo(self):
        self.assertEqual(self.denied("apply_patch", self.patch("*** Add File: a.go", "+x")), fc.fc_hooks.guard_decision("Write", str(self.repo / "a.go"), "orchestrator"))
        for head in ("*** Update File: src/a.go", "*** Delete File: src/a.go", "*** Update File: .compose/shop/scratch/a\n*** Move to: src/b.go",
                     "*** Add File: .compose/shop/scratch/a\n+x\n*** Update File: src/a.go\n@@"):
            self.assertIn("never edit code", self.denied("apply_patch", self.patch(head)) or "", head)

    def test_apply_patch_in_scratch_and_bash_without_a_target_are_allowed(self):
        self.assertIsNone(self.denied("apply_patch", self.patch("*** Add File: .compose/shop/scratch/x.json", "+{}")))
        self.assertIsNone(self.denied("Bash", {"command": "ls 2>&1"}))
        self.assertIsNone(self.denied("Bash", {"command": "ls -la && git status"}))
        self.assertIsNone(self.denied("Bash", {"command": "echo hi > .compose/shop/scratch/x"}))

    def test_bash_redirection_targets_are_denied(self):
        for cmd in ("echo hi > a.txt", "echo hi >> a.txt", "cat x 2>&1 > a.txt", "echo hi >a.txt", "ls | tee x > src/a.txt"):
            self.assertIn("never edit code", self.denied("Bash", {"command": cmd}) or "", cmd)

    def test_other_roles_and_claude_payloads(self):
        for role in ("crew", None, ""):
            self.assertIsNone(self.denied("apply_patch", self.patch("*** Add File: a.go"), role), role)
            self.assertIsNone(self.denied("Bash", {"command": "echo hi > a.txt"}, role), role)
        self.assertIn("never edit code", self.denied("Write", {"file_path": str(self.repo / "a.go")}))
        self.assertIsNone(self.denied("Write", {"file_path": str(self.repo / "a.go")}, "crew"))
        self.assertIsNone(self.denied("Read", {"file_path": str(self.repo / "a.go")}))

    def test_edit_paths_resolves_relative_paths_against_cwd(self):
        ev = {"tool_name": "apply_patch", "cwd": str(self.repo), "tool_input": self.patch("*** Add File: a", "*** Update File: b", "*** Delete File: /x/c", "*** Move to: d")}
        self.assertEqual({os.path.realpath(p) for p in fc.fc_hooks.edit_paths(ev)}, {os.path.realpath(self.repo / n) for n in "abd"} | {os.path.realpath("/x/c")})

    def test_a_sigil_prompt_enrolls_the_session_like_the_slash_command(self):
        def seen(prompt):
            self.run_hook(fc.fc_hooks.cmd_skillcheck, {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": prompt}, None)
            return fc.fc_hooks.SKILL_SEEN.exists() and (fc.fc_hooks.SKILL_SEEN / "s.json").exists()
        for p in ("$composer", "$compose:yolo", "compose-yolo", "go $compose"):
            self.assertFalse(seen(p), p)
        for p in ("$compose-yolo", "$compose", "$compose ship it"):
            self.assertTrue(seen(p), p)
            (fc.fc_hooks.SKILL_SEEN / "s.json").unlink()


class CodexHooksRow(unittest.TestCase):
    NAME = "Codex hooks"
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.bin, self.home, self.co = tmp / "bin", tmp / "home", tmp / "co"
        for d in (self.bin, self.home, self.co): d.mkdir()
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)
        self.file, self.trust = self.home / ".codex/hooks.json", self.home / ".codex/config.toml"
        names = [r[0] for r in fc_steps.STEPS]
        self.row = fc_steps.STEPS[names.index(self.NAME)]
        self.assertEqual(names[names.index(self.NAME) - 1], "Codex commands")

    def tool(self, name="codex"): (self.bin / name).write_text("#!/bin/sh\n"); (self.bin / name).chmod(stat.S_IRWXU)
    def run_row(self, row=None, dry=False): return fc_steprun.run_step(row or self.row, self.co, True, False, False, dry=dry)
    def records(self): return tomllib.loads(self.trust.read_text())["hooks"]["state"]

    def test_without_codex_the_check_passes_and_run_step_does_nothing(self):
        self.assertEqual(fc_steps.check(self.row, self.co), (True, ""))
        self.assertEqual(self.run_row(), ("ok", ""))

    def test_run_step_writes_the_hooks_file_and_four_records_then_finds_it_ok(self):
        self.tool(); self.assertFalse(fc_steps.check(self.row, self.co)[0])
        self.assertEqual(self.run_row(), ("done", "~/.codex/hooks.json; ~/.codex/config.toml"))
        self.assertEqual(sorted(json.loads(self.file.read_text())["hooks"]), ["PreToolUse", "SessionStart", "UserPromptSubmit"])
        self.assertEqual(len(self.records()), 4)
        self.assertEqual((fc_steps.check(self.row, self.co), self.run_row()), ((True, ""), ("ok", "")))

    def test_a_hooks_file_without_records_only_gets_the_records(self):
        self.tool(); fc_links.write_hooks("codex", self.co)
        self.assertEqual(self.run_row(), ("done", "~/.codex/config.toml"))
        self.assertTrue(fc_steps.check(self.row, self.co)[0])

    def test_every_harness_row_with_hooks_gets_its_own_hooks_row_after_its_commands_row(self):
        self.tool("other"); self.addCleanup(reload_steps)
        p = mock.patch.dict(fc_harness.HARNESSES, {"other": {**fc_harness.HARNESSES["codex"], "name": "Other", "exe": "other", "home_env": "OTHER_HOME", "home": "~/.other"}})
        p.start(); self.addCleanup(p.stop)
        reload_steps()
        names = [r[0] for r in fc_steps.STEPS]; i = names.index("Other hooks")
        self.assertEqual(names[i - 1], "Other commands")
        self.assertEqual(self.run_row(fc_steps.STEPS[i])[0], "done")
        self.assertTrue((self.home / ".other/hooks.json").exists() and (self.home / ".other/config.toml").exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_doctor_lists_the_missing_hooks_file_as_optional(self):
        self.tool(); out = io.StringIO()
        with contextlib.redirect_stdout(out): fc_init.check_deps()
        self.assertIn("MISSING: Codex hooks: ~/.codex/hooks.json lacks compose's guard hook (install: compose setup) - optional\n", out.getvalue())


class ReservedNames(unittest.TestCase):
    apply, exits = HarnessBlock.apply, HarnessBlock.exits
    URL = {"base_url": "http://localhost:11434/v1"}

    def setUp(self):
        HarnessBlock.setUp(self)
        for obj, k, v in ((fc_config, "PROVIDERS", fc_config.PROVIDERS), (fc_harness, "RESERVED", ("codex", "gemini", "grok"))):
            p = mock.patch.object(obj, k, v); p.start(); self.addCleanup(p.stop)

    def reserved(self, name, names="codex, gemini, grok"):
        return f"providers.{name} is reserved: {names} name harnesses, not providers"

    def test_a_harness_name_is_refused(self):
        for name in ("codex", "gemini", "grok"):
            self.exits({"providers": {name: self.URL}}, self.reserved(name))

    def test_reserved_is_checked_before_the_base_url(self):
        self.exits({"providers": {"codex": {}}}, self.reserved("codex"))

class CodexPreflight(unittest.TestCase):
    def setUp(self):
        self.avail = mock.Mock(return_value=(True, "answered"))
        self.turn = mock.Mock(return_value=("s", "OK", 0, {"cost": 0.0}))
        for obj, k, v in ((fc.fc_models, "model_available", self.avail), (fc.fc_implement, "implementer", self.turn), (fc.fc_core, "say", mock.Mock())):
            p = mock.patch.object(obj, k, v); p.start(); self.addCleanup(p.stop)

    def test_a_codex_id_is_probed_not_run(self):
        self.turn.side_effect = AssertionError("implementer")
        fc.fc_run.preflight({"codex:gpt-6-sol"})
        self.avail.assert_called_once_with("codex:gpt-6-sol")

    def test_bare_codex_is_probed_too(self):
        fc.fc_run.preflight({"codex"})
        self.avail.assert_called_once_with("codex")
        self.turn.assert_not_called()

    def test_a_failing_probe_stops_the_run_and_lists_the_id(self):
        self.avail.side_effect = lambda m: (m == "codex:good", "model is not available")
        with self.assertRaises(SystemExit) as cm:
            fc.fc_run.preflight({"codex:good", "codex:gpt-6-sol"})
        msg = str(cm.exception)
        self.assertIn("preflight failed; nothing was cloned or run.", msg)
        self.assertIn("codex:gpt-6-sol: model is not available", [l.strip() for l in msg.splitlines()])
        self.assertNotIn("codex:good:", msg)

class CodexEvents(unittest.TestCase):
    def ev(self, kind, item=None, **more):
        return json.dumps({"type": kind, **({"item": item} if item else {}), **more})

    def done(self, kind, **item):
        return self.ev("item.completed", {"id": "i", "type": kind, **item})

    def turn(self, tin, tout):
        return self.ev("turn.completed", usage={"input_tokens": tin, "cached_input_tokens": 1, "output_tokens": tout})

    def parse(self, *lines):
        return fc_harness._codex_events("\n".join(lines) + "\n")

    def test_the_sample_gives_the_last_message_one_tool_and_the_usage(self):
        sample = self.parse(
            '{"type":"thread.started","thread_id":"t"}', '{"type":"turn.started"}',
            self.done("agent_message", text="I will run it.\n"),
            self.ev("item.started", {"id": "i", "type": "command_execution", "status": "in_progress"}),
            self.done("command_execution", command="echo hi", exit_code=0, status="completed"),
            self.done("agent_message", text="ok"),
            '{"type":"turn.completed","usage":{"input_tokens":29828,"cached_input_tokens":27008,"cache_write_input_tokens":0,"output_tokens":50,"reasoning_output_tokens":0}}')
        self.assertEqual(sample, ("ok", 1, {"tokens_in": 29828, "tokens_out": 50, "cost": 0.0}))

    def test_two_turns_sum(self):
        self.assertEqual(self.parse(self.turn(10, 1), self.turn(5, 2))[2], {"tokens_in": 15, "tokens_out": 3, "cost": 0.0})

    def test_no_events_give_zeros_and_an_empty_reply(self):
        for text in ("", "\n\n", self.ev("turn.started") + "\n"):
            self.assertEqual(fc_harness._codex_events(text), ("", 0, {"tokens_in": 0, "tokens_out": 0, "cost": 0.0}))

    def test_non_json_lines_and_unknown_events_are_skipped(self):
        got = self.parse("Reading additional input from stdin...", "", self.done("agent_message", text="hi"), "{not json",
                         self.ev("something.new", {"id": "i", "type": "command_execution"}), self.ev("mystery"), self.turn(3, 4))
        self.assertEqual(got, ("hi", 0, {"tokens_in": 3, "tokens_out": 4, "cost": 0.0}))

    def test_every_item_except_messages_and_reasoning_is_a_tool(self):
        got = self.parse(self.done("reasoning", text="hm"), self.done("command_execution"), self.done("file_change"),
                         self.done("mcp_tool_call"), self.done("agent_message", text="x"))
        self.assertEqual(got[:2], ("x", 3))

    def test_the_reply_is_the_last_message_even_before_trailing_tools(self):
        self.assertEqual(self.parse(self.done("agent_message", text="a"), self.done("agent_message", text="b"), self.done("command_execution"))[:2], ("b", 1))
        self.assertEqual(self.parse(self.done("reasoning", text="r"), self.done("command_execution"))[0], "")

class CodexOnce(unittest.TestCase):
    HEAD = ["codex", "-a", "never", "exec", "--json", "--ephemeral", "--skip-git-repo-check"]
    WRITE = ["-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]

    def setUp(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        git = lambda *a, cwd: subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, text=True).stdout.strip()
        main, self.wt, self.clone = self.tmp / "main", self.tmp / "wt", self.tmp / "clone"
        for f in (main, self.clone): f.mkdir(); git("init", "-q", cwd=f)
        git("commit", "-q", "--allow-empty", "-m", "i", cwd=main); git("worktree", "add", "-q", "-b", "wt", str(self.wt), cwd=main)
        self.dirs = [str(Path(git(*a, cwd=self.wt)).resolve()) for a in (("rev-parse", "--path-format=absolute", "--git-common-dir"), ("rev-parse", "--absolute-git-dir"))]
        self.ledger = fc.fc_core.Ledger()
        p = mock.patch.object(fc.fc_core, "LEDGER", self.ledger); p.start(); self.addCleanup(p.stop)

    def argv(self, model="codex:gpt-6-sol", cwd=None, **kw): return [str(x) for x in fc_harness.once_command(model, cwd or self.wt, "do it", **kw)]

    def done(self, text, code=0, err=""):
        out = [{"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": text}}, {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}]
        return subprocess.CompletedProcess([], code, "\n".join(map(json.dumps, out)) + "\n", err)

    def test_one_git_dir_for_a_clone_and_none_outside_git(self):
        self.assertEqual(self.argv(cwd=self.clone)[-3:], ["--add-dir", os.path.realpath(self.clone / ".git"), "do it"])
        self.assertNotIn("--add-dir", self.argv(cwd=self.tmp / "gone"))

    def test_read_only_has_no_write_access_and_no_dirs(self):
        self.assertEqual(self.argv(read_only=True), self.HEAD + ["-m", "gpt-6-sol", "-s", "read-only", "do it"])

    def test_no_unfenced_sandbox_and_approval_is_top_level(self):
        for argv in (self.argv(), self.argv(read_only=True)):
            self.assertLess(argv.index("-a"), argv.index("exec"))
            for a in argv: self.assertFalse(a == "danger-full-access" or a.startswith("--dangerously"), a)

    def test_result_returns_the_tail_and_adds_a_free_ledger_row(self):
        usage = {"tokens_in": 7, "tokens_out": 3, "cost": 0.0}
        self.assertEqual(fc_harness.once_result("codex:x", self.done("ok")), (None, "ok", 0, usage))
        self.assertEqual(fc_harness.once_result("codex:x", self.done("a" * 1600 + "tail"))[1], "a" * 1496 + "tail")
        self.assertEqual(self.ledger.rows, {"codex-subscription": {"calls": 2, "tokens_in": 14, "tokens_out": 6, "cost": 0.0, "priced": True}})

    def test_failing_exit_without_a_reply_raises_but_with_a_reply_returns(self):
        with self.assertRaises(RuntimeError) as cm: fc_harness.once_result("codex:x", subprocess.CompletedProcess([], 1, "", "ERROR: boom\n"))
        self.assertEqual(str(cm.exception), "Codex failed: boom")
        self.assertEqual(self.ledger.rows, {})
        self.assertEqual(fc_harness.once_result("codex:x", self.done("partial", code=1, err="ERROR: late\n"))[1], "partial")


class CodexImplementer(unittest.TestCase):
    RESULT = (None, "ok", 1, {"tokens_in": 1, "tokens_out": 2, "cost": 0.0})

    def setUp(self):
        self.run, self.cmd, self.res = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", "")), mock.Mock(return_value=["codex", "x"]), mock.Mock(return_value=self.RESULT)
        self.other = {k: mock.Mock(return_value=("s", "r", 0, {})) for k in ("opencode", "pi_turn", "claude_once")}
        patches = [(fc.fc_proc, "run", self.run), (fc.fc_claude, "headless_env", mock.Mock(return_value={"H": "1"})), (fc.fc_herdr, "herdr_env", mock.Mock(return_value=None)),
                   (fc_harness, "once_command", self.cmd), (fc_harness, "once_result", self.res), (fc.fc_claude, "claude_once", self.other["claude_once"]),
                   (fc.fc_models, "opencode", self.other["opencode"]), (fc.fc_models, "pi_turn", self.other["pi_turn"])]
        for obj, k, v in patches: p = mock.patch.object(obj, k, v); p.start(); self.addCleanup(p.stop)

    def test_codex_turn_runs_once_with_the_row_argv_and_returns_its_result(self):
        self.assertEqual(fc.fc_implement.implementer("/w", "codex:gpt-6-sol", "do it", timeout=50), self.RESULT)
        self.cmd.assert_called_once_with("codex:gpt-6-sol", "/w", "do it")
        self.run.assert_called_once_with(["codex", "x"], cwd="/w", env={"H": "1"}, timeout=50)
        self.assertEqual(self.res.call_args.args[0], "codex:gpt-6-sol"); self.assertIs(self.res.call_args.args[1], self.run.return_value)

    def test_default_timeout_is_900_and_bare_codex_takes_the_same_path(self):
        for model in ("codex:gpt-6-sol", "codex"):
            self.run.reset_mock(); self.assertEqual(fc.fc_implement.implementer("/w", model, "do it"), self.RESULT)
            self.assertEqual(self.run.call_args.kwargs["timeout"], 900)
            self.assertEqual(self.cmd.call_args.args[0], model)

    def test_prompt_is_scrubbed_and_a_fake_402_comes_first(self):
        with mock.patch.object(fc.fc_core, "SECRET_VALUES", {"s3cret-value-xyz"}):
            fc.fc_implement.implementer("/w", "codex:gpt-6-sol", "use s3cret-value-xyz now")
            self.assertEqual(self.cmd.call_args.args[2], "use <redacted> now")
            self.run.reset_mock()
            with mock.patch.dict(os.environ, {"COMPOSE_FAKE_402": "openrouter"}), self.assertRaises(fc.fc_models.PaymentRequired): fc.fc_implement.implementer("/w", "codex:gpt-6-sol", "x")
        self.run.assert_not_called()

    def test_a_codex_fallback_of_a_pi_model_routes_here(self):
        with mock.patch.object(fc.fc_config, "FALLBACK_MODEL", "codex:gpt-6-sol"):
            self.assertEqual(fc.fc_models.fallback_for("pi:a/b"), "codex:gpt-6-sol")
            self.assertEqual(fc.fc_implement.implementer("/w", fc.fc_models.fallback_for("pi:a/b"), "x"), self.RESULT)
        self.run.assert_called_once()

class CodexReader(unittest.TestCase):
    USAGE = {"tokens_in": 1, "tokens_out": 2, "cost": 0.0}
    META = {"test": "pytest -k thing", "files": ["a.py"]}

    def setUp(self):
        self.run, self.cmd = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", "")), mock.Mock(return_value=["codex", "x"])
        self.res, self.claude = mock.Mock(return_value=(None, "  fix a.py:3  \n", 1, self.USAGE)), mock.Mock(return_value=("s", "claude reply", 0, self.USAGE))
        patches = [(fc.fc_proc, "run", self.run), (fc.fc_claude, "headless_env", mock.Mock(return_value={"H": "1"})), (fc_harness, "once_command", self.cmd),
                   (fc_harness, "once_result", self.res), (fc.fc_claude, "claude_once", self.claude)]
        for obj, k, v in patches: p = mock.patch.object(obj, k, v); p.start(); self.addCleanup(p.stop)

    def read(self, reader):
        with mock.patch.object(fc.fc_config, "TEST_READER", reader):
            return fc.fc_implement.read_failure("/w", self.META, "the output", "/tmp/log")

    def test_a_codex_reader_runs_the_read_only_turn_in_the_clone(self):
        self.assertEqual(self.read("codex:gpt-6-sol"), ("fix a.py:3", self.USAGE))
        self.run.assert_called_once_with(["codex", "x"], cwd="/w", env={"H": "1"}, timeout=fc.fc_implement.READER_TIMEOUT)
        self.assertEqual(self.cmd.call_args.args[:2], ("codex:gpt-6-sol", "/w")); self.assertIs(self.cmd.call_args.kwargs["read_only"], True)
        prompt = self.cmd.call_args.args[2] if len(self.cmd.call_args.args) > 2 else self.cmd.call_args.kwargs["prompt"]
        for part in ("pytest -k thing", "the output", "/tmp/log"): self.assertIn(part, prompt)
        self.assertEqual(self.res.call_args.args, ("codex:gpt-6-sol", self.run.return_value)); self.claude.assert_not_called()

    def test_the_brief_is_cut_to_1500_characters(self):
        self.res.return_value = (None, "x" * 2000, 0, self.USAGE)
        self.assertEqual(self.read("codex")[0], "x" * 1500)

    def test_a_timeout_or_a_failed_turn_gives_nothing(self):
        self.run.side_effect = subprocess.TimeoutExpired("codex", 120)
        self.assertEqual(self.read("codex:gpt-6-sol"), ("", None))
        self.run.side_effect = None; self.res.side_effect = RuntimeError("Codex failed: boom")
        self.assertEqual(self.read("codex:gpt-6-sol"), ("", None)); self.run.assert_called()

class ResolvedTrustKey(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.tmp, self.real, self.link, self.bin = tmp, tmp / "real", tmp / "link", tmp / "bin"
        self.real.mkdir(); self.bin.mkdir(); self.link.symlink_to(self.real); (tmp / "home").mkdir()
        (self.bin / "codex").write_text("#!/bin/sh\n"); (self.bin / "codex").chmod(stat.S_IRWXU)
        p = mock.patch.dict(os.environ, {"HOME": str(tmp / "home"), "CODEX_HOME": str(self.link), "PATH": str(self.bin)}); p.start(); self.addCleanup(p.stop)
        g = lambda m, sub: {**({"matcher": m} if m else {}), "hooks": [{"type": "command", "command": f'"/c/bin/compose" {sub}', "timeout": 10}]}
        self.hooks = {"PreToolUse": [g("Write|Edit|MultiEdit|NotebookEdit", "guard"), g("Bash", "guard")], "UserPromptSubmit": [g(None, "skillcheck")], "SessionStart": [g("compact", "reenter")]}
        self.file, self.trust = self.link / "hooks.json", self.link / "config.toml"
        self.file.write_text(json.dumps({"hooks": self.hooks}))

    def records(self, path):
        return {f"{path}:{HookTrust.SNAKE[ev]}:{i}:{j}": fc_harness._codex_hash(ev, g) for ev, gs in self.hooks.items() for i, g in enumerate(gs) for j in range(len(g["hooks"]))}

    def state(self): return {k: v["trusted_hash"] for k, v in tomllib.loads(self.trust.read_text())["hooks"]["state"].items()}

    def test_a_symlinked_codex_home_gets_keys_for_the_resolved_hooks_file_and_hooks_ok_passes(self):
        self.assertEqual([str(p) for p in fc_links.trust_hooks("codex", "/c")], [str(self.trust)])
        self.assertEqual(self.state(), self.records(self.real / "hooks.json"))
        self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (True, ""))
        self.assertEqual(fc_links.trust_hooks("codex", "/c"), [])

    def test_records_under_the_unresolved_path_are_untrusted_and_trust_hooks_adds_the_resolved_ones_after_them(self):
        old = "# mine\n" + "".join(f"[hooks.state.{json.dumps(k)}]\ntrusted_hash = {json.dumps(h)}\n\n" for k, h in self.records(self.file).items())
        self.trust.write_text(old)
        self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (False, f"{self.trust} has no trust record for compose's guard hook"))
        fc_links.trust_hooks("codex", "/c")
        self.assertTrue(self.trust.read_text().startswith(old))
        self.assertEqual(self.state(), {**self.records(self.file), **self.records(self.real / "hooks.json")})
        self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (True, ""))

    def test_a_home_with_no_symlink_in_it_keeps_its_keys(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.real)}):
            fc_links.trust_hooks("codex", "/c")
            self.assertEqual(self.state(), self.records(self.real / "hooks.json"))
            self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (True, ""))

    def test_a_hooks_file_that_is_itself_a_symlink_is_keyed_by_its_target(self):
        (self.tmp / "dot").mkdir(); (self.tmp / "dot/hooks.json").write_text(self.file.read_text()); self.file.unlink(); self.file.symlink_to(self.tmp / "dot/hooks.json")
        fc_links.trust_hooks("codex", "/c")
        self.assertEqual(self.state(), self.records(self.tmp / "dot/hooks.json"))
        self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (True, ""))

    def test_a_symlinked_dot_codex_under_home_is_resolved_without_codex_home(self):
        os.environ.pop("CODEX_HOME"); (self.tmp / "home/.codex").symlink_to(self.real)
        fc_links.trust_hooks("codex", "/c")
        self.assertEqual(self.state(), self.records(self.real / "hooks.json"))


class HookWriters(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.home, self.bin = tmp / "home", tmp / "bin"
        self.home.mkdir(); self.bin.mkdir()
        (self.bin / "codex").write_text("#!/bin/sh\n"); (self.bin / "codex").chmod(stat.S_IRWXU)
        p = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.bin)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)
        self.codex = self.home / ".codex"

    def test_a_missing_home_folder_is_made_by_each_writer(self):
        written = fc_links.write_hooks("codex", "/c")
        self.assertEqual(written, [self.codex / "hooks.json"]); self.assertIsInstance(written[0], Path)
        self.assertTrue((self.codex / "hooks.json").is_file())
        self.assertEqual(fc_links.trust_hooks("codex", "/c"), [self.codex / "config.toml"])
        self.assertTrue((self.codex / "config.toml").is_file())

    def test_a_missing_nested_codex_home_is_made_with_its_parents(self):
        os.environ["CODEX_HOME"] = str(self.home / "a/b/codex")
        self.assertEqual(fc_links.write_hooks("codex", "/c"), [self.home / "a/b/codex/hooks.json"])

    def test_a_call_that_returns_nothing_makes_no_folder(self):
        self.assertEqual(fc_links.trust_hooks("codex", "/c"), [])
        row = {"name": "Other", "home_env": "CODEX_HOME", "home": "~/.codex", "hooks_file": "hooks.json"}
        with mock.patch.dict(fc_harness.HARNESSES, {"other": row}): self.assertEqual(fc_links.write_hooks("other", "/c"), [])
        self.assertFalse(self.codex.exists())

    def test_foreign_bash_groups_of_any_shape_are_kept_at_their_index_and_compose_appends(self):
        foreign = [{"matcher": "Bash", "hooks": []}, {"matcher": "Bash"}, {"matcher": "Bash", "hooks": [{"type": "command"}]}, {"matcher": "Bash", "hooks": None}]
        self.codex.mkdir()
        (self.codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": foreign}}))
        self.assertEqual(len(fc_links.write_hooks("codex", "/c")), 1)
        pre = json.loads((self.codex / "hooks.json").read_text())["hooks"]["PreToolUse"]
        self.assertEqual(pre[:4], foreign)
        self.assertEqual([g["hooks"][0]["command"] for g in pre[4:]], ['"/c/bin/compose" guard'] * 2)
        self.assertEqual([g["matcher"] for g in pre[4:]], ["Write|Edit|MultiEdit|NotebookEdit", "Bash"])
        self.assertEqual(fc_links.write_hooks("codex", "/c"), [])


class HooksCheckBadFiles(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.home, self.bin = tmp / "home", tmp / "bin"
        self.home.mkdir(); self.bin.mkdir()
        p = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.bin)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)
        self.hooks, self.trust = self.home / ".codex/hooks.json", self.home / ".codex/config.toml"
        self.hooks.parent.mkdir()

    def codex(self): (self.bin / "codex").write_text("#!/bin/sh\n"); (self.bin / "codex").chmod(stat.S_IRWXU)

    def test_a_hooks_file_that_is_not_json_is_named_and_left_alone(self):
        self.codex()
        for text in ("{", ""):
            self.hooks.write_text(text)
            self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (False, "~/.codex/hooks.json is not valid JSON"))
            self.assertEqual(self.hooks.read_text(), text)

    def test_the_hooks_file_is_read_before_the_trust_file(self):
        self.codex(); self.hooks.write_text("{"); self.trust.write_text("[hooks")
        self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (False, "~/.codex/hooks.json is not valid JSON"))

    def test_a_trust_file_that_is_not_toml_is_named_and_left_alone(self):
        self.codex(); fc_links.write_hooks("codex", "/c"); self.trust.write_text("[hooks")
        self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (False, "~/.codex/config.toml is not valid TOML"))
        self.assertEqual(self.trust.read_text(), "[hooks")

    def test_the_names_follow_codex_home(self):
        self.codex(); os.environ["CODEX_HOME"] = str(self.home / "elsewhere"); (self.home / "elsewhere").mkdir()
        (self.home / "elsewhere/hooks.json").write_text("{")
        self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (False, "~/elsewhere/hooks.json is not valid JSON"))
        (self.home / "elsewhere/hooks.json").unlink(); fc_links.write_hooks("codex", "/c"); (self.home / "elsewhere/config.toml").write_text("[hooks")
        self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (False, "~/elsewhere/config.toml is not valid TOML"))

    def test_a_missing_hook_is_named_before_the_trust_file_is_read(self):
        self.codex(); self.trust.write_text("[hooks")
        for hooks in (None, {"hooks": {}}):
            self.hooks.unlink(missing_ok=True)
            if hooks is not None: self.hooks.write_text(json.dumps(hooks))
            ok, why = fc_checks.hooks_ok("codex", "/c")
            self.assertFalse(ok); self.assertTrue(why.endswith("lacks compose's guard hook"), why)

    def test_without_codex_or_without_hooks_neither_file_is_read(self):
        self.hooks.write_text("{"); self.trust.write_text("[hooks")
        self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (True, ""))
        self.codex()
        with mock.patch.dict(fc_harness.HARNESSES, {"codex": {k: v for k, v in fc_harness.HARNESSES["codex"].items() if k != "hooks"}}):
            self.assertEqual(fc_checks.hooks_ok("codex", "/c"), (True, ""))


class GuardRedirects(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        fc.fc_core.sh(["git", "init", "-q"], self.repo)

    def denied(self, cmd, role="orchestrator"):
        ev, out = {"tool_name": "Bash", "cwd": str(self.repo), "tool_input": {"command": cmd}}, io.StringIO()
        with mock.patch.dict(os.environ, {"COMPOSE_ROLE": role}), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(ev))), mock.patch.object(sys, "stdout", out):
            fc.fc_hooks.cmd_guard(None)
        return json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecisionReason"] if out.getvalue() else None

    def targets(self, cmd):
        return sorted(os.path.realpath(p) for p in fc.fc_hooks.edit_paths({"tool_name": "Bash", "cwd": str(self.repo), "tool_input": {"command": cmd}}))

    def test_a_redirection_character_inside_quotes_is_not_a_redirection(self):
        for cmd in ('echo "a -> b"', 'python3 -c "print(1 >= 0)"', "git log --format='%h -> %s'", "echo 'a > b.py' \"c >> d.py\""):
            self.assertIsNone(self.denied(cmd), cmd)
            self.assertEqual(self.targets(cmd), [], cmd)

    def test_a_heredoc_body_is_data_not_shell(self):
        for cmd in ("compose crew start --brief - <<'EOF'\nfix tests -> `x` and <angle> from y\nEOF", "cat <<-EOF\n  a > b.py\n  EOF", "cat <<\"EOF\"\na > b.py\nEOF"):
            self.assertIsNone(self.denied(cmd), cmd)
            self.assertEqual(self.targets(cmd), [], cmd)

    def test_a_redirection_around_a_heredoc_is_still_checked(self):
        self.assertEqual(self.targets("cat <<'EOF' > bin/a.py\na > b.py\nEOF\necho y >> c.txt"), [os.path.realpath(self.repo / n) for n in ("bin/a.py", "c.txt")])
        self.assertEqual(self.targets("cat <<< hi\necho y > c.txt"), [os.path.realpath(self.repo / "c.txt")])

    def test_redirections_to_files_in_the_repo_are_denied(self):
        for cmd in ("echo x > bin/a.py", "echo x >> bin/a.py", "echo x>bin/a.py", "echo x >| bin/a.py", "echo x &> bin/a.py", "echo x &>> bin/a.py",
                    "echo x 2>&1 > bin/a.py", 'echo "a > b" > "bin/a.py"'):
            self.assertIn("never edit code", self.denied(cmd) or "", cmd)
            self.assertEqual(self.targets(cmd), [os.path.realpath(self.repo / "bin/a.py")], cmd)

    def test_the_target_is_the_token_after_each_operator(self):
        self.assertEqual(self.targets('echo x > "a b.txt"; echo y >> c.txt && ls 2>&1'), [os.path.realpath(self.repo / n) for n in ("a b.txt", "c.txt")])

    def test_a_variable_in_the_target_is_read_as_the_shell_would(self):
        scratch = ".compose/shop/scratch/planner"
        for cmd in (f"S={scratch}; echo hi > $S/plan.txt 2>&1", f"S={scratch} D=$S/x; echo hi >> ${{D}}/plan.txt", f"S={scratch}\necho hi > \"$S/plan.txt\""):
            self.assertIsNone(self.denied(cmd), cmd)
            self.assertEqual(self.targets(cmd), [os.path.realpath(self.repo / scratch / n) for n in (("x/plan.txt",) if "D=" in cmd else ("plan.txt",))], cmd)
        self.assertIn("never edit code", self.denied("D=bin; echo x > $D/a.py") or "")
        self.assertEqual(self.targets("D=bin; echo x > $D/a.py"), [os.path.realpath(self.repo / "bin/a.py")])
        with mock.patch.dict(os.environ, {"FC_TEST_DIR": "bin"}):
            self.assertEqual(self.targets("echo x > $FC_TEST_DIR/a.py"), [os.path.realpath(self.repo / "bin/a.py")])
        self.assertEqual(self.targets("echo x > $FC_TEST_UNSET/a.py"), [os.path.realpath(self.repo / "$FC_TEST_UNSET/a.py")])

    def test_a_home_target_is_not_a_path_in_the_repo(self):
        self.assertEqual(self.targets("echo x > ~/notes.txt"), [os.path.realpath(Path.home() / "notes.txt")])
        self.assertIsNone(self.denied("echo x > ~/notes.txt"))

    def test_stream_merges_scratch_and_commands_the_shell_cannot_split_are_allowed(self):
        for cmd in ("ls 2>&1", "echo hi > .compose/shop/scratch/x", "echo x > bin/a.py 'oops"):
            self.assertIsNone(self.denied(cmd), cmd)
        self.assertEqual(self.targets("ls 2>&1"), [])
        self.assertEqual(self.targets('echo x > bin/a.py "oops'), [])

    def test_other_roles_are_not_guarded(self):
        self.assertIsNone(self.denied("echo x > bin/a.py", "crew"))


class SessionMarker(unittest.TestCase):
    def pick(self, flag=None, **env):
        with mock.patch.dict(os.environ):
            for k in ("COMPOSE_HARNESS", "CLAUDECODE", "CODEX_THREAD_ID"): os.environ.pop(k, None)
            os.environ.update(env)
            return fc_config.session_harness(flag)

    def test_codex_row_names_its_marker_and_detected_reads_it(self):
        self.assertEqual(fc_harness.HARNESSES["codex"]["session_env"], "CODEX_THREAD_ID")
        self.assertEqual(fc_harness.detected({"CODEX_THREAD_ID": "t1", "CLAUDECODE": "1"}), "codex")
        for env in ({}, {"CODEX_THREAD_ID": ""}, {"CLAUDECODE": "1"}, {"CODEX_CI": "1"}, {"CODEX_THREAD_ID_X": "t1"}):
            self.assertIsNone(fc_harness.detected(env), env)

    def test_detected_defaults_to_the_process_env(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "t1"}): self.assertEqual(fc_harness.detected(), "codex")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": ""}): self.assertIsNone(fc_harness.detected())

    def test_a_codex_marker_wins_over_claudecode(self):
        self.assertEqual(self.pick(CODEX_THREAD_ID="t1"), "codex")
        self.assertEqual(self.pick(CODEX_THREAD_ID="t1", CLAUDECODE="1"), "codex")

    def test_flag_then_compose_harness_then_marker(self):
        self.assertEqual(self.pick("claude", COMPOSE_HARNESS="gemini", CODEX_THREAD_ID="t1"), "claude")
        self.assertEqual(self.pick(COMPOSE_HARNESS="gemini", CODEX_THREAD_ID="t1", CLAUDECODE="1"), "gemini")

class NoTomllibTrust(unittest.TestCase):
    """Without tomllib, as on Python 3.9 and 3.10, the codex trust answers and bytes are the ones it gives with it (test_py39 keeps the syntax)."""
    BIN = str(Path(__file__).resolve().parents[1] / "bin")
    NO_TOMLLIB = "import sys; sys.modules['tomllib'] = None\n"  # `import tomllib` raises ImportError, as before 3.11
    SCENE = r'''
import json, os, re, sys
sys.path.insert(0, sys.argv[1])
import fc_checks, fc_links
home = os.environ["CODEX_HOME"]; trust = os.path.join(home, "config.toml")
def put(text): open(trust, "w").write(text)
ok = lambda: list(fc_checks.hooks_ok("codex", "/c"))
write = lambda: [str(p) for p in fc_links.trust_hooks("codex", "/c")]
os.makedirs(home, exist_ok=True)
for f in ("hooks.json", "config.toml"):
    if os.path.exists(os.path.join(home, f)): os.remove(os.path.join(home, f))
fc_links.write_hooks("codex", "/c")
if sys.argv[2]: put(sys.argv[2])
r = {"first": write(), "ok": ok(), "again": write(), "text": open(trust).read()}
put(r["text"][:r["text"].rindex("[hooks.state.")])
r["lost"], r["fix"], r["fixed"], r["fixed_text"] = ok(), write(), ok(), open(trust).read()
i = r["fixed_text"].index("[hooks.state.")
put(r["fixed_text"][:i] + re.sub(r'trusted_hash = "[^"]*"', 'trusted_hash = "sha256:bad"', r["fixed_text"][i:], count=1))
r["bad"], r["fix2"], r["fixed2"], r["fixed2_text"] = ok(), write(), ok(), open(trust).read()
print(json.dumps(r))
'''
    STATES = ['[hooks.state."a:b"]\ntrusted_hash = "h1"\n\n[hooks.state."c"]\nenabled = true\n\n[other]\ntrusted_hash = "x"\n',
              '[hooks.state."a"]\n[other]\ntrusted_hash = "x"\n[hooks.state."b"]\ntrusted_hash = "y"\n', "",
              '[hooks.state."/t/we ird].d#x/hooks.json:session_start:0:0"]\ntrusted_hash = "sha256:ab"\n', 'm = 1\n[hooks.state.nokey]\ntrusted_hash = "z"\n', "[unclosed"]

    def setUp(self):
        tmp = Path(tempfile.mkdtemp()); self.home, self.bin = tmp / "home", tmp / "bin"; self.home.mkdir(); self.bin.mkdir()
        (self.bin / "codex").write_text("#!/bin/sh\n"); (self.bin / "codex").chmod(stat.S_IRWXU)

    def py(self, *args, codex=".codex"):
        env = {"HOME": str(self.home), "CODEX_HOME": str(self.home / codex), "PATH": str(self.bin), "COMPOSE_CONFIG": str(self.home / "c.json"), "HERDR_ENV": "", "PYTHONDONTWRITEBYTECODE": "1"}
        p = subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True, cwd=self.home)
        self.assertEqual(p.returncode, 0, p.stderr); return p.stdout

    def scene(self, old, pre="", codex=".codex"): return json.loads(self.py("-c", (self.NO_TOMLLIB if old else "") + self.SCENE, self.BIN, pre, codex=codex))

    def test_the_trust_answers_and_bytes_are_the_same_as_on_3_11(self):
        r, trust = self.scene(True), str(self.home / ".codex/config.toml")
        want = "~/.codex/config.toml has no trust record for compose's %s hook"
        self.assertEqual((r["first"], r["ok"], r["again"]), ([trust], [True, ""], []))
        self.assertEqual((r["lost"], r["fix"], r["fixed"]), ([False, want % "reenter"], [trust], [True, ""]))
        self.assertEqual((r["bad"], r["fix2"], r["fixed2"]), ([False, want % "guard"], [trust], [True, ""]))
        self.assertNotIn("sha256:bad", r["fixed2_text"])
        self.assertEqual(r, self.scene(False))

    def test_a_path_with_brackets_dots_hash_and_quote_and_other_content_in_the_file(self):
        pre, odd = '# mine\nmodel = "x"\n\n[other]\ntrusted_hash = "keep"\n', 'we ird].d#"e'
        r = self.scene(True, pre, odd)
        self.assertEqual((r["ok"], r["again"], r["fixed2"]), ([True, ""], [], [True, ""]))
        self.assertTrue(r["text"].startswith(pre)); self.assertIn('trusted_hash = "keep"', r["fixed2_text"])
        self.assertEqual(r, self.scene(False, pre, odd))

    def test_trust_state_without_tomllib_reads_the_state_tables_and_never_raises(self):
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            got = [fc_checks.trust_state(t) for t in self.STATES]
        self.assertEqual(got[:4], [{"a:b": {"trusted_hash": "h1"}, "c": {}}, {"a": {}, "b": {"trusted_hash": "y"}}, {},
                                   {"/t/we ird].d#x/hooks.json:session_start:0:0": {"trusted_hash": "sha256:ab"}}])
        self.assertTrue(all(isinstance(g, dict) for g in got))

    def test_trust_state_with_tomllib_returns_its_tables_and_raises_value_error(self):
        self.assertEqual(fc_checks.trust_state(self.STATES[0]), tomllib.loads(self.STATES[0])["hooks"]["state"])
        self.assertEqual((fc_checks.trust_state(""), fc_checks.trust_state('model = "x"\n')), ({}, {}))
        with self.assertRaises(ValueError): fc_checks.trust_state("[unclosed")

    def test_no_bin_module_imports_tomllib_at_module_level(self):
        import ast
        for f in sorted(Path(self.BIN).glob("*.py")):
            names = [a.name for n in ast.parse(f.read_text()).body if isinstance(n, ast.Import) for a in n.names] + [n.module for n in ast.parse(f.read_text()).body if isinstance(n, ast.ImportFrom)]
            self.assertNotIn("tomllib", names, f.name)


class GuardCorpus(unittest.TestCase):
    """Every command in tests/guard_corpus.json was denied to a real teammate by an earlier guard and must be allowed: they write
    scratch files, restore files without moving HEAD, start crew, or report. The corpus grows with each false positive found."""
    CORPUS = Path(__file__).resolve().parent / "guard_corpus.json"
    FILES = ("app/src/app/page.test.tsx", "src/app/actions/billing.test.ts", "src/app/actions/billing.ts", "src/config/env.ts")

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        fc.fc_core.sh(["git", "init", "-q"], self.repo)
        for f in self.FILES:
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text("x\n")

    def decision(self, cmd, role):
        ev, out = {"tool_name": "Bash", "cwd": str(self.repo), "tool_input": {"command": cmd}}, io.StringIO()
        with mock.patch.dict(os.environ, {"COMPOSE_ROLE": role, "COMPOSE_REPO": str(self.repo)}), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(ev))), mock.patch.object(sys, "stdout", out):
            fc.fc_hooks.cmd_guard(None)
        return json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecisionReason"] if out.getvalue() else None

    def test_every_real_denied_command_is_allowed(self):
        corpus = json.loads(self.CORPUS.read_text())
        self.assertGreaterEqual(len(corpus), 27)
        for i, e in enumerate(corpus):
            cmd = e["command"].replace("{repo}", str(self.repo))
            self.assertEqual(e["expect"], "allow")
            self.assertIsNone(self.decision(cmd, e["role"]), f"corpus[{i}] ({e['denied_as']}): {cmd[:200]}")

    def test_scratch_is_always_the_team_leads_to_write(self):
        for cmd in ("echo hi > .compose/shop/scratch/shop-orchestrator/notes.md", "cd .compose/shop/scratch && echo hi > plan.md",
                    "S=.compose/shop/scratch/x; mkdir -p $S; cat > $S/story.md <<'EOF'\nbody > not a redirection\nEOF"):
            self.assertIsNone(self.decision(cmd, "orchestrator"), cmd)
            self.assertIsNone(self.decision(cmd, "planner"), cmd)

    def test_a_cd_moves_the_targets_that_follow_it(self):
        self.assertIn("never edit code", self.decision("cd src && echo x > a.py", "orchestrator") or "")
        self.assertIsNone(self.decision("cd .compose/shop/scratch; echo x > a.md", "orchestrator"))
        self.assertIsNone(self.decision("cd .compose/shop/scratch\necho x > a.md", "orchestrator"))
        self.assertIsNone(self.decision("(cd .compose/shop/scratch && echo x > a.md)", "orchestrator"))

    def test_a_file_restore_in_the_shared_worktree_never_moves_head(self):
        for cmd in ("git checkout 940bd04 -- src/app/actions/billing.test.ts", "git checkout compose/x-006 -- src/app/actions/billing.ts src/config/env.ts",
                    "git checkout app/src/app/page.test.tsx", "git checkout -- .", "git checkout -p"):
            self.assertIsNone(self.decision(cmd, "crew"), cmd)
        for cmd in ("git checkout main", "git switch -c feature", "git checkout compose/x-006", "git checkout"):
            self.assertIn("move the team lead's HEAD", self.decision(cmd, "crew") or "", cmd)
