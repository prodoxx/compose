import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, shlex, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import subprocess, sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_defaults, fc_tierspend  # noqa: E402  (bin is on sys.path after the load; same objects compose.py uses)

CREW = {"orchestrator": "claude:claude-opus-5-5", "groundwork": "claude:claude-sonnet-5-5",
        "default": "claude:claude-sonnet-5-5", "small": "claude:claude-haiku-5-5"}
SERVERS = ["claude_ai_Linear", "trail"]  # cmd_crew asks fc_mcp.installed_servers; no test runs the real `claude mcp list`


class RoleModels(unittest.TestCase):
    """Teammate sessions are Claude Code; models.crew picks the model per role, and a model id always names its harness."""
    def setUp(self):
        p = mock.patch.object(fc.fc_models, "model_available", return_value=(True, "answered")); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(fc.fc_mcp, "installed_servers", return_value=SERVERS); p.start(); self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}); p.start(); self.addCleanup(p.stop)  # the team lead starts every teammate here
        p = mock.patch.object(fc.fc_pin, "pin", return_value={}); p.start(); self.addCleanup(p.stop)  # the pin's git call is not a model probe

    def config(self, crew):
        d = Path(tempfile.mkdtemp())
        (d / "config.json").write_text(json.dumps({"models": {"implementer": "pi:x/y", "crew": crew}}))
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", d / "config.json"):
            fc.fc_config.apply_config()
        self.addCleanup(setattr, fc.fc_config, "CREW_MODELS", {})

    def test_role_then_default_then_small_then_explicit(self):
        self.config(CREW)
        self.assertEqual(fc.fc_crew.fc_crewpick.crew_model("orchestrator"), "claude:claude-sonnet-5-5")
        self.assertEqual(fc.fc_crew.fc_crewpick.crew_model("q-neb-12"), "claude:claude-sonnet-5-5")  # no entry: default
        self.assertEqual(fc.fc_crew.fc_crewpick.crew_model("revert", "small"), "claude:claude-haiku-5-5")
        self.assertEqual(fc.fc_crew.fc_crewpick.crew_model("orchestrator", "claude:claude-x"), "claude:claude-x")

    def test_an_opencode_id_for_a_crew_is_refused_naming_the_key(self):
        self.config({"tests": "openai/gpt-x"})
        with self.assertRaises(SystemExit) as e:
            fc.fc_crew.fc_crewpick.crew_model("tests")
        self.assertIn("models.crew.tests", str(e.exception))
        with self.assertRaises(SystemExit) as e:
            fc.fc_crew.fc_crewpick.crew_model("tests", "openai/gpt-x")
        self.assertIn("--model", str(e.exception))
        self.assertEqual(fc.fc_crew.fc_crewpick.crew_model("tests", "pi:openai/gpt-6-astra"), "pi:openai/gpt-6-astra")

    def test_crew_start_passes_the_model_to_claude_code(self):
        self.config(CREW)
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        calls = []
        def herdr(*args, check=False, timeout=120):
            calls.append(args)
            if args[:2] == ("tab", "create"):
                return {"tab": {"tab_id": "w9:t1"}, "root_pane": {"pane_id": "w9:p1"}}
            return None
        a = SimpleNamespace(action="start", role="groundwork", title="groundwork", brief="do it", force=False, cwd=None, model=None)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=proj), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_herdr, "herdr", herdr), \
                mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_crew, "crew_preamble", return_value=""), \
                mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_await_start"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            fc.fc_crew.cmd_crew(a)
        self.assertIn("teammate p-groundwork started in tab 'groundwork' of space p on claude:claude-sonnet-5-5", out.getvalue())
        start = next(c for c in calls if c[:2] == ("agent", "start"))
        self.assertEqual(start[start.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(json.loads((proj / "results" / "crew" / "p-groundwork.json").read_text())["model"], "claude:claude-sonnet-5-5")

    def test_task_claude_model_routes_to_claude_code_with_that_model(self):
        with mock.patch.object(fc.fc_claude, "claude_once", return_value=(None, "", 0, {})) as co, mock.patch.object(fc.fc_models, "pi_turn") as pt:
            fc.fc_implement.implementer("/tmp", "claude:claude-opus-5-5", "hi")
            fc.fc_implement.implementer("/tmp", "claude", "hi")
        self.assertEqual([c.kwargs["model"] for c in co.call_args_list], ["claude-opus-5-5", None])
        pt.assert_not_called()
        self.assertIsNone(fc.fc_models.fallback_for("claude:claude-opus-5-5"))


PLANNER = {"planner": {"model": "claude:claude-fable-5-1", "fallback": {"model": "claude:claude-opus-5-5", "effort": "high"}},
           "groundwork": "claude:claude-opus-5-5", "default": "claude:claude-sonnet-5-5"}


class PlannerChain(unittest.TestCase):
    """Planning (the architect teammate, never the team lead) runs on `planner`: its model when Claude Code can run it, else its fallback."""
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.json").write_text(json.dumps({"models": {"implementer": "pi:x/y", "crew": PLANNER}}))
        for p in (mock.patch.object(fc.fc_config, "CONFIG_PATH", d / "config.json"), mock.patch.object(fc.fc_config, "PROBE_CACHE", d / "probe.json"),
                  mock.patch.object(fc.fc_mcp, "installed_servers", return_value=SERVERS), mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}),
                  mock.patch.object(fc.fc_pin, "pin", return_value={})):  # the pin's git call is not a model probe
            p.start(); self.addCleanup(p.stop)
        fc.fc_config.apply_config(); fc.fc_models._PROBED.clear()
        self.addCleanup(fc.fc_models._PROBED.clear)
        for k in ("CREW_MODELS", "CREW_EFFORT", "CREW_FALLBACK"):
            self.addCleanup(setattr, fc.fc_config, k, {})
        self.probe_cache = d / "probe.json"

    def probe(self, ok, down=("claude-fable-5-1",)):
        """Every probe answers when ok; otherwise the models in `down` refuse with "model not found" and the rest answer."""
        def run(cmd, **kw):
            bad = not ok and cmd[cmd.index("--model") + 1] in down
            out = json.dumps({"is_error": bad, "result": "model not found" if bad else "ok"})
            return subprocess.CompletedProcess(cmd, 1 if bad else 0, out, "")
        return mock.patch.object(subprocess, "run", side_effect=run)

    def test_planning_roles_use_the_planner_and_others_do_not(self):
        with self.probe(True):
            self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick("architect")[:2], ("claude:claude-fable-5-1", None))
            self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick("orchestrator")[:2], ("claude:claude-sonnet-5-5", None))  # the team lead delegates, it does not plan
            self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick("q-neb-12")[:2], ("claude:claude-sonnet-5-5", None))  # no per-question agents any more
            self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick("groundwork")[:2], ("claude:claude-sonnet-5-5", None))
            self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick("helper")[:2], ("claude:claude-sonnet-5-5", None))

    def test_fallback_when_the_planner_model_is_unavailable(self):
        with self.probe(False):
            model, effort, note = fc.fc_crew.fc_crewpick.crew_pick("architect")
        self.assertEqual((model, effort), ("claude:claude-opus-5-5", "high"))
        self.assertIn("unavailable", note); self.assertIn("model not found", note)

    def test_the_fallbacks_are_tried_in_order(self):
        astra, fable, opus = "pi:openai/gpt-6-astra", "claude:claude-fable-5-1", "claude:claude-opus-5-5"
        chain = {"planner": {"model": astra, "effort": "high", "fallback": [fable, {"model": opus, "effort": "high"}]}}
        d = Path(tempfile.mkdtemp()); (d / "c.json").write_text(json.dumps({"models": {"crew": chain}}))
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", d / "c.json"):
            fc.fc_config.apply_config()
        for up, want in (({fable, astra}, astra), ({fable}, fable), ({opus}, opus)):  # a pi session has the Linear MCP too, so astra is probed first
            fc.fc_models._PROBED.clear()
            with mock.patch.object(fc.fc_models, "model_available", side_effect=lambda m: (m in up, "down")):
                model, effort, note = fc.fc_crew.fc_crewpick.crew_pick("architect")
            self.assertEqual(model, want)
        self.assertEqual(effort, "high")
        with mock.patch.object(fc.fc_models, "model_available", side_effect=lambda m: (False, "down")), self.assertRaises(SystemExit) as e:
            fc.fc_crew.fc_crewpick.crew_pick("architect")  # the last of the chain is probed too: nothing runs, so it fails naming each model and why
        self.assertIn(f"{fable} unavailable (down)", str(e.exception)); self.assertIn(f"{opus} unavailable (down)", str(e.exception))
        self.assertIn("models.crew.planner", str(e.exception))
        self.assertIn(f"{astra} unavailable (down), {fable} unavailable (down), using its fallback", note)
        with mock.patch.object(fc.fc_models, "model_available", return_value=(True, "")):  # a role with no Linear need still takes the pi model
            self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick("helper", "planner")[0], astra)

    def test_a_pi_model_is_probed_with_pi_and_judged_by_its_events(self):
        ok = subprocess.CompletedProcess([], 0, json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}), "")
        bad = subprocess.CompletedProcess([], 0, json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "Model not found: x"}}), "")
        self.assertEqual(fc.fc_models.probe_result("pi:openai/gpt-6-astra", ok), (True, "answered"))
        self.assertEqual(fc.fc_models.probe_result("pi:openai/x", bad)[0], False)
        self.assertIn("Model not found", fc.fc_models.probe_result("pi:openai/x", bad)[1])
        self.assertEqual(fc.fc_models.probe_command("pi:openai/gpt-6-astra")[0], "pi")
        self.assertIn("openai/gpt-6-astra", fc.fc_models.probe_command("pi:openai/gpt-6-astra"))

    def test_a_lone_model_is_probed_and_a_refusal_fails_before_any_tab(self):
        calls = []
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        a = SimpleNamespace(action="start", role="explore", repo=str(proj), project="p", model=None, title=None,
                            brief="look", cwd=None, force=False)
        with self.probe(False, down=("claude-sonnet-5-5",)), mock.patch.object(fc.fc_herdr, "herdr", lambda *x, **k: calls.append(x)), \
                mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_core, "project_dir", return_value=proj), \
                mock.patch.object(fc.fc_herdr, "project_worktree", return_value=proj), self.assertRaises(SystemExit) as e:
            fc.fc_crew.cmd_crew(a)  # models.crew.default is one entry with no fallback
        self.assertIn("claude:claude-sonnet-5-5 unavailable (model not found)", str(e.exception))
        self.assertIn("models.crew.default", str(e.exception))
        self.assertEqual(calls, [])
        with self.probe(False, down=("claude-sonnet-5-5",)), self.assertRaises(SystemExit) as e:
            fc.fc_crew.fc_crewpick.crew_pick("explore", "claude:claude-sonnet-5-5")  # an explicit --model is probed too (a failure is never cached)
        self.assertIn("--model", str(e.exception))

    def test_headless_claude_runs_carry_no_compose_or_anthropic_env(self):
        lead = {"COMPOSE_ROLE": "orchestrator", "COMPOSE_PROJECT": "p", "COMPOSE_REPO": "/r",  # the team lead's tab
                "ANTHROPIC_API_KEY": "sk-ant", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "ANTHROPIC_MODEL": "m"}
        done = subprocess.CompletedProcess([], 0, json.dumps({"is_error": False, "result": "ok"}), "")
        with mock.patch.dict(os.environ, lead), mock.patch.object(subprocess, "run", return_value=done) as run, mock.patch.object(fc.fc_proc, "run", return_value=done) as turn:
            fc.fc_models.probe_once("claude:claude-haiku-5-5", 60)  # its Stop hook would otherwise park the lead's watch in the probe
            fc.fc_claude.claude_once(".", "hi")
        self.assertEqual((run.call_count, turn.call_count), (1, 1))  # the probe stays on subprocess.run, the turn goes through fc_proc.run
        for call in run.call_args_list + turn.call_args_list:
            env = call.kwargs.get("env") or os.environ
            self.assertEqual([k for k in env if k.startswith("COMPOSE_") and k != "COMPOSE_CONFIG"], [])
            self.assertEqual([k for k in env if k.startswith("ANTHROPIC_")], [])  # the subscription pays, never a key or a proxy
            self.assertNotEqual(env["COMPOSE_CONFIG"], str(fc.fc_config.CONFIG_PATH))  # its tools run project code: a scratch config

    def test_a_claude_implementer_runs_lean_with_the_repos_own_instructions(self):
        d = Path(tempfile.mkdtemp()); (d / "bin").mkdir(); repo = d / "repo"; (repo / ".claude").mkdir(parents=True)
        fake = d / "bin" / "claude"  # records its argv and env, answers like `claude -p --output-format json`
        fake.write_text(f"#!{sys.executable}\nimport json, os, sys\nopen({str(d / 'call.json')!r}, 'w').write(json.dumps([sys.argv[1:], dict(os.environ)]))\n"
                        "print(json.dumps({'is_error': False, 'result': 'done', 'usage': {'input_tokens': 9}}))\n")
        fake.chmod(0o755)
        (repo / "CLAUDE.md").write_text("Use tabs.\n"); (repo / ".claude" / "CLAUDE.md").write_text("Run make.\n"); (repo / "AGENTS.md").write_text("Keep it short.\n")
        env = {"PATH": f"{d / 'bin'}{os.pathsep}{os.environ['PATH']}", "ANTHROPIC_API_KEY": "sk-ant", "COMPOSE_ROLE": "orchestrator"}
        with mock.patch.dict(os.environ, env):
            fc.fc_implement.implementer(str(repo), "claude:claude-haiku-5-5", "build it")
        argv, seen = json.loads((d / "call.json").read_text())
        tools = "Read,Edit,Write,Bash,Grep,Glob"
        for flags in (["--restricted"], ["--strict-mcp-config"], ["--model", "claude-haiku-5-5"], ["--tools", tools], ["--allowedTools", tools],
                      ["--settings", fc.fc_claude.NO_ATTRIBUTION]):
            self.assertIn(flags, [argv[i:i + len(flags)] for i in range(len(argv))])
        own = argv[argv.index("--append-system-prompt") + 1]  # --restricted drops every CLAUDE.md: the repo's own come back here, the user's do not
        self.assertEqual([own.index(t) < own.index(u) for t, u in (("Use tabs.", "Run make."), ("Run make.", "Keep it short."))], [True, True])
        self.assertEqual([k for k in seen if k.startswith("ANTHROPIC_") or k == "COMPOSE_ROLE"], [])
        self.assertNotEqual(seen["COMPOSE_CONFIG"], str(fc.fc_config.CONFIG_PATH))
        for f in ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md"):
            (repo / f).unlink()
        with mock.patch.dict(os.environ, env):
            fc.fc_implement.implementer(str(repo), "claude:claude-haiku-5-5", "build it")
        self.assertNotIn("--append-system-prompt", json.loads((d / "call.json").read_text())[0])

    def test_crew_start_passes_the_chosen_model_and_effort(self):
        calls = []
        def herdr(*args, check=False, timeout=120):
            calls.append(args)
            if args[:2] == ("tab", "create"):
                return {"tab": {"tab_id": "w9:t9"}, "root_pane": {"pane_id": "w9:p1"}}
            return {}
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        a = SimpleNamespace(action="start", role="architect", repo=str(proj), project="p", model=None, title=None,
                            brief="plan it", cwd=None, force=False)
        with self.probe(False), mock.patch.object(fc.fc_herdr, "herdr", herdr), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), \
                mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=proj), \
                mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_crew, "crew_preamble", return_value=""), \
                mock.patch.object(fc.fc_crew, "write_report"), mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), mock.patch.object(fc.fc_herdr, "herdr_await_start"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            fc.fc_crew.cmd_crew(a)
        self.assertIn("WARNING: architect wanted its first-choice model but claude:claude-fable-5-1 unavailable", out.getvalue())
        self.assertIn("teammate p-architect started in tab 'architect' of space p on claude:claude-opus-5-5 (high effort)", out.getvalue())
        start = next(c for c in calls if c[:2] == ("agent", "start"))
        self.assertIn("claude-opus-5-5", start); self.assertIn("--effort", start); self.assertIn("high", start)
        rec = json.loads(next((proj / "results" / "crew").glob("*.json")).read_text())
        self.assertEqual((rec["model"], rec["effort"]), ("claude:claude-opus-5-5", "high"))


class DesignerModels(unittest.TestCase):
    """design-NNN builders fall to models.crew.design; the designer defaults to Opus 5.5 at high effort."""
    def setUp(self):
        p = mock.patch.object(fc.fc_models, "model_available", return_value=(True, "answered")); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(fc.fc_mcp, "installed_servers", return_value=SERVERS); p.start(); self.addCleanup(p.stop)

    def config(self, crew):
        d = Path(tempfile.mkdtemp())
        (d / "config.json").write_text(json.dumps({"models": {"implementer": "pi:x/y", "crew": crew}}))
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", d / "config.json"), mock.patch.object(fc.fc_config, "PROBE_CACHE", d / "probe.json"):
            fc.fc_config.apply_config()
        for k in ("CREW_MODELS", "CREW_EFFORT", "CREW_FALLBACK"): self.addCleanup(setattr, fc.fc_config, k, {})

    def test_builder_check_defaults_to_opus_high_with_no_user_config(self):
        self.config(fc_defaults.DEFAULT_MODELS["crew"])
        self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick("builder-check")[:2], ("claude:claude-opus-5-5", "high"))

    def test_design_roles_use_the_design_entry_else_default(self):
        self.config({**CREW, "design": {"model": "claude:claude-opus-5-5", "effort": "high"}})
        for role in ("design-003", "design-review"):
            self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick(role)[:2], ("claude:claude-opus-5-5", "high"))
        self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick("designs-003")[:2], ("claude:claude-sonnet-5-5", None))  # first word is designs
        self.config(CREW); self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick("design-003")[:2], ("claude:claude-sonnet-5-5", None))

