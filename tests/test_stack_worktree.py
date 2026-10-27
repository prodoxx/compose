import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


class StackWorktree(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)

    def test_a_recorded_slot_of_another_repository_is_dropped_never_used(self):
        # a clone or copy that carries the project's results/slots.json rebuilt the original's branches in its stack worktree,
        # and clean wiped the tool folders in its slots
        mine, theirs, gone = self.repo / "mine", self.repo / "theirs", self.repo / "gone"
        mine.mkdir(); theirs.mkdir()
        recorded = {"stackroot": str(theirs), "tests-001": str(theirs), "tests-002": str(mine), "tests-003": str(gone)}
        (self.proj / "results" / "slots.json").write_text(json.dumps(recorded))
        common = {self.proj: Path("/a/.git"), mine: Path("/a/.git"), theirs: Path("/b/.git"), str(theirs): Path("/b/.git"), str(mine): Path("/a/.git")}
        out = io.StringIO()
        with mock.patch.object(fc.fc_worktree, "common_dir", lambda p: common.get(p)), contextlib.redirect_stdout(out):
            kept = fc.fc_worktree.slots(self.proj)
        want = {"tests-002": str(mine), "tests-003": str(gone)}
        self.assertEqual(kept, want)
        self.assertEqual(json.loads((self.proj / "results" / "slots.json").read_text()), want)
        for role in ("stackroot", "tests-001"):
            self.assertIn(f"{role} {theirs}", out.getvalue())


class BranchNames(unittest.TestCase):
    """Branch names come from a fake `git` listing (fc_core.sh): `refs` for the for-each-ref asks, `branches` for `git branch --list`."""
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        self.refs, self.branches = [], []
        patch = mock.patch.object(fc.fc_core, "sh", self.sh)
        patch.start(); self.addCleanup(patch.stop)
        fc.fc_worktree._prefixes.clear()

    def sh(self, cmd, cwd=None, **kw):
        out = self.branches if cmd[1] == "branch" else self.refs
        if cmd[1] == "for-each-ref" and any(a.startswith("refs/heads/fast/") for a in cmd):
            out = [r for r in self.refs if r.startswith(("refs/heads/fast/p/", "refs/remotes/")) and "/fast/p/" in r]
        return SimpleNamespace(stdout="\n".join(out), stderr="", returncode=0)

    def prefix(self, verify=None, *refs):
        (self.proj / "verify.json").unlink(missing_ok=True)
        if verify is not None:
            (self.proj / "verify.json").write_text(json.dumps(verify))
        self.refs = list(refs)
        return fc.fc_worktree.task_prefix(self.repo, self.proj)

    def test_the_prefix_comes_from_the_record_legacy_branches_or_the_project_branch(self):
        self.assertEqual(self.prefix(), "compose/p-")
        self.assertEqual((self.prefix({"branch": "compose/p"}), fc.fc_worktree.task_branch(self.repo, self.proj, "001")), ("compose/p-", "compose/p-001"))
        self.assertEqual((self.prefix({"branch": "p"}), fc.fc_worktree.task_branch(self.repo, self.proj, "001")), ("p-", "p-001"))
        self.assertEqual(self.prefix({"branch": "p"}, "refs/heads/fast/p-2/001", "refs/heads/fast/pp/001"), "p-")  # another project's legacy branches
        self.assertEqual(self.prefix({"task_prefix": "x/"}, "refs/remotes/origin/fast/p/001"), "x/")  # the record wins
        self.assertEqual(self.prefix({"branch": "p"}, "refs/remotes/origin/fast/p/001"), "fast/p/")  # only a remote-tracking legacy ref
        self.assertEqual(json.loads((self.proj / "verify.json").read_text()), {"branch": "p", "task_prefix": "fast/p/"})  # recorded once
        self.assertEqual(self.prefix(None, "refs/heads/fast/p/001"), "fast/p/")
        self.assertFalse((self.proj / "verify.json").exists())  # recorded only into an existing file

    def test_own_branches_are_task_bench_and_test_writer_branches_only(self):
        self.branches = ["compose/p-001", "compose/p-002-bench-x", "compose/p-tests-001", "compose/p-layer-01", "compose/p-2-001", "compose/p-0001"]
        self.assertEqual(set(fc.fc_worktree.own_branches(self.repo, self.proj)), {"compose/p-001", "compose/p-002-bench-x", "compose/p-tests-001"})

    def test_task_branches_and_the_stack_order_use_the_new_names(self):
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": "001", "after": "002"}, {"id": "002"}]}))
        (self.proj / "results" / "001.json").write_text(json.dumps({"base": "compose/p-002"}))
        self.refs = [f"refs/heads/{b}" for b in ("compose/p-002", "compose/p-001", "compose/p-tests-001", "compose/p-layer-01", "compose/p-2-001")]
        self.assertEqual(fc.fc_worktree.task_branches(self.repo, self.proj), {"compose/p-001": "compose/p-002", "compose/p-002": None})
        self.assertEqual(fc.fc_worktree.stack_order(self.repo, self.proj), ["compose/p-002", "compose/p-001"])


class ReviewGate(unittest.TestCase):
    def test_confirmed_review_findings_fail_verify(self):
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True)
        (proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
        high = {"file": "s.go", "severity": "high", "note": "a query per answer", "confirmed": True}
        rv = {"pass": False, "issues": [high], "confirmed": [high]}
        a = SimpleNamespace(skip_e2e=True, skip_review=False)
        with mock.patch.dict(os.environ, {"HERDR_ENV": ""}), mock.patch.object(fc.fc_verify.fc_review, "llm_review", return_value=rv), \
                mock.patch.object(fc.fc_verify, "review_inputs", return_value=("diff", "plan", "", [])):
            ok, _, findings = fc.fc_verify.run_stages(proj, proj, {"review": True, "jev_acceptance": False}, a)
        self.assertFalse(ok); self.assertIn("a query per answer", findings)


if __name__ == "__main__":
    unittest.main()
