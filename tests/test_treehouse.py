import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, shutil, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


class AllProjectsBranch(unittest.TestCase):
    """The checkouts of a repository come from one `git worktree list --porcelain` (faked here); the branch of each decides which folder wins."""
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, self.base, True)
        self.repo = self.base / "app"; self.repo.mkdir()

    def listing(self, *entries):
        """The porcelain text for (path, branch) entries, branch "" for a detached one."""
        return "\n".join(f"worktree {p}\nHEAD abc\n" + (f"branch refs/heads/{b}\n" if b else "detached\n") for p, b in entries)

    def project(self, wt, name="p"):
        (wt / ".compose" / name).mkdir(parents=True); (wt / ".compose" / name / "verify.json").write_text("{}")

    def test_branch_per_worktree_in_git_order(self):
        wt, det, out = self.base / "wt", self.base / "det", self.listing((self.repo, "main"), (self.base / "wt", "compose/p"), (self.base / "det", ""))
        with mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(stdout=out)) as sh:
            self.assertEqual(fc.fc_core.worktree_branches(self.repo), [(self.repo, "main"), (wt, "compose/p"), (det, "")])
            self.assertEqual(fc.fc_core.worktrees(self.repo), [self.repo, wt, det])
        self.assertEqual(sh.call_args.args[0], ["git", "worktree", "list", "--porcelain"])

    def test_a_listing_with_nothing_in_it_is_the_repository_itself(self):
        with mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(stdout="")):
            self.assertEqual(fc.fc_core.worktree_branches(self.repo), [(self.repo.resolve(), "")])

    def test_own_worktree_wins(self):
        wt = self.base / "wt"
        self.project(self.repo); self.project(wt)  # merged: the main checkout shows the folder too
        with mock.patch.object(fc.fc_core, "worktree_branches", return_value=[(self.repo, "main"), (wt, "compose/p")]):
            found = fc.fc_core.all_projects(self.repo)
        self.assertEqual(found["p"], (wt, wt / ".compose" / "p"))

    def test_detached_worktree_never_wins(self):
        det = self.base / "det"; self.project(self.repo); self.project(det)
        with mock.patch.object(fc.fc_core, "worktree_branches", return_value=[(self.repo, "main"), (det, "")]):
            self.assertEqual(fc.fc_core.all_projects(self.repo)["p"][0], self.repo)


if __name__ == "__main__":
    unittest.main()
