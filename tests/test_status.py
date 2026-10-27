import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys, time
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_ledger, fc_tierspend, fc_load, fc_shed  # noqa: E402


def board_git(case, have=(), order=(), layers=(), trunk="main"):
    """What the board asks git, answered: the task branches that exist, the stack's task branches bottom first, the layer branches."""
    for name, fake in (("project_head", lambda repo, proj: "head"), ("branch_facts", lambda repo, proj, head: {"prefix": fc.fc_worktree.task_prefix(repo, proj), "have": set(have), "merged": set()})):
        patch = mock.patch.object(fc.fc_status, name, fake); patch.start(); case.addCleanup(patch.stop)
    for mod, name, fake in ((fc.fc_core, "current_branch", lambda repo: trunk), (fc.fc_worktree, "stack_order", lambda repo, proj, tasks=None: list(order)),
                            (fc.fc_layers, "layer_branches", lambda repo, proj: list(layers))):
        patch = mock.patch.object(mod, name, fake); patch.start(); case.addCleanup(patch.stop)


class DoneEvidence(unittest.TestCase):
    def setUp(self):
        self.proj, self.repo = Path(tempfile.mkdtemp()) / "feat", Path(tempfile.mkdtemp())
        (self.proj / "results").mkdir(parents=True)
        self.tip = "9f2c1e7"
        sh = lambda cmd, cwd=None, **k: SimpleNamespace(returncode=0, stderr="", stdout=self.tip if cmd[-1] == "task-021^{commit}" else "")
        patch = mock.patch.object(fc.fc_core, "sh", sh); patch.start(); self.addCleanup(patch.stop)
        (self.proj / "verify.json").write_text('{"task_prefix": "task-"}')
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [{"id": "021", "title": "a", "linear": "ENG-4"}, {"id": "022", "title": "b", "linear": "ENG-5"}, {"id": "023", "title": "c"}]}))
        self.calls = mock.Mock()
        for mod, name, kw in ((fc.fc_core, "project_dir", {"return_value": self.proj}), (fc_ledger, "run_id", {"side_effect": lambda proj, tid: "run-" + tid}),
                              (fc_ledger, "criterion_ids", {"side_effect": lambda meta: [meta["id"] + ".1:x"]}), (fc_ledger, "event", {"side_effect": lambda *a: a}),
                              (fc_ledger, "record", {"return_value": True}), (fc_ledger, "record_task", {"return_value": True}), (fc.fc_linear, "linear_event", {})):
            p = mock.patch.object(mod, name, **kw); self.calls.attach_mock(p.start(), name); self.addCleanup(p.stop)

    def done(self, only):
        out = io.StringIO()
        with redirect_stdout(out):
            fc.fc_status.cmd_done(SimpleNamespace(repo=str(self.repo), only=only, note="by hand"))
        return out.getvalue()

    def recorded(self):
        """(issue, events) per write, in call order: `record(issue, events)` or `record_task(repo, proj, meta, events)`."""
        return [c[1] if c[0] == "record" else (c[1][2].get("linear"), c[1][3]) for c in self.calls.mock_calls if c[0] in ("record", "record_task")]

    def test_each_task_records_one_done_event_on_its_own_issue(self):
        (self.proj / "results" / "021.json").write_text('{"branch": "task-021"}')
        self.done("021,022,023")
        done = lambda tid, sha: [(f"run-{tid}", sha, [tid], [f"{tid}.1:x"], "done", "done", "pass", None, "hand done: by hand")]
        self.assertEqual(self.recorded(), [("ENG-4", done("021", self.tip)), ("ENG-5", done("022", "")), (None, done("023", ""))])

    def test_a_result_without_a_worktree_fetches_nothing(self):
        """A design task's result has a figma: route for a branch and no worktree: the empty worktree read as the cwd, a git
        checkout, and `git fetch . figma:KEY:figma:KEY` failed with an invalid refspec."""
        (self.proj / "results" / "021.json").write_text('{"branch": "figma:wUa6g7vNP1il73eflFGXU0"}')
        with mock.patch.object(fc.fc_core, "sh", wraps=fc.fc_core.sh) as sh:
            self.assertNotIn("not brought in", self.done("021"))
        self.assertEqual([c for c in sh.call_args_list if "fetch" in c.args[0]], [])

    def test_the_write_is_recorded_once_beside_the_hand_done_linear_event(self):
        self.done("021")
        names = [c[0] for c in self.calls.mock_calls if c[0] in ("linear_event", "record", "record_task")]
        self.assertIn(names, (["linear_event", "record"], ["record_task", "linear_event"]))

    def test_unknown_id_records_nothing_and_writes_no_result(self):
        out = self.done("999")
        self.assertEqual((out, self.recorded(), list((self.proj / "results").glob("999.json"))), ("999: no such task (run `compose sync` if it is new on Linear)\n", [], []))


