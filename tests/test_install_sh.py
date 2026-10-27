import hermetic  # first: no test sees the real home
import fcntl, os, pty, subprocess, tempfile, termios, unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "install.sh"
COMPOSE = 'printf "%s|%s|%s\\n" "$0" "$*" "$PATH" >> "$HOME/compose.log"'
UV_INSTALLER = 'echo "$UV_NO_MODIFY_PATH" > "$HOME/uv.flag"; mkdir -p "$HOME/.local/bin"; : > "$HOME/.local/bin/uv"'
FAKES = {"uname": 'echo "${FAKE_OS:-Darwin}"', "uv": "true",
         "curl": f'echo "$*" >> "$HOME/curl.log"\ncat <<\'EOF\'\n{UV_INSTALLER}\nEOF',
         "git": 'echo "$*" >> "$HOME/git.log"\ncase "$*" in\n*clone*) for d; do :; done; mkdir -p "$d/bin"; '
                'cp "$FAKE_COMPOSE" "$d/bin/compose";;\n*pull*) [ -z "${PULL_FAIL:-}" ] || '
                '{ echo "fatal: cannot fast-forward" >&2; exit 1; };;\nesac'}

def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(f"#!/bin/sh\n{body}\n"); path.chmod(0o755)

def link(path, to):
    path.parent.mkdir(parents=True, exist_ok=True); path.symlink_to(to)


