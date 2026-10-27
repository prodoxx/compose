import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

GIT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class AllProjectsBranch(unittest.TestCase):
    """Real temp repo with linked worktrees: one git call reads every branch."""
    def setUp(self):
        p = mock.patch.dict(os.environ, GIT); p.start(); self.addCleanup(p.stop)
        self.base = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, self.base, True)
        self.repo = self.base / "app"; self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main"); self.git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")

    def git(self, cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def add(self, name, *flags):
        wt = self.base / name
        self.git(self.repo, "worktree", "add", "-q", *flags, str(wt))
        return wt

    def listed(self):
        return [Path(l[len("worktree "):]) for l in self.git(self.repo, "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]

    def project(self, wt, name="p"):
        (wt / ".compose" / name).mkdir(parents=True); (wt / ".compose" / name / "verify.json").write_text("{}")

    def test_branch_per_worktree_in_git_order(self):
        wt = self.add("wt", "-b", "compose/p"); det = self.add("det", "--detach")
        branch, listed = {self.repo: "main", wt: "compose/p", det: ""}, self.listed()
        self.assertEqual(fc.fc_core.worktree_branches(self.repo), [(d, branch[d]) for d in listed])
        self.assertEqual(fc.fc_core.worktrees(self.repo), listed)

    def test_git_order_holds_whatever_the_directory_times_and_a_gone_directory_is_listed(self):
        a = self.add("a", "-b", "compose/a"); b = self.add("b", "-b", "compose/b"); c = self.add("c", "--detach")
        for i, d in enumerate((a, b, c)): os.utime(d, (1_000_000 - i * 1000, 1_000_000 - i * 1000))  # the first listed is the newest
        listed = self.listed()
        self.assertEqual(sorted(listed), sorted([self.repo, a, b, c]))
        self.assertEqual(fc.fc_core.worktrees(self.repo), listed)
        shutil.rmtree(b)  # git still lists it until pruned
        self.assertEqual(fc.fc_core.worktrees(self.repo), listed)
        self.assertEqual(dict(fc.fc_core.worktree_branches(self.repo))[b], "compose/b")

    def test_own_worktree_wins_with_one_git_process(self):
        wt = self.add("wt", "-b", "compose/p")
        self.project(self.repo); self.project(wt)  # merged: the main checkout shows the folder too
        real, calls = subprocess.run, []
        def spy(cmd, *a, **k): calls.append(cmd); return real(cmd, *a, **k)
        with mock.patch("subprocess.run", side_effect=spy):
            found = fc.fc_core.all_projects(self.repo)
        self.assertEqual(found["p"], (wt, wt / ".compose" / "p"))
        self.assertEqual(len([c for c in calls if c[0] == "git"]), 1)

    def test_detached_worktree_never_wins(self):
        det = self.add("det", "--detach"); self.project(self.repo); self.project(det)
        self.assertEqual(fc.fc_core.all_projects(self.repo)["p"][0], self.repo)


if __name__ == "__main__":
    unittest.main()