class PiKeepsLinear(unittest.TestCase):
    """A pi session loads its MCP servers (Linear among them), so the Linear roles probe a pi model like any other."""
    astra, sonnet = "pi:openai/gpt-6-astra", "claude:claude-sonnet-5-5"
    setUp = PlannerChain.setUp

    def chain(self, role):
        d = Path(tempfile.mkdtemp()); (d / "c.json").write_text(json.dumps({"models": {"crew": {role: {"model": self.astra, "fallback": self.sonnet}}}}))
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", d / "c.json"):
            fc.fc_config.apply_config()
        fc.fc_models._PROBED.clear()

    def test_linear_roles_take_the_pi_model(self):
        self.assertFalse(hasattr(fc, "LINEAR_ROLES"))
        for role in ("architect", "planner", "designer", "planner-2"):
            self.chain(role.split("-")[0])
            with mock.patch.object(fc.fc_models, "model_available", return_value=(True, "")) as probe:
                model, _, note, skipped = fc.fc_crew.fc_crewpick.crew_pick_full(role)
            self.assertEqual((model, skipped), (self.astra, []), role)
            self.assertNotIn("Linear", note)
            self.assertEqual(probe.call_args_list[0].args, (self.astra,))  # probed, not filtered out

    def test_a_failed_pi_probe_reports_its_error(self):
        for role in ("architect", "planner", "designer"):
            self.chain(role)
            with mock.patch.object(fc.fc_models, "model_available", side_effect=lambda m: (m != self.astra, "model not found")):
                model, _, note, skipped = fc.fc_crew.fc_crewpick.crew_pick_full(role)
            self.assertEqual(model, self.sonnet)
            self.assertEqual(skipped, [f"{self.astra} unavailable (model not found)"])
            self.assertNotIn("a pi session has no Linear tools", note)
        with mock.patch.object(fc.fc_models, "model_available", return_value=(False, "down")), self.assertRaises(SystemExit) as e:
            fc.fc_crew.fc_crewpick.crew_pick_full("designer")  # nothing runs: still exits naming each probe error
        self.assertIn(f"{self.astra} unavailable (down)", str(e.exception))


