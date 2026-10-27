import hermetic  # first: no test sees the real home
import json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
from layers_kit import fc, unit
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""


class StackFolds(unittest.TestCase):
    """Adjacent layers fold until the stack has no more PRs than max_stack_prs: same kind first, then any; recorded layers never."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve()
        for target in ("subprocess.run", "subprocess.Popen"):
            patch = mock.patch(target, side_effect=AssertionError("a faked test starts no process"))
            patch.start(); self.addCleanup(patch.stop)

    def groups(self, units, cap):
        return [(b, [u["name"] for u in g], f) for b, g, f in fc.fc_layers.group_by_record(self.proj, units, 300, cap)]

    def test_twelve_one_task_layers_fold_the_lowest_pair_into_ten_the_lower_on_a_tie(self):
        us = [unit(f"{i:03d}", 400) for i in range(1, 13)]
        alone = [(None, [u["name"]], False) for u in us]
        self.assertEqual(self.groups(us, None), alone)
        self.assertEqual(self.groups(us, 12), alone)
        self.assertEqual(self.groups(us, 10), [(None, ["001", "002"], True), (None, ["003", "004"], True)] + alone[4:])
        self.assertEqual(self.groups([unit("001", 400), unit("002", 500), unit("003", 350)], 2), [(None, ["001"], False), (None, ["002", "003"], True)])

    def test_groundwork_and_a_task_fold_together_only_when_no_same_kind_pair_is_left(self):
        us = [unit("groundwork", 10), unit("001", 400), unit("002", 400), unit("groundwork", 10)]
        self.assertEqual(self.groups(us, 3), [(None, ["groundwork"], False), (None, ["001", "002"], True), (None, ["groundwork"], False)])
        self.assertEqual(self.groups(us, 2), [(None, ["groundwork", "001", "002"], True), (None, ["groundwork"], False)])

    def test_a_recorded_layer_keeps_its_tasks_and_never_folds_even_above_the_cap(self):
        (self.proj / "layers.json").write_text(json.dumps({"fast/p/layer-01": ["001"], "fast/p/layer-02": ["002"]}))
        us = [unit(f"{i:03d}", 400) for i in range(1, 5)]
        self.assertEqual(self.groups(us, 2), [("fast/p/layer-01", ["001"], False), ("fast/p/layer-02", ["002"], False), (None, ["003", "004"], True)])


class StackLimit(unittest.TestCase):
    """The layers over the budget say so."""

    def test_a_folded_layer_over_the_budget_says_so_on_its_layer_line(self):
        l = {"tasks": ["001", "002"], "lines": 400, "generated": {}, "fold_cap": 3}
        self.assertEqual(fc.fc_layers.layer_note(l, 300), "task 001, 002: 400 lines; over the 300-line budget to stay within 3 PRs")
        self.assertEqual(fc.fc_layers.layer_note({**l, "lines": 250}, 300), "task 001, 002: 250 lines")
        self.assertEqual(fc.fc_layers.layer_note({"tasks": ["003"], "lines": 155, "generated": {}}, 30), "task 003: 155 lines; over the 30-line budget, a task is never split")
        g = {"tasks": ["groundwork"], "lines": 400, "generated": {}}  # a groundwork unit is one commit
        self.assertEqual(fc.fc_layers.layer_note(g, 300), "groundwork x1: 400 lines; over the 300-line budget, a commit is never split")


if __name__ == "__main__":
    unittest.main()
