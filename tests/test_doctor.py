import hermetic  # first: no test sees the real home
import io, json, os, re, shutil, stat, subprocess, sys, tempfile, unittest, urllib.error, urllib.parse
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
sys.modules.pop("compose", None)  # another test file may have imported it against the fc_ modules just dropped
import compose as fc
import fc_steps, fc_auth, fc_checks

ORIGINAL_PATH = os.environ["PATH"]
CLI = str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")
TOOLS = {"herdr": "0.9.3", "treehouse": "v3.1.2", "trail": "0.23.0", "gh": "gh version 2.94.0", "pi": "1.0.3", "claude": "2.1.301 (Claude Code)", "npx": "11.19.0", "bp": ""}
SKILLS = (".claude/skills/herdr/SKILL.md", ".claude/plugins/marketplaces/i-have-adhd/skills/i-have-adhd/SKILL.md",
          ".claude/plugins/cache/official/superpowers/7.0.0/skills/writing-plans/SKILL.md", ".config/figma-context-mcp/.env")


class Doctor(unittest.TestCase):
    """check_deps walks the setup steps table with its check: one MISSING line per row that is not ok, and only the required
    rows come back (those stop init)."""
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp()) / "bin"
        self.bin.mkdir()
        self.home = Path(tempfile.mkdtemp())
        for tool, version in TOOLS.items():
            self.fake(tool, version)
        for s in SKILLS:
            (self.home / s).parent.mkdir(parents=True)
            (self.home / s).write_text("# skill")
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"framelink-figma": {"command": "npx"}}}))
        self.herdr_config('[ui.toast]\ndelivery = "off"\n\n[ui.sound]\nenabled = false\n')
        self.env = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def fake(self, tool, version, body=""):
        f = self.bin / tool
        f.write_text(f"#!/bin/sh\n{body}\necho '{version}'\n")
        f.chmod(stat.S_IRWXU)

    def herdr_config(self, text):
        (self.home / ".config" / "herdr").mkdir(parents=True, exist_ok=True)
        (self.home / ".config" / "herdr" / "config.toml").write_text(text)

    def deps(self, role=None):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            bad = fc.fc_init.check_deps(role)
        return bad, [l for l in out.getvalue().splitlines() if l.startswith("MISSING")]

    def rows(self, *names):  # the real rows by name, in table order
        return mock.patch.object(fc_steps, "STEPS", tuple(r for r in fc_steps.STEPS if r[0] in names))

    def test_rows_report_misses_floors_and_levels(self):
        fake = mock.patch.object(fc_steps, "STEPS", (
            ("herdr", "herdr", "see https://herdr.dev", "required", False, "run"),
            ("treehouse", "treehouse", "brew install treehouse", "default", False, "run"),
            ("gh stack", ["gh", "stack", "--version"], "gh extension install github/gh-stack", "required", False, "run"),
            ("herdr skill", SKILLS[0], "herdr --skill", "required", False, "run"),
            ("superpowers", ".claude/plugins/cache/*/superpowers/*/skills/writing-plans/SKILL.md", "claude plugin install superpowers", "default", True, "run"),
            ("framelink-figma", lambda c: ("framelink-figma" in fc_steps.user_mcp_servers(), ""), "claude mcp add x", "designer", True, "run")))
        with fake:
            self.assertEqual(self.deps(), ([], []))
            self.fake("herdr", "herdr 0.8.0")
            self.fake("treehouse", "v2.0.1")
            self.fake("gh", "gh version 2.94.0", body='[ "$1" = stack ] && exit 1')
            (self.home / SKILLS[0]).unlink()
            (self.home / SKILLS[2]).unlink()
            (self.home / ".claude.json").unlink()
            self.assertEqual(self.deps(), (["herdr", "gh stack", "herdr skill"], [
                "MISSING: herdr: 0.8.0 is older than 0.9.3 (install: see https://herdr.dev)",
                "MISSING: treehouse: 2.0.1 is older than 3.1.2 (install: brew install treehouse) - optional",
                "MISSING: gh stack (install: gh extension install github/gh-stack)",
                "MISSING: herdr skill (install: herdr --skill)",
                "MISSING: superpowers (install: claude plugin install superpowers) - optional"]))
            self.assertEqual(self.deps("designer"), (["framelink-figma"], ["MISSING: framelink-figma (install: claude mcp add x)"]))

    def test_dangling_owned_link(self):
        self.home = self.home.resolve()
        os.environ["HOME"] = str(self.home)
        link = self.home / ".local" / "bin" / "compose"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.home / "gone" / "compose")
        with self.rows("compose CLI link"):
            self.assertEqual(self.deps(), (["compose CLI link"], [
                f"MISSING: compose CLI link: dangling link {link} -> {self.home}/gone/compose (install: compose setup)"]))

    def test_key_rows_name_the_key_never_a_value(self):
        auth = self.home / ".config" / "compose" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"workspaces": {"default": {"openrouter": "sk-or-test"}}}))
        (self.home / SKILLS[-1]).write_text("FIGMA_API_KEY=figd-test\n")
        out = io.StringIO()
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", auth.parent / "config.json"), mock.patch.object(fc.fc_config, "AUTH_PATH", auth), \
             mock.patch.object(fc.fc_config, "ENV_FILE", auth.parent / ".env"), mock.patch.object(fc_steps, "FRAMELINK_ENV", self.home / SKILLS[-1]), \
             self.rows(*(r[0] for r in fc_steps.STEPS if r[5] == "key")), mock.patch("sys.stdout", out):
            bad = fc.fc_init.check_deps()
        self.assertEqual(("TypeSafe key" in bad, "OpenRouter key" in bad, "framelink-figma token" in out.getvalue()), (True, False, False))
        self.assertEqual(len([l for l in out.getvalue().splitlines() if l.startswith("MISSING: TypeSafe key") and "(install: " in l]), 1)
        self.assertFalse({"sk-or-test", "figd-test"} & set(re.findall(r"[\w-]+", out.getvalue())))

    def test_init_and_designer_start_stop_on_a_miss(self):
        (self.home / ".claude.json").unlink()
        repo = Path(tempfile.mkdtemp())  # not even a git repository: init must stop before it touches anything
        env = {**os.environ, "COMPOSE_NO_TREEHOUSE": "1"}
        r = subprocess.run([sys.executable, CLI, "init", "--project", "p", "--repo", str(repo)], capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, list(repo.iterdir())), (1, []))
        self.assertIn("init stopped", r.stderr)
        r = subprocess.run([sys.executable, CLI, "designer", "--project", "p", "--repo", str(repo)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("MISSING: framelink-figma (", r.stdout)
        self.assertIn("designer stopped", r.stderr)

    def test_herdr_toasts_and_sounds_warn_when_not_off(self):
        """herdr's own toast and sound fire for every agent that finishes or needs input, so doctor names the two keys to set
        when either is on; delivery defaults to off, sound to on, so a missing key counts as its default."""
        self.assertIsNone(fc.fc_init.herdr_toast_warning())
        self.herdr_config('onboarding = false\n[ui]\nagent_panel_sort = "spaces"  # a comment\n\n[ui.toast]\ndelivery = "system"\n')
        w = fc.fc_init.herdr_toast_warning()
        self.assertTrue(w.startswith("WARNING: herdr alerts the product owner for every agent, not only the engineering manager (ui.toast.delivery=system, ui.sound.enabled=true)"), w)
        self.assertIn('set [ui.toast] delivery = "off" and [ui.sound] enabled = false in ~/.config/herdr/config.toml', w)
        self.assertEqual(len(w.splitlines()), 1)
        self.herdr_config('[ui.sound]\nenabled = false\n')  # no [ui.toast] at all: herdr's default delivery is off
        self.assertIsNone(fc.fc_init.herdr_toast_warning())
        (self.home / ".config" / "herdr" / "config.toml").unlink()  # no config: sounds are on by default
        self.assertIn("(ui.sound.enabled=true)", fc.fc_init.herdr_toast_warning())


    GH_WARNING = "WARNING: gh is not logged in: compose bug reports are saved, not filed on GitHub (run: gh auth login)"
    READY = "every step compose needs is ready"

    def doctor(self, fails=()):
        """cmd_doctor in process with a gh that logs its calls and exits 1 (chattering on both streams) for each `fails` first
        word pair, such as "auth status"; returns (exit status, stdout lines, gh calls)."""
        log = self.bin.parent / "gh-calls"
        checks = "".join(f'[ "$1 $2" = "{f}" ] && {{ echo "not logged in to github.com"; echo "token invalid" >&2; exit 1; }}\n' for f in fails)
        self.fake("gh", "gh version 2.94.0", body=f'echo "$*" >> {log}\n{checks}')
        out, status = io.StringIO(), 0
        with mock.patch("sys.stdout", out), self.rows("gh", "gh stack", "claude"):  # the rows these fakes stand for
            try:
                fc.fc_init.cmd_doctor(None)
            except SystemExit as e:
                status = 1 if isinstance(e.code, str) else e.code
        return status, out.getvalue().splitlines(), log.read_text().splitlines() if log.exists() else []

    def test_doctor_warns_when_gh_is_logged_out(self):
        status, lines, calls = self.doctor(fails=["auth status"])
        self.assertEqual((status, lines), (0, [self.GH_WARNING, self.READY]))  # gh's own text stays out
        self.assertIn("auth status", calls)

    def test_doctor_is_quiet_when_gh_is_logged_in(self):
        status, lines, calls = self.doctor()
        self.assertEqual((status, lines), (0, [self.READY]))
        self.assertIn("auth status", calls)

    def test_doctor_shows_the_gh_warning_and_herdr_row_and_still_passes(self):
        self.herdr_config('[ui.toast]\ndelivery = "system"\n')
        status, lines, _ = self.doctor(fails=["auth status"])
        self.assertEqual(status, 0)
        self.assertEqual(sorted(l.split(":")[0] for l in lines[:-1]), ["MISSING", "WARNING"])
        self.assertIn(self.GH_WARNING, lines)
        self.assertEqual(lines[-1], self.READY)

    def test_a_missing_tool_still_runs_the_gh_auth_check_then_exits_1(self):
        (self.bin / "claude").unlink()
        for fails, miss, last in ((["auth status"], "MISSING: claude (install: curl -fsSL https://claude.ai/install.sh | bash)", "1 required step missing"),
                                  (["auth status", "stack --version"], "MISSING: gh stack (install: gh extension install github/gh-stack)",
                                   "1 required step missing: run compose setup")):
            status, lines, calls = self.doctor(fails=fails)
            self.assertEqual((status, miss in lines, self.GH_WARNING in lines, "auth status" in calls), (1, True, True, True), lines)
            self.assertEqual(lines[-1], last)  # claude is manual: setup never installs it
            self.fake("claude", TOOLS["claude"])  # then gh itself on PATH without its stack extension: the same

class DoctorNotes(unittest.TestCase):
    """Without claude, doctor names the rows that need it on one line under the Claude Code install line; keys still in
    config.json or .env get one WARNING naming each key and file, never a value."""
    setUp, fake, herdr_config = Doctor.setUp, Doctor.fake, Doctor.herdr_config
    CURL = "curl -fsSL https://claude.ai/install.sh | bash"
    ROWS = (("claude", "claude", CURL, "required", False, "manual"), ("gh", "gh", "brew install gh", "required", False, "run"),
            ("i-have-adhd skill", lambda c: (False, ""), "claude plugin install a", "required", True, "run"),
            ("superpowers", lambda c: (False, ""), "claude plugin install b", "default", True, "run"),
            ("herdr claude integration", lambda c: (True, ""), "herdr integration install claude", "default", True, "run"),
            ("uv", lambda c: (False, ""), "brew install uv", "default", False, "run"))

    def doctor(self, rows=ROWS, keys=()):
        out, code = io.StringIO(), 0
        with mock.patch.object(fc_steps, "STEPS", rows), mock.patch.object(fc_auth, "old_keys", return_value=list(keys)) as self.old:
            with mock.patch("sys.stdout", out):
                try:
                    fc.fc_init.cmd_doctor(None)
                except SystemExit as e:
                    code = e.code
            with mock.patch("sys.stdout", io.StringIO()):
                self.bad = fc.fc_init.check_deps()
        return code, out.getvalue().splitlines()

    def test_rows_waiting_on_claude_share_one_line(self):
        (self.bin / "claude").unlink()
        code, lines = self.doctor()
        missing = [l for l in lines if l.startswith("MISSING")]
        self.assertEqual((code, self.bad, len(missing)), (1, ["claude", "i-have-adhd skill"], 2))
        self.assertTrue(re.match(r"MISSING: claude\b", missing[0]) and self.CURL in missing[0] and missing[1].startswith("MISSING: uv"), missing)
        wait = [l for l in lines if l.startswith("waiting on Claude Code: ")]
        self.assertEqual(len(wait), 1, lines)
        self.assertGreater(lines.index(wait[0]), lines.index(missing[0]))
        self.assertEqual([n in wait[0] for n in ("i-have-adhd skill", "superpowers", "herdr claude integration", "uv", "gh")], [True, True, False, False, False])

    def test_old_keys_warn_once_by_name_and_file(self):
        cfg, env = self.home / "config.json", self.home / ".env"
        cfg.write_text(json.dumps({"openrouter_api_key": "sk-or-test", "workspaces": {"w": {"linear_api_key": "lin-test"}}}))
        env.write_text("TYPESAFE_API_KEY=ts-test\n")
        keys = [("openrouter_api_key", cfg), ("workspaces.w.linear_api_key", cfg), ("TYPESAFE_API_KEY", env)]
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", cfg), mock.patch.object(fc.fc_config, "ENV_FILE", env):
            self.assertEqual(self.doctor(self.ROWS[:2]), (0, [Doctor.READY]))
            code, lines = self.doctor(self.ROWS[:2], keys)
            self.assertEqual((self.old.call_args, lines[-1]), (mock.call(cfg, env), Doctor.READY))
            warn = [l for l in lines if l.startswith("WARNING:")]
            self.assertEqual((code, warn), (0, [ClashLines.line(n, "~/" + f.name) for n, f in keys]), lines)
            self.assertFalse({"sk-or-test", "lin-test", "ts-test"} & set(re.findall(r"[\w-]+", "\n".join(lines))))
            self.herdr_config('[ui.toast]\ndelivery = "system"\n')
            code, lines = self.doctor(self.ROWS[:2] + tuple(r for r in fc_steps.STEPS if r[0] == "herdr config"), keys)
            self.assertEqual(([l for l in lines if l.startswith("WARNING:")], [l for l in lines if l.startswith("MISSING: herdr config: ")]),
                             (warn, ["MISSING: herdr config: ui.toast.delivery=system, ui.sound.enabled=true (install: compose setup) - optional"]))

class ClashLines(unittest.TestCase):
    """Each key old_keys keeps gets its own WARNING line, in old_keys order, with the home directory at the start of its
    path shown as ~; nothing else doctor prints changes, and no key value is printed."""
    setUp, fake, herdr_config, doctor = Doctor.setUp, Doctor.fake, Doctor.herdr_config, DoctorNotes.doctor
    ROWS = DoctorNotes.ROWS[:2]  # claude and gh, both ok

    @staticmethod
    def line(name, shown):
        return f"WARNING: {name} in {shown} differs from the key auth.json holds, so it was kept: remove the one you do not want"

    def clash(self, keys, rows=ROWS):  # (code, the key lines, every other line)
        code, lines = self.doctor(rows, keys)
        self.assertEqual(self.old.call_args, mock.call(fc.fc_config.CONFIG_PATH, fc.fc_config.ENV_FILE))
        warn = [l for l in lines if "auth.json" in l]
        return code, warn, [l for l in lines if l not in warn]

    def test_one_line_per_key_in_old_keys_order_with_home_as_tilde(self):
        (self.home / ".compose").mkdir()
        cfg, env = self.home / ".compose" / "config.json", self.home / ".compose" / ".env"
        cfg.write_text(json.dumps({"openrouter_api_key": "sk-or-test", "workspaces": {"w": {"linear_api_key": "lin-test"}}}))
        env.write_text("TEXT_MODEL_API_KEY=sk-tm-test\nTYPESAFE_API_KEY=ts-test\n")
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", cfg), mock.patch.object(fc.fc_config, "ENV_FILE", env):
            for rows in (self.ROWS, self.ROWS + (DoctorWarnings.MISS[0],)):  # ready, and a required miss that exits 1
                with self.subTest(rows=[r[0] for r in rows]):
                    base = self.clash([], rows)
                    self.assertEqual(base[1], [])  # no kept key: no line about keys
                    self.assertEqual(self.clash([("TEXT_MODEL_API_KEY", env)], rows),
                                     (base[0], [self.line("TEXT_MODEL_API_KEY", "~/.compose/.env")], base[2]))
                    keys = [("TYPESAFE_API_KEY", env), ("workspaces.w.linear_api_key", cfg), ("openrouter_api_key", cfg)]
                    code, warn, rest = self.clash(keys, rows)
                    self.assertEqual((code, warn, rest), (base[0], [self.line("TYPESAFE_API_KEY", "~/.compose/.env"),
                        self.line("workspaces.w.linear_api_key", "~/.compose/config.json"), self.line("openrouter_api_key", "~/.compose/config.json")], base[2]))
                    words = set(re.findall(r"[\w-]+", "\n".join(warn + rest)))
                    self.assertFalse({"sk-or-test", "lin-test", "sk-tm-test", "ts-test"} & words)

class DoctorWarnings(unittest.TestCase):
    """doctor prints every line (gh, old keys, herdr config) before a required miss exits 1, and ends with one summary;
    loud herdr config is an optional row; init's stop line names the tool rows that stop it."""
    setUp, fake, herdr_config = Doctor.setUp, Doctor.fake, Doctor.herdr_config
    OK = (("gh", "gh", "brew install gh", "required", False, "run"), ("claude", "claude", "curl x", "required", False, "manual"))
    MISS = (("npx", "no-such-npx", "brew install node", "required", False, "run"),
            ("gh auth", lambda c: (False, "not logged in"), "compose setup", "required", False, "interactive"),
            ("uv", lambda c: (False, "not on PATH"), "brew install uv", "default", False, "run"))
    HERDR, READY = "MISSING: herdr config: {} (install: compose setup) - optional", "every step compose needs is ready"

    def doctor(self, rows, keys=()):  # gh is logged out; a real "herdr config" row, if the table has one, stays
        self.fake("gh", "gh version 2.94.0", body='[ "$1 $2" = "auth status" ] && exit 1')
        rows += tuple(r for r in fc_steps.STEPS if r[0] == "herdr config")
        out, code = io.StringIO(), 0
        with mock.patch.object(fc_steps, "STEPS", rows), mock.patch.object(fc_auth, "old_keys", return_value=list(keys)), \
             mock.patch("sys.stdout", out):
            try:
                fc.fc_init.cmd_doctor(None)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue().splitlines()

    def test_a_required_miss_still_prints_every_line_then_exits_1(self):
        self.herdr_config('[ui.toast]\ndelivery = "system"\n')
        keys = [("openrouter_api_key", self.home / "config.json"), ("TYPESAFE_API_KEY", self.home / ".env")]
        code, lines = self.doctor(self.OK + self.MISS, keys)
        self.assertEqual((code, lines[-1]), (1, "2 required steps missing: run compose setup"))
        for line in (Doctor.GH_WARNING, self.HERDR.format("ui.toast.delivery=system, ui.sound.enabled=true"),
                     "MISSING: gh auth: not logged in (install: compose setup)", "MISSING: uv: not on PATH (install: brew install uv) - optional"):
            self.assertIn(line, lines)
        self.assertEqual([l for l in lines if l.startswith("WARNING: herdr") or "every tool" in l], [])
        self.assertEqual([l for l in lines if "_API_KEY" in l or "_api_key" in l],
                         [ClashLines.line("openrouter_api_key", "~/config.json"), ClashLines.line("TYPESAFE_API_KEY", "~/.env")])

    def test_herdr_config_row_quiet_absent_and_sound_only(self):
        self.assertEqual(self.doctor(self.OK), (0, [Doctor.GH_WARNING, self.READY]))  # quiet: no herdr line at all
        for text, loud in ((None, "ui.sound.enabled=true"), ('[ui.toast]\ndelivery = "off"\n[ui.sound]\nenabled = true\n', "ui.sound.enabled=true"),
                           ('[ui.toast]\ndelivery = "system"\n[ui.sound]\nenabled = false\n', "ui.toast.delivery=system")):
            with self.subTest(text=text):
                (self.home / ".config" / "herdr" / "config.toml").unlink(missing_ok=True)
                text and self.herdr_config(text)
                code, lines = self.doctor(self.OK)  # optional: exit 0, and the summary still says ready
                self.assertEqual((code, lines[-1], sorted(lines[:-1])), (0, self.READY, sorted([Doctor.GH_WARNING, self.HERDR.format(loud)])))

    def test_init_stop_line_names_the_tool_rows_that_stop_it(self):
        skill = ("herdr skill", ".claude/skills/none/SKILL.md", "herdr --skill", "required", False, "run")
        (self.home / "repo").mkdir()  # not a git repository: init must stop before it touches it
        a = mock.Mock(repo=str(self.home / "repo"), project="p", branch=None, here=False, after=None)
        with mock.patch.object(fc_steps, "STEPS", self.OK + self.MISS + (skill,)), mock.patch("sys.stdout", io.StringIO()), \
             self.assertRaises(SystemExit) as e:
            fc.fc_init.cmd_init(a)
        m = re.match(r"init stopped: (.+?) missing; ", str(e.exception.code))
        self.assertTrue(m, e.exception.code)
        self.assertEqual([n in m.group(1) for n in ("npx", "herdr skill", "gh auth", "uv")], [True, True, False, False])

class DesignerExtras(unittest.TestCase):
    """With no role the designer rows are opt-in: no MISSING line, never on the waiting line, never returned, and one
    designer extras line after the rows' lines when any is not ok. The designer's start reports them as today."""
    setUp, fake, herdr_config = Doctor.setUp, Doctor.fake, Doctor.herdr_config
    LINE = "designer extras not set up: compose setup, choose the designer rows"

    def doctor(self, figma, token, role=None):
        rows = (("claude", "claude", "curl x", "required", False, "manual"), ("gh", "gh", "brew install gh", "required", False, "run"),
                ("superpowers", lambda c: (False, ""), "claude plugin install b", "default", True, "run"),
                ("uv", lambda c: (False, ""), "brew install uv", "default", False, "run"),
                ("framelink-figma", lambda c: (figma, ""), "claude mcp add x", "designer", True, "run"),
                ("framelink-figma token", lambda c: (token, ""), "compose setup", "designer", False, "key"))
        out, alone, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(fc_steps, "STEPS", rows), mock.patch.object(fc_auth, "old_keys", return_value=[]):
            with mock.patch("sys.stdout", out):
                try:
                    role or fc.fc_init.cmd_doctor(None)
                except SystemExit as e:
                    code = e.code
            with mock.patch("sys.stdout", alone):
                self.bad = fc.fc_init.check_deps(role)
        return code, (out if role is None else alone).getvalue().splitlines()

    def test_not_ok_rows_share_one_line_after_the_rows(self):
        for figma, token in ((False, False), (True, False), (False, True)):
            with self.subTest(figma=figma, token=token):
                code, lines = self.doctor(figma, token)
                self.assertEqual((code, self.bad, lines.count(self.LINE)), (0, [], 1), lines)
                self.assertEqual([l for l in lines if "framelink" in l or "needed for the designer" in l], [])
                self.assertIn("MISSING: uv (install: brew install uv) - optional", lines)
                self.assertEqual(lines.index(self.LINE), max(i for i, l in enumerate(lines[:lines.index(Doctor.READY)]) if l.startswith("MISSING")) + 1)

    def test_without_claude_framelink_stays_off_the_waiting_line(self):
        (self.bin / "claude").unlink()
        code, lines = self.doctor(False, False)
        wait = [l for l in lines if l.startswith("waiting on Claude Code: ")]
        self.assertEqual((code, self.bad, lines.count(self.LINE), len(wait)), (1, ["claude"], 1, 1), lines)
        self.assertTrue("superpowers" in wait[0] and "framelink" not in wait[0], wait)
        self.assertLess(lines.index(self.LINE), lines.index(wait[0]))
        self.assertGreater(lines.index(self.LINE), lines.index("MISSING: uv (install: brew install uv) - optional"))

class InitGate(unittest.TestCase):
    """init stops only on a missing required tool row, one whose check is a command, a command list, or a file under home;
    a required row checked by a function prints its MISSING line and init goes on. doctor exits 1 on either."""
    fake = Doctor.fake
    SKILL = ".claude/skills/herdr/SKILL.md"
    TOOLS = (("npx", "npx", "brew install node", "required", False, "run"),
             ("gh stack", ["gh", "stack", "--version"], "gh extension install github/gh-stack", "required", False, "run"),
             ("herdr skill", SKILL, "herdr --skill", "required", False, "run"))
    NOT_TOOLS = tuple((n, lambda c: (False, ""), "compose setup", "required", False, m) for n, m in (
        ("gh auth", "interactive"), ("compose CLI link", "write"), ("~/.local/bin on PATH", "manual"), ("OpenRouter key", "key")))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin, self.home, self.repo = self.tmp / "bin", self.tmp / "home", self.tmp / "repo"
        for d in (self.bin, (self.home / self.SKILL).parent, self.repo):
            d.mkdir(parents=True)
        (self.home / self.SKILL).write_text("# skill")
        self.fake("npx", "11.19.0"), self.fake("gh", "gh version 2.94.0", body='[ "$1" = stack ] || exit 1')  # gh auth fails
        env = mock.patch.dict(os.environ, {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "HERDR_ENV": "",
                                           "COMPOSE_CONFIG": str(self.home / ".compose" / "config.json"), "COMPOSE_NO_TREEHOUSE": "1", "COMPOSE_NO_TRAIL": "1"})
        env.start(), self.addCleanup(env.stop)
        [os.environ.pop(k, None) for k in ("OPENROUTER_API_KEY", "TEXT_MODEL_API_KEY", "OPENCODE_CONFIG_CONTENT", "TYPESAFE_API_KEY")]
        subprocess.run("git init -q && git -c user.name=t -c user.email=t@t commit -q --allow-empty -m i", shell=True, cwd=self.repo, check=True)

    def run_cmd(self, cmd, rows):
        out, code = io.StringIO(), 0
        a = mock.Mock(repo=str(self.repo), project="p", branch=None, here=False, after=None)
        with mock.patch.object(fc_steps, "STEPS", rows), mock.patch("sys.stdout", out):
            try:
                cmd(a)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def assert_init(self, rows, stops):
        code, out = self.run_cmd(fc.fc_init.cmd_init, rows)
        created = (self.tmp / "compose-p" / ".compose" / "p" / "verify.json").exists()
        self.assertEqual(("init stopped" in str(code), created), (stops, not stops), out)
        if stops:  # before anything is created: no worktree, no branch
            self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["bin", "home", "repo"])
            self.assertEqual(subprocess.run(["git", "branch", "--list", "compose/p"], cwd=self.repo, capture_output=True, text=True).stdout, "")
        self.assertEqual(self.run_cmd(fc.fc_init.cmd_doctor, rows)[0], 1)
        return out

    def test_required_rows_checked_by_a_function_never_stop_init(self):
        out = self.assert_init(self.TOOLS + self.NOT_TOOLS, stops=False)
        self.assertEqual([n for n, *_ in self.NOT_TOOLS if f"MISSING: {n} (" not in out], [])

    def test_each_kind_of_missing_tool_row_stops_init(self):
        for i, check in enumerate(("no-such-tool", ["gh", "no-such-extension"], ".claude/skills/none/SKILL.md")):
            with self.subTest(check=check):
                self.assert_init(self.TOOLS[:i] + ((self.TOOLS[i][0], check) + self.TOOLS[i][2:],) + self.TOOLS[i + 1:] + self.NOT_TOOLS, stops=True)

    def test_the_check_kind_and_level_decide_not_the_name(self):
        uv = ("uv", "uv", "brew install uv", "required", False, "run")  # a default row in the real table, made required here
        self.assert_init(self.TOOLS + (uv,), stops=True)
        git = ("git", lambda c: (False, ""), "xcode-select --install", "required", False, "manual")
        self.assert_init(self.TOOLS + (git, uv[:3] + ("default",) + uv[4:]), stops=False)


class SummaryFix(unittest.TestCase):
    """doctor's summary says run compose setup only when a missing required row is one setup fixes: any mode but manual."""
    setUp, fake, herdr_config, doctor = Doctor.setUp, Doctor.fake, Doctor.herdr_config, DoctorWarnings.doctor
    LOCAL = ("~/.local/bin on PATH", lambda c: (False, ""), 'export PATH="$HOME/.local/bin:$PATH"', "required", False, "manual")
    CLAUDE = ("claude", lambda c: (False, ""), "curl -fsSL https://claude.ai/install.sh | bash", "required", False, "manual")
    GIT = ("git", "git", "xcode-select --install", "required", False, "manual")  # on the fake PATH: ok
    OLD = [("openrouter_api_key", Path("/c/config.json"))]

    def miss(self, mode):
        return (f"{mode} row", lambda c: (False, ""), "compose setup", "required", False, mode)

    def test_only_manual_rows_missing_name_no_setup(self):
        self.fake("git", "git version 2.50.0")
        for rows, last in (((self.GIT, self.LOCAL), "1 required step missing"), ((self.LOCAL, self.GIT, self.CLAUDE), "2 required steps missing"),
                           ((self.LOCAL, self.miss("manual"), self.CLAUDE), "3 required steps missing")):
            with self.subTest(rows=[r[0] for r in rows]):
                code, lines = self.doctor(rows + (self.miss("run")[:3] + ("default", False, "run"),), self.OLD)  # an optional miss changes nothing
                self.assertEqual((code, lines[-1]), (1, last), lines)
                self.assertNotIn("compose setup", lines[-1])
                self.assertIn('MISSING: ~/.local/bin on PATH (install: export PATH="$HOME/.local/bin:$PATH")', lines)
                self.assertEqual(sum(l.startswith("WARNING:") for l in lines), 2, lines)  # gh logged out, old key

    def test_a_row_setup_fixes_brings_setup_back(self):
        for mode in ("key", "interactive", "run", "write"):
            for rows, n in (((self.LOCAL, self.miss(mode)), 2), ((self.miss(mode),), 1), ((self.miss(mode), self.CLAUDE, self.LOCAL), 3)):
                with self.subTest(mode=mode, n=n):
                    code, lines = self.doctor(rows)
                    self.assertEqual((code, lines[-1]), (1, f"{n} required step{'s' * (n > 1)} missing: run compose setup"), lines)

    def test_nothing_required_missing_is_ready(self):
        self.fake("git", "git version 2.50.0")
        code, lines = self.doctor((self.GIT, ("pi auth.json override",) + self.LOCAL[1:3] + ("default", False, "manual")), self.OLD)
        self.assertEqual((code, lines[-1], sum(l.startswith("WARNING:") for l in lines)), (0, DoctorWarnings.READY, 2), lines)


class Answer:
    """What urlopen returns: a context manager with a status and a JSON body."""
    def __init__(self, body, status=200): self.body, self.status = body, status
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, *n): return json.dumps(self.body).encode()


