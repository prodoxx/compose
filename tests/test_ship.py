import hermetic  # first: no test sees the real home
import argparse, io, json, os, subprocess, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from ship_kit import Ship, fc
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""


class LayerChecks(Ship):
    def test_a_failing_layer_refuses_the_submit_and_is_named_with_nothing_pushed(self):
        self.config(checks=["! test -f 002.src"])  # layer 1 holds the setup files and passes; layer 2 has 002.src
        out, code = self.submit()
        self.assertEqual(code, 1)
        stop = [l for l in out if "STOP" in l]
        self.assertEqual(len(stop), 1)
        self.assertIn("layer 2 of 3 (compose/p-layer-02) fails `! test -f 002.src` on its own tree", stop[0])
        self.assertIn("Nothing was pushed", stop[0])
        self.assertTrue(any("layers ok" in l and "compose/p-layer-01" in l for l in out))
        self.assertFalse(any("stack submit" in c for c in self.log()))
        self.assertFalse(self.origin_has("refs/heads/compose/p-layer-01"))
        self.assertFalse((self.proj / "layers.json").exists())

    def test_a_check_whose_script_a_later_layer_adds_waits_for_it_and_the_top_still_runs_every_check(self):
        missing = """echo 'error: Script not found "build-storybook"'; exit 1"""
        self.config(checks=[f"test -f 002.src || {{ {missing}; }}"])  # the script arrives with 002.src in layer 2
        out, code = self.submit()
        self.assertEqual(code, 0, out)
        self.assertTrue(any("layers later" in l and "compose/p-layer-01" in l and "build-storybook" in l for l in out), out)
        self.assertTrue(self.origin_has("refs/heads/compose/p-layer-01"))
        self.config(checks=[f"test -f nowhere || {{ {missing}; }}"])  # no layer adds it: the top refuses the submit
        out, code = self.submit()
        self.assertEqual(code, 1)
        stop = [l for l in out if "STOP" in l]
        self.assertIn("layer 3 of 3 (compose/p-layer-03) fails", stop[0])

    def test_every_layer_runs_the_checks_once_and_an_unchanged_layer_is_not_run_again(self):
        counter = Path(tempfile.mkdtemp()) / "runs"
        self.config(checks=[f"echo run >> {counter}"])
        out, code = self.submit()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(counter.read_text().splitlines()), 3)
        self.assertTrue(any("stack submit --auto" in c for c in self.log()))
        self.assertTrue(self.origin_has("refs/heads/compose/p-layer-03"))
        out, code = self.submit()
        self.assertEqual(len(counter.read_text().splitlines()), 3)
        self.assertEqual(len([l for l in out if "unchanged since its checks passed" in l]), 3)
        self.config(checks=[f"echo run >> {counter}", "true"])  # a new check runs on every layer; the one that was green on its tree is taken
        out, code = self.submit()
        self.assertEqual(len(counter.read_text().splitlines()), 3)
        self.assertEqual(len([l for l in out if f"echo run >> {counter} green on tree" in l and "not rerun" in l]), 3)
        self.submit_fresh()
        self.assertEqual(len(counter.read_text().splitlines()), 6)

    def submit_fresh(self):
        return self.run_cmd(fc.fc_ship.cmd_stack, submit=True, fresh=True)

    def test_a_layer_whose_tree_has_a_recorded_green_is_not_run_and_the_others_still_are(self):
        counter = Path(tempfile.mkdtemp()) / "runs"
        cmd = f"echo run >> {counter}"
        self.config(checks=[cmd])
        self.run_cmd(fc.fc_ship.cmd_stack, submit=False)
        top = fc.fc_green.tree_of(self.repo, "compose/p-layer-03")
        fc.fc_green.record(self.proj, top, cmd, 0)  # what verify left on the top of the stack
        out, code = self.submit()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(counter.read_text().splitlines()), 2)  # layers 1 and 2 hold other trees
        taken = [l for l in out if "not rerun" in l]
        self.assertEqual(len(taken), 1)
        self.assertIn(f"{cmd} green on tree {top[:12]} at ", taken[0])
        self.assertIn("compose/p-layer-03", taken[0])

    def test_a_red_result_is_never_taken_and_a_green_one_is_forgotten_when_the_layer_goes_red(self):
        counter = Path(tempfile.mkdtemp()) / "runs"
        cmd = f"echo run >> {counter}; ! test -f 002.src"
        self.config(checks=[cmd])
        for _ in range(2):
            out, code = self.submit()
            self.assertEqual(code, 1)
        self.assertEqual(len(counter.read_text().splitlines()), 3)  # layer 1 once, layer 2 on both submits
        self.assertFalse(any("not rerun" in l for l in out))