class TaskState(unittest.TestCase):
    def state(self, status, have, merged):
        proj = Path(tempfile.mkdtemp()) / "feat"
        (proj / "results").mkdir(parents=True)
        (proj / "results" / "021.json").write_text(json.dumps({"status": status}))
        facts = {"prefix": "task-", "have": have, "merged": merged}
        return fc.fc_status.task_state(proj, proj, {"id": "021", "title": "a"}, "", facts)["state"]

    def test_a_passed_task_is_merged_with_its_branch_in_the_trunk_or_without_a_branch_and_an_already_green_one_stays_so(self):
        self.assertEqual([self.state("passed", {"task-021"}, {"task-021"}), self.state("passed", set(), set()), self.state("passed", {"task-021"}, set())], ["merged", "merged", "passed"])
        self.assertEqual([self.state("already_green", {"task-021"}, {"task-021"}), self.state("already_green", set(), set())], ["already_green"] * 2)


    def test_a_paused_task_shows_its_reason_and_the_work_it_kept(self):
        def row(**extra):
            proj = Path(tempfile.mkdtemp()) / "feat"
            (proj / "results").mkdir(parents=True)
            paused = {"files": ["a.txt", "b.txt"], "reason": "D2 changed", "ref": "refs/compose/p/paused/001", **extra}
            (proj / "results" / "021.json").write_text(json.dumps({"status": "paused", "paused": paused}))
            return fc.fc_status.task_state(proj, proj, {"id": "021", "title": "a"}, "", {"prefix": "task-", "have": set(), "merged": set()})
        self.assertEqual((row()["state"], row()["note"]), ("paused", "D2 changed; work kept (2 files)"))
        self.assertEqual(row(choice="part", keep=["a.txt"])["note"], "D2 changed; work kept (2 files); part kept (a.txt)")