class ProbeCache(unittest.TestCase):
    """Only a pass is cached for the day: a failed probe is asked again on the next call, and its message is kept whole."""
    M, MID = "claude:claude-fable-5-1", "claude-fable-5-1"
    LONG = "  model not found: /Users/someone/.pi/agent/" + "very/long/" * 30 + "models.json (check the openrouter-fast preset)\n"

    def setUp(self):
        self.cache = Path(os.environ["HOME"]) / ".compose" / "model-probe.json"
        p = mock.patch.object(fc.fc_config, "PROBE_CACHE", self.cache); p.start(); self.addCleanup(p.stop)
        fc.fc_models._PROBED.clear(); self.addCleanup(fc.fc_models._PROBED.clear)

    def probes(self, *answers):
        return mock.patch.object(fc.fc_models, "probe_once", side_effect=list(answers))

    def entry(self, mid=None):
        return json.loads(self.cache.read_text()).get(mid or self.MID) if self.cache.exists() else None

    def test_a_pass_is_cached_for_the_day(self):
        with self.probes((True, "answered", False)) as run:
            self.assertEqual([fc.fc_models.model_available(self.M) for _ in "ab"], [(True, "answered")] * 2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.entry(), {"date": time.strftime("%Y-%m-%d"), "ok": True, "why": "answered"})
        fc.fc_models._PROBED.clear()  # a new CLI process the same day reads the file
        with self.probes() as run:
            self.assertEqual(fc.fc_models.model_available(self.M), (True, "answered"))
        run.assert_not_called()

    def test_a_failed_probe_is_not_cached_and_the_next_call_probes_again(self):
        for failed in ([(False, self.LONG.strip(), False)], [(False, "overloaded", True)] * 2):  # definite, and transient twice
            with self.subTest(failed=failed[0][1][:20]):
                fc.fc_models._PROBED.clear(); self.cache.unlink(missing_ok=True)
                with self.probes(*failed, (True, "answered", False)) as run:
                    self.assertEqual(fc.fc_models.model_available(self.M), (False, failed[0][1]))
                    self.assertNotIn(self.MID, fc.fc_models._PROBED)
                    self.assertIsNone(self.entry())
                    self.assertEqual(fc.fc_models.model_available(self.M), (True, "answered"))
                self.assertEqual(run.call_count, len(failed) + 1)
                self.assertTrue(self.entry()["ok"])

    def test_an_old_failing_entry_is_probed_again_and_replaced(self):
        today = time.strftime("%Y-%m-%d")
        self.cache.parent.mkdir(parents=True); self.cache.write_text(json.dumps({self.MID: {"date": today, "ok": False, "why": "no key"},
                                          "claude-opus-5-5": {"date": today, "ok": True, "why": "answered"}}))
        with self.probes((True, "answered", False)) as run:
            self.assertEqual(fc.fc_models.model_available(self.M), (True, "answered"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.entry(), {"date": today, "ok": True, "why": "answered"})
        self.assertTrue(self.entry("claude-opus-5-5")["ok"])  # other models' passes stay

class ClaudeProcGroup(unittest.TestCase):
    """Task 002 (NEB-919): claude_once starts through fc_proc.run; timeout and stop."""
    def setUp(self):
        self.addCleanup(fc.fc_proc.STOP.clear); self.addCleanup(fc.fc_proc.HOOKS.clear); self.addCleanup(fc.fc_proc.RUNNING.clear)

    def test_claude_once_starts_through_fc_proc_run_with_todays_argv_env_and_timeout(self):
        d = tempfile.mkdtemp(); (Path(d) / "AGENTS.md").write_text("Keep it short.\n")
        with mock.patch.object(fc.fc_proc, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"is_error": False, "result": "ok"}), "")) as run, \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("went around fc_proc.run")):
            self.assertEqual(fc.fc_claude.claude_once(d, "hi", timeout=9)[1], "ok")
        (argv,), kw = run.call_args
        self.assertEqual((argv[:3], argv[-2], kw["cwd"], kw["timeout"]), (["claude", "-p", "hi"], "--append-system-prompt", d, 9))
        self.assertEqual({k: v for k, v in kw["env"].items() if k != "COMPOSE_CONFIG"}, {k: v for k, v in fc.fc_claude.headless_env().items() if k != "COMPOSE_CONFIG"})

    def test_after_the_stop_claude_never_starts(self):
        d = Path(tempfile.mkdtemp()); (d / "bin").mkdir(); fake = d / "bin" / "claude"
        fake.write_text(f"#!/bin/sh\ntouch {d / 'started'}\necho '{{\"is_error\": false, \"result\": \"ok\"}}'\n"); fake.chmod(0o755)
        fc.fc_proc.STOP.set()
        with mock.patch.dict(os.environ, {"PATH": f"{d / 'bin'}{os.pathsep}{os.environ['PATH']}"}), self.assertRaises(fc.fc_proc.Stopped):
            fc.fc_claude.claude_once(str(d), "hi")
        self.assertFalse((d / "started").exists(), "claude started after the stop")


