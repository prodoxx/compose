import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
import sys
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

class OutsideBlockers(unittest.TestCase):
    """Project `found` builds NEB-10 (task 001) and NEB-11 (task 002); project `app` has a task that waits on one of them.
    What git would say is `self.trunk` (the refs that are ancestors of main), `self.twins` (ref -> the main commit carrying its patch) and `self.heads`."""
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.trunk, self.twins, self.heads = set(), {}, ["main"]
        for name, fake in (("in_base", lambda repo, ref, base: ref in self.trunk), ("landed_as", lambda repo, ref, base: self.twins.get(ref, "")),
                           ("missing", lambda repo, ref, base: f"{ref} is not in {base}")):
            patch = mock.patch.object(fc.fc_blockers, name, fake); patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(fc.fc_worktree, "heads", lambda repo: self.heads); patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(fc.fc_core, "worktree_branches", lambda repo: [(self.repo, "main")]); patch.start(); self.addCleanup(patch.stop)
        self.found = self.project("found", "NEB-1", [("001", "NEB-10", "passed"), ("002", "NEB-11", "passed")])
        self.app = self.project("app", "NEB-2", [("010", "NEB-20", None)], waits=["NEB-10 (In Review)"])

    def project(self, name, feature, tasks, waits=()):
        proj = self.repo / ".compose" / name
        (proj / "results").mkdir(parents=True)
        (proj / "verify.json").write_text(json.dumps({"task_prefix": f"{name}-"}))
        metas = [{"id": tid, "linear": lid, "kind": "code", "linear_state": "In Review", "waits_on": list(waits)} for tid, lid, _ in tasks]
        (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {"id": feature}, "tasks": metas, "issues": {}}))
        for tid, _, status in tasks:
            if status:
                (proj / "results" / f"{tid}.json").write_text(json.dumps({"status": status}))
        return proj

    def released(self, base="main"):
        return fc.fc_blockers.released(self.repo, self.app, fc.fc_tasks.load_tasks(self.app), base)

    def test_a_blocker_whose_task_branch_is_in_the_trunk_holds_nothing_whatever_its_state(self):
        self.trunk = {"found-001"}
        for state in ("In Review", "Todo"):
            (self.app / "results" / "tasks.json").write_text(json.dumps({"feature": {"id": "NEB-2"}, "issues": {}, "tasks": [
                {"id": "010", "kind": "code", "linear": "NEB-20", "waits_on": [f"NEB-10 ({state})"]}]}))
            out, still = self.released()
            self.assertEqual((list(out), still), (["NEB-10"], {}), state)
            self.assertIn("found-001", out["NEB-10"])
            self.assertEqual(fc.fc_tasks.waiting(fc.fc_tasks.load_tasks(self.app), out), {})

    def test_a_blocker_whose_task_branch_is_not_in_the_trunk_stays_held_with_the_reason(self):
        out, still = self.released()
        self.assertEqual(out, {})
        self.assertEqual(still, {"NEB-10": "found task 001: found-001 is not in main; found: no stack built yet"})
        self.assertIn("NEB-10", fc.fc_tasks.waiting(fc.fc_tasks.load_tasks(self.app), out)["010"]["reason"])

    def test_a_route_hold_is_no_blocker_and_is_not_judged(self):
        (self.app / "results" / "tasks.json").write_text(json.dumps({"feature": {"id": "NEB-2"}, "issues": {}, "tasks": [
            {"id": "010", "kind": "code", "linear": "NEB-20", "waits_on": ["the route (policies unreadable: x)"]}]}))
        self.assertEqual(self.released(), ({}, {}))

    def test_a_task_rebased_into_the_stack_counts_when_the_stack_top_is_in_the_trunk(self):
        self.heads = ["main", "found-001", "found-layer-01"]
        self.assertEqual(self.released()[0], {})
        self.trunk = {"found-layer-01"}
        out, still = self.released()
        self.assertEqual((list(out), still), (["NEB-10"], {}))
        self.assertIn("found-layer-01", out["NEB-10"])

    def test_a_task_commit_rebased_into_the_trunk_under_another_hash_counts_by_its_patch(self):
        (self.found / "results" / "002.json").unlink()  # the stack top proves nothing: task 002 is open
        self.twins = {"found-001": "abcdef1234"}
        out, still = self.released()
        self.assertEqual((list(out), still), (["NEB-10"], {}))
        self.assertIn("found-001 is in main as abcdef1", out["NEB-10"])

    def test_a_task_commit_whose_patch_is_not_in_the_trunk_stays_held(self):
        out, still = self.released()
        self.assertEqual(out, {})
        self.assertIn("found-001 is not in main", still["NEB-10"])

    def test_a_stack_whose_tasks_have_not_all_passed_proves_nothing(self):
        (self.found / "results" / "002.json").unlink()
        self.heads, self.trunk = ["main", "found-layer-01"], {"found-layer-01"}
        self.assertEqual(self.released(), ({}, {"NEB-10": "found task 001: found-001 is not in main; found: task 002 not passed yet"}))

    def test_a_blocker_no_project_has_stays_held(self):
        (self.app / "results" / "tasks.json").write_text(json.dumps({"feature": {"id": "NEB-2"}, "issues": {}, "tasks": [
            {"id": "010", "kind": "code", "linear": "NEB-20", "waits_on": ["NEB-99 (In Review)"]}]}))
        self.assertEqual(self.released(), ({}, {"NEB-99": "no project of this repository has NEB-99 in its last sync"}))

    def test_a_backup_branch_beside_the_layers_is_not_a_layer(self):
        self.heads, self.trunk = ["main", "found-layer-01", "found-layer-02", "found-layer-01-prerebase"], {"found-layer-02"}
        out, still = self.released()
        self.assertEqual((list(out), still), (["NEB-10"], {}))
        self.assertIn("found-layer-02", out["NEB-10"])

    def test_status_shows_the_task_as_pending_once_its_blocker_is_in_the_trunk(self):
        meta, facts = fc.fc_tasks.load_tasks(self.app)[0], {"prefix": "app-", "have": set(), "merged": set()}
        self.assertEqual(fc.fc_status.task_state(self.repo, self.app, meta, "", facts)["state"], "held")
        row = fc.fc_status.task_state(self.repo, self.app, meta, "", {**facts, "released": {"NEB-10": "proof"}})
        self.assertEqual(row["state"], "pending")


if __name__ == "__main__":
    unittest.main()
