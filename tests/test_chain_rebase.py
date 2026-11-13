import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
L = fc.fc_layers


class Dag:
    """A repository as a commit graph that answers the git calls `_linearize` makes, and records its rebases. A commit named with a
    trailing ' is a copy of the one without: the same patch-id under another hash. Compose runs no git in a test (tests/hermetic.py)."""
    def __init__(self, commits, tips, forks=None, conflicts=None):
        self.commits, self.tips, self.forks, self.conflicts = commits, dict(tips), forks or {}, conflicts or {}
        self.rebases, self.calls = [], []

    def anc(self, c):
        out, todo = set(), [self.tips.get(c, c)]
        while todo:
            x = todo.pop()
            if x not in out:
                out.add(x)
                todo += self.commits[x]
        return out

    def base(self, a, b):
        common = self.anc(a) & self.anc(b)
        return max(common, key=lambda c: len(self.anc(c))) if common else ""

    def sh(self, cmd, cwd=None, check=True, env=None, **kw):
        self.calls.append(cmd)
        out, rc = "", 0
        g = cmd[1:]
        if g[0] == "merge-base" and g[1] == "--is-ancestor":
            rc = 0 if self.tips.get(g[2], g[2]) in self.anc(g[3]) else 1
        elif g[0] == "merge-base" and g[1] == "--fork-point":
            out = self.forks.get((g[2], g[3]), "")
            rc = 0 if out else 1
        elif g[0] == "merge-base":
            out = self.base(g[1], g[2])
        elif g[0] == "rev-parse":
            out = self.tips.get(g[1], g[1])
        elif g[0] == "rev-list" and g[1] == "--count":
            a, _, b = g[2].partition("..")
            out = str(len(self.anc(b) - self.anc(a)))
        elif g[0] == "rev-list":  # --no-merges up..b
            a, _, b = g[2].partition("..")
            out = "\n".join(sorted(self.anc(b) - self.anc(a)))
        elif g[0] == "cherry":
            base, b, up = g[1:4]
            theirs = {c.rstrip("'") for c in self.anc(base) - self.anc(b)}
            mine = sorted(self.anc(b) - self.anc(up), key=lambda c: len(self.anc(c)))
            out = "".join(("- " if c.rstrip("'") in theirs else "+ ") + c + "\n" for c in mine)
        elif g[0] == "rebase" and g[1] == "--abort":
            self.aborted = True
        elif g[0] == "rebase":
            onto, up = g[g.index("--onto") + 1:g.index("--onto") + 3]
            drop = (env or {}).get("COMPOSE_DROP", "").split()
            b = self.on
            self.rebases.append({"b": b, "onto": onto, "upstream": up, "drop": drop, "interactive": "-i" in g})
            if (b, onto) in self.conflicts:
                rc = 1
            else:
                prev = self.tips.get(onto, onto)
                for c in sorted(self.anc(b) - self.anc(up), key=lambda c: len(self.anc(c))):
                    if c not in drop:
                        self.commits[c + "*"] = [prev]
                        prev = c + "*"
                self.tips[b] = prev
        elif g[0] == "reset":
            self.rebases.append({"b": self.on, "reset": g[-1]})
            self.tips[self.on] = self.tips.get(g[-1], g[-1])
        elif g[0] == "checkout" and g[1] == "-q" and g[2] != "--detach":
            self.on = g[2]
        elif g[0] == "diff":
            out = " ".join(self.conflicts.get((self.on, self.rebases[-1]["onto"]), ()))
        elif g[0] == "log":
            out = "abc1234 the layer's own change"
        return subprocess.CompletedProcess(cmd, rc, out + ("\n" if out else ""), "")