class ClaudeReadGuard(unittest.TestCase):
    """Task 008 (NEB-942): claude_once under --restricted, with the read guard hook when a guard file exists."""
    def claude(self, d):
        """claude_once in the clone `d / "repo"` with a fake `claude` first on PATH: its argv and its parsed --settings."""
        (d / "bin").mkdir(); (d / "repo").mkdir(); fake = d / "bin" / "claude"
        fake.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(d / 'argv.json')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                        "print(json.dumps({'is_error': False, 'result': 'ok'}))\n")
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{d / 'bin'}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(fc.fc_claude.claude_once(str(d / "repo"), "hi", model="claude-haiku-5-5")[1], "ok")
        argv = json.loads((d / "argv.json").read_text())
        self.assertIn("--restricted", argv); self.assertNotIn("--safe-mode", argv)  # under --safe-mode no --settings hook fires
        tools = "Read,Edit,Write,Bash,Grep,Glob"
        for flags in (["--strict-mcp-config"], ["--model", "claude-haiku-5-5"], ["--tools", tools], ["--allowedTools", tools]):
            self.assertIn(flags, [argv[i:i + len(flags)] for i in range(len(argv))])
        return argv, argv[argv.index("--settings") + 1]

    def test_without_a_guard_file_beside_the_clone_the_settings_stay_no_attribution(self):
        d = Path(tempfile.mkdtemp()); (d / "other.guard.json").write_text("{}")  # another clone's guard does not count
        self.assertEqual(self.claude(d)[1], fc.fc_claude.NO_ATTRIBUTION)

    def test_a_guard_file_adds_a_pre_tool_use_hook_that_blocks_whenever_the_guard_fails(self):
        d = Path(tempfile.mkdtemp()); guard = d / "repo.guard.json"; guard.write_text("{}"); script = d / "guard.sh"
        line = lambda g: f"sh {shlex.quote(str(script))} {shlex.quote(str(g))}"
        with mock.patch.object(fc.fc_readguard, "command", side_effect=line) as command:
            settings = json.loads(self.claude(d)[1])
        self.assertEqual([str(a) for (a,), _ in command.call_args_list], [str(guard)])
        hook = line(guard) + " || exit 2"
        self.assertEqual(settings, {**json.loads(fc.fc_claude.NO_ATTRIBUTION), "hooks": {"PreToolUse": [
            {"matcher": "Read|Grep|Glob|Bash", "hooks": [{"type": "command", "command": hook}]}]}})
        event = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}})
        deny = '{"hookSpecificOutput": {"permissionDecision": "deny"}}'
        for body, code, out, err in (("echo bad guard >&2; exit 1", 2, "", "bad guard\n"), ("exit 2", 2, "", ""),
                                     ("kill -9 $$", 2, "", None), ("exec no-such-python-x -I", 2, "", None),
                                     ("cat > /dev/null", 0, "", ""), (f"echo '{deny}'", 0, deny + "\n", "")):
            script.write_text(body + "\n")
            p = subprocess.run(["sh", "-c", hook], input=event, capture_output=True, text=True, cwd=d)
            self.assertEqual((p.returncode, p.stdout), (code, out), body)
            if err is not None:
                self.assertEqual(p.stderr, err, body)


