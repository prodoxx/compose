import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(ROOT / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

KEY = "sk-or-v1-test" + "0" * 52  # the workspace's OpenRouter key
TYPESAFE = "ts-test-" + "1" * 32  # TypeSafe's key in auth.json
OTHER = "other-test-" + "2" * 32  # any *_API_KEY under `env`
LINEAR = "lin-test-" + "3" * 32  # the Linear key in auth.json
SECRET_NAMES = ("OPENROUTER_API_KEY", "TEXT_MODEL_API_KEY", "TYPESAFE_API_KEY", "OPENCODE_CONFIG_CONTENT", "OTHER_API_KEY")
USER_AUTH = json.dumps({"workspaces": {"default": {"openrouter": "sk-or-v1-user" + "4" * 52}}})  # the user's own ~/.compose/auth.json, under the test's HOME


class Secrets(unittest.TestCase):
    """compose's keys never reach project code's env, and never a text it sends to a model or writes to results/."""
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        cfg = self.root / "home" / "config.json"
        cfg.parent.mkdir()
        cfg.write_text(json.dumps({"env": {"OTHER_API_KEY": OTHER, "PLAIN": "x"}}))
        (cfg.parent / "auth.json").write_text(json.dumps({"workspaces": {"default": {"openrouter": KEY, "linear": LINEAR}}, "typesafe": TYPESAFE}))
        (self.root / "user" / ".compose").mkdir(parents=True)
        (self.root / "user" / ".compose" / "auth.json").write_text(USER_AUTH)
        for p in (mock.patch.object(fc.fc_config, "CONFIG_PATH", cfg), mock.patch.object(fc.fc_config, "ENV_FILE", cfg.parent / ".env"),
                  mock.patch.object(fc.fc_config, "AUTH_PATH", cfg.parent / "auth.json"),
                  mock.patch.object(fc.fc_config, "OPENROUTER_KEY", None), mock.patch.object(fc.fc_config, "LINEAR_KEY", None),
                  mock.patch.object(fc.fc_core, "SECRET_VARS", set(fc.fc_core.SECRET_VARS)), mock.patch.object(fc.fc_core, "SECRET_VALUES", set()),
                  mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": "{}", "HOME": str(self.root / "user")})):
            p.start()
            self.addCleanup(p.stop)
        fc.fc_config.apply_config()

    def assertScratch(self, config):
        """config is a COMPOSE_CONFIG in a directory of its own, not the user's ~/.compose and not the CLI's own config."""
        self.assertNotIn(str(Path.home()), config)
        self.assertNotEqual(Path(config), fc.fc_config.CONFIG_PATH)
        self.assertEqual(Path(config).name, "config.json")

    def assertClean(self, text, names=True):
        for v in (KEY, TYPESAFE, OTHER, LINEAR):
            self.assertNotIn(v, text)
        for n in SECRET_NAMES if names else ():
            self.assertNotIn(n, text)

    def run_test(self, test):
        """A task's test command, the way run and the stack run it, in a plain folder."""
        return fc.fc_implement.run_test({"test": test}, self.root)

    def results_text(self, proj):
        return "\n".join(f.read_text() for f in sorted((proj / "results").rglob("*")) if f.is_file())

    # ---------- the env half ----------

    def test_a_task_test_runs_without_the_secret_vars(self):
        out = self.run_test("env | grep -E '^(PLAIN|COMPOSE_CONFIG)=|_API_KEY|^OPENCODE_|-test-?[0-9]'; true").stdout
        self.assertIn("PLAIN", out)  # the rest of the env is there
        self.assertIn("COMPOSE_CONFIG", out)
        self.assertClean(out)
        self.assertScratch(next(l for l in out.splitlines() if l.startswith("COMPOSE_CONFIG=")).split("=", 1)[1])

    def test_compose_timed_passes_the_callers_own_keys_through_and_redacts_them_in_its_output(self):
        env = {**os.environ, "OPENROUTER_API_KEY": KEY, "TYPESAFE_API_KEY": TYPESAFE}
        env.pop("COMPOSE_CONFIG", None)
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "compose.py"), "timed", "30", "--",
                            f'test "$OPENROUTER_API_KEY" = {KEY} && test "$TYPESAFE_API_KEY" = {TYPESAFE} && env'], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("OPENROUTER_API_KEY=<redacted>", p.stdout)
        self.assertIn("TYPESAFE_API_KEY=<redacted>", p.stdout)
        self.assertNotIn(KEY, p.stdout)
        self.assertNotIn(TYPESAFE, p.stdout)

    def test_compose_code_under_test_keeps_its_state_out_of_the_users_compose_home_and_a_write_there_is_flagged(self):
        env = {**os.environ}
        env.pop("COMPOSE_CONFIG", None)
        timed = lambda cmd: subprocess.run([sys.executable, str(ROOT / "bin" / "compose.py"), "timed", "30", "--", cmd],
                                           capture_output=True, text=True, env=env, cwd=self.root)
        where = ("import sys; sys.path.insert(0, sys.argv[1]); import fc_config, fc_hooks; "
                 "print(fc_config.CONFIG_PATH, fc_config.ENV_FILE, fc_config.PROBE_CACHE, fc_hooks.SKILL_SEEN)")
        p = timed(f"python3 -c '{where}' {ROOT / 'bin'}")
        self.assertEqual(p.returncode, 0, p.stderr)
        paths = [Path(x) for x in p.stdout.split()]
        self.assertScratch(str(paths[0]))
        self.assertEqual({x.parent for x in paths}, {paths[0].parent})
        p = timed("python3 -c \"import os, pathlib; pathlib.Path(os.environ['COMPOSE_CONFIG']).write_text('{}')\"")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("WARNING", p.stdout + p.stderr)
        p = timed("echo '{}' > \"$HOME/.compose/config.json\"; echo x > \"$HOME/.compose/auth.json\"")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("WARNING: ~/.compose changed while", p.stdout)
        self.assertIn("auth.json, config.json", p.stdout)
        (self.root / "user" / ".compose" / "auth.json").write_text(USER_AUTH)
        p = timed(f"env -u COMPOSE_CONFIG {sys.executable} {ROOT / 'bin' / 'compose.py'} workspace add mine < /dev/null")  # a compose it runs on the user's config
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("WARNING: ~/.compose changed while", p.stdout)
        self.assertIn("config.json", p.stdout)
        writer = self.root / "writer.py"  # a compose that ignores its scratch COMPOSE_CONFIG, in a session of its own
        writer.write_text("import pathlib, subprocess, sys\n"
                          "if sys.argv[1] == 'detach':\n"
                          "    sys.exit(subprocess.run([sys.executable, __file__, 'write', sys.argv[2]], start_new_session=True).returncode)\n"
                          "sys.path.insert(0, sys.argv[2]); import fc_core\n"
                          "fc_core.atomic_write(pathlib.Path.home() / '.compose' / 'config.json', '{\"workspace\": \"x\"}')\n")
        p = timed(f"{sys.executable} {writer} detach {ROOT / 'bin'}")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("WARNING: ~/.compose changed while", p.stdout)
        self.assertIn("config.json", p.stdout)

    def test_a_write_another_compose_makes_to_the_users_compose_home_or_a_change_to_the_cli_checkout_is_not_blamed_on_a_timed_command(self):
        home = self.root / "user" / ".compose"
        (home / "src").mkdir()
        env = {**os.environ}
        env.pop("COMPOSE_CONFIG", None)
        started, go = self.root / "started", self.root / "go"
        p = subprocess.Popen([sys.executable, str(ROOT / "bin" / "compose.py"), "timed", "30", "--",
                              f"touch {started}; while [ ! -e {go} ]; do sleep 0.05; done"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(200):
            if started.exists():
                break
            time.sleep(0.05)
        w = subprocess.run([sys.executable, str(ROOT / "bin" / "compose.py"), "workspace", "add", "other"], capture_output=True, text=True,
                           env=env, stdin=subprocess.DEVNULL)  # another session's compose writes config.json and auth.json
        self.assertEqual(w.returncode, 0, w.stderr)
        (home / "src" / "new-worktree").mkdir()  # git or treehouse in another session
        (home / "auth.json").touch()  # rewritten with the same bytes
        (home / "live").mkdir()  # a run in another project holds it with its pid's file (fc_update.hold)
        (home / "live" / "27545.json").write_text("{}")
        (home / "linear-get").mkdir()  # another session's `compose linear get` caches a read
        (home / "linear-get" / "0123456789ab-ba9876543210.json").write_text("{}")
        for d in ("started", "usage"):  # another session's timed run, implementer or status call records itself and its session usage
            (home / d).mkdir(exist_ok=True)
            (home / d / "1.json").write_text("{}")
        for f in ("load.json", "load.lock", "shed.json"):  # the machine-wide load hold, its lock and the shed's state
            (home / f).write_text("{}")
        go.touch()
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("other", (home / "config.json").read_text())
        self.assertNotIn("WARNING", out + err)

    def test_the_registry_files_another_compose_keeps_in_the_users_compose_home_are_not_blamed_on_a_timed_command_but_a_stray_file_is(self):
        home = self.root / "user" / ".compose"
        env = {**os.environ}
        env.pop("COMPOSE_CONFIG", None)
        started, go = self.root / "started", self.root / "go"
        p = subprocess.Popen([sys.executable, str(ROOT / "bin" / "compose.py"), "timed", "30", "--",
                              f"touch {started}; while [ ! -e {go} ]; do sleep 0.05; done"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(200):
            if started.exists():
                break
            time.sleep(0.05)
        hold = ("import sys; sys.path.insert(0, sys.argv[1]); import fc_update; fc_update.hold('run', 'other', sys.argv[2]); "
                "open(sys.argv[3], 'a').write('{}\\n')")
        w = subprocess.run([sys.executable, "-c", hold, str(ROOT / "bin"), str(self.root), str(home / "tool-bugs.jsonl")],
                           capture_output=True, text=True, env=env)  # another project's run registers itself, a toolsmith files a tool-bug
        self.assertEqual(w.returncode, 0, w.stderr)
        (home / "test-slots").mkdir(exist_ok=True)
        (home / "live").mkdir(exist_ok=True)
        (home / "live" / "1.json").write_text("{}")  # a run still alive when the command ends
        go.touch()
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertNotIn("WARNING", out + err)
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "compose.py"), "timed", "30", "--", "echo x > \"$HOME/.compose/stray.json\""],
                           capture_output=True, text=True, env=env, cwd=self.root)
        self.assertIn("WARNING: ~/.compose changed while", p.stdout)
        self.assertIn("stray.json", p.stdout)

    def test_the_journal_of_writes_to_the_users_compose_home_stays_small_and_keeps_the_newest(self):
        home = self.root / "user" / ".compose"
        with mock.patch.object(fc.fc_core, "HOME_WRITES_MAX", 2000):
            for i in range(100):
                fc.fc_core.atomic_write(home / "config.json", json.dumps({"n": i}))
        journal = home / fc.fc_core.HOME_WRITES
        lines = [json.loads(l) for l in journal.read_text().splitlines()]
        self.assertLess(journal.stat().st_size, 2300)
        self.assertEqual(lines[-1]["sha"], fc.fc_core.entry_state(home / "config.json"))
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        fc.fc_core.atomic_write(self.root / "elsewhere.json", "{}")  # only writes to the user's ~/.compose are journaled
        self.assertEqual(len(journal.read_text().splitlines()), len(lines))

    def test_verify_checks_and_e2e_drivers_run_without_them(self):
        # verify and jev keep only an output's tail, so the check prints just the lines asserted on, not a caller's long env:
        # PLAIN, COMPOSE_CONFIG, and any secret's name or value, matched without spelling one out (the command lands in findings)
        env = "env | grep -E '^(PLAIN|COMPOSE_CONFIG)=|_API_KEY|^OPENCODE_|-test-?[0-9]'; exit 1"
        r = fc.fc_jev.driver_e2e({"name": "d", "setup": f"env > {self.root / 'setup-env'}", "setup_wait": 1, "driver": env}, self.root)
        self.assertIn("PLAIN", r["stdout"])
        self.assertClean(r["stdout"])
        self.assertClean((self.root / "setup-env").read_text())
        a = SimpleNamespace(skip_e2e=True, skip_review=True)
        with mock.patch.object(fc.fc_herdr, "herdr_verify_panes", return_value={}), mock.patch.object(fc.fc_green, "tree_of", return_value=""):
            ok, report, findings = fc.fc_verify.run_stages(self.root, self.root, {"checks": [env]}, a)
        self.assertFalse(ok)
        self.assertIn("PLAIN", report["checks"][0]["output"])
        self.assertClean(report["checks"][0]["output"])
        self.assertClean(findings)
        self.assertScratch(next(l for l in report["checks"][0]["output"].splitlines() if l.startswith("COMPOSE_CONFIG=")).split("=", 1)[1])
        self.assertScratch(next(l for l in r["stdout"].splitlines() if l.startswith("COMPOSE_CONFIG=")).split("=", 1)[1])
        out = io.StringIO()
        with mock.patch.object(fc.fc_herdr, "herdr_verify_panes", return_value={}), mock.patch.object(fc.fc_green, "tree_of", return_value=""), mock.patch("sys.stdout", out):
            fc.fc_verify.run_stages(self.root, self.root, {"checks": ["echo x > \"$HOME/.compose/auth.json\""]}, a)
        self.assertIn("WARNING: ~/.compose changed while", out.getvalue())
        self.assertIn("auth.json", out.getvalue())

    def test_harnesses_get_the_key_but_no_other_secret(self):
        run = mock.Mock(return_value=mock.Mock(stdout='{"type":"tool_execution_start"}', stderr="", returncode=0))
        with mock.patch.object(fc.fc_proc, "run", run), mock.patch.object(subprocess, "run", side_effect=AssertionError("went around fc_proc.run")):
            fc.fc_models.pi_turn(str(self.root), "openrouter-fast/x/y", "hi")
        cmd, env = run.call_args[0][0], run.call_args[1]["env"]
        self.assertClean(json.dumps(cmd))  # pi's key is in its own env only, never in argv
        self.assertTrue(env.pop("OPENROUTER_API_KEY") == KEY, "pi's env lacks the workspace key")
        self.assertIn("PLAIN", env)
        self.assertClean(json.dumps(env))
        self.assertScratch(env["COMPOSE_CONFIG"])

        run = mock.Mock(return_value=mock.Mock(stdout='{"type":"text","sessionID":"s","part":{"text":"ok"}}', stderr="", returncode=0))
        with mock.patch.object(fc.fc_proc, "run", run), mock.patch.object(subprocess, "run", side_effect=AssertionError("went around fc_proc.run")):
            fc.fc_models.opencode(str(self.root), "openrouter/x", "hi")
        env = run.call_args[1]["env"]
        self.assertEqual(env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"], "1")  # its ~/.claude/skills scan can hang on ENAMETOOLONG
        self.assertClean(json.dumps({k: v for k, v in env.items() if k != "OPENCODE_CONFIG_CONTENT"}))
        self.assertNotIn(KEY, env["OPENCODE_CONFIG_CONTENT"])
        f = Path(json.loads(env["OPENCODE_CONFIG_CONTENT"])["provider"]["openrouter"]["options"]["apiKey"][len("{file:"):-1])
        self.assertEqual(f.read_text(), KEY)
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")

        self.assertClean(json.dumps(fc.fc_claude.headless_env()))
        self.assertScratch(fc.fc_claude.headless_env()["COMPOSE_CONFIG"])

    def test_the_herdr_pi_pane_gets_only_the_openrouter_key_in_its_env(self):
        panes, starts = [], []
        with mock.patch.object(fc.fc_herdr, "herdr", return_value=None), \
                mock.patch.object(fc.fc_herdr, "herdr_implementer_pane", lambda space, cwd, env, label: panes.append(env) or "p1"), \
                mock.patch.object(fc.fc_herdr, "herdr_agent_start", lambda *a: starts.append(a) or {}), \
                mock.patch.object(fc.fc_herdr, "herdr_error", return_value=(None, "timeout")):
            with self.assertRaises(subprocess.TimeoutExpired):
                fc.fc_implement.herdr_pi(str(self.root / "wt"), "openrouter-fast/x/y", "hi", None,
                                         {"meta": {"id": "001"}, "proj": self.root / "proj", "space": "s"}, timeout=1)
        self.assertTrue(panes[0].pop("OPENROUTER_API_KEY") == KEY, "the pane's env lacks the workspace key")
        self.assertClean(json.dumps(panes[0]))
        self.assertScratch(panes[0]["COMPOSE_CONFIG"])
        self.assertClean(json.dumps(starts[0]))

    # ---------- the scrub half ----------

    def test_test_output_that_prints_a_key_is_scrubbed(self):
        (self.root / "leak").write_text(f"{KEY} {TYPESAFE} {OTHER} {LINEAR}")
        r = self.run_test(f"cat '{self.root / 'leak'}'")
        self.assertIn(fc.fc_core.REDACTED, r.stdout)
        self.assertClean(r.stdout, names=False)

    def test_model_payloads_are_scrubbed_and_the_auth_header_kept(self):
        sent = []
        def urlopen(req, timeout=None):
            sent.append(req)
            body = {"choices": [{"message": {"content": '{"summary": "x"}'}}], "usage": {}}
            return mock.MagicMock(__enter__=lambda s: io.StringIO(json.dumps(body)), __exit__=lambda *a: False)
        with mock.patch("urllib.request.urlopen", urlopen):
            fc.fc_models.fast_json({"TEXT_MODEL_API_KEY": KEY}, f"system {KEY}", f"user {TYPESAFE}")
            fc.fc_models.jev_ask({"TYPESAFE_API_KEY": TYPESAFE}, {}, {"evidence": f"saw {OTHER}"}, {"q": {"instructions": KEY}}, "t")
        self.assertEqual([r.get_header("Authorization") for r in sent], [f"Bearer {KEY}", f"Bearer {TYPESAFE}"])
        for r in sent:
            self.assertClean(r.data.decode(), names=False)
            self.assertIn(fc.fc_core.REDACTED, r.data.decode())
        seen = []
        with mock.patch.object(fc.fc_models, "pi_turn", lambda cwd, model, prompt, session, timeout: seen.append(prompt) or ("s", "", 0, {})):
            fc.fc_implement.implementer(str(self.root), "pi:x/y", f"fix this: {KEY}")
        self.assertEqual(seen, [f"fix this: {fc.fc_core.REDACTED}"])

    def test_results_files_and_the_cli_output_are_scrubbed_but_auth_json_keeps_its_keys(self):
        proj = self.root / "proj"
        (proj / "results").mkdir(parents=True)
        fc.fc_core.atomic_write(proj / "results" / "001.json", json.dumps({"log": [{"test_output": KEY}]}))
        fc.fc_change.write_change(proj, {"id": "C1", "results": {"001": {"log": [{"test_output": TYPESAFE}]}}})
        fc.fc_change.reflect(proj, "task:001", f"task 001 failed: {OTHER}")
        self.assertClean(self.results_text(proj), names=False)
        self.assertIn(fc.fc_core.REDACTED, (proj / "results" / "changes" / "C1.json").read_text())
        out = io.StringIO()
        fc.fc_core.Scrubbed(out).write(f"error {LINEAR}\n")
        self.assertEqual(out.getvalue(), f"error {fc.fc_core.REDACTED}\n")
        fc.fc_config.fc_auth.set_key(fc.fc_config.AUTH_PATH, "w", "openrouter", OTHER)
        self.assertEqual(fc.fc_config.fc_auth.read_auth(fc.fc_config.AUTH_PATH)["workspaces"], {"default": {"openrouter": KEY, "linear": LINEAR}, "w": {"openrouter": OTHER}})


if __name__ == "__main__":
    unittest.main()
