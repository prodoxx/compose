import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)



def fake_git(case, have=(), merged=(), layers=(), trunk="compose/p"):
    """What the board asks git, answered from `case.have`, `case.merged` and `case.layers` (lists the test may change between reads);
    returns the three fakes, so a test can count the questions a board read asks."""
    case.have, case.merged, case.layers = list(have), list(merged), list(layers)
    asked = SimpleNamespace(head=mock.Mock(return_value="head"), facts=mock.Mock(), order=mock.Mock())
    asked.facts.side_effect = lambda repo, proj, head: {"prefix": "compose/p-", "have": set(case.have), "merged": set(case.merged)}
    asked.order.side_effect = lambda repo, proj, tasks=None: [b for b in case.have if b not in case.merged]
    for mod, name, fake in ((fc.fc_status, "project_head", asked.head), (fc.fc_status, "branch_facts", asked.facts), (fc.fc_core, "current_branch", lambda repo: trunk),
                            (fc.fc_worktree, "stack_order", asked.order), (fc.fc_worktree, "task_prefix", lambda repo, proj: "compose/p-"), (fc.fc_layers, "layer_branches", lambda repo, proj: list(case.layers))):
        patch = mock.patch.object(mod, name, fake); patch.start(); case.addCleanup(patch.stop)
    return asked


