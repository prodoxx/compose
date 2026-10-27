import hermetic  # first: no test sees the real home
import io, os, shutil, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import importlib.machinery, importlib.util, sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

TREE = "a" * 40


class Repo(unittest.TestCase):
    """A repo folder and a project folder beside it; the tree the repo has is whatever `self.tree` says."""
    def setUp(self):
        root = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, root, True)
        self.repo, self.proj, self.counter = root / "repo", root / "proj", root / "runs"
        self.repo.mkdir(); self.proj.mkdir()
        self.tree = TREE
        patch = mock.patch.object(fc.fc_green, "tree_of", lambda repo, rev=None: self.tree); patch.start(); self.addCleanup(patch.stop)

    def runs(self):
        return len(self.counter.read_text().split()) if self.counter.exists() else 0

    @property
    def count(self):
        return f"echo run >> {self.counter}"


class GreenStore(Repo):
    def test_a_green_result_is_taken_on_the_same_tree_and_command_with_one_line_saying_so(self):
        tree = self.tree
        fc.fc_green.record(self.proj, tree, "make test", 0)
        line = fc.fc_green.taken(self.proj, tree, "make test")
        self.assertRegex(line, rf"^make test green on tree {tree[:12]} at \S+( \S+)?, not rerun$")

    def test_a_red_unknown_other_command_or_other_tree_is_never_taken(self):
        tree = self.tree
        fc.fc_green.record(self.proj, tree, "make test", 1)
        fc.fc_green.record(self.proj, tree, "make lint", 124)
        self.assertIsNone(fc.fc_green.taken(self.proj, tree, "make test"))
        self.assertIsNone(fc.fc_green.taken(self.proj, tree, "make lint"))
        fc.fc_green.record(self.proj, tree, "make test", 0)
        self.assertIsNone(fc.fc_green.taken(self.proj, tree, "make other"))
        self.assertIsNone(fc.fc_green.taken(self.proj, "0" * 40, "make test"))
        self.assertIsNone(fc.fc_green.taken(self.proj, "", "make test"))

    def test_a_red_run_on_a_tree_takes_back_its_earlier_green(self):
        tree = self.tree
        fc.fc_green.record(self.proj, tree, "make test", 0)
        fc.fc_green.record(self.proj, tree, "make test", 1)
        self.assertIsNone(fc.fc_green.taken(self.proj, tree, "make test"))

    def test_fresh_takes_nothing(self):
        tree = self.tree
        fc.fc_green.record(self.proj, tree, "make test", 0)
        self.assertIsNone(fc.fc_green.taken(self.proj, tree, "make test", fresh=True))

class VerifyChecks(Repo):
    def stages(self, checks, **kw):
        out = io.StringIO()
        with mock.patch.object(fc.fc_herdr, "herdr_verify_panes", return_value={}), redirect_stdout(out), mock.patch.object(fc.fc_core, "say", lambda s: print(s)):
            ok, report, _ = fc.fc_verify.run_stages(self.repo, self.proj, {"checks": checks}, SimpleNamespace(skip_e2e=True, skip_review=True, **kw))
        return ok, report, out.getvalue()

    def test_a_check_green_on_this_tree_is_not_run_again_and_says_so(self):
        self.assertTrue(self.stages([self.count])[0])
        ok, report, said = self.stages([self.count])
        self.assertTrue(ok)
        self.assertEqual(self.runs(), 1)
        self.assertRegex(said, rf"{self.count} green on tree {self.tree[:12]} at .*, not rerun")
        self.assertEqual([(c["cmd"], c["exit"]) for c in report["checks"]], [(self.count, 0)])  # the report still lists it, passed

    def test_a_changed_tree_a_red_result_and_fresh_run_the_check(self):
        self.stages([self.count])
        self.tree = "b" * 40
        self.stages([self.count])
        self.assertEqual(self.runs(), 2)
        self.stages([self.count], fresh=True)
        self.assertEqual(self.runs(), 3)
        flag = self.proj / "flag"
        red = f"{self.count}; test -f {flag}"
        self.assertFalse(self.stages([red])[0])
        self.assertFalse(self.stages([red])[0])
        self.assertEqual(self.runs(), 5)


if __name__ == "__main__":
    unittest.main()