class Reword(Ship):
    def test_set_rewords_one_commit_keeps_the_trees_and_never_pushes(self):
        self.run_cmd(fc.fc_ship.cmd_stack, submit=False)
        tips = {b: self.git("rev-parse", b) for b in ("compose/p-layer-01", "compose/p-layer-02", "compose/p-layer-03")}
        sha = self.git("log", "-1", "--format=%H", "--grep=^Build 001", "compose/p-layer-02")
        out, code = self.run_cmd(fc.fc_ship.cmd_reword, set=[f"{sha[:8]}=Add the first thing\n\nIt reads the file."], as_is=False)
        self.assertEqual(code, 0, out)
        self.assertIn("Add the first thing", self.subjects("compose/p-layer-02"))
        self.assertNotIn("Build 001", self.subjects("compose/p-layer-02"))
        for b, tip in tips.items():
            self.assertEqual(self.git("rev-parse", f"{b}^{{tree}}"), self.git("rev-parse", f"{tip}^{{tree}}"))
        self.assertEqual(self.git("rev-parse", "compose/p-layer-01"), tips["compose/p-layer-01"])  # a layer below the change is untouched
        self.assertEqual(self.subjects("compose/p-layer-03")[0], "Build 003")
        self.assertFalse(self.origin_has("refs/heads/compose/p-layer-02"))
        self.assertFalse(any(c.startswith(("pr ", "stack submit", "stack push")) for c in self.log()))
        self.assertTrue(any("nothing pushed" in l for l in out))
        # the choice lives on the commit: any later rebuild applies it again
        self.run_cmd(fc.fc_ship.cmd_stack, submit=False)
        self.assertIn("Add the first thing", self.subjects("compose/p-layer-02"))


class Faked(unittest.TestCase):
    """Git is faked (fc_core.sh answers for the commits in `self.commits`) and no process can start."""
    commits = {}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.root / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        for target in ("subprocess.run", "subprocess.Popen"):
            patch = mock.patch(target, side_effect=AssertionError("a test with a faked git starts no process"))
            patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(fc.fc_core, "sh", self.git)
        patch.start(); self.addCleanup(patch.stop)

    def git(self, cmd, cwd=None, **kw):
        if cmd[1:] == ["rev-parse", "--path-format=absolute", "--git-common-dir"]:
            out = str(self.root / ".git")
        else:
            message, files = self.commits[cmd[-1]]
            out = "\n".join(files) if "--name-only" in cmd else {"--format=%B": message, "--format=%s": message.split("\n")[0]}.get(cmd[3], "t <t@t> 2026-01-01")
        return SimpleNamespace(stdout=out + "\n", stderr="", returncode=0)

    def config(self, **cfg):
        (self.proj / "verify.json").write_text(json.dumps(cfg))


class FakedLayerChecks(Faked):
    def test_missing_script_reads_what_bun_npm_pnpm_and_yarn_say(self):
        for out, script in (('error: Script not found "build-storybook"', "build-storybook"), ('npm error Missing script: "test:storybook"', "test:storybook"),
                            ("ERR_PNPM_NO_SCRIPT  Missing script: build-storybook", "build-storybook"), ('Command "build-storybook" not found.', "build-storybook"),
                            ('Usage Error: Couldn\'t find a script named "lint".', "lint"), ("FAIL src/a.test.ts > renders", None), ("", None)):
            self.assertEqual(fc.fc_ship.missing_script(out), script, out)

    def test_a_project_without_checks_says_so_and_checks_nothing(self):
        said = []
        with mock.patch.object(fc.fc_core, "say", said.append):
            self.assertEqual(fc.fc_ship.layer_checks(self.root, self.proj, [], "compose/p", self.root), "")
        self.assertEqual(said, ["layers no checks in verify.json: the layers are not checked before submit"])


class FakedLayerMessage(Faked):
    commits = {"c1": ("task 001: wire compose verify\n\nDone by the implementer.\nIndex the title.", ["001.src"]), "c2": ("Add the reader", ["002.src"])}

    def message(self, c):
        return fc.fc_layers.layer_message(self.root, self.proj, c)

    def test_a_message_that_names_the_process_comes_back_scrubbed_with_the_rule_it_broke(self):
        message, why = self.message("c1")
        self.assertEqual(message, "Update 001.src\n\nIndex the title.")
        self.assertIn("mentions the process (`task 001`)", why)

    def test_a_reword_chosen_with_compose_reword_wins_and_a_clean_message_gives_none(self):
        self.assertIsNone(self.message("c2"))
        for c in ("c1", "c2"):
            (self.proj / "rewords.json").write_text(json.dumps({fc.fc_layers.commit_key(self.root, c): {"message": "Add the first thing", "was": "x"}}))
            self.assertEqual(self.message(c), ("Add the first thing", "chosen with compose reword"))


