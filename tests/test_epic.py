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


class InitBranchNames(unittest.TestCase):
    """init names a project's branch from the prefix, the workspace's over the top level's, and --after reaches the other project's branch the same way."""
    def test_the_prefix_is_compose_slash_unless_the_config_or_its_workspace_says_otherwise(self):
        prefix = fc.fc_init.branch_prefix
        self.assertEqual(prefix({}), "compose/")
        self.assertEqual(prefix({"branch_prefix": "team/"}), "team/")
        self.assertEqual(prefix({"branch_prefix": "team/", "workspace": "work", "workspaces": {"work": {"branch_prefix": "w/"}}}), "w/")
        self.assertEqual(prefix({"branch_prefix": "team/", "workspace": "work", "workspaces": {"work": {}}}), "team/")  # a workspace that says nothing keeps the top level's

    def test_the_branch_is_the_prefix_and_the_name_unless_branch_is_given(self):
        self.assertEqual(fc.fc_init.project_branch_name("compose/", "b"), "compose/b")
        self.assertEqual(fc.fc_init.project_branch_name("w/", "b", None), "w/b")
        self.assertEqual(fc.fc_init.project_branch_name("w/", "b", "mine"), "mine")

    def test_after_an_existing_project_builds_on_its_top_layer_or_branch_and_otherwise_on_the_prefix_and_name(self):
        after = fc.fc_init.after_branch
        self.assertEqual(after("compose/", "a", True, "compose/a-003"), "compose/a-003")  # a project that exists: where its code is
        self.assertEqual(after("w/", "a", False, None), "w/a")
        self.assertIsNone(after("w/", None, False, None))

    def test_after_an_unknown_project_refuses_naming_the_branch_and_the_project_to_init_first(self):
        self.assertEqual(fc.fc_init.unknown_after("compose/nope", "nope"), "no branch compose/nope: `compose init --project nope` that project first")

    def test_init_exits_with_that_message_when_the_branch_check_says_no(self):
        a = mock.Mock(repo=tempfile.mkdtemp(), project="b", branch=None, here=False, after="nope")
        with mock.patch.object(fc.fc_init, "check_deps", return_value=[]), mock.patch.object(fc.fc_config, "read_config", return_value={}), \
                mock.patch.object(fc.fc_core, "all_projects", return_value={}), mock.patch.object(fc.fc_init, "branch_exists", return_value=False) as exists, \
                self.assertRaises(SystemExit) as cm:
            fc.fc_init.cmd_init(a)
        self.assertEqual(str(cm.exception), "no branch compose/nope: `compose init --project nope` that project first")
        self.assertEqual(exists.call_args.args[1], "compose/nope")


if __name__ == "__main__":
    unittest.main()