class TestCrewTiers(unittest.TestCase):
    """A teammate starts on its role's tier under the default config; a pin lowers it, a raw --model never changes, and frontier starts drop to standard over the limit."""
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.json").write_text(json.dumps({"models": {"implementer": "pi:x/y", "crew": fc_defaults.DEFAULT_MODELS["crew"], "tiers": fc.fc_config.DEFAULT_TIERS}}))
        for p in (mock.patch.object(fc.fc_config, "CONFIG_PATH", d / "config.json"), mock.patch.object(fc.fc_config, "PROBE_CACHE", d / "probe.json"),
                  mock.patch.object(fc.fc_models, "model_available", return_value=(True, "answered")), mock.patch.object(fc.fc_mcp, "installed_servers", return_value=SERVERS),
                  mock.patch.object(fc.fc_pin, "cli_commit", return_value="abc1234"), mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"})):
            p.start(); self.addCleanup(p.stop)
        fc.fc_config.apply_config()
        for k in ("CREW_MODELS", "CREW_EFFORT", "CREW_FALLBACK", "TIERS"):
            self.addCleanup(setattr, fc.fc_config, k, {})

    def start(self, role, model=None, spend=0.0, pins=None):
        proj = self.proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        if pins:
            (proj / "results" / "tier.json").write_text(json.dumps(pins))
        herdr = lambda *args, check=False, timeout=120: {"tab": {"tab_id": "w9:t9"}, "root_pane": {"pane_id": "w9:p1"}} if args[:2] == ("tab", "create") else {}
        a = SimpleNamespace(action="start", role=role, repo=str(proj), project="p", model=model, title=None, brief="go", cwd=None, force=False)
        with mock.patch.object(fc.fc_herdr, "herdr", herdr), mock.patch.object(fc.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_core, "project_dir", return_value=proj), \
                mock.patch.object(fc.fc_herdr, "project_worktree", return_value=proj), mock.patch.object(fc.fc_herdr, "herdr_space", return_value="w9"), mock.patch.object(fc.fc_crew, "crew_preamble", return_value=""), \
                mock.patch.object(fc.fc_crew, "write_report"), mock.patch.object(fc.fc_claude, "claude_trusted", return_value=True), \
                mock.patch.object(fc.fc_herdr, "herdr_await_start"), mock.patch.object(fc_tierspend, "frontier", return_value=spend), contextlib.redirect_stdout(io.StringIO()):
            fc.fc_crew.cmd_crew(a)
        return json.loads(next((proj / "results" / "crew").glob("*.json")).read_text())["model"]

    def test_each_role_starts_on_its_tier(self):
        got = {r: fc.fc_crew.fc_crewpick.crew_model(r) for r in ("orchestrator", "groundwork", "explore", "tests", "planner", "architect")}
        sonnet, haiku, opus, fable = (f"claude:claude-{m}" for m in ("sonnet-5-5", "haiku-5-5", "opus-5-5", "fable-5-1"))
        self.assertEqual(got, {"orchestrator": sonnet, "groundwork": sonnet, "explore": sonnet, "tests": sonnet, "planner": opus, "architect": fable})

    def test_a_pin_lowers_the_start(self):
        self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick("planner", None, "no-frontier")[0], "claude:claude-sonnet-5-5")
        self.assertEqual(fc.fc_crew.fc_crewpick.crew_pick("orchestrator", None, "cheap")[0], "claude:claude-haiku-5-5")

    def test_frontier_starts_run_at_standard_over_the_limit_and_raw_models_do_not(self):
        self.assertEqual(self.start("planner", spend=25.0), "claude:claude-sonnet-5-5")
        self.assertEqual(self.start("planner", spend=1.0), "claude:claude-opus-5-5")
        self.assertEqual(self.start("planner", "claude:claude-opus-5-5", spend=25.0), "claude:claude-opus-5-5")

    def test_a_small_start_is_cheap_work_the_frontier_limit_neither_holds_nor_reports(self):
        status = lambda: self.proj / "results" / "crew" / "frontier-budget.status"
        self.assertEqual(self.start("planner", "small", spend=25.0), "claude:claude-haiku-5-5")
        self.assertFalse(status().exists())
        self.start("planner", spend=25.0); self.assertTrue(status().exists())

    def test_a_project_pin_changes_the_tier_a_teammate_starts_on(self):
        self.assertEqual(self.start("orchestrator"), "claude:claude-sonnet-5-5")
        self.assertEqual(self.start("orchestrator", pins={"project": "cheap"}), "claude:claude-haiku-5-5")
        self.assertEqual(self.start("planner", pins={"project": "no-frontier"}), "claude:claude-sonnet-5-5")