class Preflight(unittest.TestCase):
    """The checks run and verify make before any work, and doctor reports: every problem at once with its fix."""
    fake, herdr_config = Doctor.fake, Doctor.herdr_config

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        Doctor.setUp(self)
        os.environ["PATH"] += os.pathsep + str(Path(shutil.which("git", path=ORIGINAL_PATH)).parent)  # the real git, after the fake tools
        os.environ.pop("COMPOSE_NO_PREFLIGHT", None)  # hermetic sets it; restored with the rest of the environment
        for p in (mock.patch.object(fc.fc_config, "OPENROUTER_KEY", "or-key"), mock.patch.object(fc.fc_config, "LINEAR_KEY", "lin-key"),
                  mock.patch.object(fc.fc_config, "WORKSPACE", "work"), mock.patch.object(fc_checks, "machine_warnings", return_value=[]),
                  mock.patch.object(fc_checks, "battery_report", return_value="")):  # never the real battery: on battery it stops a start
            p.start(); self.addCleanup(p.stop)
        self.requests = []

    def serve(self, **answers):
        """urlopen answering by host: an Answer, an exception, or a status number for an HTTP error."""
        def urlopen(req, timeout=None):
            self.requests.append(req)
            what = answers[urllib.parse.urlparse(req.full_url).hostname.split(".")[-2]]
            if isinstance(what, int):
                raise urllib.error.HTTPError(req.full_url, what, "no", {}, None)
            if isinstance(what, Exception):
                raise what
            return what
        return mock.patch("urllib.request.urlopen", urlopen)

    def test_a_key_its_service_rejects_is_a_problem_with_the_fix_and_credit_used_up_is_one_too(self):
        with self.serve(openrouter=401, linear=400):
            problems, warnings = fc.fc_init.key_problems()
        self.assertEqual(warnings, [])
        self.assertEqual(problems, [
            "the OpenRouter key is rejected (HTTP 401): get a new key at openrouter.ai/keys, then: pbpaste | compose workspace key work",
            "the Linear key is rejected (HTTP 400): get a new key at linear.app/settings/api, then: pbpaste | compose workspace key work --linear"])
        auth = {r.full_url.split("/")[2]: r.get_header("Authorization") for r in self.requests}
        self.assertEqual(auth, {"openrouter.ai": "Bearer or-key", "api.linear.app": "lin-key"})  # Linear takes the bare key
        with self.serve(openrouter=Answer({"data": {"limit_remaining": 0}}), linear=Answer({"data": {"viewer": {"id": "u"}}})):
            self.assertEqual(fc.fc_init.key_problems(), (["the OpenRouter key has no credit left: raise its limit or add credit at openrouter.ai/settings/credits"], []))
        with self.serve(openrouter=Answer({"data": {"limit_remaining": None}}), linear=Answer({})):  # no limit set
            self.assertEqual(fc.fc_init.key_problems(), ([], []))

    def test_a_service_that_does_not_answer_is_a_warning_never_a_verdict(self):
        with self.serve(openrouter=urllib.error.URLError("offline"), linear=503):
            problems, warnings = fc.fc_init.key_problems()
        self.assertEqual(problems, [])
        self.assertEqual(warnings, ["WARNING: could not check the OpenRouter key (no answer): the work may stop later if it is bad",
                                    "WARNING: could not check the Linear key (HTTP 503): the work may stop later if it is bad"])

    def test_unset_keys_and_the_opt_out_make_no_call(self):
        with self.serve() as urlopen:
            with mock.patch.object(fc.fc_config, "OPENROUTER_KEY", None), mock.patch.object(fc.fc_config, "LINEAR_KEY", ""):
                self.assertEqual(fc.fc_init.key_problems(), ([], []))
            with mock.patch.dict(os.environ, {"COMPOSE_NO_PREFLIGHT": "1"}):
                self.assertEqual(fc.fc_init.key_problems(), ([], []))
        self.assertEqual(self.requests, [])

    def stop(self, **kw):
        say = []
        with mock.patch.object(fc.fc_core, "say", say.append), self.assertRaises(SystemExit) as e:
            fc.fc_init.start_checks(self.repo, **kw)
        return str(e.exception), say

    def test_run_reports_the_dirty_tree_the_bad_key_and_the_logged_out_gh_all_at_once(self):
        (self.repo / "new_test.py").write_text("x\n")
        (self.repo / "graft").mkdir(); (self.repo / "graft" / "map.json").write_text("{}")  # the tools' own files never count
        (self.repo / ".trail").mkdir(); (self.repo / ".trail" / "hook.cjs").write_text("")
        (self.repo / ".claude").mkdir(); (self.repo / ".claude" / "settings.json").write_text("{}")
        self.fake("gh", "gh version 2.94.0", body='[ "$1 $2" = "auth status" ] && exit 1')
        with self.serve(openrouter=401, linear=Answer({})):
            msg, say = self.stop(hint="commit the tests first so worktrees include them")
        self.assertEqual(msg.splitlines()[0], "preflight stopped, nothing was cloned or run. Fix all 3 of these, then run again:")
        self.assertEqual(msg.splitlines()[1:], [
            "  repo has uncommitted changes (new_test.py); commit the tests first so worktrees include them",
            "  the OpenRouter key is rejected (HTTP 401): get a new key at openrouter.ai/keys, then: pbpaste | compose workspace key work",
            "  gh is not logged in, so the stack cannot be submitted: gh auth login (or run with --no-stack)"])

    def test_no_stack_skips_gh_and_verify_skips_the_tree_and_a_clean_start_goes_on_with_its_warnings(self):
        (self.repo / "new_test.py").write_text("x\n")
        self.fake("gh", "gh version 2.94.0", body="exit 1")
        with self.serve(openrouter=Answer({}), linear=Answer({})):
            fc.fc_init.start_checks(self.repo, stack=False, tree=False)  # nothing to stop on
            with mock.patch.object(fc_checks, "machine_warnings", return_value=["WARNING: the Mac is on battery"]), \
                 mock.patch.object(fc.fc_core, "say") as say:
                fc.fc_init.start_checks(self.repo, stack=False, tree=False)
            say.assert_called_once_with("WARNING: the Mac is on battery")
            msg, _ = self.stop(stack=False)
        self.assertEqual(msg.splitlines()[0], "preflight stopped, nothing was cloned or run. Fix this, then run again:")
        self.assertIn("uncommitted changes (new_test.py); commit or discard them", msg)

    def test_a_mac_on_battery_stops_the_start_with_the_fix_unless_forced_and_doctor_only_warns(self):
        self.fake("gh", "gh version 2.94.0")
        low = "the Mac is on battery at 6%: it can die before the work ends; plug it in, or run again with --force to start anyway; `compose watch` says `event power=ac` once it is plugged in"
        with self.serve(openrouter=Answer({}), linear=Answer({})), mock.patch.object(fc_checks, "battery_report", return_value="Now drawing from 'Battery Power'\n 6%; discharging"):
            msg, _ = self.stop()
            self.assertEqual(msg.splitlines()[1:], ["  " + low])
            fc.fc_init.start_checks(self.repo, force=True)
            problems, _ = fc.fc_init.start_problems(self.repo)
        self.assertEqual(problems, [])  # doctor reports the machine as a warning: only a start refuses

    def project(self, database=None):
        proj = self.repo / ".compose" / "p"
        proj.mkdir(parents=True, exist_ok=True)
        cfg = {"checks": [], "branch": "compose/p", **({"database": database} if database is not None else {})}
        (proj / "verify.json").write_text(json.dumps(cfg))
        return proj

    def test_the_first_start_records_the_database_targets_and_prints_the_checklist(self):
        self.fake("gh", "gh version 2.94.0")
        (self.repo / ".gitignore").write_text(".env*\n"); (self.repo / ".env").write_text("DATABASE_URL=postgresql://u:p@localhost:5433/dtt\n")
        subprocess.run(["git", "-C", str(self.repo), "add", ".gitignore"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ignore"], check=True, capture_output=True)
        proj, say = self.project(), []
        with self.serve(openrouter=Answer({}), linear=Answer({})), mock.patch.object(fc.fc_core, "say", say.append), \
                mock.patch.object(fc_checks, "battery_report", return_value="Now drawing from 'AC Power'\n 90%; charged"), mock.patch.object(fc_checks.platform, "system", return_value="Linux"), mock.patch.dict(os.environ, {"COMPOSE_NO_AWAKE": ""}):
            fc.fc_init.start_checks(self.repo, proj=proj)
        self.assertEqual(json.loads((proj / "verify.json").read_text())["database"], {".env DATABASE_URL": "localhost:5433/dtt"})
        self.assertEqual(say, ["startup  power: AC 90%", "startup  docker: not installed", "startup  database: .env DATABASE_URL -> localhost:5433/dtt",
                               "startup  sleep: no caffeinate here; keep the machine from sleeping yourself"])
        (self.repo / ".env").write_text("DATABASE_URL=postgresql://u:p@localhost:5432/website\n")  # the target moved under the project
        with self.serve(openrouter=Answer({}), linear=Answer({})):
            msg, _ = self.stop(proj=proj)
            problems, _ = fc.fc_init.start_problems(self.repo, proj=proj)  # doctor --project names it too
        self.assertEqual(msg.splitlines()[1:], ["  database target changed: .env DATABASE_URL points at localhost:5432/website, the project started on localhost:5433/dtt; "
                                                "confirm the change with the user, then set `database` {\".env DATABASE_URL\": \"localhost:5432/website\"} in verify.json (or restore the env file)"])
        self.assertEqual(len(problems), 1)
        with self.serve(openrouter=Answer({}), linear=Answer({})):
            problems, _ = fc.fc_init.start_problems(self.repo)  # without the project there is no record to hold it to
        self.assertEqual(problems, [])

    def test_the_opt_out_keeps_only_the_tree_check(self):
        (self.repo / "new_test.py").write_text("x\n")
        self.fake("gh", "gh version 2.94.0", body="exit 1")
        with mock.patch.dict(os.environ, {"COMPOSE_NO_PREFLIGHT": "1"}), mock.patch.object(fc_checks, "machine_warnings", side_effect=AssertionError):
            msg, _ = self.stop()
        self.assertEqual(msg.splitlines()[1:], ["  repo has uncommitted changes (new_test.py); commit or discard them"])

    def test_run_resume_and_verify_hold_the_mac_awake_and_verify_leaves_the_tree_to_itself(self):
        stop = RuntimeError("stop here")
        with mock.patch.object(fc.fc_proc, "keep_awake") as awake, mock.patch.object(fc.fc_run, "preflight", side_effect=stop), \
             mock.patch.object(fc.fc_tasks, "load_tasks", side_effect=stop), mock.patch.object(fc.fc_init, "start_checks") as checks, \
             mock.patch.object(fc.fc_core, "project_dir", return_value=self.repo / ".compose" / "p"), \
             mock.patch.object(fc.fc_linear, "refresh_tasks"):  # no Linear here: the tasks stand as stored
            with self.assertRaises(RuntimeError):
                fc.fc_run.run_tasks(self.repo, self.repo, [{"model": "m"}], None)
            self.assertEqual(awake.call_count, 1)
            with self.assertRaises(RuntimeError):
                fc.fc_verify.cmd_verify(type("A", (), {"repo": str(self.repo), "project": "p", "skip_review": True, "fix": 0})())
            self.assertEqual(awake.call_count, 2)
            checks.assert_called_once_with(self.repo, stack=False, tree=False, force=False, proj=self.repo / ".compose" / "p")  # verify tolerates a dirty tree and submits nothing
            with self.assertRaises(RuntimeError):
                fc.fc_run.cmd_run(type("A", (), {"repo": str(self.repo), "project": "p", "only": None, "no_stack": True})())
            checks.assert_called_with(self.repo, False, hint="commit the tests first so worktrees include them", force=False, proj=self.repo / ".compose" / "p")  # --no-stack: gh need not be logged in
            with self.assertRaises(RuntimeError):
                fc.fc_run.cmd_run(type("A", (), {"repo": str(self.repo), "project": "p", "only": None, "no_stack": True, "force": True})())
            checks.assert_called_with(self.repo, False, hint="commit the tests first so worktrees include them", force=True, proj=self.repo / ".compose" / "p")  # --force starts on battery

    def doctor(self, args, rows=()):
        out, code = io.StringIO(), 0
        with mock.patch.object(fc_steps, "STEPS", rows), mock.patch("sys.stdout", out):
            try:
                fc.fc_init.cmd_doctor(args)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue().splitlines()

    def test_doctor_prints_each_problem_and_exits_1_and_otherwise_stays_ready(self):
        (self.repo / "new_test.py").write_text("x\n")
        args = type("A", (), {"repo": str(self.repo), "project": "p"})
        self.fake("gh", "gh version 2.94.0")
        found = {"p": (self.repo, self.repo / ".compose" / "p")}
        with mock.patch.object(fc.fc_core, "all_projects", return_value=found), mock.patch.object(fc_checks, "machine_warnings", return_value=["WARNING: load average 51 on 8 cores"]):
            with self.serve(openrouter=401, linear=Answer({})):
                code, lines = self.doctor(args)
            self.assertEqual((code, lines), (1, [
                "WARNING: load average 51 on 8 cores",
                "PROBLEM: repo has uncommitted changes (new_test.py); commit or discard them",
                "PROBLEM: the OpenRouter key is rejected (HTTP 401): get a new key at openrouter.ai/keys, then: pbpaste | compose workspace key work",
                "2 problems to fix before work starts"]))
            (self.repo / "new_test.py").unlink()
            with self.serve(openrouter=Answer({}), linear=Answer({})):
                self.assertEqual(self.doctor(args), (0, ["WARNING: load average 51 on 8 cores", "every step compose needs is ready"]))
        with mock.patch.object(fc.fc_core, "all_projects", return_value={}), self.serve(openrouter=Answer({}), linear=Answer({})):
            code, lines = self.doctor(args)
            self.assertEqual(code, 1)
            self.assertTrue(lines[0].startswith(f"PROBLEM: no project 'p' in {self.repo}"), lines)
        with self.serve(openrouter=Answer({}), linear=Answer({})):  # no project named: no tree to check
            self.assertEqual(self.doctor(None), (0, ["every step compose needs is ready"]))


if __name__ == "__main__":
    unittest.main()