class InstallSh(unittest.TestCase):
    def setUp(self):  # sysbin is /bin and /usr/bin without the real git, curl, and uv
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); root = Path(os.path.realpath(t.name))
        self.home, self.bin, self.sysbin, self.fake = root / "home", root / "fake", root / "sys", root / "compose"
        for p in [*Path("/bin").iterdir(), *Path("/usr/bin").iterdir()]:
            if p.name not in ("git", "curl", "uv") and not os.path.lexists(self.sysbin / p.name): link(self.sysbin / p.name, p)
        script(self.fake, COMPOSE); self.home.mkdir(); [script(self.bin / n, b) for n, b in FAKES.items()]
    def log(self, name):
        return (self.home / name).read_text() if (self.home / name).exists() else ""
    def run_install(self, ok=True, tty=None, **env):  # no /dev/tty in a new session unless tty (a pty fd) becomes it
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:{self.sysbin}", "FAKE_COMPOSE": str(self.fake), **env}
        claim = None if tty is None else (lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        p = subprocess.run(["/bin/sh", str(SCRIPT)], env=env, stdin=subprocess.DEVNULL if tty is None else tty,
                           capture_output=True, text=True, start_new_session=True, preexec_fn=claim, timeout=60)
        self.assertEqual(p.returncode, 0 if ok else 1, p.stdout + p.stderr)
        return p.stdout + p.stderr, self.log("compose.log").strip().split("|")
    def test_refuses_other_systems_and_missing_tools(self):
        for env, drop, word in (({"FAKE_OS": "Linux"}, "uv", "macOS"), ({}, "git", "git"), ({}, "curl", "curl")):
            with self.subTest(word):
                os.rename(self.bin / drop, self.home / drop)
                try: out, ran = self.run_install(ok=False, **env)
                finally: os.rename(self.home / drop, self.bin / drop)
                self.assertEqual((len(out.strip().splitlines()), ran), (1, [""])); self.assertIn(word, out)
    def test_clones_to_default_when_skill_link_has_no_compose(self):
        skill, src = self.home / ".claude/skills/compose", self.home / ".compose/src"; link(skill, self.home)
        self.assertEqual(self.run_install()[1][:2], [str(src / "bin/compose"), "setup --yes"])
        self.assertIn("https://github.com/prodoxx/compose", self.log("git.log")); self.assertEqual(self.log("curl.log"), "")
        self.assertEqual([Path(os.readlink(p)) for p in (self.home / ".local/bin/compose", skill)], [src / "bin/compose", src])
    def test_skill_link_checkout_wins_and_stale_cli_link_is_repointed(self):
        linked, other, cli = self.home / "linked", self.home / "other", self.home / ".local/bin/compose"
        [script(c / "bin/compose", COMPOSE) for c in (linked, other)]; link(cli, self.home / "gone")
        link(self.home / ".claude/skills/compose", linked); ran = self.run_install(COMPOSE_INSTALL_DIR=str(other))[1]
        self.assertEqual((ran[:2], cli.readlink()), ([str(linked / "bin/compose"), "setup --yes"], linked / "bin/compose"))
        self.assertNotIn("clone", self.log("git.log"))
    def test_install_dir_failed_pull_real_file_and_missing_uv(self):
        mine, cli = self.home / "mine", self.home / ".local/bin/compose"; script(mine / "bin/compose", COMPOSE)
        (self.bin / "uv").unlink(); cli.parent.mkdir(parents=True); cli.write_text("mine")
        out, ran = self.run_install(COMPOSE_INSTALL_DIR=str(mine), PULL_FAIL="1")
        self.assertEqual(ran[:2], [str(mine / "bin/compose"), "setup --yes"]); self.assertIn("cannot fast-forward", out)
        self.assertEqual((cli.read_text(), self.log("uv.flag")), ("mine", "1\n")); self.assertIn("pull --ff-only", self.log("git.log"))
        self.assertIn("https://astral.sh/uv/install.sh", self.log("curl.log")); self.assertTrue(ran[2].startswith(f"{self.home}/.local/bin:"))
        self.assertEqual([f for f in os.listdir(self.home) if f.endswith(("rc", "profile"))], [])


FETCH = "From https://github.com/prodoxx/compose\n * branch            main       -> FETCH_HEAD\n"
HINTS = FETCH + "".join(
    f"hint: {h}\n" for h in ("Diverging branches can't be fast-forwarded, you need to either:", "", "\tgit merge --no-ff",
                             "", "or:", "", "\tgit rebase", "", 'Disable this message with "git config set advice.diverging false"'))
PULL_GIT = ('echo "$*" >> "$HOME/git.log"\ncase "$*" in\n'
            '*pull*) printf "%s" "${PULL_OUT:-}"; printf "%s" "${PULL_ERR:-}" >&2; exit "${PULL_CODE:-0}";;\nesac')


class InstallMessages(unittest.TestCase):  # the InstallSh fakes, with a git whose pull talks like the real one
    setUp, log, run_install = InstallSh.setUp, InstallSh.log, InstallSh.run_install

    def checkout(self, **pull):
        (self.home / "compose.log").unlink(missing_ok=True); mine = self.home / "mine"
        script(mine / "bin/compose", COMPOSE); script(self.bin / "git", PULL_GIT)
        out, ran = self.run_install(COMPOSE_INSTALL_DIR=str(mine), **pull)
        self.assertEqual(ran[:2], [str(mine / "bin/compose"), "setup --yes"])
        self.assertIn("pull --ff-only", self.log("git.log"))
        return out, out.splitlines(), mine

    def test_failed_pull_is_one_warn_line_with_gits_last_error(self):
        for last in ("fatal: Not possible to fast-forward, aborting.", "fatal: couldn't find remote ref main",
                     "fatal: unable to access 'https://example.invalid/a%20b/%s': Could not resolve host: example.invalid"):
            with self.subTest(last):
                out, lines, mine = self.checkout(PULL_ERR=HINTS + last + "\n", PULL_CODE="1")
                warn = f"  warn  git pull failed ({last}); going on with the checkout as it is"
                self.assertEqual([l for l in lines if "git pull" in l or "fatal" in l], [warn], out)
                self.assertNotIn("hint", out); self.assertNotIn("FETCH_HEAD", out)
                self.assertIn(f"  ok    checkout: {mine}", lines)

    def test_good_pull_prints_no_git_output_and_no_terminal_names_compose_setup(self):
        pulled = "Updating 1a2b3c4..5d6e7f8\nFast-forward\n bin/compose | 2 +-\n"
        out, lines, mine = self.checkout(PULL_OUT=pulled, PULL_ERR=FETCH)
        for said in ("Updating", "Fast-forward", "bin/compose |", "FETCH_HEAD", "From https", "warn", "guided", "plain", "starting"):
            self.assertNotIn(said, out)
        self.assertIn(f"  ok    checkout: {mine}", lines)
        self.assertEqual(lines[-1], "no terminal; running compose setup with every default (compose setup --yes)", out)

    def test_terminal_says_compose_setup_and_hands_it_the_terminal(self):
        master, slave = pty.openpty(); self.addCleanup(os.close, master); self.addCleanup(os.close, slave)
        mine = self.home / "mine"; script(mine / "bin/compose", COMPOSE + '\n[ -t 0 ] && echo stdin-tty >> "$HOME/compose.log"')
        out, ran = self.run_install(tty=slave, COMPOSE_INSTALL_DIR=str(mine))
        self.assertEqual(ran[:2], [str(mine / "bin/compose"), "setup"])
        self.assertEqual(self.log("compose.log").splitlines()[1], "stdin-tty")
        self.assertEqual(out.splitlines()[-1], "starting compose setup", out)
        for gone in ("no terminal", "guided", "plain"):
            self.assertNotIn(gone, out)
