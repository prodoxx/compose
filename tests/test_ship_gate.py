import hermetic  # first: no test sees the real home
import json, os, subprocess, tempfile, unittest
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

OLD = "FAIL: gone_test (legacy.Old.gone_test)"  # a failing test that names a file no tree has


def done(out="", code=0):
    return subprocess.CompletedProcess([], code, out, "")


class FakedGate(unittest.TestCase):
    def test_a_check_that_times_out_is_124_with_the_limit_and_a_tool_error_is_retried_once(self):
        with mock.patch.object(fc.fc_core, "sh", side_effect=subprocess.TimeoutExpired("c", 1)), mock.patch.object(fc.fc_gate, "CHECKS_TIMEOUT", 1):
            self.assertEqual(fc.fc_ship.run_check(Path("."), "sleep 5"), (124, "timed out after 1s"))
        runs = [done("boom", 1), done("fine", 0)]
        with mock.patch.object(fc.fc_core, "sh", side_effect=runs) as sh, mock.patch.object(fc.fc_jev, "tool_error", return_value=True), mock.patch.object(fc.fc_core, "say"):
            self.assertEqual(fc.fc_ship.run_check(Path("."), "c"), (0, "fine"))
        self.assertEqual(sh.call_count, 2)

    def test_a_failing_install_is_refused_with_its_output_and_nothing_pushed(self):
        err = RuntimeError("install failed\nerror: @slack-notification not found in workspace")
        with mock.patch.object(fc.fc_worktree, "ready_checkout", side_effect=err), self.assertRaises(fc.fc_ship.InstallFailed) as raised:
            fc.fc_ship.ready(Path("."), Path("."), {"install": "bun install"})
        self.assertEqual(str(raised.exception), "error: @slack-notification not found in workspace")
        mine, base = fc.fc_ship.install_refusal("layer 1 of 3 (compose/p-layer-01)", raised.exception), fc.fc_ship.install_refusal("layer 2 of 3", "x", "a" * 40)
        self.assertIn("layer 1 of 3 (compose/p-layer-01): install failed (verify.json `install`)", mine)
        self.assertIn("Nothing was pushed", mine)
        self.assertIn(f"install failed on the base {'a' * 9}, where its failing checks are compared", base)

    def test_a_layer_refusal_names_the_check_keeps_the_output_tail_and_says_nothing_was_pushed(self):
        said = fc.fc_ship.layer_refusal("layer 2 of 3 (compose/p-layer-02)", "! test -f 002.src", "")
        self.assertIn("layer 2 of 3 (compose/p-layer-02) fails `! test -f 002.src` on its own tree. Nothing was pushed.", said)
        self.assertIn("(a task's `after`)", said)
        self.assertIn(":\n" + "y" * 800 + "\n", fc.fc_ship.layer_refusal("l", "c", "x" * 50 + "y" * 800))

    def test_a_failure_on_the_base_is_accepted_only_where_the_stack_does_not_touch_it_and_a_new_one_is_not(self):
        ran = {"c": {"failing": ["old_a", "old_b"], "unknown": False}}
        touched = lambda t: t == "old_b"
        with mock.patch.object(fc.fc_core, "sh", return_value=done("f.py\n")), mock.patch.object(fc.fc_gate, "touches", lambda wt, base, changed: touched):
            verdict = fc.fc_ship.against_base(Path("."), {}, Path("."), "b" * 40, "top", {"c": ["old_a", "old_b", "new_c"]}, ran)
        self.assertEqual(verdict, {"c": (["new_c"], ["old_b"], ["old_a"], False)})  # (new, touched, accepted, unknown)

    def test_the_base_runs_a_command_once_and_not_at_all_when_it_has_run(self):
        out = {"failing": ["t"], "unknown": False}
        with mock.patch.object(fc.fc_core, "sh", return_value=done()), mock.patch.object(fc.fc_ship, "ready"), \
                mock.patch.object(fc.fc_gate, "check_state", return_value={"c": out}) as state, mock.patch.object(fc.fc_gate, "touches", lambda *a: lambda t: False):
            ran = {}
            fc.fc_ship.against_base(Path("."), {}, Path("."), "b" * 40, "top", {"c": ["t"]}, ran)
            fc.fc_ship.against_base(Path("."), {}, Path("."), "b" * 40, "top", {"c": ["t"]}, ran)
        self.assertEqual(state.call_count, 1)


