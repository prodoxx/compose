import hermetic  # first: no test sees the real home
import contextlib, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
from layers_kit import fc, unit
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""


def numstat_sh(rows, deleted=(), added=()):
    """The git a repo would answer for one diff: `rows` are (added, deleted, path) numstat rows, `deleted` and `added` the paths with that status."""
    def sh(cmd, *a, **k):
        out = {"--numstat": "".join(f"{x}\t{y}\t{f}\n" for x, y, f in rows), "--name-only": "\n".join(f for _, _, f in rows)}
        if "--diff-filter=D" in cmd: out["--name-only"] = "\n".join(deleted)
        if "--diff-filter=A" in cmd: out["--name-only"] = "\n".join(added)
        return subprocess.CompletedProcess(cmd, 0, next((v for flag, v in out.items() if flag in cmd), ""), "")
    return sh


class DeletionBudget(unittest.TestCase):
    """Task 014 (NEB-951): branch_budget counts a whole-file deletion and each delete: path as one line."""

    def budget(self, rows, allowed, delete=(), deleted=()):
        with mock.patch.object(fc.fc_core, "sh", numstat_sh(rows, deleted)):
            return fc.fc_gate.branch_budget(Path("."), "main", allowed=allowed, delete=delete)

    def test_deleting_a_400_line_file_the_task_owns_counts_one_line(self):
        self.assertEqual(self.budget([(0, 400, "big.py")], ["big.py"], deleted=["big.py"]), (1, [], []))

    def test_a_deleted_delete_directory_counts_one_line_and_its_files_are_not_stray(self):
        rows = [(0, 100, "old/a.py"), (0, 50, "old/sub/b.py")]
        self.assertEqual(self.budget(rows, [], delete=["old/"], deleted=["old/a.py", "old/sub/b.py"]), (1, [], []))

    def test_a_deletion_outside_allowed_and_every_delete_path_is_still_stray(self):
        rows = [(0, 100, "old/a.py"), (0, 80, "older.py")]
        self.assertEqual(self.budget(rows, [], delete=["old"], deleted=["old/a.py", "older.py"]), (2, [], ["older.py"]))

    def test_edits_lockfiles_and_an_emptied_file_count_as_before_beside_a_deletion(self):
        rows = [(1, 1, "a.py"), (0, 10, "b.py"), (5000, 0, "yarn.lock"), (0, 300, "gone.py")]
        self.assertEqual(self.budget(rows, ["a.py", "b.py", "yarn.lock", "gone.py"], deleted=["gone.py"]), (2 + 10 + 1, [], []))


class WholeFileDeletion(unittest.TestCase):
    """Task 018: commit_facts counts a file the commit deletes whole as one line."""

    def facts(self, rows, deleted=()):
        with mock.patch.object(fc.fc_core, "sh", numstat_sh(rows, deleted)):
            return fc.fc_layers.commit_facts(Path("."), "HEAD")

    def test_a_whole_file_deletion_counts_one_line(self):
        self.assertEqual(self.facts([("0", "400", "old.py")], ["old.py"]), (["old.py"], 1, {}))

    def test_a_deletion_beside_an_edit_counts_one_plus_the_edit(self):
        self.assertEqual(self.facts([("0", "400", "old.py"), ("5", "0", "a.py")], ["old.py"]), (["old.py", "a.py"], 6, {}))

    def test_an_emptied_file_still_counts_every_line(self):
        self.assertEqual(self.facts([("0", "400", "old.py")]), (["old.py"], 400, {}))

    def test_a_deleted_lockfile_stays_generated_with_its_full_count(self):
        self.assertEqual(self.facts([("0", "300", "package-lock.json")], ["package-lock.json"]), (["package-lock.json"], 0, {"package-lock.json": 300}))

    def test_each_deleted_file_counts_one_even_in_a_subdirectory_or_binary(self):
        rows, gone = [("0", "100", "old/a.py"), ("0", "50", "old/sub/b.py"), ("-", "-", "logo.png")], ["old/a.py", "old/sub/b.py", "logo.png"]
        self.assertEqual(self.facts(rows, gone), (["old/a.py", "old/sub/b.py", "logo.png"], 3, {}))

    def test_a_moved_file_counts_its_old_path_as_one_and_its_new_path_in_full(self):
        self.assertEqual(self.facts([("400", "0", "new.py"), ("0", "400", "old.py")], ["old.py"]), (["new.py", "old.py"], 401, {}))

    def test_a_delete_only_unit_shares_the_layer_of_the_200_line_unit_before_it(self):
        lines = self.facts([("0", "400", "old.py")], ["old.py"])[1]
        layers = fc.fc_layers.group_units([unit("001", 200), unit("002", lines)], 300)
        self.assertEqual([[u["name"] for u in g] for g in layers], [["001", "002"]])