class Board(unittest.TestCase):
    """`status` and `watch` read the board every two seconds, from many loops at once: it must cost no process while nothing changed."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "verify.json").write_text(json.dumps({"task_prefix": "compose/p-"}))
        self.tasks(["001", "002", "003"])
        # 001: a branch merged into the project branch; 002: a branch not merged; 003: no branch yet
        self.asked = fake_git(self, have=["compose/p-001", "compose/p-002"], merged=["compose/p-001"])
        self.refs = self.repo / "refs" / "heads"; self.refs.mkdir(parents=True)  # the refs the stamp watches: a new branch writes one
        patch = mock.patch.object(fc.fc_status, "git_dirs", lambda repo: (self.repo, self.repo)); patch.start(); self.addCleanup(patch.stop)
        self.result("001", "passed"); self.result("002", "passed")

    def branch(self, name, merged=False):
        self.have.append(name)
        self.merged += [name] * merged
        (self.refs / name.replace("/", "-")).write_text("x")

    def tasks(self, ids):
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": i, "title": f"t{i}"} for i in ids]}))

    def result(self, tid, status, **kv):
        (self.proj / "results" / f"{tid}.json").write_text(json.dumps({"status": status, **kv}))

    def states(self, board):
        return {r["id"]: r["state"] for r in board["rows"]}

    def questions(self):
        """How many times a board read has asked git so far."""
        return self.asked.head.call_count + self.asked.facts.call_count + self.asked.order.call_count

    def test_the_board_tells_each_tasks_state_from_the_branches(self):
        b = fc.fc_status.board(self.repo, self.proj)
        self.assertEqual(self.states(b), {"001": "merged", "002": "passed", "003": "pending"})
        self.assertEqual([r["note"] for r in b["rows"]], ["", "compose/p-002", ""])
        self.assertEqual(b["trunk"], "compose/p")
        self.assertEqual(b["layers"], {"compose/p-002": 1})

    def test_the_board_costs_no_process_while_nothing_changed(self):
        first = fc.fc_status.board(self.repo, self.proj)
        n = self.questions()
        self.assertGreater(n, 0)
        self.assertEqual(fc.fc_status.board(self.repo, self.proj), first)
        self.assertEqual(self.questions(), n, "a second read with nothing changed ran git")

    def test_the_boards_git_cost_does_not_grow_with_the_task_count(self):
        fc.fc_status.board(self.repo, self.proj)
        few = self.questions()
        self.tasks([f"{i:03}" for i in range(1, 25)])
        fc.fc_status.board(self.repo, self.proj)
        self.assertLessEqual(self.questions() - few, few)

    def test_a_new_branch_changes_the_board(self):
        fc.fc_status.board(self.repo, self.proj)
        time.sleep(0.01)
        self.branch("compose/p-003")
        self.assertEqual(self.states(fc.fc_status.board(self.repo, self.proj))["003"], "pending")
        self.result("003", "passed")
        self.assertEqual(self.states(fc.fc_status.board(self.repo, self.proj))["003"], "passed")
        time.sleep(0.01)
        self.merged.append("compose/p-003"); (self.refs / "moved").write_text("x")
        self.assertEqual(self.states(fc.fc_status.board(self.repo, self.proj))["003"], "merged")

    def test_a_result_written_by_another_process_changes_the_board(self):
        fc.fc_status.board(self.repo, self.proj)
        time.sleep(0.01)
        self.result("003", "running", attempts=1)
        (self.proj / "results" / "run.json").write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-10-19 01:00:00"}))
        b = fc.fc_status.board(self.repo, self.proj)
        self.assertEqual(self.states(b)["003"], "running")
        self.assertEqual(b["rows"][2]["attempts"], 1)

    def test_a_held_task_changes_the_board(self):
        fc.fc_status.board(self.repo, self.proj)
        time.sleep(0.01)
        (self.proj / "results" / "hold.json").write_text(json.dumps({"003": {"reason": "plan changes", "at": "x"}}))
        self.assertEqual(self.states(fc.fc_status.board(self.repo, self.proj))["003"], "held")

    def test_a_run_that_died_shows_without_any_file_changing(self):
        self.result("003", "running")
        marker = self.proj / "results" / "run.json"
        marker.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-10-19 01:00:00"}))
        self.assertEqual(self.states(fc.fc_status.board(self.repo, self.proj))["003"], "running")
        with mock.patch.object(fc.fc_tasks, "pid_alive", return_value=False):
            self.assertEqual(self.states(fc.fc_status.board(self.repo, self.proj))["003"], "interrupted")

    def test_watch_snapshot_costs_no_process_while_nothing_changed(self):
        with mock.patch.object(fc.fc_herdr, "herdr", return_value=None):
            first = fc.fc_watch.watch_snapshot(self.repo, self.proj)
            n = self.questions()
            self.assertEqual(fc.fc_watch.watch_snapshot(self.repo, self.proj), first)
            self.assertEqual(self.questions(), n)
        self.assertEqual({t: v["state"] for t, v in first["tasks"].items()}, {"001": "merged", "002": "passed", "003": "pending"})

    def test_a_watch_loop_asks_herdr_for_its_agents_once_per_interval(self):
        clock, agents = [1000.0], fc.fc_watch.Agents()
        with mock.patch.object(fc.fc_herdr, "herdr", return_value={"agents": [{"name": "p-orchestrator", "agent_status": "idle"}]}) as h, \
                mock.patch.object(fc.fc_watch.time, "monotonic", lambda: clock[0]):
            for _ in range(3):
                snap = fc.fc_watch.watch_snapshot(self.repo, self.proj, agents)
            self.assertEqual(snap["agents"], {"p-orchestrator": "idle"})
            self.assertEqual(h.call_count, 1)
            clock[0] += fc.fc_watch.AGENTS_INTERVAL + 1
            fc.fc_watch.watch_snapshot(self.repo, self.proj, agents)
            self.assertEqual(h.call_count, 2)
            fc.fc_watch.watch_snapshot(self.repo, self.proj)  # a one-off read, as `watch --now` does, asks every time
            self.assertEqual(h.call_count, 3)


class ReadyList(unittest.TestCase):
    """`status` and `projects` show what can start now, read from results/ready.json."""

    ITEMS = [{"id": "NEB-1", "title": "A"}, {"id": "NEB-2", "title": "B"}]

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        fake_git(self)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "verify.json").write_text(json.dumps({"task_prefix": "compose/p-"}))
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": "001", "title": "t001"}]}))
        # ready_line's wording belongs to another unit; this stand-in shows which items reached it
        p = mock.patch.object(fc.fc_linear, "ready_line", side_effect=lambda items: "READY " + ", ".join(f"{i['id']} {i['title']}" for i in items))
        self.line = p.start(); self.addCleanup(p.stop)

    def ready(self, items):
        tasks, ready = self.proj / "results" / "tasks.json", self.proj / "results" / "ready.json"
        ready.write_text(json.dumps(items, indent=2))
        os.utime(ready, ns=(tasks.stat().st_atime_ns, tasks.stat().st_mtime_ns))  # keeps `projects`' last activity as it was

    def status(self):
        lines = fc.fc_status.render_status(self.repo, [self.proj]).splitlines()
        return lines, next(n for n, l in enumerate(lines) if l.startswith("project p "))

    def projects(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(fc.fc_core, "all_projects", return_value={"p": (self.repo, self.proj)}):
            fc.fc_status.cmd_projects(SimpleNamespace(repo=str(self.repo)))
        return next(l for l in out.getvalue().splitlines() if l.startswith("p "))

    def test_status_prints_the_ready_line_right_after_the_project_line(self):
        before, _ = self.status()
        for items in (self.ITEMS, []):
            self.ready(items)
            lines, i = self.status()
            self.assertEqual(lines[i + 1], "  " + fc.fc_linear.ready_line(items))
            self.assertEqual(lines[:i + 1] + lines[i + 2:], before)

    def test_projects_ends_the_project_line_with_the_ready_line(self):
        before = self.projects()
        for items in (self.ITEMS, []):
            self.ready(items)
            self.assertEqual(self.projects(), before + "  " + fc.fc_linear.ready_line(items))
            self.assertEqual(fc.fc_status.project_summary(self.repo, self.proj)["ready"], items)

    def test_without_ready_json_neither_prints_a_ready_line(self):
        (lines, i), line = self.status(), self.projects()
        self.assertTrue(lines[i + 1].startswith("  cost ") and line.endswith("]"), (lines, line))
        self.line.assert_not_called()


class RouteColumns(unittest.TestCase):
    """`status` shows each task's route, its fallback switches and the verify rounds that fixed it, aligned under the header."""

    HEADER = "  id   title                                  state            route                             att esc fix     time     cost  model          note"
    FALL = {"fallback": {"from": "a", "to": "b", "why": "x"}}

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        fake_git(self)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)

    def write(self, name, data):
        (self.proj / "results" / name).write_text(json.dumps(data))

    def table(self):
        """{task id: (route, att, esc, fix, cost)}, each cell cut at the header's own column."""
        lines = fc.fc_status.render_status(self.repo, [self.proj]).splitlines()
        self.assertIn(self.HEADER, lines)
        h, rows = self.HEADER, lines[lines.index(self.HEADER) + 1:]
        cut = lambda l, a, b: l[h.index(a):h.index(a) + b].strip()
        return {l[2:6].strip(): (l[h.index("route"):h.index("att")].strip(), cut(l, "att", 3), cut(l, "esc", 3), cut(l, "fix", 3), l[h.index("cost") - 4:h.index("cost") + 4].strip())
                for l in rows if l[2:5].isdigit()}

    def test_each_row_shows_its_route_escalations_and_fix_rounds_under_the_header(self):
        route = lambda body: {"description": "## Route\n\n" + body}
        self.write("tasks.json", {"feature": {}, "tasks": [
            {"id": "001", "title": "routed", "linear": "XX-1"}, {"id": "002", "title": "unrouted"},
            {"id": "003", "title": "a title that is far longer than the thirty-eight characters it gets", "linear": "XX-3"},
            {"id": "004", "title": "partly routed", "linear": "XX-4"}],
            "issues": {"XX-1": route("* workflow: test-first\n* depth: FAST\n"), "XX-3": route("* workflow: fix-then-regression-test\n* depth: STANDARD\n"),
                       "XX-4": route("* depth: DEEP\n")}})
        self.write("001.json", {"status": "passed", "attempts": 3, "cost": 0.42, "log": [{"attempt": 1}, self.FALL, {"attempt": 2}, self.FALL]})
        self.write("003.json", {"status": "passed", "log": [{"attempt": 1}]})
        self.write("verify.json", {"rounds": [{"fix_tasks": ["001", "002"]}, {"fix_tasks": ["001"]}, {}, {"fix_tasks": ["003"]}]})
        self.assertEqual(self.table(), {"001": ("FAST test-first", "3", "2", "2", "$0.4200"), "002": ("-", "0", "0", "1", "$0.0000"),
                                        "003": ("STANDARD fix-then-regression-test", "0", "0", "1", "$0.0000"), "004": ("DEEP -", "0", "0", "0", "$0.0000")})

    def test_status_needs_no_verify_json_and_no_sync_cache_and_reads_no_linear(self):
        self.write("tasks.json", {"feature": {}, "tasks": [{"id": "001", "title": "t", "linear": "XX-1"}]})
        self.write("001.json", {"status": "passed", "attempts": 1, "log": [self.FALL]})
        with mock.patch.object(fc.fc_linear, "linear_gql", side_effect=AssertionError("status read Linear")):
            self.assertEqual(self.table(), {"001": ("-", "1", "1", "0", "$0.0000")})


