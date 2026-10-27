import hermetic  # first: no test sees the real home
import json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
from layers_kit import LayersBase, fc, unit
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""


class LayerNames(LayersBase):
    """The Layers project with its task branches under the task prefix: the layers and the chain take their names from it."""

    def rename(self, prefix):
        for t in ("001", "002", "003"):
            self.git("branch", "-m", f"compose/p-{t}", f"{prefix}{t}")

    def layers(self, prefix="compose/p-"):
        return self.git("branch", "--list", f"{prefix}layer-*", "--format=%(refname:short)").split()

    def test_a_new_project_cuts_its_task_branches_into_layers_named_from_its_branch(self):
        out = self.stack()
        self.assertEqual(self.layers(), ["compose/p-layer-01", "compose/p-layer-02", "compose/p-layer-03"])
        self.assertEqual(self.changed("compose/p-layer-02", "compose/p-layer-01"), {"t001.test", "001.src", "t002.test", "002.src"})
        self.assertEqual(self.changed("compose/p-layer-03", "compose/p-layer-02"), {"t003.test", "003.src"})
        self.assertEqual(self.git("diff", "compose/p-layer-03", "compose/p-003"), "")
        self.assertTrue(any(l.startswith("layer  layer-01") for l in out))
        self.assertEqual(json.loads((self.proj / "verify.json").read_text())["layer_prefix"], "compose/p-")
        before = self.git("rev-parse", *self.layers())
        self.stack()  # the layer branches share the task prefix, and are never taken for tasks
        self.assertEqual(self.git("rev-parse", *self.layers()), before)

    def test_a_task_whose_tip_holds_another_sits_above_it(self):
        self.git("checkout", "-q", "-B", "compose/p-001", "compose/p-003")
        self.write("001.src", "c\n" * 100); self.commit("task 001 on 003")
        self.git("checkout", "-q", "compose/p")
        out = self.stack()
        self.assertEqual([l for l in out if "STOP" in l], [])
        self.assertEqual(self.changed("compose/p-layer-03", "compose/p-layer-02"), {"t001.test", "001.src"})
        self.assertEqual(self.git("diff", "compose/p-layer-03", "compose/p-001"), "")

    def test_a_replaced_tip_is_dropped_from_the_layer_above_and_its_ref_deleted(self):
        self.git("checkout", "-q", "-B", "compose/p-003", "compose/p-002")
        self.write("003.src", "c\n" * 150); self.commit("task 003 on 002")
        fc.fc_worktree.replace_branch(self.repo, self.proj, "compose/p-002")
        self.git("checkout", "-q", "-b", "compose/p-002", "compose/p")
        self.write("002.src", "d\n" * 120); self.commit("task 002 again")
        self.git("checkout", "-q", "compose/p")
        out = self.stack()
        self.assertEqual([l for l in out if "STOP" in l], [])
        self.assertEqual(self.git("show", "compose/p-layer-03:002.src"), "d\n" * 119 + "d")
        self.assertEqual(self.git("diff", "compose/p-layer-03", "compose/p-003"), "")
        self.assertEqual(self.git("for-each-ref", "refs/compose/replaced/"), "")

    def test_a_legacy_project_never_pushed_takes_the_new_names_before_its_layers_are_cut(self):
        self.rename("fast/p/")
        self.git("branch", "fast/p/layer-09", "main")  # an earlier stack's layer, never pushed
        out = self.stack(branch="compose/p")
        self.assertIn("names  renamed 4 never-pushed fast/p/* branch(es) to compose/p-*", out)
        self.assertEqual(self.git("branch", "--list", "fast/*"), "")
        self.assertEqual(self.layers(), ["compose/p-layer-01", "compose/p-layer-02", "compose/p-layer-03"])
        self.assertEqual(self.git("diff", "compose/p-layer-03", "compose/p-003"), "")
        cfg = json.loads((self.proj / "verify.json").read_text())
        self.assertEqual((cfg["task_prefix"], cfg["layer_prefix"]), ("compose/p-", "compose/p-"))

    def test_a_legacy_project_with_a_pushed_branch_keeps_fast_names_unless_a_prefix_is_recorded(self):
        self.rename("fast/p/")
        self.git("update-ref", "refs/remotes/origin/fast/p/layer-01", "main")
        self.stack(branch="compose/p")
        self.assertEqual(self.layers("fast/p/"), ["fast/p/layer-01", "fast/p/layer-02", "fast/p/layer-03"])
        self.assertEqual(json.loads((self.proj / "verify.json").read_text())["layer_prefix"], "fast/p/")
        self.stack(branch="compose/p", layer_prefix="review/p-")
        self.assertEqual(self.layers("review/p-"), ["review/p-layer-01", "review/p-layer-02", "review/p-layer-03"])


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


class StackLimit(LayersBase):
    """A stack is capped at max_stack_prs layers, and the layers over the budget say so."""

    def test_a_folded_layer_over_the_budget_says_so_on_its_layer_line_and_size_line(self):
        out = [l for l in self.stack(review_layer_lines=30, max_stack_prs=3) if l.startswith("layer ")]  # natural: 20, 20, 105, 125, 155
        self.assertEqual(len(out), 3)
        self.assertRegex(out[0], r"layer-01  groundwork x2: 40 lines.*; over the 30-line budget to stay within 3 PRs")
        self.assertRegex(out[1], r"layer-02  task 001, 002: 230 lines.*; over the 30-line budget to stay within 3 PRs")
        self.assertIn("task 003: 155 lines; over the 30-line budget, a task is never split", out[2])
        self.assertNotIn("stay within", out[2])
        l = {"tasks": ["001", "002"], "lines": 400, "generated": {}, "fold_cap": 3}
        self.assertEqual(fc.fc_layers.layer_note(l, 300), "task 001, 002: 400 lines; over the 300-line budget to stay within 3 PRs")
        self.assertEqual(fc.fc_layers.layer_note({**l, "lines": 250}, 300), "task 001, 002: 250 lines")
        g = {"tasks": ["groundwork"], "lines": 400, "generated": {}}  # a groundwork unit is one commit
        self.assertEqual(fc.fc_layers.layer_note(g, 300), "groundwork x1: 400 lines; over the 300-line budget, a commit is never split")

    def test_the_cap_is_ten_unless_set_and_the_workspace_beats_the_top_level(self):
        for i in range(8):  # 13 layers at a 1-line budget: two groundwork commits, these eight, three tasks
            self.write(f"g{i}.txt", "g\n"); self.commit(f"groundwork g{i}")
        cfg = Path(tempfile.mkdtemp()) / "config.json"
        count = lambda **v: len([l for l in self.stack(review_layer_lines=1, **v) if l.startswith("layer ")])
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", cfg):
            self.assertEqual(count(), 10)
            cfg.write_text(json.dumps({"max_stack_prs": 5, "workspace": "w", "workspaces": {"w": {"max_stack_prs": 4}}}))
            self.assertEqual(count(), 4)


if __name__ == "__main__":
    unittest.main()
