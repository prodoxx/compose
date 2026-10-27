import hermetic  # first: no test sees the real home
import argparse, io, json, os, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import importlib.machinery, importlib.util, sys
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""


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
        for name, fake in (("sh", self.git), ("checkout", lambda path: fc.fc_core.Checkout((self.root, self.root / ".git", self.root / ".git")))):
            patch = mock.patch.object(fc.fc_core, name, fake)
            patch.start(); self.addCleanup(patch.stop)

    def git(self, cmd, cwd=None, **kw):
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


if __name__ == "__main__":
    unittest.main()
