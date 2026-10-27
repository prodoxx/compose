import hermetic  # first: no test sees the real home
import json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
from layers_kit import fc
os.environ["HERDR_ENV"] = ""


class MoveOnto(unittest.TestCase):
    """An epic's dependent feature: `init --after a` builds an existing project on a's branch, while it has no commits of its own and
    its worktree is clean but for agent wiring; otherwise it exits with the reason and changes nothing."""
    def setUp(self):
        self.wt = Path(tempfile.mkdtemp()).resolve(); self.proj = self.wt / ".compose" / "b"
        self.proj.mkdir(parents=True)
        self.vfile = self.proj / "verify.json"
        self.vfile.write_text(json.dumps({"review_base": "main"}))
        self.here, self.own, self.dirty, self.calls = "compose/b", [], [], []
        def sh(cmd, *a, **k):
            self.calls.append(cmd[1:])
            out = {"--show-current": self.here, "--format=%s": "".join(s + "\n" for s in self.own)}
            return subprocess.CompletedProcess(cmd, 0, next((v for flag, v in out.items() if flag in cmd), ""), "")
        for p in (mock.patch.object(fc.fc_core, "sh", sh), mock.patch.object(fc.fc_core, "project_branch", return_value="compose/b"),
                  mock.patch.object(fc.fc_worktree, "review_base", return_value="main"), mock.patch.object(fc.fc_init, "created_from", return_value=None),
                  mock.patch.object(fc.fc_worktree, "dirty_paths", lambda wt: self.dirty), mock.patch("builtins.print")):
            p.start(); self.addCleanup(p.stop)

    def move(self):
        fc.fc_init.move_onto(self.wt, self.proj, "compose/a", "a")

    def cfg(self):
        return json.loads(self.vfile.read_text())

    def test_a_project_with_no_commits_of_its_own_and_a_clean_tree_moves_onto_the_other_branch_and_records_it(self):
        self.own = ["Ignore local tool directories"]  # init's own commit does not count
        self.move()
        self.assertIn(["reset", "-q", "--keep", "compose/a"], self.calls)
        self.assertEqual(self.cfg(), {"review_base": "compose/a", "after": "a"})

    def test_a_project_with_a_commit_of_its_own_refuses_and_changes_nothing(self):
        self.own = ["Build the thing", "Ignore local tool directories"]
        with self.assertRaises(SystemExit) as e:
            self.move()
        self.assertIn("project b has 1 commit of its own on compose/b, so --after a cannot move it", str(e.exception))
        self.assertNotIn(["reset", "-q", "--keep", "compose/a"], self.calls)
        self.assertEqual(self.cfg(), {"review_base": "main"})

    def test_a_dirty_tree_refuses_naming_what_is_dirty_and_changes_nothing(self):
        self.dirty = ["notes.txt"]  # fc_worktree.dirty_paths already leaves the agent wiring out
        with self.assertRaises(SystemExit) as e:
            self.move()
        self.assertIn("uncommitted changes (notes.txt)", str(e.exception))
        self.assertNotIn(["reset", "-q", "--keep", "compose/a"], self.calls)
        self.assertEqual(self.cfg(), {"review_base": "main"})

    def test_a_worktree_off_its_branch_refuses(self):
        self.here = "elsewhere"
        with self.assertRaises(SystemExit) as e:
            self.move()
        self.assertIn("is not on compose/b: check it out, then run init --after again", str(e.exception))

    def test_the_same_after_again_changes_nothing(self):
        self.vfile.write_text(json.dumps({"review_base": "compose/a", "after": "a"}))
        self.move()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.cfg(), {"review_base": "compose/a", "after": "a"})


if __name__ == "__main__":
    unittest.main()
