import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys, time
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


class SkillCheck(unittest.TestCase):
    """The UserPromptSubmit and PostToolUse hook: tell a compose session once when the skill's files changed under it."""
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.skill, self.state = d / "skill", d / "state"
        (self.skill / "sections").mkdir(parents=True)
        (self.skill / "commands").mkdir()
        (self.skill / "SKILL.md").write_text("v1\n")
        (self.skill / "sections" / "lead.md").write_text("lead v1\n")
        (self.skill / "commands" / "planner.md").write_text("planner v1\n")
        (self.skill / "README.md").write_text("not the skill\n")
        self.patches = [mock.patch.object(fc.fc_hooks, "SKILL_DIR", self.skill), mock.patch.object(fc.fc_hooks, "SKILL_SEEN", self.state)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def hook(self, ev, role="crew"):
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_ROLE"} | ({"COMPOSE_ROLE": role} if role else {})
        stdin = io.StringIO(ev if isinstance(ev, str) else json.dumps(ev))
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", out):
            fc.fc_hooks.cmd_skillcheck(None)
        return out.getvalue()

    def prompt(self, session="s1", text="go", role="crew"):
        return self.hook({"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": text}, role)

    def context(self, out):
        h = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "UserPromptSubmit")
        return h["additionalContext"]

    def test_first_prompt_records_and_unchanged_prints_nothing(self):
        self.assertEqual(self.prompt(), "")
        self.assertEqual(self.prompt(), "")

    def test_a_changed_file_is_named_once(self):
        self.prompt()
        (self.skill / "SKILL.md").write_text("v2\n")
        note = self.context(self.prompt())
        self.assertIn("compose's skill changed since you read it:", note)
        self.assertIn(str(self.skill / "SKILL.md"), note)  # absolute: a teammate's worktree holds an older SKILL.md of its own
        self.assertNotIn("lead.md", note)
        self.assertIn("Re-read only those files before acting; nothing else changed.", note)
        self.assertEqual(self.prompt(), "")

    def test_added_and_removed_files_are_named(self):
        self.prompt()
        (self.skill / "sections" / "planner.md").write_text("new\n")
        (self.skill / "commands" / "planner.md").unlink()
        note = self.context(self.prompt())
        self.assertIn(str(self.skill / "sections" / "planner.md"), note)
        self.assertIn(str(self.skill / "commands" / "planner.md"), note)

    def test_other_files_are_not_the_skill(self):
        self.prompt()
        (self.skill / "README.md").write_text("changed\n")
        self.assertEqual(self.prompt(), "")

    def test_each_session_keeps_its_own_version(self):
        self.prompt("s1")
        (self.skill / "SKILL.md").write_text("v2\n")
        self.assertEqual(self.prompt("s2"), "")  # s2 first saw v2
        self.assertTrue(self.prompt("s1"))

    def test_a_session_without_a_role_that_never_loaded_the_skill_is_left_alone(self):
        self.assertEqual(self.prompt(role=None), "")
        (self.skill / "SKILL.md").write_text("v2\n")
        self.assertEqual(self.prompt(role=None), "")
        self.assertFalse(self.state.exists() and any(self.state.iterdir()))

    def test_loading_the_skill_with_the_skill_tool_enrolls_the_session(self):
        for name in ("other-skill", "composex"):
            self.hook({"hook_event_name": "PostToolUse", "session_id": "m", "tool_name": "Skill", "tool_input": {"skill": name}}, None)
        self.assertFalse(self.state.exists() and any(self.state.iterdir()))
        self.hook({"hook_event_name": "PostToolUse", "session_id": "m", "tool_name": "Skill", "tool_input": {"skill": "compose"}}, None)
        self.assertEqual(self.prompt("m", role=None), "")
        (self.skill / "SKILL.md").write_text("v2\n")
        self.assertIn(str(self.skill / "SKILL.md"), self.context(self.prompt("m", role=None)))

    def test_a_typed_slash_command_enrolls_the_session(self):
        self.assertEqual(self.prompt("m", "/compose:planner NEB-1", role=None), "")
        (self.skill / "commands" / "planner.md").write_text("planner v2\n")
        self.assertIn("planner.md", self.context(self.prompt("m", role=None)))

    def test_errors_print_nothing(self):
        self.assertEqual(self.hook("not json"), "")
        with mock.patch.object(fc.fc_hooks, "SKILL_DIR", self.skill / "missing"):
            self.assertEqual(self.prompt(), "")
        self.assertEqual(self.hook({"hook_event_name": "UserPromptSubmit", "prompt": "no session"}), "")

    def test_an_empty_session_gets_no_notice(self):  # the Stop hook's stdin without a session_id: sessions would share one file
        self.assertIsNone(fc.fc_hooks.skill_notice(""))
        (self.skill / "SKILL.md").write_text("v2\n")
        self.assertIsNone(fc.fc_hooks.skill_notice(""))
        self.assertFalse(self.state.exists() and any(self.state.iterdir()))

    def crew_start(self, proj, model):
        def herdr(*args, **kw):
            if args[:2] == ("agent", "list"):
                return {"agents": []}
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "working"}}
            return {"tab": {"tab_id": "w9:t3"}, "root_pane": {"pane_id": "w9:p7"}} if args[:2] == ("tab", "create") else {}
        a = SimpleNamespace(action="start", role="eol", title="eol", brief="Do it.", model=None, cwd=None)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "herdr", herdr), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), \
                mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_herdr, "herdr_find_space", return_value="w9"), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), mock.patch.object(fc.fc_pin, "cli_commit", return_value="abc1234"), \
                mock.patch.object(fc.fc_crew.fc_crewpick, "crew_pick_full", return_value=(model, None, "", [])), mock.patch.object(time, "sleep"), mock.patch("builtins.print"), \
                mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}):  # the team lead starts it, whatever this test's caller is
            fc.fc_crew.cmd_crew(a)
        return json.loads((proj / "results" / "crew" / "shop-eol.json").read_text())

    def snapshot(self, proj, live):
        agents = {"agents": [{"name": "shop-eol", "agent_status": "working"}] if live else []}
        with mock.patch.object(fc.fc_herdr, "herdr", return_value=agents), mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[]), \
                mock.patch.object(fc.fc_status, "board", return_value={"rows": []}):
            return fc.fc_watch.watch_snapshot(Path("."), proj)

    def test_a_live_pi_teammate_hears_once_through_watch(self):  # pi runs no Claude hooks: watch tells the team lead, who relays it
        proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        proj.mkdir(parents=True)
        self.assertEqual(self.crew_start(proj, "pi:a/b")["skill"], fc.fc_hooks.skill_hashes())
        before = self.snapshot(proj, live=True)
        self.assertEqual(fc.fc_watch.watch_events(before, self.snapshot(proj, live=True)), [])
        (self.skill / "SKILL.md").write_text("v2\n")
        after = self.snapshot(proj, live=True)
        ev = fc.fc_watch.watch_events(before, after)
        self.assertEqual(ev, [f"event crew=shop-eol skill=stale changed={(self.skill / 'SKILL.md').resolve()}"])
        self.assertEqual(fc.fc_watch.watch_filter(ev, "shop", []), ev)  # the team lead's watch
        self.assertEqual(fc.fc_watch.watch_events(after, self.snapshot(proj, live=True)), [])  # said once
        self.assertEqual(fc.fc_watch.watch_events(before, self.snapshot(proj, live=False)), [])  # a closed teammate needs nothing

    def test_a_claude_teammate_is_left_to_its_hooks(self):
        proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        proj.mkdir(parents=True)
        self.assertNotIn("skill", self.crew_start(proj, "claude"))
        before = self.snapshot(proj, live=True)
        (self.skill / "SKILL.md").write_text("v2\n")
        self.assertEqual(fc.fc_watch.watch_events(before, self.snapshot(proj, live=True)), [])

    def test_the_plugin_registers_the_hook(self):
        hooks = json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text())["hooks"]
        prompt = [h["command"] for e in hooks["UserPromptSubmit"] for h in e["hooks"]]
        skill = [h["command"] for e in hooks["PostToolUse"] if e.get("matcher") == "Skill" for h in e["hooks"]]
        self.assertTrue(any(c.endswith(" skillcheck") for c in prompt), prompt)
        self.assertTrue(any(c.endswith(" skillcheck") for c in skill), skill)


if __name__ == "__main__":
    unittest.main()