class RouteSummary(unittest.TestCase):
    """`status` sums attempts, escalations, fix rounds and cost per workflow and per depth, under the task table."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        fake_git(self)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "verify.json").write_text(json.dumps({"task_prefix": "compose/p-"}))
        import fc_routecost
        self.summary = fc_routecost.summary

    def rows(self, *specs):
        return [dict(zip(("workflow", "depth", "attempts", "escalations", "fix_rounds", "cost"), s)) for s in specs]

    def test_the_example_gives_the_header_and_a_line_per_value_that_has_rows(self):
        self.assertEqual(self.summary(self.rows(("test-first", "FAST", 1, 0, 0, 0.0123), ("test-first", "FAST", 3, 1, 2, 0.0456), ("-", "-", 0, 0, 0, 0.0))), [
            "  route cost                         tasks  att  esc  fix      cost",
            "  workflow test-first                    2    4    1    2   $0.0579",
            "  workflow -                             1    0    0    0   $0.0000",
            "  depth FAST                             2    4    1    2   $0.0579",
            "  depth -                                1    0    0    0   $0.0000"])
        self.assertEqual(self.summary([]), [])

    def test_lines_follow_the_declared_order_whatever_the_row_order_and_skip_values_without_rows(self):
        lines = self.summary(self.rows(("fix-then-regression-test", "DEEP", 1, 0, 1, 0.5), ("-", "STANDARD", 2, 1, 0, 0.25),
                                       ("test-first", "-", 3, 0, 0, 1.0), ("implement-then-test", "DEEP", 4, 2, 3, 0.125)))
        self.assertEqual([(l[2:36].strip(), l[36:].split()) for l in lines[1:]], [
            ("workflow test-first", ["1", "3", "0", "0", "$1.0000"]), ("workflow implement-then-test", ["1", "4", "2", "3", "$0.1250"]),
            ("workflow fix-then-regression-test", ["1", "1", "0", "1", "$0.5000"]), ("workflow -", ["1", "2", "1", "0", "$0.2500"]),
            ("depth STANDARD", ["1", "2", "1", "0", "$0.2500"]), ("depth DEEP", ["2", "5", "2", "4", "$0.6250"]), ("depth -", ["1", "3", "0", "0", "$1.0000"])])



class RouteRow(unittest.TestCase):
    """`task_state` adds the task's workflow, depth, escalations and fix rounds to its row."""

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)

    def write(self, name, data):
        (self.proj / "results" / name).write_text(json.dumps(data))

    def row(self, meta):
        facts = {"prefix": "compose/p-", "have": set(), "merged": set()}
        with mock.patch.object(fc.fc_linear, "linear_gql", side_effect=AssertionError("task_state read Linear")):
            r = fc.fc_status.task_state(self.proj.parent.parent, self.proj, meta, "", facts)
        return {k: r[k] for k in ("workflow", "depth", "escalations", "fix_rounds")}

    def test_a_routed_task_shows_its_route_its_fallback_switches_and_the_verify_rounds_that_fixed_it(self):
        fall = {"fallback": {"from": "a", "to": "b", "why": "x"}}
        self.write("tasks.json", {"feature": {}, "tasks": [], "issues": {"XX-1": {"description": "## Route\n\n* workflow: test-first\n* depth: FAST\n"}}})
        self.write("001.json", {"status": "passed", "log": [{"attempt": 1}, fall, {"attempt": 2}, fall]})
        self.write("verify.json", {"rounds": [{"fix_tasks": ["001", "002"]}, {"fix_tasks": ["002"]}, {}, {"fix_tasks": ["001"]}]})
        self.assertEqual(self.row({"id": "001", "linear": "XX-1"}), {"workflow": "test-first", "depth": "FAST", "escalations": 2, "fix_rounds": 2})

    def test_a_task_without_a_linear_key_or_a_route_section_has_no_route(self):
        self.write("tasks.json", {"feature": {}, "tasks": [], "issues": {"XX-2": {"description": "no section here"}}})
        none = {"workflow": "-", "depth": "-", "escalations": 0, "fix_rounds": 0}
        self.assertEqual(self.row({"id": "001"}), none)
        self.assertEqual(self.row({"id": "001", "linear": "XX-2"}), none)