class Chain(unittest.TestCase):
    def linearize(self, dag, chain, replaced=(), **kw):
        said = []
        with mock.patch.object(L, "chain_order", return_value=chain), mock.patch.object(L.fc_worktree, "task_prefix", return_value="p-"), \
             mock.patch.object(L.fc_worktree, "replaced_tips", return_value=list(replaced)), mock.patch.object(L.fc_core, "sh", dag.sh), \
             mock.patch.object(L.fc_core, "say", said.append):
            self.addCleanup(setattr, L, "STACK_STOP", "")
            return L._linearize(Path("."), Path("p"), "main", Path("."), **kw), said

    def test_a_layer_cut_from_the_same_parent_as_the_one_below_it_replays_only_its_own_commit(self):
        """#472: 308 and 309 sit on the layer 307, which `through` leaves out of the chain; the old base is 307's tip, not the trunk's."""
        dag = Dag({"T": [], "L1": ["T"], "L2": ["L1"], "P": ["L2"], "B1": ["P"], "B2": ["P"]},
                  {"main": "T", "l1": "L1", "l2": "L2", "l3": "P", "b1": "B1", "b2": "B2"}, forks={("main", "b2"): "T"})
        linear, _ = self.linearize(dag, ["l1", "l2", "l3", "b1", "b2"], through=["b1", "b2"])
        self.assertEqual(linear, ["b1", "b2"])
        self.assertEqual([(r["b"], r["onto"], r["upstream"]) for r in dag.rebases], [("b2", "b1", "P")])  # not the trunk's T: that replays L1, L2 and P

    def test_a_layer_whose_trunk_was_rewritten_still_starts_above_the_trunks_old_tip(self):
        """The shared commit of prev and the layer is older than the trunk's old tip: the old tip stays the deeper base."""
        dag = Dag({"R": [], "OLD": ["R"], "NEW": ["R"], "A": ["OLD"]}, {"main": "NEW", "a": "A"}, forks={("main", "a"): "OLD"})
        linear, _ = self.linearize(dag, ["a"])
        self.assertEqual((linear, [(r["onto"], r["upstream"]) for r in dag.rebases]), (["a"], [("main", "OLD")]))

    def test_commits_the_base_holds_under_other_hashes_are_dropped_from_the_replay(self):
        """#450: the layer carries copies of the lower layers' commits; git compares them with the upstream side only, so they are dropped here."""
        dag = Dag({"T": [], "c1": ["T"], "c2": ["c1"], "c3": ["c2"], "c1'": ["T"], "c2'": ["c1'"], "c3'": ["c2'"], "D": ["c3'"]},
                  {"main": "T", "p3": "c3", "d": "D"})
        linear, _ = self.linearize(dag, ["p3", "d"])
        (r,) = [r for r in dag.rebases if r["b"] == "d"]
        self.assertEqual((linear, r["onto"], r["upstream"], r["interactive"], sorted(r["drop"])), (["p3", "d"], "p3", "T", True, ["c1'", "c2'", "c3'"]))
        self.assertEqual([c for c in dag.commits if c.endswith("*")], ["D*"])  # only the layer's own commit went onto p3

    def test_a_layer_with_nothing_of_its_own_becomes_the_layer_below(self):
        dag = Dag({"T": [], "c1": ["T"], "c1'": ["T"]}, {"main": "T", "p1": "c1", "d": "c1'"})
        linear, _ = self.linearize(dag, ["p1", "d"])
        self.assertEqual((linear, dag.rebases, dag.tips["d"]), (["p1", "d"], [{"b": "d", "reset": "p1"}], "c1"))

    def test_a_layer_with_no_copies_is_rebased_the_plain_way(self):
        dag = Dag({"T": [], "c1": ["T"], "d1": ["T"]}, {"main": "T", "p1": "c1", "d": "d1"})
        self.linearize(dag, ["p1", "d"])
        self.assertEqual([(r["onto"], r["upstream"], r["interactive"], r["drop"]) for r in dag.rebases], [("p1", "T", False, [])])

    def test_the_todo_editor_removes_only_the_picks_of_the_dropped_commits(self):
        todo = Path(tempfile.mkdtemp()) / "todo"
        todo.write_text("pick 1111111 Add the arm catalog\npick 2222222 the layer's own\npick 3333333 Another copy\n# Rebase\n")
        env = {**os.environ, "COMPOSE_DROP": "1111111aaaa 3333333bbbb"}
        subprocess.run([sys.executable, "-c", L.DROP_PICKS, str(todo)], env=env, check=True)
        self.assertEqual(todo.read_text(), "pick 2222222 the layer's own\n# Rebase\n")

    def test_a_stop_names_the_commit_and_says_which_copies_the_hand_fix_drops(self):
        dag = Dag({"T": [], "c1": ["T"], "c1'": ["T"], "D": ["c1'"]}, {"main": "T", "p1": "c1", "d": "D"}, conflicts={("d", "p1"): ["a.py"]})
        linear, said = self.linearize(dag, ["p1", "d"])
        self.assertEqual(linear, ["p1"])
        self.assertEqual(L.STACK_STOP, "d conflicts when rebased onto p1 in a.py (at abc1234 the layer's own change); the layers below it are kept. Resolve by hand: "
                                       "git checkout d && git rebase -i --onto p1 T (the commit it shares with p1) (drop c1', which p1 holds already), then re-run")
        self.assertEqual(said, ["stack  STOP  " + L.STACK_STOP])

    def test_a_layer_standing_on_one_this_run_builds_again_waits_for_it_and_is_rebased_onto_the_new_one(self):
        """#453: 002 is set aside for its redo; 003 carries its old commits and needs its code, so it is no stop, and the next pass moves it onto the new 002."""
        dag = Dag({"T": [], "A1": ["T"], "A2": ["A1"], "A3": ["A2"]}, {"main": "T", "b1": "A1", "b3": "A3"}, conflicts={("b3", "b1"): ["chain.py"]})
        replaced = [("b2", "A2")]
        linear, said = self.linearize(dag, ["b1", "b3"], replaced, rebuild=["b2"])
        self.assertEqual((linear, L.STACK_STOP, dag.tips["b3"]), (["b1"], "", "A3"))  # 003 stays as it was
        self.assertEqual(said, ["stack  wait  b3 stands on b2, which this run builds again; it is rebased onto the new layer then"])
        linear, _ = self.linearize(dag, ["b1", "b3"], replaced)  # without `rebuild` a conflict is a stop, as before
        self.assertEqual(linear, ["b1"])
        self.assertIn("b3 conflicts when rebased onto b1", L.STACK_STOP)
        dag.commits["N2"], dag.tips["b2"], dag.conflicts = ["A1"], "N2", {}  # the redo built 002 again, on 001
        linear, said = self.linearize(dag, ["b1", "b2", "b3"], replaced)
        self.assertEqual((linear, L.STACK_STOP, said), (["b1", "b2", "b3"], "", []))
        (r,) = [r for r in dag.rebases if r["b"] == "b3" and r["onto"] == "b2"]
        self.assertEqual(r["upstream"], "A2")  # only 003's own commit goes onto the new 002

    def test_a_layer_that_does_not_stand_on_a_rebuilt_one_still_stops_the_chain(self):
        dag = Dag({"T": [], "A1": ["T"], "X": ["T"]}, {"main": "T", "b1": "A1", "b4": "X"}, conflicts={("b4", "b1"): ["x.py"]})
        linear, _ = self.linearize(dag, ["b1", "b4"], [("b2", "A9")], rebuild=["b2"])
        self.assertEqual(linear, ["b1"])
        self.assertIn("b4 conflicts when rebased onto b1 in x.py", L.STACK_STOP)


