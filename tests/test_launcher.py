import hermetic  # first: no test sees the real home
import json, os, subprocess, tempfile, unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""
import test_modules

LAUNCHER = Path(__file__).resolve().parents[1] / "bin" / "compose"


class Launcher(unittest.TestCase):
    """bin/compose finds compose.py next to its real self, so the usual install (a symlink on PATH) and a link to
    that link both work exactly like calling it by its path in the repo."""
    def run_help(self, path):
        r = subprocess.run([str(path), "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"^usage: compose")
        return r

    def test_called_by_its_path(self):
        self.run_help(LAUNCHER)

    def test_called_through_a_symlink_and_a_chain_of_symlinks(self):
        tmp = Path(tempfile.mkdtemp())
        link = tmp / "bin" / "compose"; link.parent.mkdir()
        os.symlink(LAUNCHER, link)
        chain = tmp / "other" / "fc"; chain.parent.mkdir()
        os.symlink(link, chain)
        self.run_help(link)
        self.run_help(chain)
        relative = tmp / "rel"
        os.symlink(os.path.relpath(link, tmp), relative)  # a relative link resolves against its own directory
        self.run_help(relative)


class GuardFastPath(unittest.TestCase):
    """`compose guard` runs on every tool call: it loads fc_hooks and what fc_hooks imports, and every other command runs in one process."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "sitecustomize.py").write_text(
            "import atexit, os, sys\nopen(os.environ['STARTS'], 'a').write('x')\n"
            "atexit.register(lambda: open(os.environ['MODS'], 'w').write(' '.join(m for m in sys.modules if m.startswith('fc_'))))\n")
        self.env = {**os.environ, "PYTHONPATH": str(self.tmp), "STARTS": str(self.tmp / "starts"), "MODS": str(self.tmp / "mods"), "COMPOSE_ROLE": "orchestrator"}
        self.repo = self.tmp / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def run_cmd(self, *args, stdin="", launcher=LAUNCHER, **env):
        return subprocess.run([str(launcher), *args], input=stdin, capture_output=True, text=True, env={**self.env, **env})

    def event(self, path=None):
        return json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(path or self.repo / "app.py")}, "cwd": str(self.repo)})

    def test_decides_as_before_by_path_and_through_a_symlink(self):
        link = self.tmp / "compose"; os.symlink(LAUNCHER, link)
        for launcher in (LAUNCHER, link):
            denied = self.run_cmd("guard", stdin=self.event(), launcher=launcher)
            self.assertEqual(json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"], "deny", denied.stderr)
            self.assertEqual(self.run_cmd("guard", stdin=self.event(), launcher=launcher, COMPOSE_ROLE="crew").stdout, "")
        for r in (self.run_cmd("guard", stdin=self.event(self.tmp / "note.md")), self.run_cmd("guard", stdin="not json")):
            self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_loads_only_fc_hooks_and_its_imports(self):
        self.run_cmd("guard", stdin=self.event())
        allowed, todo = set(), ["fc_hooks"]
        while todo:
            m = todo.pop()
            if m not in allowed:
                allowed.add(m); todo += test_modules.fc_imports(test_modules.parse(m + ".py"))
        loaded = set((self.tmp / "mods").read_text().split())
        self.assertIn("fc_hooks", loaded)
        self.assertEqual(loaded - allowed, set())

    def test_other_commands_run_in_one_process(self):
        link = self.tmp / "fc"; os.symlink(LAUNCHER, link)
        for launcher in (LAUNCHER, link):
            (self.tmp / "starts").write_text("")
            self.assertRegex(self.run_cmd("--help", launcher=launcher).stdout, r"^usage: compose")
            self.assertEqual((self.tmp / "starts").read_text(), "x")
        self.assertEqual(self.run_cmd("no-such-command").returncode, 2)

if __name__ == "__main__":
    unittest.main()