class FakedReword(Faked):
    commits = {"a" * 40: ("feat: add the reader", []), "b" * 40: ("Add the writer", []), "abc123" + "1" * 34: ("Fix it", []), "abc123" + "2" * 34: ("Fix it too", [])}

    def setUp(self):
        super().setUp()
        self.config(commit_convention={"types": ["feat", "fix"], "example": "feat: x"})
        self.live = self.root / "live"
        layers = [("compose/p-layer-01", ["a" * 40, "b" * 40]), ("compose/p-layer-02", ["abc123" + "1" * 34, "abc123" + "2" * 34])]
        for target, name, value in ((fc.fc_ship, "layer_commits", layers), (fc.fc_layers, "layer_prefix", "compose/p-"),
                                    (fc.fc_core, "project_dir", self.proj), (fc.fc_update, "live_dir", self.live)):
            patch = mock.patch.object(target, name, return_value=value)
            patch.start(); self.addCleanup(patch.stop)

    def reword(self, **kw):
        """The said lines, the printed lines, and the exit code (0 when it returned) of `compose reword`."""
        said, buf, code = [], io.StringIO(), 0
        args = argparse.Namespace(repo=str(self.root), project="p", trunk="compose/p", **{"set": None, "as_is": False, **kw})
        with mock.patch.object(fc.fc_core, "say", said.append), redirect_stdout(buf):
            try:
                fc.fc_ship.cmd_reword(args)
            except SystemExit as e:
                code = e.code
        return said, buf.getvalue().splitlines(), code

    def test_it_lists_each_layer_commit_and_follows_a_breaking_one_with_the_reason(self):
        said, printed, code = self.reword()
        self.assertEqual(code, 0)
        why = "it has no type prefix; the repo's commit convention starts with one of feat, fix"
        self.assertEqual([l for l in printed if l.startswith("  ")], [
            f"  layer-01  {'a' * 9}  feat: add the reader",
            f"  layer-01  {'b' * 9}  Add the writer   <- {why}",
            "  layer-02  abc123111  Fix it   <- " + why,
            "  layer-02  abc123222  Fix it too   <- " + why])

    def test_set_refuses_a_sha_that_names_no_commit_or_several(self):
        for sha, n in (("deadbeef", 0), ("", 0), ("abc123", 2)):
            said, _, code = self.reword(set=[f"{sha}=Add it"])
            self.assertEqual(code, 1)
            self.assertEqual(said, [f"reword FAIL  {sha!r} names {n} commits of the stack; `compose reword --project p` lists them"])
            self.assertFalse((self.proj / "rewords.json").exists())

    def test_set_refuses_a_breaking_message_unless_taken_as_is(self):
        sha = "b" * 8
        said, _, code = self.reword(set=[f"{sha}=Wave 2 of compose run"])
        self.assertEqual(code, 1)
        self.assertTrue(any("mentions the process" in l for l in said), said)
        self.assertFalse((self.proj / "rewords.json").exists())
        with mock.patch.object(fc.fc_stack, "register_stack", return_value=True):
            _, _, code = self.reword(set=[f"{sha}=Wave 2 of compose run"], as_is=True)
        self.assertEqual(code, 0)
        self.assertEqual([m["message"] for m in json.loads((self.proj / "rewords.json").read_text()).values()], ["Wave 2 of compose run"])

    def test_set_waits_while_a_verify_is_running_on_the_project(self):
        self.live.mkdir()
        (self.live / "1.json").write_text(json.dumps({"pid": os.getppid(), "kind": "verify", "project": "p", "repo": str(self.root), "since": "10:00"}))
        _, _, code = self.reword(set=[f"{'b' * 8}=Add the writer"])
        self.assertIn("verify is running on p", code)
        self.assertFalse((self.proj / "rewords.json").exists())


class SharedFixture(Ship):
    seen = []

    @classmethod
    def setUpClass(cls):
        def gone():
            for d in (cls.root, cls.template):
                assert not Path(d).exists(), f"{d} is left behind"
        cls.addClassCleanup(gone)  # registered first, so it runs after the kit's own cleanups
        super().setUpClass()

    def setUp(self):
        with mock.patch("subprocess.run", wraps=subprocess.run) as run:
            super().setUp()
        self.setup_ran_git = run.called

    def leave_leftovers(self):
        cls = type(self)
        self.assertFalse(self.setup_ran_git, "setUp runs no git process")
        self.assertTrue({"root", "template"} <= set(vars(cls)) and Path(cls.template).is_dir(), "each class builds its own")
        mine = (self.repo, self.origin, self.state, cls.template)
        self.assertTrue(all(s == mine for s in cls.seen), "the same paths in every test")
        cls.seen.append(mine)
        self.assertEqual(self.git("branch", "--show-current"), "compose/p")
        self.assertEqual(self.git("branch", "--list", "leak"), "")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(self.origin_has("refs/heads/leak"))
        self.assertEqual(self.log(), [])
        self.assertEqual(json.loads((self.proj / "verify.json").read_text()), {"review_base": "main"})
        self.assertEqual(len(json.loads((self.proj / "results" / "tasks.json").read_text())["tasks"]), 3)
        self.git("checkout", "-q", "-b", "leak"); self.write("leak.txt", "x\n"); self.commit("leak")
        self.git("push", "-q", "origin", "leak"); self.write("stray.txt", "x\n")
        self.assertTrue(self.origin_has("refs/heads/leak"))
        self.config(checks=["false"])
        (self.proj / "results" / "tasks.json").write_text('{"feature": {}, "tasks": []}')
        self.state.write_text('{"prs": {}, "log": ["leak"]}')

    def test_what_one_test_commits_pushes_and_changes_is_gone_in_the_next(self):
        self.leave_leftovers()

    def test_what_another_test_commits_pushes_and_changes_is_gone_in_the_next(self):
        self.leave_leftovers()


if __name__ == "__main__":
    unittest.main()