class BoardOneListing(unittest.TestCase):
    """What `branch_facts` reads from git: the branches that start with the prefix, and those merged into the trunk."""

    def setUp(self):
        for d in (fc.fc_worktree._heads, fc.fc_worktree._prefixes, fc.fc_worktree._commons): d.clear()
        self.repo = Path(tempfile.mkdtemp())
        self.asked = []
        def sh(cmd, cwd=None, **k):
            self.asked.append(cmd)
            merged = "refs/heads/compose/p-001\n" if any(a.startswith("--merged") for a in cmd) else ""
            return SimpleNamespace(returncode=0, stdout=merged, stderr="")
        for mod, name, fake in ((fc.fc_core, "sh", sh), (fc.fc_worktree, "heads", lambda repo: ["main", "compose/p-001", "compose/p-002", "compose/q-001", "other/p-009"])):
            patch = mock.patch.object(mod, name, fake); patch.start(); self.addCleanup(patch.stop)

    def facts(self, name, trunk="trunk"):
        proj = self.repo / ".compose" / name
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "verify.json").write_text(json.dumps({"task_prefix": f"compose/{name}-"}))
        return fc.fc_status.branch_facts(self.repo, proj, trunk)

    def test_have_is_the_branches_that_start_with_the_prefix_and_merged_those_in_trunk(self):
        self.assertEqual(self.facts("p"), {"prefix": "compose/p-", "have": {"compose/p-001", "compose/p-002"}, "merged": {"compose/p-001"}})
        self.assertEqual(self.facts("p", trunk="")["merged"], set())