class SetAside(unittest.TestCase):
    """replace_branch sets a task branch aside for its rebuild; restore_branch puts it back when the rebuild never started."""
    def sh(self, cmd, cwd=None, check=True, **kw):
        self.calls.append(cmd[1:])
        out = self.branches.get(cmd[-1], "") if cmd[1] == "rev-parse" else ""
        return subprocess.CompletedProcess(cmd, 0 if out or cmd[1] != "rev-parse" else 1, out + "\n", "")

    def setUp(self):
        self.calls, self.branches = [], {"fast/p/001": "abc"}
        for p in (mock.patch.object(fc.fc_core, "sh", self.sh), mock.patch.object(fc.fc_worktree, "task_prefix", return_value="fast/p/")):
            p.start(); self.addCleanup(p.stop)
        self.proj = Path("/x/p")

    def test_replace_branch_returns_the_old_tip_and_restore_branch_puts_it_back(self):
        self.assertEqual(fc.fc_worktree.replace_branch(Path("."), self.proj, "fast/p/001"), "abc")
        self.assertEqual(self.calls[1:], [["update-ref", "refs/compose/replaced/p/001/abc", "abc"], ["branch", "-D", "fast/p/001"]])
        self.calls.clear(); self.branches.clear()  # the branch is gone now
        self.assertTrue(fc.fc_layers.restore_branch(Path("."), self.proj, "fast/p/001", "abc"))
        self.assertEqual(self.calls[1:], [["branch", "fast/p/001", "abc"], ["update-ref", "-d", "refs/compose/replaced/p/001/abc"]])

    def test_restore_branch_leaves_a_branch_that_exists_again_and_a_task_that_had_none(self):
        self.assertFalse(fc.fc_layers.restore_branch(Path("."), self.proj, "fast/p/001", "abc"))  # rebuilt meanwhile
        self.assertFalse(fc.fc_layers.restore_branch(Path("."), self.proj, "fast/p/002", ""))  # never had a branch
        self.assertEqual([c for c in self.calls if c[0] in ("branch", "update-ref")], [])
        self.branches.clear()
        self.assertEqual(fc.fc_worktree.replace_branch(Path("."), self.proj, "fast/p/009"), "")


if __name__ == "__main__":
    unittest.main()
