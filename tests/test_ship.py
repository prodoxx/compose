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


class FakedGateRun(unittest.TestCase):
    """layer_checks end to end with git and the checks faked: `results` says what a command prints on a layer, `runs` which ran where."""
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.root / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "verify.json").write_text(json.dumps({"checks": ["c"]}))
        self.layers = [{"branch": f"compose/p-layer-0{i}", "tested": []} for i in (1, 2, 3)]
        self.at, self.runs, self.results, self.said = "", [], {}, []
        for target, name, fake in ((fc.fc_core, "sh", self.sh), (fc.fc_core, "say", self.said.append), (fc.fc_core, "main_checkout", lambda r: r),
                                   (fc.fc_stack, "layer_base", lambda *a: "main"), (fc.fc_ship, "run_check", self.run_check), (fc.fc_ship, "ready", lambda *a, **k: None),
                                   (fc.fc_ship, "layer_commands", lambda proj, l, checks, top: list(checks)), (fc.fc_verify, "at_root", lambda cmds, roots, wt: cmds),
                                   (fc.fc_green, "tree_of", lambda repo, tip: f"tree-{tip}")):
            patch = mock.patch.object(target, name, fake)
            patch.start(); self.addCleanup(patch.stop)

    def sh(self, cmd, cwd=None, **kw):
        if cmd[1] == "checkout":
            self.at = cmd[-1].removeprefix("tip-")
        return SimpleNamespace(stdout={"rev-parse": f"tip-{cmd[-1]}", "merge-base": "basecommit"}.get(cmd[1], ""), stderr="", returncode=0)

    def run_check(self, wt, c):
        self.runs.append((self.at, c))
        return self.results.get((self.at, c), (0, ""))

    def check(self, **kw):
        return fc.fc_ship.layer_checks(self.root, self.proj, self.layers, "compose/p", self.root, **kw)

    def ran(self):
        return [b.removeprefix("compose/p-") for b, _ in self.runs]

    def test_a_failing_layer_refuses_the_submit_names_it_and_the_layers_above_never_run(self):
        self.results[("compose/p-layer-02", "c")] = (1, "boom")
        why = self.check()
        self.assertIn("layer 2 of 3 (compose/p-layer-02) fails `c` on its own tree (it names no failing test, so it cannot be compared with the base)", why)
        self.assertIn("Nothing was pushed", why)
        self.assertEqual(self.ran(), ["layer-01", "layer-02"])

    def test_a_check_whose_script_a_later_layer_adds_waits_and_the_top_still_refuses_it(self):
        missing = (1, 'error: Script not found "build-storybook"')
        self.results[("compose/p-layer-01", "c")] = missing
        self.assertEqual(self.check(), "")
        self.assertTrue(any("layers later" in l and "compose/p-layer-01" in l and "build-storybook" in l for l in self.said), self.said)
        self.results[("compose/p-layer-03", "c")] = missing
        self.assertIn("layer 3 of 3 (compose/p-layer-03) fails `c`", self.check(fresh=True))

    def test_a_layer_whose_tree_has_a_recorded_green_is_not_run_and_the_others_still_are(self):
        fc.fc_green.record(self.proj, "tree-tip-compose/p-layer-03", "c", 0)
        self.assertEqual(self.check(), "")
        self.assertEqual(self.ran(), ["layer-01", "layer-02"])
        taken = [l for l in self.said if "not rerun" in l]
        self.assertEqual(len(taken), 1)
        self.assertIn("compose/p-layer-03", taken[0])

    def test_a_layer_unchanged_since_its_checks_passed_is_not_run_again_until_a_fresh_run(self):
        self.assertEqual(self.check(), "")
        self.assertEqual(len(self.runs), 3)
        self.assertEqual(self.check(), "")
        self.assertEqual(len(self.runs), 3)
        self.assertEqual(len([l for l in self.said if "unchanged since its checks passed" in l]), 3)
        self.assertEqual(self.check(fresh=True), "")
        self.assertEqual(len(self.runs), 6)


class FakedLayerDecisions(unittest.TestCase):
    def test_a_layer_is_unchanged_only_when_its_stamp_or_lean_stamp_was_kept_and_never_on_a_fresh_run(self):
        seen = {"b1": "s1", "b2": "s2:base:top"}
        for branch, fresh, want in (("b1", False, True), ("b2", False, True), ("b3", False, False), ("b1", True, False)):
            self.assertEqual(fc.fc_ship.unchanged_layer(seen, branch, "s1" if branch == "b1" else "s2", "s2:base:top", fresh), want, (branch, fresh))
        self.assertFalse(fc.fc_ship.unchanged_layer(seen, "b1", "changed", "changed:base:top"))

    def test_a_failing_check_waits_below_the_top_for_a_script_it_names_and_is_refused_when_it_names_no_test(self):
        state = lambda exit, failing=(), unknown=False: {"exit": exit, "failing": list(failing), "unknown": unknown}
        missing = 'error: Script not found "build"'
        outcome = lambda s, top=False, c="c", own=("c",), out="": fc.fc_ship.check_outcome(s, top, c, set(own), out)
        self.assertEqual(outcome(state(1, unknown=True), out=missing), ("later", "build"))
        self.assertEqual(outcome(state(1, unknown=True), top=True, out=missing)[0], "refuse")  # the top runs every check
        self.assertEqual(outcome(state(1, unknown=True), own=(), out=missing)[0], "refuse")  # a command that is not one of the project's checks
        self.assertEqual(outcome(state(124, unknown=True)), ("refuse", ""))
        self.assertIn("names no failing test", outcome(state(1, unknown=True))[1])
        self.assertEqual(outcome(state(1, ["t_a"])), ("fail", ["t_a"]))
        self.assertEqual(outcome(state(0)), ("pass", None))

    def test_a_failure_the_base_shares_outside_the_stack_is_accepted_and_the_others_refuse_with_their_reason(self):
        why = lambda new=(), hit=(), ok=(), unknown=False: fc.fc_ship.base_why(list(new), list(hit), list(ok), unknown, "b" * 40)
        self.assertEqual(why(ok=["old"]), "")
        self.assertEqual(why(), "")
        self.assertEqual(why(new=["n"]), f"passes on the base {'b' * 9}, fails here: n")
        self.assertEqual(why(new=["n"], hit=["h"], ok=["o"]), f"passes on the base {'b' * 9}, fails here: n; fails on the base too, in what this stack changes: h; 1 more already fail on the base and are ignored")
        self.assertIn(f"cannot tell whether n fail on the base too: its run on the base {'b' * 9} timed out", why(new=["n"], unknown=True))


if __name__ == "__main__":
    unittest.main()
