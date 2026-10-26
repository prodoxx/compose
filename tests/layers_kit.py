import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
import sys
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

ENV = {"HERDR_ENV": "", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

def unit(name, lines):
    return {"name": name, "commits": [], "lines": lines, "generated": {}}

FAKE_GH_STACK = """#!/usr/bin/env python3
# gh stack's local side, no login or network: init records the stack in the repo's git dir, view shows it from one of its branches
import json, subprocess, sys
from pathlib import Path
git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()
a, f = sys.argv[1:], Path(git("rev-parse", "--path-format=absolute", "--git-common-dir")) / "fake-gh-stack.json"
s = json.loads(f.read_text()) if f.exists() else None
open(f.with_name("fake-gh-calls"), "a").write(" ".join(a) + "\\n")
if a[:2] == ["stack", "init"]:
    f.write_text(json.dumps({"trunk": a[a.index("--base") + 1], "branches": [{"name": b} for b in a[4:]]}))
elif a[:2] == ["stack", "view"] and s and git("branch", "--show-current") in [b["name"] for b in s["branches"]]:
    print(json.dumps(s))
elif a[:2] == ["stack", "unstack"]:
    f.unlink(missing_ok=True)
else:
    sys.exit(1)
"""


class LayersBase(unittest.TestCase):
    """The fixture the layer tests share: a project branch with groundwork and every task's test on it, and task branches above it."""
    @classmethod
    def setUpClass(cls):
        LayersBase._build(cls("setUp"))

    def setUp(self):
        cls = type(self)
        shutil.rmtree(cls.root)
        shutil.copytree(cls.template, cls.root, symlinks=True)
        self.env = mock.patch.dict(os.environ, {**ENV, "PATH": f"{cls.root / 'bin'}:{os.environ['PATH']}"}); self.env.start(); self.addCleanup(self.env.stop)
        self.repo, self.proj = cls.root / "repo", cls.root / "repo" / ".compose" / "p"
        self.sizes = {"001": 100, "002": 120, "003": 150}

    def _build(self):
        cls = type(self)
        cls.root, cls.template = Path(tempfile.mkdtemp()).resolve(), Path(tempfile.mkdtemp()).resolve() / "template"
        cls.addClassCleanup(shutil.rmtree, cls.template.parent)
        cls.addClassCleanup(shutil.rmtree, cls.root)
        bin_ = cls.root / "bin"; bin_.mkdir()
        (bin_ / "gh").write_text(FAKE_GH_STACK); (bin_ / "gh").chmod(0o755)
        env = mock.patch.dict(os.environ, {**ENV, "PATH": f"{bin_}:{os.environ['PATH']}"}); env.start()
        self.repo = cls.root / "repo"; self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "https://github.com/o/r.git")
        self.write("README.md", "hi\n"); self.commit("init")
        self.git("checkout", "-q", "-b", "compose/p")
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.repo / ".git" / "info" / "exclude").write_text("/.compose/\n")
        self.write("lock.txt", ""); self.write("bun.lock", "x\n" * 4000); self.write("setup.txt", "s\n" * 20); self.commit("groundwork one")
        self.write("stub.txt", "s\n" * 20); self.commit("groundwork two")
        tasks = [{"id": t, "protected": [f"t{t}.test"], "files": [f"{t}.src"], "after": "", "test": f"make {t}", "title": f"Build {t}", "linear": f"ENG-{t}", "body": f"## Goal\nBuild {t}.\n"}
                 for t in ("001", "002", "003")]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        for t in ("001", "002", "003"):
            (self.proj / "results" / f"{t}.json").write_text(json.dumps({"status": "passed"}))  # done: a submit pushes their layers
        for t in ("001", "002", "003"):
            self.write(f"t{t}.test", "t\n" * 5); self.commit(f"test {t}")
        self.sizes = {"001": 100, "002": 120, "003": 150}
        for t, base in (("001", "compose/p"), ("002", "compose/p"), ("003", "compose/p")):
            self.git("checkout", "-q", "-b", f"compose/p-{t}", base)
            self.write(f"{t}.src", "c\n" * self.sizes[t]); self.commit(f"Build {t}")
            self.git("checkout", "-q", "compose/p")
        env.stop()
        shutil.copytree(cls.root, cls.template, symlinks=True)

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, name, text):
        (self.repo / name).write_text(text)

    def commit(self, msg):
        self.git("add", "-A"); self.git("commit", "-qm", msg)

    def stack(self, **cfg):
        (self.proj / "verify.json").write_text(json.dumps({"review_base": "main", **cfg}))
        out = []
        with mock.patch.object(fc.fc_core, "say", out.append):
            self.assertTrue(fc.fc_stack.register_stack(self.repo, self.proj, "compose/p"), out)
        return out

    def changed(self, branch, below):
        return set(self.git("diff", "--name-only", below, branch).splitlines())

    def pr_file(self, name, text):
        (self.proj / "pr").mkdir(exist_ok=True)
        (self.proj / "pr" / f"{name}.md").write_text(text)

    WRITTEN = ("# Keep the toggle's state across reloads\n\n## Why\nThe toggle forgot its state on every reload.\nThis stores it.\n\n"
               "## What changed\n- Store the toggle's state\n- Read it back on load\n")