class WatchFilesStamp(unittest.TestCase):
    """A `watch` saves its memory between ticks: that must not make the cached board stale, while every other state change must."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.refs = self.repo / "refs" / "heads"; self.refs.mkdir(parents=True)  # a branch is a file here
        patch = mock.patch.object(fc.fc_status, "git_dirs", lambda repo: (self.repo, self.repo)); patch.start(); self.addCleanup(patch.stop)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "results" / "verify.json").write_text("{}")
        (self.proj / "layers.json").write_text("{}")

    def stamp(self):
        return fc.fc_status.board_stamp(self.repo, self.proj)

    def test_writing_a_watch_file_leaves_the_stamp_as_it_was(self):
        for name in ("watch-all.json", "watch-001.json"):
            before = self.stamp()
            (self.proj / "results" / name).write_text("{}")
            self.assertEqual(self.stamp(), before, f"new {name}")
            (self.proj / "results" / name).write_text('{"seen": ["a", "b"]}')
            self.assertEqual(self.stamp(), before, f"rewritten {name}")

    def test_rewriting_another_json_changes_the_stamp(self):
        for path in (self.proj / "results" / "verify.json", self.proj / "results" / "001.json", self.proj / "layers.json", self.proj / "watch-x.json"):
            before = self.stamp()
            path.write_text('{"changed": true}')
            self.assertNotEqual(self.stamp(), before, str(path.relative_to(self.proj)))

    def test_a_new_branch_changes_the_stamp_even_beside_watch_writes(self):
        before = self.stamp()
        (self.proj / "results" / "watch-all.json").write_text("{}")
        (self.refs / "compose-p-001").write_text("x")
        self.assertNotEqual(self.stamp(), before)


if __name__ == "__main__":
    unittest.main()
