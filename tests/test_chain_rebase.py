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



if __name__ == "__main__":
    unittest.main()