class FakedLayerCommands(unittest.TestCase):
    def test_a_layer_runs_its_checks_then_its_tasks_tests_each_command_once_and_a_lower_one_skips_a_suite(self):
        proj = Path(tempfile.mkdtemp())
        (proj / "results").mkdir()
        tasks = [{"id": "001", "test": ""}, {"id": "002", "test": "echo same"}, {"id": "003", "test": "echo same"}, {"id": "004", "test": "echo four"}]
        (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        checks = ["python3 -m tests.run", "python3 -m compileall -q bin", "echo same"]
        layer = lambda *ids: {"tested": list(ids)}
        with mock.patch("subprocess.run", side_effect=AssertionError), mock.patch("subprocess.Popen", side_effect=AssertionError):
            self.assertEqual(fc.fc_ship.layer_commands(proj, layer("001"), checks, False), ["python3 -m compileall -q bin", "echo same"])
            self.assertEqual(fc.fc_ship.layer_commands(proj, layer("002", "003", "004"), checks, False), ["python3 -m compileall -q bin", "echo same", "echo four"])
            self.assertEqual(fc.fc_ship.layer_commands(proj, layer("001", "004"), checks, True), [*checks, "echo four"])


class FakedLayerGate(unittest.TestCase):
    """The submit's gate with git and gh faked: `heads` is what GitHub holds per layer (None: no open PR), `tips` the local layers, `pushes` what was pushed."""
    BRANCHES = [f"compose/p-layer-0{i}" for i in (1, 2, 3)]

    def setUp(self):
        self.heads = {b: f"{i}" * 40 for i, b in enumerate(self.BRANCHES, 1)}
        self.tips, self.pushes, self.said, self.refuse, self.diffs = {b: "x" + f"{i}" * 39 for i, b in enumerate(self.BRANCHES, 1)}, [], [], None, {}
        self.layers = [{"branch": b} for b in self.BRANCHES]
        for target, name, fake in ((fc.fc_ship, "layer_checks", lambda *a, **k: ""), (fc.fc_stack, "layer_base", lambda *a: "main"), (fc.fc_ship, "pr_info", self.pr_info),
                                   (fc.fc_core, "sh", self.sh), (fc.fc_core, "say", self.said.append)):
            patch = mock.patch.object(target, name, fake)
            patch.start(); self.addCleanup(patch.stop)

    def pr_info(self, b, cwd):
        return {"number": self.BRANCHES.index(b) + 1, "state": "OPEN", "headRefOid": self.heads[b]} if self.heads[b] else None

    def sh(self, cmd, cwd=None, **kw):
        if cmd[1] == "push":
            self.pushes.append(cmd)
            return subprocess.CompletedProcess(cmd, int(self.refuse == cmd[-1].split(":")[0]), "", "nope")
        if cmd[1] == "rev-parse":
            return done(self.tips[cmd[-1]])
        return done("\n".join(self.diffs.get(tuple(cmd[3:]), [])))

    def gate(self, update=False):
        return fc.fc_ship.layer_gate(SimpleNamespace(update=update, fresh=False), Path("."), Path("."), self.layers, "compose/p", Path("."))

    def test_a_layer_changed_on_github_stops_the_submit_with_its_own_files_or_the_moved_base_as_the_cause(self):
        self.diffs[("compose/p-layer-01", "compose/p-layer-02")] = ["001.src"]
        self.diffs[(self.heads[self.BRANCHES[1]], self.tips[self.BRANCHES[1]])] = ["001.src"]
        self.heads[self.BRANCHES[0]] = self.tips[self.BRANCHES[0]]
        why = self.gate()
        self.assertEqual(why, fc.fc_ship.changed_stop(2, self.BRANCHES[1], {"number": 2, "headRefOid": "2" * 40}, "x" + "2" * 39, "its own files changed: 001.src"))
        self.assertIn(f"layer 2 (compose/p-layer-02) changed since PR #2: GitHub has {'2' * 9}, here x{'2' * 8}; its own files changed: 001.src. Nothing was pushed.", why)
        self.assertEqual(self.pushes, [])
        self.heads[self.BRANCHES[1]] = None  # a layer without an open PR is not compared
        self.assertIn("layer 3 (compose/p-layer-03) changed since PR #3", self.gate())
        self.assertIn("a layer below it or the base moved", self.gate())

    def test_update_pushes_each_changed_layer_bottom_up_with_a_lease_and_a_refused_push_stops_the_ones_after_it(self):
        self.refuse = self.BRANCHES[1]
        why = self.gate(update=True)
        self.assertEqual(why, "push of compose/p-layer-02 refused: nope")
        self.assertEqual([c[3] for c in self.pushes], [f"--force-with-lease={b}:{self.heads[b]}" for b in self.BRANCHES[:2]])
        self.assertEqual(self.said, [f"submit update {self.BRANCHES[0]} x{'1' * 8} (was {'1' * 9})"])
        self.pushes.clear(); self.said.clear(); self.refuse = None
        self.assertEqual(self.gate(update=True), "")
        self.assertEqual([c[-1] for c in self.pushes], [f"{b}:{b}" for b in self.BRANCHES])
        self.assertEqual(len(self.said), 3)

    def test_a_layer_whose_head_matches_or_whose_pr_is_not_open_is_not_a_change(self):
        for info, tip, want in ((None, "t", False), ({"state": "CLOSED", "headRefOid": "h"}, "t", False), ({"state": "OPEN", "headRefOid": "t"}, "t", False), ({"state": "OPEN", "headRefOid": "h"}, "t", True)):
            self.assertIs(fc.fc_ship.layer_changed(info, tip), want, info)
        self.assertEqual(fc.fc_ship.change_cause(["a", "b"], ["b", "c"]), "its own files changed: b")
        self.assertEqual(fc.fc_ship.change_cause(["a"], ["c"]), "a layer below it or the base moved")
        self.assertEqual(fc.fc_ship.lease_push("b", "h"), ["git", "push", "-q", "--force-with-lease=b:h", "origin", "b:b"])

    def test_a_failed_layer_check_stops_the_gate_before_any_pr_is_read(self):
        with mock.patch.object(fc.fc_ship, "layer_checks", return_value="layer 1 fails"), mock.patch.object(fc.fc_ship, "pr_info", side_effect=AssertionError):
            self.assertEqual(self.gate(update=True), "layer 1 fails")
        self.assertEqual(self.pushes, [])

    def test_stack_submit_hands_register_stack_a_gate_that_knows_update(self):
        gates = []
        with mock.patch.object(fc.fc_stack, "register_stack", lambda *a, **kw: gates.append(kw["gate"]) or [["x"]]), mock.patch.object(fc.fc_update, "refuse_while_verifying"), \
                mock.patch.object(fc.fc_core, "project_dir", return_value=Path(".")):
            fc.fc_ship.cmd_stack(SimpleNamespace(repo=".", project="p", trunk="compose/p", submit=True, update=True, fresh=False))
        self.assertEqual(gates[0](Path("."), Path("."), self.layers, "compose/p", Path(".")), "")
        self.assertEqual(len(self.pushes), 3)


if __name__ == "__main__":
    unittest.main()