class TestTiers(unittest.TestCase):
    """`status` prints the tier pins and the spend per tier right after the route cost lines."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        board_git(self, trunk="compose/p")
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "verify.json").write_text(json.dumps({"task_prefix": "compose/p-"}))
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": "001", "title": "a"}]}))

    def status(self):
        return fc.fc_status.render_status(self.repo, [self.proj]).split("\n")

    def test_pins_then_spend_close_the_project_block_after_the_route_cost_lines(self):
        (self.proj / "results" / "tier.json").write_text(json.dumps({"project": "no-frontier", "stories": {"NEB-9": "cheap"}}))
        with mock.patch.object(fc.fc_budget, "limit", return_value=40.0), mock.patch.object(fc_tierspend, "lines", return_value=["  spend header", "  spend row"]) as spend:
            lines = self.status()
        self.assertEqual(lines[-4:], ["  tier pins  project no-frontier, NEB-9 cheap", "  spend header", "  spend row", ""])
        self.assertTrue(lines[-5].startswith("  depth "), lines)
        self.assertTrue(any(l.startswith("  route cost ") for l in lines[:-5]), lines)
        spend.assert_called_once_with(self.proj, 40.0)

    def test_a_frontier_result_shows_the_header_and_the_default_limit(self):
        (self.proj / "results" / "001.json").write_text(json.dumps({"id": "001", "status": "passed", "model": "claude:claude-opus-5-5", "cost": 0.0, "reference_cost": 0.5}))
        lines = self.status()
        header = f"  {'tier spend':12} {'real $':>9} {'ref $':>9}"
        self.assertIn(header, lines)
        at = lines.index(header)
        frontier = [l for l in lines[at:] if l.startswith("  frontier")]
        self.assertEqual(len(frontier), 1, lines)
        self.assertTrue(frontier[0].endswith("  limit 25.00"), frontier)
        self.assertFalse(any("tier pins" in l for l in lines), lines)

class StackNote(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.repo / ".compose" / "p"; (self.proj / "results").mkdir(parents=True)
        self.layers = [f"compose/p-layer-{n}" for n in ("01", "02", "03")]
        board_git(self, have=[f"compose/p-{t}" for t in ("001", "002", "003")], order=[f"compose/p-{t}" for t in ("001", "002", "003")], layers=self.layers, trunk="compose/p")
        (self.proj / "verify.json").write_text(json.dumps({"review_base": "main", "task_prefix": "compose/p-"}))
        (self.proj / "results" / "verify.json").write_text(json.dumps({"passed": True}))
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": t, "title": f"Build {t}", "after": ""} for t in ("001", "002", "003")]}))
        for t in ("001", "002", "003"):
            (self.proj / "results" / f"{t}.json").write_text(json.dumps({"status": "passed", "model": "pi:p/m"}))
        (self.proj / "layers.json").write_text(json.dumps({"compose/p-layer-01": ["groundwork"], "compose/p-layer-02": ["001", "002"], "compose/p-layer-03": ["003"]}))
        (self.proj / "results" / "prs.json").write_text(json.dumps({"compose/p-layer-01": 1, "compose/p-layer-02": 2}))

    def render(self):
        lines = fc.fc_status.render_status(self.repo, [self.proj]).splitlines()
        return {l.split()[0]: l for l in lines if l.startswith("  00")}, lines[0]

    def test_submitted_layers_show_their_pr_and_the_project_line_counts_them(self):
        rows, head = self.render()
        self.assertTrue(rows["001"].endswith("compose/p-001  layer 2  PR #2") and rows["002"].endswith("compose/p-002  layer 2  PR #2"))
        self.assertTrue(rows["003"].endswith("compose/p-003  layer 3"))
        self.assertRegex(head, r"verify PASS .* at \d\d:\d\d  stack 2/3 submitted")

    def test_a_submitted_task_without_a_branch_keeps_its_layer_and_pr(self):
        fc.fc_status._board.clear()
        board_git(self, have=["compose/p-001", "compose/p-003"], order=["compose/p-001", "compose/p-003"], layers=self.layers, trunk="compose/p")
        self.assertTrue(self.render()[0]["002"].endswith("layer 2  PR #2"))

    def test_missing_records_leave_the_notes_and_no_layer_branches_leave_no_stack_text(self):
        for gone, stack in (("prs.json", "stack 0/3 submitted"), ("layers.json", "stack 2/3 submitted")):
            for p in (self.proj / gone, self.proj / "results" / gone):
                if p.exists(): p.rename(p.with_suffix(".bak"))
            rows, head = self.render()
            self.assertTrue(rows["001"].endswith("compose/p-001  layer 1") and rows["003"].endswith("compose/p-003  layer 3") and stack in head, gone)
            for p in list(self.proj.glob("*.bak")) + list((self.proj / "results").glob("*.bak")): p.rename(p.with_suffix(".json"))
        self.layers.clear()
        self.assertNotIn("stack", self.render()[1])


class NoWatchFlag(unittest.TestCase):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["compose", *argv]), redirect_stdout(out), mock.patch.object(sys, "stderr", err):
            try:
                fc.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def board(self, *argv):
        projects = {"p": (Path("/wt"), Path("/wt/.compose/p"))}
        with mock.patch.object(fc.fc_core, "all_projects", return_value=projects), mock.patch.object(fc.fc_status, "render_status", return_value="BOARD\n") as render, \
             mock.patch.object(fc.fc_status, "digest", return_value="DIGEST") as digest, mock.patch.object(fc.fc_history, "read", return_value=["h"]), \
             mock.patch.object(fc.fc_history, "render", return_value="HISTORY"), mock.patch("time.sleep", side_effect=AssertionError("redraw loop")):
            return *self.main("status", "--repo", "/wt", *argv), render.call_count, digest.call_count

    def test_the_watch_flag_is_rejected_by_argparse_before_anything_runs(self):
        with mock.patch.object(fc.fc_status, "cmd_status") as status:
            code, out, err = self.main("status", "--watch")
        self.assertEqual((code, out, status.call_count), (2, "", 0))
        self.assertIn("unrecognized arguments: --watch", err)

class StatusLoadLine(unittest.TestCase):
    def lines(self, held=False, since=None, log=(), avg=13.26):
        state = {"held": held, "since": since, "n": len(log), "log": list(log), "avg": avg, "new": []}
        with mock.patch.object(fc_load, "update", return_value=state) as update, mock.patch("os.cpu_count", return_value=8):
            return fc.fc_status.load_lines(), update.call_count

    def released(self, ago, *older):
        return [*older, {"n": 2, "at": time.time() - ago, "kind": "released", "avg": 7.0}]

    def clock(self, ago):
        return time.strftime("%H:%M", time.localtime(time.time() - ago))

    def test_held_gives_the_held_line_with_its_since_time(self):
        lines, calls = self.lines(held=True, since=time.time() - 3600, log=[{"n": 1, "at": time.time() - 3600, "kind": "held", "avg": 13.0}])
        self.assertEqual(calls, 1)
        self.assertRegex(lines[0], r"^load: 13\.3 on 8 cores, held since %s: nothing new starts until it falls below \d+\.\d$" % self.clock(3600))
        self.assertEqual(len(lines), 1)

    def test_a_release_under_ten_minutes_ago_gives_the_released_line_and_older_gives_none(self):
        lines, _ = self.lines(log=self.released(540))
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^load: 13\.3 on 8 cores, released at %s \(below \d+\.\d\)$" % self.clock(540))
        self.assertEqual(self.lines(log=self.released(660))[0], [])

    def test_a_hold_after_a_recent_release_gives_only_the_held_line_and_the_empty_state_none(self):
        lines, _ = self.lines(held=True, since=time.time() - 60, log=self.released(120))
        self.assertEqual((len(lines), "held since" in lines[0]), (1, True))
        self.assertEqual(self.lines()[0], [])

    def status(self, *flags):
        a = SimpleNamespace(repo="/wt", project=None, digest="--digest" in flags, history=0 if "--history" in flags else None, watch=False)
        state = {"held": True, "since": time.time(), "n": 1, "log": [], "avg": 13.0, "new": []}
        projects = {"p": (Path("/wt"), Path("/wt/.compose/p"))}
        out = io.StringIO()
        with mock.patch.object(fc_load, "update", return_value=state), mock.patch("os.cpu_count", return_value=8), mock.patch.object(fc.fc_core, "all_projects", return_value=projects), \
             mock.patch.object(fc.fc_status, "render_status", return_value="BOARD\n"), mock.patch.object(fc.fc_status, "digest", return_value="DIGEST"), \
             mock.patch.object(fc.fc_history, "read", return_value=[]), mock.patch.object(fc.fc_history, "render", return_value="HISTORY"), redirect_stdout(out):
            fc.fc_status.cmd_status(a)
        return out.getvalue()

    def test_plain_status_prints_the_lines_once_before_the_projects_and_digest_and_history_do_not(self):
        out = self.status()
        self.assertTrue(out.startswith("load: 13.0 on 8 cores, held since ") and out.endswith("\nBOARD\n"), out)
        self.assertEqual(out.count("load:"), 1)
        self.assertEqual((self.status("--digest"), self.status("--history")), ("DIGEST\n", "HISTORY\n"))


class StatusLoadWarn(unittest.TestCase):
    def lines(self, held=False, **extra):
        state = {"held": held, "since": time.time() - 60 if held else None, "n": 3, "avg": 11.26, "new": [], **extra}
        with mock.patch.object(fc_load, "update", return_value=state), mock.patch("os.cpu_count", return_value=8):
            return fc.fc_status.load_lines()

    def log(self, *kinds):
        return [{"n": i + 1, "at": time.time() - age, "kind": kind, "avg": 11.0} for i, (kind, age) in enumerate(kinds)]

    def clock(self, ago):
        return time.strftime("%H:%M", time.localtime(time.time() - ago))

    def test_warned_and_not_held_gives_the_warn_line_with_the_last_warn_entrys_time(self):
        lines = self.lines(warn=True, log=self.log(("warn", 7200), ("cleared", 5400), ("warn", 3600)))
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^load: 11\.3 on 8 cores is above the warn mark \d+\.\d since %s: a hold starts above \d+\.\d$" % self.clock(3600))

    def test_held_shows_only_the_held_line_even_when_warned(self):
        lines = self.lines(held=True, warn=True, log=self.log(("warn", 600), ("held", 60)))
        self.assertEqual((len(lines), "held since" in lines[0], "warn mark" in lines[0]), (1, True, False))

    def test_cleared_or_a_state_without_warn_gives_no_warn_line(self):
        self.assertEqual(self.lines(warn=False, log=self.log(("warn", 900), ("cleared", 60))), [])
        self.assertEqual(self.lines(log=[]), [])


class StatusShedLine(unittest.TestCase):
    HOT = {"pid": 11, "cpu": 54.6, "command": "node"}

    def lines(self, hold=None, freed=(), hot=None, held=False):
        load = {"held": held, "since": time.time() if held else None, "n": 1, "log": [], "avg": 13.0, "new": []}
        shed = {"hold": hold, "freed": list(freed), "hot": hot, "paused": []}
        with mock.patch.object(fc_load, "update", return_value=load), mock.patch.object(fc_shed, "state", return_value=shed), mock.patch("os.cpu_count", return_value=8):
            return fc.fc_status.load_lines()

    def freed(self, *ats):
        return [{"pid": 100 + i, "kind": "agent", "project": "myproj", "why": "waiter gone", "at": at} for i, at in enumerate(ats)]

    def test_a_hold_counts_the_freed_entries_at_or_after_its_time_and_gives_the_hot_line(self):
        t = time.time() - 3600
        lines = self.lines({"n": 1, "at": t}, self.freed(t - 60, t, t + 10), self.HOT)
        self.assertEqual(lines, ["shed: freed 2 since " + time.strftime("%H:%M", time.localtime(t)), "shed: hottest not started by compose: node (pid 11, 55% cpu), left alone"])

    def test_the_hot_line_shows_alone_and_no_freed_since_the_hold_gives_no_freed_line(self):
        t = time.time() - 600
        self.assertEqual(self.lines({"n": 1, "at": t}, hot=self.HOT), ["shed: hottest not started by compose: node (pid 11, 55% cpu), left alone"])
        self.assertEqual(self.lines({"n": 1, "at": t}, self.freed(t - 1)), [])

    def test_no_hold_gives_no_shed_lines_whatever_else_is_in_the_state(self):
        self.assertEqual(self.lines(None, self.freed(time.time() - 60), self.HOT), [])

class StatusPausedLine(unittest.TestCase):
    def lines(self, paused, hold=None, hot=None, freed=()):
        load = {"held": False, "since": None, "n": 1, "log": [], "avg": 3.0, "new": []}
        shed = {"hold": hold, "freed": list(freed), "hot": hot, "paused": paused}
        with mock.patch.object(fc_load, "update", return_value=load), mock.patch.object(fc_shed, "state", return_value=shed), mock.patch("os.cpu_count", return_value=8):
            return fc.fc_status.load_lines()

    def paused(self, *specs):
        return [{"pid": pid, "kind": kind, "project": project, "at": 1000.0 + i} for i, (pid, kind, project) in enumerate(specs)]

    def test_paused_agents_give_one_line_in_the_order_given(self):
        lines = self.lines(self.paused((41, "implementer", "myproj"), (52, "teammate", "other"), (63, "implementer", "-")))
        self.assertEqual(lines, ["shed: paused 3: implementer 41 (myproj), teammate 52 (other), implementer 63 (-)"])
