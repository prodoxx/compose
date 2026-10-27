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


class LegacyNames(unittest.TestCase):
    def test_the_prefix_falls_back_to_the_legacy_fast_prefix_only_while_such_refs_exist_and_the_record_wins(self):
        choose = fc.fc_worktree.choose_prefix
        self.assertEqual(choose({}, "p", "refs/heads/fast/p/001", "compose/p"), "fast/p/")
        self.assertEqual(choose({}, "p", "", "compose/p"), "compose/p-")
        self.assertEqual(choose({"task_prefix": "x/"}, "p", "refs/heads/fast/p/001", "compose/p"), "x/")
        self.assertEqual(choose({"task_prefix": "x/"}, "p", "", "compose/p"), "x/")

    def test_only_a_never_pushed_legacy_project_with_no_live_run_and_no_name_clash_may_be_renamed(self):
        can = fc.fc_worktree.can_rename
        legacy = "fast/p/"
        self.assertEqual(can(legacy, legacy, ""), (True, ""))
        self.assertEqual(can("compose/p-", legacy, ""), (False, ""))  # already moved, or recorded otherwise
        self.assertEqual(can(legacy, legacy, "refs/remotes/origin/fast/p/001"), (False, ""))  # a PR carries its names
        self.assertEqual(can(legacy, legacy, "", ["compose/p-001", "compose/p-002"]), (False, "compose/p-001 already exists"))
        self.assertEqual(can(legacy, legacy, "", [], 4242), (False, "run pid 4242 is live"))
        self.assertEqual(can(legacy, legacy, "", ["compose/p-001"], 4242), (False, "compose/p-001 already exists"))
        self.assertEqual(can(legacy, legacy, "refs/remotes/origin/fast/p/001", ["compose/p-001"], 4242), (False, ""))

    def test_a_rename_blocked_by_a_taken_name_or_a_live_run_says_why_and_moves_nothing(self):
        repo = Path(tempfile.mkdtemp()).resolve(); proj = repo / ".compose" / "p"
        (proj / "results").mkdir(parents=True)
        said, asked, clash = [], [], []
        def sh(cmd, cwd=None, **kw):
            asked.append(cmd)
            out = "refs/heads/fast/p/001\n" if cmd[1] == "for-each-ref" and "refs/heads/fast/p/" in cmd else ""
            return SimpleNamespace(stdout=out, stderr="", returncode=0 if clash and cmd[1] == "rev-parse" else 1 if cmd[1] == "rev-parse" else 0)
        run = {"pid": os.getppid()}  # a process that stays alive
        with mock.patch.object(fc.fc_core, "sh", sh), mock.patch.object(fc.fc_core, "say", said.append), mock.patch.object(fc.fc_worktree, "task_prefix", return_value="fast/p/"), \
                mock.patch.object(fc.fc_tasks, "run_marker", return_value=run):
            self.assertEqual(fc.fc_worktree.rename_legacy_branches(repo, proj), {})
            self.assertEqual(said, [f"names  fast/p/ kept for now: run pid {os.getppid()} is live"])
            clash.append(1)
            self.assertEqual(fc.fc_worktree.rename_legacy_branches(repo, proj), {})
            self.assertEqual(said[-1], "names  fast/p/ kept for now: compose/p-001 already exists")
        self.assertFalse([c for c in asked if c[1] == "branch"])


class DirtyPaths(unittest.TestCase):
    def test_the_tools_own_files_and_the_agent_wiring_never_count_as_changes(self):
        status = lambda *entries: "\0".join(entries) + "\0"
        own = fc.fc_worktree.user_changes
        self.assertEqual(own(""), [])
        self.assertEqual(own(status(" M a.txt", "?? new_test.py")), ["a.txt", "new_test.py"])
        self.assertEqual(own(status(" M .claude/settings.json", " M .claude/settings.local.json", "?? .ignore", "?? graft/map.json", "?? .trail/cache/x.json", "?? .compose/q/notes.md")), [])
        self.assertEqual(own(status("?? sub/graft", "?? .claude/other.json", "?? .composed")), ["sub/graft", ".claude/other.json", ".composed"])  # only the tools' folders at the root

    def test_a_rename_or_copy_lists_its_new_path_and_skips_its_source_field(self):
        own = fc.fc_worktree.user_changes
        self.assertEqual(own("R  b.txt\0a.txt\0 M c.txt\0"), ["b.txt", "c.txt"])
        self.assertEqual(own("C  copy.txt\0graft/src.txt\0"), ["copy.txt"])  # the source is never read as an entry
        self.assertEqual(own("R  graft/new.txt\0old.txt\0"), [])


if __name__ == "__main__":
    unittest.main()