class ReadinessModel(unittest.TestCase):
    def read(self, value):
        d = Path(tempfile.mkdtemp())
        models = {"implementer": "pi:x/y", **({} if value is None else {"readiness": value})}
        (d / "config.json").write_text(json.dumps({"models": models}))
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", d / "config.json"), mock.patch.object(fc.fc_models, "model_available", return_value=(True, "answered")), \
                mock.patch.object(fc.fc_pin, "pin", return_value={}):
            fc.fc_config.apply_config()
        self.addCleanup(setattr, fc.fc_config, "READINESS_MODEL", ("claude:claude-opus-5-5", "high"))
        return fc.fc_config.READINESS_MODEL

    def test_readiness_model_reads_a_string_or_an_entry_and_must_be_claude(self):
        self.assertEqual(self.read("claude:claude-fable-5-1"), ("claude:claude-fable-5-1", None))
        self.assertEqual(self.read({"model": "claude:claude-sonnet-5-5", "effort": "medium"}), ("claude:claude-sonnet-5-5", "medium"))
        self.assertEqual(self.read(None), ("claude:claude-opus-5-5", "high"))
        with self.assertRaises(SystemExit) as e:
            self.read("pi:x/y")
        self.assertIn("models.readiness must be a claude: model id", str(e.exception))


class NightModel(unittest.TestCase):
    def read(self, value):
        d = Path(tempfile.mkdtemp())
        models = {"implementer": "pi:x/y", **({} if value is None else {"night": value})}
        (d / "config.json").write_text(json.dumps({"models": models}))
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", d / "config.json"), mock.patch.object(fc.fc_models, "model_available", return_value=(True, "answered")), \
                mock.patch.object(fc.fc_pin, "pin", return_value={}):
            fc.fc_config.apply_config()
        self.addCleanup(setattr, fc.fc_config, "NIGHT_MODEL", ("claude:claude-opus-5-5", "high"))
        return fc.fc_config.NIGHT_MODEL

    def test_night_model_reads_a_string_or_an_entry_and_must_be_claude(self):
        self.assertEqual(self.read("claude:claude-fable-5-1"), ("claude:claude-fable-5-1", None))
        self.assertEqual(self.read(None), ("claude:claude-opus-5-5", "high"))
        with self.assertRaises(SystemExit) as e:
            self.read("pi:x/y")
        self.assertIn("models.night must be a claude: model id", str(e.exception))


if __name__ == "__main__":
    unittest.main()