class FailingIds(unittest.TestCase):
    def ids(self, out):
        return fc.fc_gate.FAILING_RE.findall(out)

    def test_a_unittest_summary_line_is_never_a_test_id(self):
        for summary in ("FAILED (failures=3)", "FAILED (errors=2)", "FAILED (failures=1, errors=1)", "FAILED (skipped=2)", "FAILED (failures=1, errors=1, skipped=2)"):
            self.assertEqual(self.ids(f"Ran 5 tests in 0.1s\n\n{summary}\n"), [], summary)

    def test_real_ids_from_each_runner_still_match(self):
        out = ("FAIL: test_x (mod.Class.test_x)\nERROR: test_z (mod.Class.test_z)\nFAILED (failures=1, errors=1)\n"
               "FAILED tests/x.py::test_y - AssertionError\nnot ok 1 - name\n--- FAIL: TestX (0.00s)\n")
        self.assertEqual(self.ids(out), ["test_x (mod.Class.test_x)", "test_z (mod.Class.test_z)", "tests/x.py::test_y - AssertionError", "name", "TestX"])

    def test_the_same_failing_ids_under_a_different_summary_are_no_regression(self):
        state = lambda summary: {"c": {"exit": 1, "failing": sorted(set(self.ids(f"FAIL: test_a (m.C.test_a)\n{summary}\n")))}}
        self.assertEqual(fc.fc_gate.regressions(state("FAILED (errors=3)"), state("FAILED (failures=3)")), ([], []))


class UnknownBaseline(unittest.TestCase):
    def run_of(self, script, limit=1):
        return lambda cmd, wt, lim, env=None: fc.fc_implement.timed(script, wt, lim, env=env)

    def base(self, d, name=""):
        """(repo, head): the base a baseline is taken on; its scratch clone is the plain folder `d/name`."""
        d = Path(d, name); d.mkdir(exist_ok=True)
        return d, f"head-{name}"

    def setUp(self):  # the scratch clone of the base is the repo folder itself
        p = mock.patch.object(fc.fc_gate, "base_tree", lambda wt, base, delete: contextlib.nullcontext(wt)); p.start(); self.addCleanup(p.stop)

    def test_a_baseline_cut_off_by_its_limit_is_unknown_and_not_kept(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(fc.fc_gate, "CHECKS_TIMEOUT", 0.3):
            script = "echo 'FAIL: test_a (t.T.test_a)'; printf '%05000d' 0; sleep 30"
            repo, head = self.base(d)
            got = fc.fc_gate.baseline_state(self.run_of(script), ["suite"], repo, (head, ()), 0.3)
            self.assertEqual((got["suite"]["exit"], got["suite"]["unknown"]), (124, True))
            self.assertNotIn(((head, ()), ("suite",)), fc.fc_gate.BASELINES)

    def test_a_clean_or_listed_baseline_is_known_and_kept(self):
        with tempfile.TemporaryDirectory() as d:
            for name, script, exit_ in (("clean", "true", 0), ("listed", "echo 'FAIL: test_a (t.T.test_a)'; exit 1", 1)):
                repo, head = self.base(d, name)
                got = fc.fc_gate.baseline_state(self.run_of(script), [name], repo, (head, ()), 5)
                self.assertEqual((got[name]["exit"], got[name]["unknown"]), (exit_, False))
                self.assertIn(((head, ()), (name,)), fc.fc_gate.BASELINES)

    def test_a_non_zero_exit_that_lists_no_id_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            got = fc.fc_gate.check_state(self.run_of("echo boom; exit 2"), ["c"], Path(d), 5)
            self.assertEqual((got["c"]["failing"], got["c"]["unknown"]), ([], True))

    def test_an_id_red_on_an_unknown_baseline_rejects_only_when_the_diff_reaches_its_test(self):
        before = {"c": {"exit": 124, "failing": [], "unknown": True}}
        after = {"c": {"exit": 1, "failing": ["test_a (tests.test_ship.S.test_a)", "test_b (tests.test_other.O.test_b)"], "unknown": False}}
        broke, unknown = fc.fc_gate.regressions(before, after, lambda tid: "test_ship" in tid)
        self.assertEqual((broke, unknown), (["test_a (tests.test_ship.S.test_a)"], ["test_b (tests.test_other.O.test_b)"]))

    def test_an_id_red_on_a_known_baseline_is_never_excused(self):
        before = {"c": {"exit": 0, "failing": [], "unknown": False}}
        after = {"c": {"exit": 1, "failing": ["test_a (tests.test_ship.S.test_a)"], "unknown": False}}
        self.assertEqual(fc.fc_gate.regressions(before, after, lambda tid: False), (["test_a (tests.test_ship.S.test_a)"], []))

    def test_touches_names_the_test_file_and_the_modules_it_imports(self):
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            (wt / "tests").mkdir(); (wt / "bin").mkdir()
            (wt / "tests" / "test_ship.py").write_text("import fc_ship\nfc = load()\nfc.fc_status.x()\n")
            (wt / "tests" / "test_other.py").write_text("import unittest\n")
            ship, status, other = "test_a (tests.test_ship.S.test_a)", "test_b (tests.test_other.O.test_b)", "tests/test_ship.py::test_c - assert"
            check = fc.fc_gate.touches(wt, "HEAD", ["bin/fc_status.py"])
            self.assertEqual((check(ship), check(status), check(other)), (False, False, False))  # fc_status is named in no import line
            check = fc.fc_gate.touches(wt, "HEAD", ["bin/fc_ship.py"])
            self.assertEqual((check(ship), check(status), check(other)), (True, False, True))
            check = fc.fc_gate.touches(wt, "HEAD", ["tests/test_other.py", "bin/fc_ship.py"])
            self.assertEqual((check(ship), check(status), check(other)), (True, True, True))
            self.assertFalse(fc.fc_gate.touches(wt, "HEAD", ["tests/test_other.py"])("TestUnnamed"))
            self.assertFalse(fc.fc_gate.touches(wt, "HEAD", [])(ship))


class AfterChecks(unittest.TestCase):
    """Task 003 (NEB-1705): after_checks keeps the non-suite checks, then the test: of each code task that owns a changed path."""

    CHECKS = ["python3 -m compileall -q bin", "python3 -m tests.run", "python3 -m unittest discover -s tests"]
    TASKS = [{"id": "001", "files": ["a.py"], "protected": ["tests/test_a.py"], "test": "python3 -m unittest tests.test_a"},
             {"id": "002", "files": ["b.py"], "protected": ["tests/test_b.py"], "test": "python3 -m unittest tests.test_b"},
             {"id": "003", "kind": "design", "files": ["c.md"], "protected": ["tests/test_c.py"], "test": "true"},
             {"id": "004", "files": ["b.py", "d.py"], "protected": [], "test": "python3 -m unittest tests.test_d"}]

    def after(self, changed, own="", checks=CHECKS, tasks=TASKS):
        return fc.fc_gate.after_checks(list(checks), [dict(t) for t in tasks], changed, own)

    def test_a_suite_check_is_left_out_and_a_non_suite_check_is_kept_in_order(self):
        self.assertEqual(self.after([]), ["python3 -m compileall -q bin"])
        self.assertEqual(self.after([], checks=["true", "python3 -m tests.run", "python3 -m compileall -q bin", "false"]), ["true", "python3 -m compileall -q bin", "false"])

    def test_the_test_of_each_code_task_owning_a_changed_path_follows_the_checks_in_task_order(self):
        compile_ = "python3 -m compileall -q bin"
        self.assertEqual(self.after(["b.py"]), [compile_, "python3 -m unittest tests.test_b", "python3 -m unittest tests.test_d"])
        self.assertEqual(self.after(["d.py", "a.py"]), [compile_, "python3 -m unittest tests.test_a", "python3 -m unittest tests.test_d"])

    def test_a_protected_path_selects_its_task_and_a_design_task_never_runs(self):
        self.assertEqual(self.after(["tests/test_b.py"])[1:], ["python3 -m unittest tests.test_b"])
        self.assertEqual(self.after(["c.md", "tests/test_c.py"]), ["python3 -m compileall -q bin"])

    def test_a_path_in_no_task_adds_no_test(self):
        self.assertEqual(self.after(["README.md", "x/y.py"]), ["python3 -m compileall -q bin"])

    def test_the_own_test_is_never_in_the_list_even_as_a_check_or_for_a_second_task(self):
        own = "python3 -m unittest tests.test_a"
        self.assertEqual(self.after(["a.py"], own=own), ["python3 -m compileall -q bin"])
        self.assertEqual(self.after([], own="python3 -m compileall -q bin"), [])
        self.assertEqual(self.after(["b.py"], own="python3 -m unittest tests.test_d")[1:], ["python3 -m unittest tests.test_b"])

    def test_no_command_appears_twice(self):
        shared = [dict(t, test="python3 -m unittest tests.test_a") for t in self.TASKS[:2]]
        checks = ["python3 -m unittest tests.test_a", "python3 -m compileall -q bin", "python3 -m compileall -q bin"]
        self.assertEqual(self.after(["a.py", "b.py"], checks=checks, tasks=shared), ["python3 -m unittest tests.test_a", "python3 -m compileall -q bin"])


if __name__ == "__main__":
    unittest.main()
