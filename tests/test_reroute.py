import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_reroute  # noqa: E402  (nothing imports it into `fc` yet; compose.py put bin/ on sys.path)
import fc_depth, fc_evidence, fc_ledger, fc_policy, fc_route, fc_routectx, fc_routerec  # noqa: E402  (the modules fc_reroute holds: a later test file swaps sys.modules)

FILES = ["src/keep.py", "src/new.py", "src/gone.py", "src/a.py", "src/b.py"]
SIGNALS = {"changed": [{"path": "docs/note.md", "added": 1, "deleted": 0}, {"path": "src/greet.py", "added": 1, "deleted": 0}], "lines": 2, "outside": ["docs/note.md"], "untouched": []}


def setUpModule():
    patch = mock.patch.object(fc.fc_worktree, "task_prefix", lambda repo, proj: "t/")  # the one git fact every layer name needs
    patch.start(); unittest.addModuleCleanup(patch.stop)


def git_says(out):
    """An `sh` fake that answers every call with `out` on stdout."""
    return lambda cmd, *a, **kw: SimpleNamespace(stdout=out, stderr="", returncode=0)


NUMSTAT = "-\t-\tassets/logo.png\n1\t0\tdocs/outside.txt\n2\t1\tsrc/a.py\n0\t2\tsrc/gone.py\n3\t0\tsrc/new.py\n"


class Signals(unittest.TestCase):
    def signals(self, files):
        with mock.patch.object(fc.fc_core, "sh", git_says(NUMSTAT)):
            return fc_reroute.diff_signals(Path("repo"), {"files": files}, "main", "task")

    def test_changed_lines_outside_and_untouched_come_from_the_three_dot_diff(self):
        got = self.signals(FILES)
        self.assertEqual(set(got), {"changed", "lines", "outside", "untouched"})
        self.assertEqual(got["changed"], [{"path": "assets/logo.png", "added": 0, "deleted": 0}, {"path": "docs/outside.txt", "added": 1, "deleted": 0},
                                          {"path": "src/a.py", "added": 2, "deleted": 1}, {"path": "src/gone.py", "added": 0, "deleted": 2},
                                          {"path": "src/new.py", "added": 3, "deleted": 0}])
        self.assertEqual(got["lines"], 9)
        self.assertEqual(got["outside"], ["assets/logo.png", "docs/outside.txt"])
        self.assertEqual(got["untouched"], ["src/keep.py", "src/b.py"])

    def test_without_files_every_changed_path_is_outside(self):
        got = self.signals([])
        self.assertEqual(got["outside"], [c["path"] for c in got["changed"]])
        self.assertEqual((len(got["outside"]), got["untouched"]), (5, []))

class LayerBase(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        # stack, bottom to top: 005, 010, 011, 007 (007 is after 011); 020 has a branch but is no synced task, 030 has no branch
        tasks = [{"id": t, "after": after} for t, after in (("005", ""), ("007", "011"), ("010", ""), ("011", "010"), ("030", ""))]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        self.unmerged = "\n".join(self.branch(t) for t in ("005", "007", "010", "011", "020") ) + "\nt/tests-005"

    def branch(self, tid):
        return fc.fc_worktree.task_branch(self.repo, self.proj, tid)

    def base(self, tid):
        with mock.patch.object(fc.fc_core, "sh", git_says(self.unmerged)):
            return fc_reroute.layer_base(self.repo, self.proj, tid, "trunk")

    def test_a_task_gets_the_branch_right_below_it_in_the_stack_order(self):
        self.assertEqual([self.base(t) for t in ("010", "011", "007")], [self.branch(t) for t in ("005", "010", "011")])

    def test_the_bottom_layer_gets_the_trunk(self):
        self.assertEqual(self.base("005"), "trunk")

    def test_the_results_file_and_its_base_are_not_read(self):
        (self.proj / "results" / "011.json").write_text(json.dumps({"status": "passed", "base": self.branch("005")}))
        (self.proj / "results" / "005.json").write_text("not json")
        self.assertEqual((self.base("011"), self.base("005")), (self.branch("010"), "trunk"))


def _hit(path, *hits):
    return {"symbol": {"path": path}, "hits": [{"path": h} for h in hits]}


DIFF = """--- a/src/a.py
+++ b/src/a.py
@@ -2 +2 @@
@@ -8,0 +9 @@
@@ -11,2 +10,0 @@
--- a/src/c.py
+++ b/src/c.py
@@ -1 +1 @@
--- a/src/gone.py
+++ /dev/null
@@ -1 +0,0 @@
--- a/src/n.py
+++ b/src/n.py
@@ -3 +3 @@
"""


class Callers(unittest.TestCase):
    SPANS = {"src/a.py": {"f1": "L1-L3", "f2": "L4-L6", "f3": "L7-L8", "f4": "L9-L10", "f5": "L12-L12"}, "src/c.py": {"g": "L1-L1"}, "src/n.py": {"h": "L1-L1"}, "src/gone.py": {"x": "L1-L1"}}
    CALLERS = {"f1": [_hit("src/a.py", "src/z.py", "src/peer.py", "src/a.py", "src/z.py"), _hit("src/other.py", "src/intruder.py")], "f3": [_hit("src/a.py", "src/y.py", "src/z.py")],
               "g": [_hit("src/c.py", *[f"c/n{i % 25:02}.py" for i in range(50, 0, -1)])]}

    def setUp(self):
        self.asked = []
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        for f in ("src/a.py", "src/c.py", "src/n.py"): (self.repo / f).parent.mkdir(exist_ok=True); (self.repo / f).write_text("x\n")

    def sh(self, cmd, cwd=None, *a, **kw):
        if cmd[0] == "git": return SimpleNamespace(stdout=DIFF, stderr="", returncode=0)
        self.asked.append(cmd[2])
        body = {"entries": [{"name": n, "span": s} for n, s in self.SPANS[cmd[2]].items()]} if cmd[1] == "skeleton" else {"matches": self.CALLERS.get(cmd[2], [])}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")

    def test_callers_of_the_symbols_whose_span_meets_an_old_side_range(self):
        with mock.patch.object(fc.fc_core, "sh", self.sh):
            got = fc_reroute.diff_callers(self.repo, "main", "task", {"files": ["src/a.py", "src/c.py", "src/peer.py"]})
        self.assertEqual(got, {"callers": {"src/a.py": ["src/y.py", "src/z.py"], "src/c.py": [f"c/n{i:02}.py" for i in range(20)], "src/n.py": []}})
        self.assertEqual(sorted(a for a in self.asked if not a.startswith("src/")), ["f1", "f3", "f5", "g"])
        self.assertNotIn("src/gone.py", self.asked)


class Unavailable(unittest.TestCase):
    BAD = ["not json", "{}", "[]", '{"entries": [{"name": "f"}]}', '{"matches": [{"hits": []}]}']

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        for n in "ac": (self.repo / f"src/{n}.py").parent.mkdir(exist_ok=True); (self.repo / f"src/{n}.py").write_text("1\n2\n3\n")

    def run_callers(self, fail=None, how=None):
        """diff_callers with trail faked; call number `fail` (0-based) breaks as `how`. Returns (result, trail calls made)."""
        calls = []
        def sh(cmd, cwd=None, *a, **kw):
            if cmd[0] != "trail": return SimpleNamespace(stdout=DIFF, stderr="", returncode=0)
            calls.append(cmd)
            body = json.dumps({"entries": [{"name": "f", "span": "L1-L3"}]} if cmd[1] == "skeleton" else {"matches": [{"symbol": {"path": "src/a.py"}, "hits": [{"path": "z.py"}]}]})
            if len(calls) - 1 != fail: return subprocess.CompletedProcess(cmd, 0, body, "")
            if how == "missing": raise FileNotFoundError("trail")
            return subprocess.CompletedProcess(cmd, how, body, "") if isinstance(how, int) else subprocess.CompletedProcess(cmd, 0, how, "")
        with mock.patch.object(fc.fc_core, "sh", sh):
            return fc_reroute.diff_callers(self.repo, "main", "task", {"files": ["src/a.py", "src/c.py"]}), calls

    def test_the_first_failing_call_gives_its_why_and_stops_the_calls(self):
        ok, calls = self.run_callers()
        self.assertEqual(list(ok), ["callers"])
        self.assertGreater(len(calls), 1)
        for n in range(len(calls)):
            for how, why in [("missing", "trail not installed"), (1, "trail exited 1"), (127, "trail exited 127")] + [(b, "trail gave unreadable output") for b in self.BAD]:
                with self.subTest(call=n, how=how):
                    got, made = self.run_callers(n, how)
                    self.assertEqual(got, {"unavailable": why})
                    self.assertEqual(len(made), n + 1)


class Fired(unittest.TestCase):
    QUERY = "query($id: String!, $after: String) { issue(id: $id) { comments(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { body } } } }"

    @staticmethod
    def page(*bodies, cursor=None):
        return {"issue": {"comments": {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": [{"body": b} for b in bodies]}}}

    def fired(self, *pages):
        with mock.patch.object(fc.fc_linear, "linear_gql", side_effect=list(pages)) as gql:
            return fc_reroute.fired("XX-2"), gql.call_args_list

    def test_every_page_is_read_with_the_previous_cursor_and_each_trigger_gives_a_pair(self):
        got, calls = self.fired(self.page("* rerouted: diff (run aaaaaaaaaaaa)", cursor="c1"), self.page("## Route\n\n* rerouted: diff, scope (run bbbbbbbbbbbb)", cursor="c2"), self.page())
        self.assertEqual(got, {("diff", "aaaaaaaaaaaa"), ("diff", "bbbbbbbbbbbb"), ("scope", "bbbbbbbbbbbb")})
        self.assertEqual([c.args for c in calls], [(self.QUERY, {"id": "XX-2", "after": a}) for a in (None, "c1", "c2")])

    def test_dash_and_star_bullets_count_on_any_stripped_line_and_other_lines_give_none(self):
        got, calls = self.fired(self.page("intro\n  - rerouted: scope (run r1)  \n\t* rerouted: diff (run r2)\n- rerouted: diff (run r2)", "no bullet\nrerouted: diff (run r3)\n* rerouted: diff\n* routed: diff (run r4)\n* rerouted: diff (run r5) extra", ""))
        self.assertEqual((got, len(calls)), ({("scope", "r1"), ("diff", "r2")}, 1))

    def test_a_null_issue_raises_and_a_linear_error_propagates(self):
        with self.assertRaisesRegex(RuntimeError, "no issue XX-2"):
            self.fired({"issue": None})
        with self.assertRaisesRegex(RuntimeError, "Linear: boom"):
            self.fired(self.page("* rerouted: diff (run r1)", cursor="c1"), RuntimeError("Linear: boom"))


class JevState(unittest.TestCase):
    ROUTE = "## Route\n\n* workflow: implement-then-test\n* depth: STANDARD\n* verification requirements: make lint, make e2e\n\n"
    CALLERS = {"callers": {"src/greet.py": ["src/app.py"]}}
    ANS = {"depth": {"choice": "FAST", "probabilities": {"FAST": 0.9}, "confidence": 0.9}, "policy0": {"choice": "DOES_NOT_APPLY", "probabilities": {"DOES_NOT_APPLY": 0.9}, "confidence": 0.9}}
    DOCS = {"name": "Docs", "statement": "Docs stay true.", "globs": ["docs/*"], "checks": ["make docs"]}
    PLAIN = {"name": "Keys", "statement": "Keys are checked.", "globs": [], "checks": ["make keys"]}

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')

    def reassess(self, route=ROUTE, jev=None, policies=()):
        """Reassess the synced task whose recorded route is `route`; Jev replies `jev` (raised when an exception), `policies` is what read_policies gives."""
        desc = f"## Goal\nGreet.\n\n## Acceptance criteria\n* greets\n\n{route}```compose\nid: 001\ntest: t\nfiles: src/greet.py\n```\n"
        tasks = fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1"}, "issues": [{"id": "ENG-2", "title": "Greet", "description": desc, "status": "Todo"}]})
        kv = lambda v, d: {"side_effect" if isinstance(v, Exception) else "return_value": v or d}
        with mock.patch.object(fc.fc_models, "jev_ask", **kv(jev, {"model": "jev-test", "answers": self.ANS})) as self.ask, \
                mock.patch.object(fc_policy, "read_policies", **kv(policies, ([], ""))), mock.patch.object(fc_reroute, "diff_callers", return_value=self.CALLERS), \
                mock.patch.object(fc_route, "record_route"), mock.patch.object(fc_reroute, "diff_signals", return_value=SIGNALS), mock.patch.object(fc_routectx, "context_hash", return_value="h"):
            return fc_reroute.reassess(self.repo, self.proj, tasks[0], tasks, ("diff", "scope"), "r1", "main", "task")

    def test_jev_sees_the_diff_callers_recorded_route_and_triggers_and_is_not_asked_for_the_workflow(self):
        route = self.reassess(policies=([self.DOCS, self.PLAIN], ""))
        state, questions = self.ask.call_args.args[2:4]
        self.assertEqual(state["diff"], {**SIGNALS, **self.CALLERS})
        self.assertEqual(state["recorded"], {"workflow": "implement-then-test", "depth": "STANDARD", "checks": ["make lint", "make e2e"]})
        self.assertEqual((state["triggers"], state["pin"], state["task"]), (["diff", "scope"], "implement-then-test", "Greet"))
        self.assertEqual(("workflow" in questions, "Keys are checked." in json.dumps(questions), "Docs stay true." in json.dumps(questions)), (False, True, False))
        self.assertEqual((self.ask.call_count, route["workflow"]), (1, "implement-then-test"))

    def test_a_policy_matching_a_changed_path_outside_files_floors_deep_with_its_checks(self):
        fast = "## Route\n\n* workflow: implement-then-test\n* depth: FAST\n* verification requirements: none\n\n"  # no recorded floor under the policy's
        route = self.reassess(fast, policies=([self.DOCS, self.PLAIN, {**self.DOCS, "name": "Bin", "globs": ["bin/*"], "checks": ["make bin"]}], ""))
        self.assertEqual((route["depth"], route["checks"]), ("DEEP", ["make docs"]))
        self.assertEqual(self.reassess(fast, policies=([self.PLAIN], ""))["depth"], "FAST")

    def test_without_a_recorded_workflow_the_pin_is_test_first_and_none_means_no_checks(self):
        route = self.reassess("## Route\n\n* depth: FAST\n* verification requirements: none\n\n")
        state, questions = self.ask.call_args.args[2:4]
        self.assertEqual((state["recorded"], state["pin"], route["workflow"], "workflow" in questions), ({"workflow": None, "depth": "FAST", "checks": []}, "test-first", "test-first", False))
        self.reassess("")
        self.assertEqual(self.ask.call_args.args[2]["recorded"], {"workflow": None, "depth": None, "checks": []})

    def test_jev_unavailable_is_the_standard_fallback_on_the_recorded_workflow(self):
        route = self.reassess(jev=RuntimeError("no key"))
        self.assertEqual((route["depth"], route["workflow"], route["confidence"]), ("STANDARD", "implement-then-test", None))

    def test_unreadable_policies_stop_before_jev_and_payment_required_propagates(self):
        with self.assertRaisesRegex(RuntimeError, "policies unreadable: boom"):
            self.reassess(policies=RuntimeError("policies unreadable: boom"))
        self.assertEqual(self.ask.call_count, 0)
        with self.assertRaises(fc.fc_models.PaymentRequired):
            self.reassess(jev=fc.fc_models.PaymentRequired("typesafe", "top up"))


class Floor(unittest.TestCase):
    DOCS = {"name": "Docs", "statement": "Docs stay true.", "globs": ["docs/*"], "checks": ["make e2e", "make docs"]}
    KEPT = "; depth DEEP kept from the recorded route"

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')

    def route(self, recorded="DEEP", jev="FAST", checks="make lint, make e2e", policies=(), triggers=("diff",)):
        """The route reassess returns for a recorded `recorded` depth and checks, Jev answering depth `jev` (or raising) and `policies`."""
        bullet = f"* depth: {recorded}\n" if recorded else ""
        desc = f"## Goal\nGreet.\n\n## Acceptance criteria\n* greets\n\n## Route\n\n* workflow: implement-then-test\n{bullet}* verification requirements: {checks}\n\n```compose\nid: 001\ntest: t\nfiles: src/greet.py\n```\n"
        tasks = fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1"}, "issues": [{"id": "ENG-2", "title": "Greet", "description": desc, "status": "Todo"}]})
        reply = jev if isinstance(jev, Exception) else {"model": "jev-test", "answers": {"depth": {"choice": jev, "probabilities": {jev: 0.9}, "confidence": 0.9}}}
        with mock.patch.object(fc.fc_models, "jev_ask", **{"side_effect" if isinstance(jev, Exception) else "return_value": reply}), mock.patch.object(fc_reroute, "diff_callers", return_value={"callers": {}}), \
                mock.patch.object(fc_policy, "read_policies", return_value=(list(policies), "")), mock.patch.object(fc_route, "record_route"), \
                mock.patch.object(fc.fc_linear, "linear_gql", return_value={}), mock.patch.object(fc_reroute, "diff_signals", return_value=SIGNALS), mock.patch.object(fc_routectx, "context_hash", return_value="h"):
            return fc_reroute.reassess(self.repo, self.proj, tasks[0], tasks, list(triggers), "3f9a0c1b2d4e", "main", "task")

    def test_the_depth_is_the_higher_of_jevs_and_the_recorded_and_the_note_says_when_the_recorded_won(self):
        for recorded, jev, depth, kept in [("DEEP", "FAST", "DEEP", True), ("DEEP", "STANDARD", "DEEP", True), ("STANDARD", "FAST", "STANDARD", True), ("STANDARD", "DEEP", "DEEP", False),
                                           ("FAST", "STANDARD", "STANDARD", False), ("STANDARD", "STANDARD", "STANDARD", False), ("DEEP", "DEEP", "DEEP", False)]:
            with self.subTest(recorded=recorded, jev=jev):
                got = self.route(recorded, jev, triggers=("diff", "scope"))
                self.assertEqual(got["depth"], depth)
                self.assertEqual(got["note"], "rerouted after diff, scope (run 3f9a0c1b2d4e)" + (f"; depth {recorded} kept from the recorded route" if kept else ""))

    def test_a_missing_or_unknown_recorded_depth_is_ignored(self):
        for recorded in ("", "ULTRA"):
            got = self.route(recorded, "FAST")
            self.assertEqual((got["depth"], got["note"]), ("FAST", "rerouted after diff (run 3f9a0c1b2d4e)"))

    def test_with_jev_unavailable_the_depth_is_standard_unless_the_recorded_is_higher(self):
        got = [self.route(r, RuntimeError("no key")) for r in ("FAST", "STANDARD", "DEEP")]
        self.assertEqual([g["depth"] for g in got], ["STANDARD"] * 2 + ["DEEP"])
        self.assertEqual([g["note"].endswith(self.KEPT) for g in got], [False, False, True])

    def test_checks_keep_the_recorded_first_then_add_new_ones_once_each(self):
        self.assertEqual(self.route(policies=[self.DOCS])["checks"], ["make lint", "make e2e", "make docs"])
        self.assertEqual(self.route(checks="none", policies=[self.DOCS])["checks"], ["make e2e", "make docs"])
        self.assertEqual(self.route(jev=RuntimeError("no key"))["checks"], ["make lint", "make e2e"])


class Record(unittest.TestCase):
    HASH = "0123456789abcdef"

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')

    def reassess(self, triggers=("diff",), **record):
        """Reassess a synced task with a recorded route; returns (route, record_route mock, context_hash mock, the policies read_policies gave)."""
        desc = "## Goal\nGreet.\n\n## Acceptance criteria\n* greets\n\n## Route\n\n* workflow: implement-then-test\n* depth: STANDARD\n* verification requirements: make lint\n\n```compose\nid: 001\ntest: t\nfiles: a.py\n```\n"
        tasks = fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1"}, "issues": [{"id": "ENG-2", "title": "Greet", "description": desc, "status": "Todo"}]})
        self.meta, policies = tasks[0], [{"name": "Keys", "statement": "Keys are checked.", "globs": [], "checks": ["make keys"]}]
        reply = {"model": "jev-test", "answers": {"depth": {"choice": "DEEP", "probabilities": {"DEEP": 0.9}, "confidence": 0.9}}}
        with mock.patch.object(fc.fc_models, "jev_ask", return_value=reply), mock.patch.object(fc_reroute, "diff_callers", return_value={"callers": {}}), \
                mock.patch.object(fc_policy, "read_policies", return_value=(policies, "")), mock.patch.object(fc.fc_linear, "linear_gql", return_value={}), \
                mock.patch.object(fc_reroute, "diff_signals", return_value=SIGNALS), mock.patch.object(fc_routectx, "context_hash", return_value=self.HASH) as ctx, mock.patch.object(fc_route, "record_route", **record) as rec:
            return fc_reroute.reassess(self.repo, self.proj, self.meta, tasks, list(triggers), "3f9a0c1b2d4e", "main", "task"), rec, ctx, policies

    def test_the_route_is_hashed_with_the_policies_and_its_reason_ends_with_the_note(self):
        route, rec, ctx, policies = self.reassess()
        self.assertEqual((route["hash"], ctx.call_args.args), (self.HASH, (self.repo, self.meta, policies)))
        self.assertTrue(route["note"].startswith("rerouted after diff") and route["reason"].endswith(route["note"]))

    def test_the_section_is_the_route_section_then_the_rerouted_bullet_with_every_trigger_and_the_run_id(self):
        route, rec, _, _ = self.reassess(("diff", "scope"))
        proj, meta, section = rec.call_args.args
        self.assertEqual((rec.call_count, proj, meta), (1, self.proj, self.meta))
        self.assertEqual(section, fc_routerec.route_section(route) + "\n* rerouted: diff, scope (run 3f9a0c1b2d4e)")
        self.assertTrue(section.endswith(f"\n* context hash: {self.HASH}\n* rerouted: diff, scope (run 3f9a0c1b2d4e)"))

class Sweep(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        # 001, 005 and 006 passed with an issue; 002 failed, 003 has no result file, 004 has no issue
        self.tasks = [{"id": t, **({} if t == "004" else {"linear": f"XX-{t}"})} for t in ("001", "002", "003", "004", "005", "006")]
        for t, status in (("001", "passed"), ("002", "failed"), ("004", "passed"), ("005", "passed"), ("006", "passed")):
            (self.proj / "results" / f"{t}.json").write_text(json.dumps({"id": t, "status": status}))

    def sweep(self, ids=("001", "002", "003", "004", "005", "006"), key="lin_test", kinds=("diff",), reassess=None, run_id=lambda proj, t: f"run-{t}"):
        """catch_up with `reassess` mocked; returns (its return value, the mock)."""
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", key), mock.patch.object(fc_reroute, "reassess", **reassess or {}) as re, mock.patch.object(fc_reroute, "fired", return_value=set()), \
                mock.patch.object(fc_reroute, "layer_base", side_effect=lambda repo, proj, t, trunk: f"{trunk}/below-{t}"), mock.patch.object(fc_ledger, "run_id", side_effect=run_id):
            return fc_reroute.catch_up(self.repo, self.proj, self.tasks, list(ids), "main", kinds), re

    def test_each_passed_task_with_an_issue_is_reassessed_for_diff_in_order_with_its_run_id_layer_base_and_branch(self):
        got, re = self.sweep(("006", "001", "005"))
        self.assertEqual(got, {})
        self.assertEqual([c.args for c in re.call_args_list], [(self.repo, self.proj, next(m for m in self.tasks if m["id"] == t), self.tasks, ["diff"], f"run-{t}", f"main/below-{t}",
                                                                  fc.fc_worktree.task_branch(self.repo, self.proj, t)) for t in ("006", "001", "005")])

    def test_tasks_not_passed_without_a_result_file_or_without_an_issue_are_skipped(self):
        (self.proj / "results" / "006.json").write_text(json.dumps({"id": "006", "status": "already_green"}))
        got, re = self.sweep()
        self.assertEqual((got, [c.args[2]["id"] for c in re.call_args_list]), ({}, ["001", "005"]))

    def test_compose_no_policies_reassesses_and_records_nothing(self):
        with mock.patch.dict(os.environ, {"COMPOSE_NO_POLICIES": "1"}):
            got, re = self.sweep()
        self.assertEqual((got, re.call_count), ({}, 0))

    def test_a_failure_is_returned_under_its_id_and_the_other_tasks_go_on(self):
        def boom(repo, proj, meta, *a):
            if meta["id"] == "001": raise RuntimeError("Linear: down")
        got, re = self.sweep(reassess={"side_effect": boom})
        self.assertEqual((got, re.call_count), ({"001": "Linear: down"}, 3))
        got, re = self.sweep(run_id=lambda proj, t: [][0] if t == "005" else "r")
        self.assertEqual((list(got), re.call_count), (["005"], 2))

    def test_payment_required_propagates_and_stops_the_sweep(self):
        seen = []
        def broke(repo, proj, meta, *a): seen.append(meta["id"]); raise fc.fc_models.PaymentRequired("typesafe", "top up")
        with self.assertRaises(fc.fc_models.PaymentRequired):
            self.sweep(reassess={"side_effect": broke})
        self.assertEqual(seen, ["001"])


class Once(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        self.tasks = [{"id": t, "linear": f"XX-{t}"} for t in ("001", "002", "003")]
        for t in ("001", "002", "003"):
            (self.proj / "results" / f"{t}.json").write_text(json.dumps({"id": t, "status": "passed"}))

    def once(self, fired, ids=("001", "002", "003")):
        """catch_up with `fired(issue)` and `reassess` mocked; returns (its return value, fired mock, reassess mock)."""
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", "lin_test"), mock.patch.object(fc_reroute, "fired", side_effect=fired) as fi, mock.patch.object(fc_reroute, "reassess") as re, \
                mock.patch.object(fc_reroute, "layer_base", return_value="main"), mock.patch.object(fc_ledger, "run_id", side_effect=lambda proj, t: f"run-{t}"):
            return fc_reroute.catch_up(self.repo, self.proj, self.tasks, list(ids), "main", ("diff",)), fi, re

    def test_a_trigger_fired_for_the_run_id_calls_no_reassess_and_each_task_reads_its_own_issue(self):
        got, fi, re = self.once(lambda issue: {("diff", "run-001"), ("diff", "run-003")})
        self.assertEqual((got, [c.args[0] for c in fi.call_args_list]), ({}, ["XX-001", "XX-002", "XX-003"]))
        self.assertEqual([(c.args[2]["id"], c.args[4], c.args[5]) for c in re.call_args_list], [("002", ["diff"], "run-002")])

    def test_a_fired_error_is_returned_under_its_id_and_the_other_tasks_go_on(self):
        def fired(issue):
            if issue == "XX-002": raise RuntimeError("Linear: boom")
            return set()
        got, fi, re = self.once(fired)
        self.assertEqual((got, [c.args[2]["id"] for c in re.call_args_list]), ({"002": "Linear: boom"}, ["001", "003"]))


class AfterRun(unittest.TestCase):
    TASKS = [{"id": t, "linear": f"XX-{t}"} for t in ("001", "002", "003", "004")]

    def run_after(self, statuses, **catch_up):
        """after_run over results with these statuses, catch_up mocked; returns (its return value, catch_up mock, say mock)."""
        results = [{"id": t, "status": s} for t, s in statuses]
        with mock.patch.object(fc_reroute, "catch_up", **catch_up) as cu, mock.patch.object(fc.fc_core, "say") as say:
            return fc_reroute.after_run("repo", "proj", results, self.TASKS, "main"), cu, say

    def test_catch_up_gets_the_passed_ids_only_in_order_with_diff_and_scope(self):
        got, cu, say = self.run_after([("004", "passed"), ("002", "failed"), ("001", "passed"), ("003", "already_green")], return_value={})
        self.assertEqual((got, cu.call_args_list, say.call_count), (None, [mock.call("repo", "proj", self.TASKS, ["004", "001"], "main", ("diff", "scope"))], 0))

    def test_each_route_not_recorded_is_said_with_its_issue_and_why(self):
        got, cu, say = self.run_after([("001", "passed"), ("002", "passed"), ("003", "passed")], return_value={"003": "Linear: down", "001": "policies unreadable: boom"})
        self.assertEqual((got, [c.args[0] for c in say.call_args_list]), (None, ["linear XX-003: route not recorded (Linear: down)", "linear XX-001: route not recorded (policies unreadable: boom)"]))

    def test_a_payment_required_is_returned_not_raised_and_nothing_is_said(self):
        err = fc.fc_models.PaymentRequired("typesafe", "top up")
        got, cu, say = self.run_after([("001", "passed")], side_effect=err)
        self.assertEqual((got, say.call_count), (err, 0))

class RunHook(unittest.TestCase):
    def run_tasks(self, ids=("001", "002"), status=None, hook=None):
        """run_tasks over fake tasks with after_run, triage and payment_halt recorded on one mock; returns (exit code, order, repo, proj, tasks)."""
        repo = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, repo, True); proj = repo / ".compose" / "p"; proj.mkdir(parents=True)
        tasks = [{"id": t, "title": t, "after": "", "files": [], "protected": [], "test": "true", "model": "m", "body": ""} for t in ids]
        pay = [{"payment_required": {"provider": "glm", "message": "pay"}}]
        run_task = lambda r, p, meta, n: {"id": meta["id"], "title": meta["id"], "status": (status or {}).get(meta["id"], "passed"), "attempts": 1, "seconds": 1.0, "cost": 0.0,
                                          "branch": f"b/{meta['id']}", "log": pay if (status or {}).get(meta["id"]) == "payment_required" else []}
        order = mock.Mock(); order.after.return_value = hook
        with mock.patch.object(fc.fc_proc, "keep_awake"), mock.patch.object(fc.fc_run, "preflight"), mock.patch.object(fc.fc_herdr, "herdr_space"), mock.patch.object(fc.fc_core, "say"), \
             mock.patch.object(fc.fc_implement, "run_task", run_task), mock.patch.object(fc_ledger, "run_events", return_value=[]), mock.patch.object(fc_ledger, "record_task"), mock.patch.object(fc.fc_models, "payment_halt", order.halt), mock.patch.object(fc.fc_run, "triage", order.triage), \
             mock.patch.object(fc_reroute, "after_run", order.after), mock.patch("builtins.print", order.print), self.assertRaises(SystemExit) as e:
            fc.fc_run.run_tasks(repo, proj, tasks, mock.Mock(trunk="main", attempts=1, parallel=1, no_stack=True))
        return e.exception.code, order, repo, proj, tasks

    def test_after_run_is_called_once_with_the_results_after_the_table_and_before_triage(self):
        code, order, repo, proj, tasks = self.run_tasks()
        names = [c[0] for c in order.mock_calls]
        self.assertEqual(names.count("after"), 1)
        self.assertLess(max(i for i, n in enumerate(names) if n == "print"), names.index("after"))
        self.assertLess(names.index("after"), names.index("triage"))
        args = order.after.call_args.args
        self.assertEqual((args[0], args[1], [r["id"] for r in args[2]], args[3], args[4]), (repo, proj, ["001", "002"], tasks, "main"))
        self.assertEqual((order.halt.call_count, code), (0, 0))

    def test_a_returned_payment_required_halts_the_run_with_it_after_triage(self):
        err = fc.fc_models.PaymentRequired("typesafe", "top up")
        code, order, *_ = self.run_tasks(hook=err)
        self.assertEqual([c.args[-1] for c in order.halt.call_args_list], [err])
        self.assertEqual([c[0] for c in order.mock_calls if c[0] != "print"], ["after", "triage", "halt"])

    def test_the_hook_runs_when_no_task_passed_and_the_exit_code_stays_failed(self):
        code, order, *_ = self.run_tasks(("001",), {"001": "failed"})
        self.assertEqual((order.after.call_count, order.triage.call_count, order.halt.call_count, code), (1, 0, 0, 1))

    def test_a_providers_halt_is_kept_over_the_hooks(self):
        mine = fc.fc_models.PaymentRequired("typesafe", "top up")
        code, order, *_ = self.run_tasks(("001",), {"001": "payment_required"}, mine)
        halted = order.halt.call_args.args[-1]
        self.assertEqual((order.halt.call_count, halted.provider), (1, "glm"))


class VerifyCatchUp(unittest.TestCase):
    def verify(self, linear=("001", "002"), key=None, **catch_up):
        """cmd_verify over a two-layer stack of passed tasks with its setup and stages faked; catch_up is a mock when given, else the real one.
        Returns (exit code or None, one mock recording catch_up, rounds, say and payment_halt, the repo, the proj)."""
        repo = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, repo, True); proj = repo / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        tasks = [{"id": t, "after": "", "files": [], **({"linear": f"XX-{t}"} if t in linear else {})} for t in ("001", "002")]
        (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        for t in ("001", "002"):
            (proj / "results" / f"{t}.json").write_text(json.dumps({"id": t, "status": "passed"}))
        order = mock.Mock(); order.catch_up.configure_mock(**catch_up)
        a = mock.Mock(repo=str(repo), project="p", fix=0, skip_review=True, skip_e2e=True, review_only=False)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_init, "start_checks"), mock.patch.object(fc.fc_proc, "keep_awake"), \
             mock.patch.object(fc.fc_verify, "verify_root", return_value=(repo, "main", None)), mock.patch.object(fc.fc_verify, "verify_rounds", order.rounds), \
             mock.patch.object(fc.fc_core, "say", order.say), mock.patch.object(fc.fc_models, "payment_halt", order.halt), mock.patch.object(fc.fc_config, "LINEAR_KEY", key), \
             mock.patch.object(fc.fc_worktree, "stack_order", return_value=["t/001", "t/002"]), mock.patch.object(fc_depth, "record_depth"), mock.patch.object(fc_reroute, "layer_base", return_value="main"), \
             mock.patch.object(fc_reroute, "diff_signals", return_value={"outside": []}), mock.patch.object(fc_reroute, "catch_up", *([order.catch_up] if catch_up else [fc_reroute.catch_up])):
            try: fc.fc_verify.cmd_verify(a); code = None
            except SystemExit as e: code = e.code
        return code, order, repo, proj

    def test_the_stack_is_caught_up_once_with_diff_and_scope_before_any_stage(self):
        code, order, repo, proj = self.verify(return_value={})
        self.assertEqual(order.catch_up.call_args_list, [mock.call(repo, proj, fc.fc_tasks.load_tasks(proj), ["001", "002"], "main", ("diff", "scope"))])
        self.assertEqual(([c[0] for c in order.mock_calls if c[0] in ("catch_up", "rounds")], code), (["catch_up", "rounds"], None))

    def test_each_route_not_recorded_is_said_exactly_and_verify_exits_1_without_a_stage(self):
        code, order, *_ = self.verify(return_value={"003": "Linear timeout", "001": "Linear: down"})
        said = [c.args[0] for c in order.say.call_args_list]
        self.assertEqual([s for s in said if "FAIL" in s], ["verify FAIL  route not recorded for 003: Linear timeout", "verify FAIL  route not recorded for 001: Linear: down", "verify FAIL  fix what Linear refused (`compose linear get <issue>` shows the task), or set COMPOSE_NO_POLICIES=1 to verify without recording routes"])
        self.assertEqual((code, order.rounds.call_count), (1, 0))

    def test_a_payment_required_reaches_payment_halt_without_a_stage(self):
        err = fc.fc_models.PaymentRequired("typesafe", "top up")
        code, order, *_ = self.verify(side_effect=err)
        self.assertEqual(([c.args[-1] for c in order.halt.call_args_list], order.rounds.call_count), ([err], 0))

    def test_without_a_linear_key_nothing_is_asked_or_written_and_the_stages_run(self):
        with mock.patch.object(fc.fc_models, "jev_ask") as ask, mock.patch.object(fc.fc_linear, "linear_gql") as gql, mock.patch.object(fc_reroute, "reassess") as re:
            code, order, *_ = self.verify()
        self.assertEqual((ask.call_count, gql.call_count, re.call_count, order.rounds.call_count, code), (0, 0, 0, 1, None))

    def test_a_stack_task_without_a_linear_issue_is_neither_reassessed_nor_failed(self):
        with mock.patch.object(fc_reroute, "reassess") as re, mock.patch.object(fc_reroute, "fired", return_value=set()):
            code, order, *_ = self.verify(linear=("001",), key="lin_test")
        self.assertEqual(([c.args[2]["id"] for c in re.call_args_list], order.rounds.call_count, code), (["001"], 1, None))


class PassScope(unittest.TestCase):
    OUTSIDE = {"001": ["logo.png"], "002": [], "003": ["src/c.py"]}  # 001 leaves its files (also a logo), 002 stays inside, 003 has no files: so every changed file is outside

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        self.tasks = [{"id": t, "after": "", "files": [] if t == "003" else [f"src/{'ab'[int(t) - 1]}.py"], "linear": f"XX-{t}"} for t in self.OUTSIDE]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": self.tasks}))
        for t in self.OUTSIDE: (self.proj / "results" / f"{t}.json").write_text(json.dumps({"id": t, "status": "passed"}))
        self.signals = lambda repo, meta, *a: {"outside": self.OUTSIDE[meta["id"]]}

    def run_hook(self, hook, fired=(), **kw):
        """`hook(**kw)` with the pairs `fired` on every issue; returns (its return value, {task id: reassess call args})."""
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", "lin_test"), mock.patch.object(fc_reroute, "reassess") as re, mock.patch.object(fc_reroute, "fired", return_value=set(fired)), \
                mock.patch.object(fc_ledger, "run_id", side_effect=lambda proj, t: f"run-{t}"), mock.patch.object(fc_reroute, "diff_signals", lambda *a: self.signals(*a)), \
                mock.patch.object(fc.fc_worktree, "stack_order", return_value=["t/001", "t/002", "t/003"]), mock.patch.object(fc.fc_core, "sh", git_says("t/001\nt/002\nt/003")):
            got = hook(**kw)
        return got, {c.args[2]["id"]: c.args for c in re.call_args_list}

    def catch_up(self, kinds, fired=(), ids=("001", "002", "003")):
        return self.run_hook(lambda: fc_reroute.catch_up(self.repo, self.proj, self.tasks, list(ids), "main", kinds), fired)

    def test_after_run_asks_diff_and_scope_for_a_layer_outside_its_files_and_diff_only_inside_with_the_layer_base_and_branch(self):
        got, calls = self.run_hook(lambda: fc_reroute.after_run(self.repo, self.proj, [{"id": t, "status": "passed"} for t in ("001", "002", "003")], self.tasks, "main"))
        self.assertEqual((got, {t: a[4] for t, a in calls.items()}), (None, {"001": ["diff", "scope"], "002": ["diff"], "003": ["diff", "scope"]}))
        branch = lambda t: f"t/{t}"
        self.assertEqual([(a[5], a[6], a[7]) for a in calls.values()], [("run-001", "main", branch("001")), ("run-002", branch("001"), branch("002")), ("run-003", branch("002"), branch("003"))])

    def test_catch_up_with_scope_alone_asks_only_the_tasks_outside_their_files(self):
        got, calls = self.catch_up(("scope",))
        self.assertEqual((got, {t: a[4] for t, a in calls.items()}), ({}, {"001": ["scope"], "003": ["scope"]}))

    def test_before_verify_asks_scope_for_the_stack_tasks_whose_comments_hold_diff_but_not_scope(self):
        fired = [("diff", "run-001"), ("diff", "run-002"), ("diff", "run-003"), ("scope", "run-003")]
        got, calls = self.run_hook(lambda: fc_reroute.before_verify(self.repo, self.proj, self.tasks, "main"), fired)
        self.assertEqual({t: a[4] for t, a in calls.items()}, {"001": ["scope"]})
        got, calls = self.run_hook(lambda: fc_reroute.before_verify(self.repo, self.proj, self.tasks, "main"), fired + [("scope", "run-001")])
        self.assertEqual(calls, {})

    def test_a_diff_signals_error_is_that_tasks_failure_and_the_others_go_on(self):
        def signals(repo, meta, *a):
            if meta["id"] == "001": raise RuntimeError("git diff failed")
            return {"outside": self.OUTSIDE[meta["id"]]}
        self.signals = signals
        got, calls = self.catch_up(("scope",))
        self.assertEqual((got, list(calls)), ({"001": "git diff failed"}, ["003"]))


class FixScope(unittest.TestCase):
    def rounds(self, stages, outside=True, layers=True, key="lin_test", reassess=None):
        """verify_rounds on a stack (001 inside src/a.py, 002 on src/b.py plus a logo when `outside`) with the stages `stages` (ok per round) and a fixer
        that edits src/b.py. Returns (exit code, reassess call args, said lines, stages run, passed from verify.json)."""
        wt = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, wt, True)
        proj = wt / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        tasks = [{"id": t, "after": "", "title": t, "body": "", "test": "true","protected": [], "files": [f"src/{f}.py"], "linear": f"XX-{t}"} for t, f in (("001", "a"), ("002", "b"))]
        (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        for t in ("001", "002"): (proj / "results" / f"{t}.json").write_text(json.dumps({"id": t, "status": "passed"}))
        asked, said, ran, done, touched = [], [], [], set(), {}
        def re(*args):
            asked.append(args); done.update((k, args[5]) for k in args[4])
            if reassess: raise reassess
        def stage(*args):
            ran.append(len(asked)); return (stages[len(ran) - 1], {"checks": [{"cmd": "c", "exit": 1}]}, "finding")
        def fixer(cwd, model, prompt): touched["src/b.py"] = f"fix {len(ran)}"; return None, "Tidy b", 1, {"cost": 0.0}
        patches = [mock.patch.object(fc_reroute, "reassess", re), mock.patch.object(fc_reroute, "fired", lambda issue: set(done)), mock.patch.object(fc.fc_config, "LINEAR_KEY", key), mock.patch.object(fc_reroute, "after_round", return_value=([], {})),
                   mock.patch.object(fc_reroute, "layer_base", return_value="main"), mock.patch.object(fc_reroute, "diff_signals", return_value={"outside": ["logo.png"] if outside else []}),
                   mock.patch.object(fc_ledger, "run_id", side_effect=lambda proj, t: f"run-{t}"), mock.patch.object(fc.fc_verify, "run_stages", stage), mock.patch.object(fc.fc_verify, "changed_files", lambda repo: dict(touched)),
                   mock.patch.object(fc.fc_worktree, "stack_order", return_value=["t/001", "t/002"] if layers else []), mock.patch.object(fc.fc_worktree, "review_base", return_value="main"), mock.patch.object(fc.fc_worktree, "dirty_paths", return_value=[]),
                   mock.patch.object(fc.fc_implement, "implementer", fixer), mock.patch.object(fc.fc_implement.fc_trailpack, "trail_pack", return_value=""), mock.patch.object(fc.fc_worktree, "ready_checkout"), mock.patch.object(fc.fc_proc, "run", return_value=mock.Mock(returncode=0)),
                   mock.patch.object(fc.fc_verify, "verify_failed"), mock.patch.object(fc_ledger, "verify_record"), mock.patch.object(fc.fc_core, "say", said.append), mock.patch.object(fc.fc_core, "sh", git_says(""))]
        with contextlib.ExitStack() as stack:
            for p in patches: stack.enter_context(p)
            with self.assertRaises(SystemExit) as exit:
                fc.fc_verify.verify_rounds(wt, proj, {"main_repo": str(wt)}, SimpleNamespace(fix=len(stages) - 1, skip_review=True), [], 0)
        return exit.exception.code, asked, said, ran, json.loads((proj / "results" / "verify.json").read_text())["passed"]

    def test_a_kept_fix_reassesses_the_top_task_for_scope_once_before_the_next_round_and_a_second_fix_asks_nothing(self):
        code, asked, _, ran, passed = self.rounds([False, False, True])
        self.assertEqual(([a[2]["id"] for a in asked], [a[4] for a in asked], [a[5] for a in asked], ran, code, passed), (["002"], [["scope"]], ["run-002"], [0, 1, 1], 0, True))

    def test_a_top_layer_inside_its_files_or_a_stack_without_layers_or_a_key_reassesses_nothing_and_fails_nothing(self):
        for kw in {"outside": False}, {"layers": False}, {"key": None}:
            with self.subTest(**kw):
                code, asked, said, ran, passed = self.rounds([False, True], **kw)
                self.assertEqual((asked, [s for s in said if "route not recorded" in s], ran, code, passed), ([], [], [0, 0], 0, True))

    def test_a_scope_reassessment_not_recorded_says_why_runs_no_further_round_and_fails_verify(self):
        code, asked, said, ran, passed = self.rounds([False, True], reassess=RuntimeError("Linear: down"))
        self.assertEqual((len(asked), "verify FAIL  route not recorded for 002: Linear: down" in said, ran, code, passed), (1, True, [0], 1, False))


class LedgerVerdicts(unittest.TestCase):
    def triggers(self, *rows, kept=None):
        rows = [{"event_id": f"e{i}", "run_id": "r1", "commit_sha": "c", "task_ids": ["001"], "criterion_id": "k", "fails_on_base": None, "detail": "", "at": "t", "shared": False, "source": s, "phase": p, "result": r} for i, (s, p, r) in enumerate(rows)]
        with mock.patch.object(fc_ledger, "counted", return_value=rows if kept is None else kept) as counted:
            got = fc_reroute.ledger_triggers("repo", "proj", {"id": "001"}, rows)
        self.assertEqual(counted.call_args.args, ("repo", "proj", {"id": "001"}, rows))
        return got

    def test_a_counted_row_names_its_trigger_and_every_other_row_names_none(self):
        failed, invalid, unmet = ["failed tests"], ["invalid evidence"], ["unmet requirements"]
        for row, want in [(("verify", "checks", "fail"), failed), (("verify", "scoped", "fail"), failed), (("verify", "e2e", "fail"), failed), (("verify", "checks", "pass"), []), (("run", "checks", "fail"), []),
                          (("run", "red", "fail"), []), (("run", "attempt", "fail"), []), (("run", "final", "fail"), []), (("verify", "review", "fail"), []),
                          (("verify", "acceptance", "fail"), unmet), (("verify", "acceptance", "unclear"), unmet), (("verify", "sufficiency", "unproven"), unmet),
                          (("verify", "acceptance", "pass"), []), (("verify", "acceptance", "error"), []), (("verify", "sufficiency", "pass"), []),
                          (("verify", "checks", "contradicted"), invalid), (("verify", "acceptance", "contradicted"), invalid)]:
            with self.subTest(row=row):
                self.assertEqual(self.triggers(row), want)

    def test_each_trigger_appears_once_in_ledger_order_however_many_rows_fire_it(self):
        rows = [("verify", "acceptance", "fail"), ("verify", "sufficiency", "unproven"), ("verify", "e2e", "contradicted"), ("verify", "scoped", "fail"), ("verify", "checks", "fail"), ("verify", "acceptance", "unclear")]
        self.assertEqual(self.triggers(*rows), ["failed tests", "invalid evidence", "unmet requirements"])
        self.assertEqual(self.triggers(*rows[:2]), ["unmet requirements"])

    def test_a_row_counted_drops_fires_nothing_and_only_the_kept_rows_fire(self):
        rows = [("verify", "checks", "fail"), ("verify", "acceptance", "fail"), ("verify", "e2e", "contradicted")]
        self.assertEqual(self.triggers(*rows, kept=[]), [])
        kept = [{"source": "verify", "phase": "acceptance", "result": "unclear"}]
        self.assertEqual(self.triggers(*rows, kept=kept), ["unmet requirements"])


class LedgerInvalid(unittest.TestCase):
    def triggers(self, *rows, unsaved=()):
        led = fc_ledger
        counted = lambda repo, proj, meta, rs: [r for r in rs if r["run_id"] == "r1" and "001" in r["task_ids"]]
        with mock.patch.object(led, "counted", side_effect=counted) as c, mock.patch.object(led, "criterion_ids", return_value=["001.1:aaaaaaaa", "001.2:bbbbbbbb"]), mock.patch.object(led, "unsaved", return_value=list(unsaved)):
            return fc_reroute.ledger_triggers("repo", "proj", {"id": "001"}, list(rows)), c

    def row(self, ids=(), run="r1", tasks=("001",), at=("verify", "checks", "pass")):
        return {"event_id": "e", "run_id": run, "task_ids": list(tasks), "criterion_id": list(ids), "source": at[0], "phase": at[1], "result": at[2]}

    def test_a_counted_row_holding_an_edited_criterion_of_the_task_is_voided_and_no_other_row_is(self):
        for kw, want in [(dict(ids=["001.1:deadbeef"]), True), (dict(ids=["001.1:aaaaaaaa", "001.3:deadbeef"]), True), (dict(ids=["001.1:aaaaaaaa", "001.2:bbbbbbbb"]), False), (dict(ids=["002.1:cccccccc"]), False),
                         (dict(ids=["0010.1:cccccccc"]), False), (dict(), False), (dict(ids=["001.1:deadbeef"], run="r2"), False), (dict(ids=["001.1:deadbeef"], tasks=["002"]), False)]:
            with self.subTest(kw=kw):
                got, counted = self.triggers(self.row(**kw))
                self.assertEqual(got, ["invalid evidence"] if want else [])
        got, counted = self.triggers(self.row(ids=["001.1:deadbeef"]))
        self.assertIn(True, [len(c.args[3]) == 1 and c.args[3][0]["criterion_id"] == [] for c in counted.call_args_list])

    def test_an_unsaved_row_naming_the_task_or_no_task_gives_invalid_evidence_and_one_naming_only_others_does_not(self):
        for tasks, want in [([], True), (["001"], True), (["002", "001"], True), (["002"], False), (["002", "003"], False)]:
            with self.subTest(tasks=tasks):
                self.assertEqual(self.triggers(unsaved=[self.row(tasks=tasks)])[0], ["invalid evidence"] if want else [])

    def test_invalid_evidence_appears_once_between_the_other_triggers_however_many_rows_fire_it(self):
        rows = [self.row(at=("verify", "acceptance", "fail")), self.row(ids=["001.1:deadbeef"]), self.row(ids=["001.2:deadbeef"]), self.row(at=("verify", "checks", "fail"))]
        self.assertEqual(self.triggers(*rows, unsaved=[self.row(tasks=[]), self.row()])[0], ["failed tests", "invalid evidence", "unmet requirements"])


class LedgerCatchUp(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
        self.tasks = [{"id": t, "linear": f"XX-{t}"} for t in ("001", "002", "003")]
        for t in ("001", "002", "003"): (self.proj / "results" / f"{t}.json").write_text(json.dumps({"id": t, "status": "passed"}))

    def go(self, kinds, fired=(), fire=("failed tests", "invalid evidence", "unmet requirements"), ids=("001", "002", "003"), rows=None):
        """catch_up with everything but its own logic mocked; returns (its value, reassess, task_rows, ledger_triggers)."""
        ev = fc_evidence
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", "lin_test"), mock.patch.object(fc_reroute, "reassess") as re, mock.patch.object(fc_reroute, "fired", return_value=set(fired)), \
                mock.patch.object(fc_reroute, "layer_base", return_value="main"), mock.patch.object(fc_reroute, "diff_signals", return_value={"outside": ["x"]}), \
                mock.patch.object(fc_ledger, "run_id", side_effect=lambda proj, t: f"run-{t}"), mock.patch.object(ev, "task_rows", **rows or {"return_value": ["row"]}) as tr, \
                mock.patch.object(fc_reroute, "ledger_triggers", side_effect=fire if isinstance(fire, Exception) else lambda repo, proj, meta, rs: list(fire)) as lt:
            return fc_reroute.catch_up(self.repo, self.proj, self.tasks, list(ids), "main", kinds), re, tr, lt

    def test_the_asked_ledger_triggers_the_rows_fire_join_diff_and_scope_in_one_reassess_in_ledger_order(self):
        got, re, tr, lt = self.go(("diff", "scope", "unmet requirements", "failed tests"), ids=("001", "002"))
        self.assertEqual((got, [(c.args[2]["id"], c.args[4]) for c in re.call_args_list]), ({}, [(t, ["diff", "scope", "failed tests", "unmet requirements"]) for t in ("001", "002")]))
        self.assertEqual(self.go(("invalid evidence",), ids=("001",))[1].call_args.args[4], ["invalid evidence"])

    def test_a_trigger_fired_for_the_run_id_is_dropped_and_with_none_left_reassess_is_not_called(self):
        for fired, want in ([("failed tests", "run-001"), ("diff", "run-001")], ["unmet requirements"]), ([("failed tests", "old"), ("unmet requirements", "run-002")], ["diff", "failed tests", "unmet requirements"]), \
                           ([("diff", "run-001"), ("failed tests", "run-001"), ("unmet requirements", "run-001")], None):
            with self.subTest(fired=fired):
                got, re, tr, lt = self.go(("diff", "failed tests", "unmet requirements"), fired, ids=("001",))
                self.assertEqual(re.call_args.args[4] if re.call_args else None, want)

    def test_the_rows_are_read_once_per_task_from_its_issue_the_feature_and_its_id_and_never_without_a_ledger_kind(self):
        got, re, tr, lt = self.go(("diff", "failed tests"))
        self.assertEqual([c.args for c in tr.call_args_list], [(f"XX-{t}", "ENG-1", t) for t in ("001", "002", "003")])
        self.assertEqual([c.args[3] for c in lt.call_args_list], [["row"]] * 3)
        (self.proj / "linear.json").unlink()
        self.assertEqual([c.args for c in self.go(("failed tests",), ids=("001",))[2].call_args_list], [("XX-001", None, "001")])
        for kinds in (("diff", "scope"), ()): self.assertEqual((self.go(kinds)[2].call_count, self.go(kinds)[3].call_count), (0, 0))

    def test_a_task_rows_or_ledger_triggers_error_is_that_tasks_failure_and_the_others_go_on(self):
        def rows(issue, feature, tid):
            if tid == "002": raise RuntimeError("Linear: boom")
            return ["row"]
        got, re, tr, lt = self.go(("diff", "failed tests"), rows={"side_effect": rows})
        self.assertEqual((got, [c.args[2]["id"] for c in re.call_args_list]), ({"002": "Linear: boom"}, ["001", "003"]))
        self.assertEqual(self.go(("failed tests",), fire=RuntimeError("bad rows"), ids=("001",))[0], {"001": "bad rows"})


class LedgerRound(unittest.TestCase):
    ORDER = ("005", "003", "006", "001", "007", "002", "004")  # stack order, not id order
    BEFORE = {"005": "bogus", "003": "FAST", "006": "DEEP", "001": "STANDARD", "007": "STANDARD", "002": None, "004": "DEEP"}
    AFTER = {"005": "FAST", "003": "STANDARD", "006": "FAST", "001": "STANDARD", "007": None, "002": "FAST", "004": "DEEP"}

    def round(self, failed=None, ready=True):
        """after_round over a temp project whose cached issue descriptions turn from BEFORE to AFTER when catch_up runs; returns (value, catch_up, say, stack_order, tasks)."""
        proj = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, proj, True); (proj / "results").mkdir()
        tasks = [{"id": t, "linear": f"XX-{t}"} for t in sorted(self.ORDER)]
        write = lambda d: (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks, "issues": {f"XX-{t}": {"description": v} for t, v in d.items() if v}}))
        write(self.BEFORE)
        def catch_up(*args): write(self.AFTER); return failed or {}
        with mock.patch.object(fc.fc_linear, "linear_ready", return_value=ready), mock.patch.object(fc.fc_worktree, "stack_order", return_value=[f"b-{t}" for t in self.ORDER]) as order, \
             mock.patch.object(fc.fc_worktree, "task_prefix", return_value="b-"), mock.patch.object(fc_reroute, "catch_up", side_effect=catch_up) as cu, mock.patch.object(fc.fc_core, "say") as say, \
             mock.patch.object(fc_routerec, "parse_route", side_effect=lambda d: {"depth": d}):
            got = fc_reroute.after_round("repo", proj, "main")
        return got, cu, say, order, tasks

    def test_catch_up_is_called_once_with_the_stack_ids_in_order_and_the_ledger_kinds(self):
        got, cu, say, order, tasks = self.round()
        self.assertEqual(cu.call_args_list, [mock.call("repo", cu.call_args.args[1], tasks, list(self.ORDER), "main", fc_reroute.LEDGER)])

    def test_the_tasks_whose_depth_rose_are_raised_in_stack_order_and_no_other(self):
        got, *_ = self.round()
        self.assertEqual(got, (["005", "003", "002"], {}))  # unknown -> FAST, FAST -> STANDARD, none -> FAST; kept, lowered, dropped and DEEP stay out

    def test_each_route_not_recorded_is_said_with_the_exact_line_and_returned_in_failed(self):
        failed = {"003": "Linear: down", "001": "policies unreadable: boom"}
        got, cu, say, *_ = self.round(failed)
        self.assertEqual((got[1], [c.args[0] for c in say.call_args_list]), (failed, ["verify FAIL  route not recorded for 003: Linear: down", "verify FAIL  route not recorded for 001: policies unreadable: boom", "verify FAIL  fix what Linear refused (`compose linear get <issue>` shows the task), or set COMPOSE_NO_POLICIES=1 to verify without recording routes"]))
        self.assertEqual(self.round()[2].call_count, 0)

class LedgerRerun(unittest.TestCase):
    def rounds(self, stages, after, fix=0, main=None):
        """verify_rounds with its stages (ok per run), evidence record, sufficiency and after_round (one return per call) faked; the fixer is a mock.
        Returns (exit code, call order, run_stages calls, rounds, after_round calls, fixer mock, passed from verify.json, the repo, the proj, the stage reports)."""
        repo = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, repo, True); proj = repo / ".compose" / "p"; proj.mkdir(parents=True)
        order, reports, rounds = [], [{"run": i} for i in range(len(stages))], []
        def note(name, value=None): return mock.Mock(side_effect=lambda *a: order.append(name) or value)
        run = mock.Mock(side_effect=lambda *a: (order.append("stages"), (stages[order.count("stages") - 1], reports[order.count("stages") - 1], "finding"))[1])
        cu = mock.Mock(side_effect=lambda *a: (order.append("after_round"), after[order.count("after_round") - 1])[1]); fixer = mock.Mock()
        with mock.patch.object(fc.fc_verify, "run_stages", run), mock.patch.object(fc_ledger, "verify_record", note("record")), \
             mock.patch.object(fc.fc_accept, "jev_sufficiency", note("sufficiency", "judged")), mock.patch.object(fc_reroute, "after_round", cu), mock.patch.object(fc.fc_implement, "implementer", fixer), \
             mock.patch.object(fc.fc_worktree, "review_base", return_value="BASE"), mock.patch.object(fc.fc_worktree, "dirty_paths", return_value=[]), mock.patch.object(fc.fc_worktree, "ready_checkout"), \
             mock.patch.object(fc.fc_verify, "verify_failed"), mock.patch.object(fc.fc_linear, "linear_reconcile"), mock.patch.object(fc.fc_core, "say"), mock.patch.object(fc.fc_core, "sh", git_says("")):
            with self.assertRaises(SystemExit) as exit:
                fc.fc_verify.verify_rounds(repo, proj, {"main_repo": main} if main else {}, SimpleNamespace(fix=fix, skip_review=False), rounds, 0)
        return exit.exception.code, order, run.call_args_list, rounds, cu.call_args_list, fixer, json.loads((proj / "results" / "verify.json").read_text())["passed"], repo, proj, reports

    def test_without_a_raise_the_stages_run_once_and_after_round_follows_the_recorded_evidence_with_the_main_checkout_and_base(self):
        for main in None, "/main/checkout":
            with self.subTest(main=main):
                code, order, ran, rounds, asked, _, passed, repo, proj, reports = self.rounds([True], [([], {})], main=main)
                self.assertEqual((order, asked, rounds, (code, passed)), (["stages", "record", "sufficiency", "after_round"], [mock.call(main or repo, proj, "BASE")], reports, (0, True)))

    def test_each_raise_reruns_the_stages_with_the_same_tests_records_each_run_and_ends_when_no_depth_rises(self):
        code, order, ran, rounds, asked, _, passed, repo, proj, reports = self.rounds([True, True, True], [(["001"], {}), (["002"], {}), ([], {})])
        self.assertEqual((order, len(asked), rounds, len({c.args[4] for c in ran}), (code, passed)), (["stages", "record", "sufficiency", "after_round"] * 3, 3, reports, 1, (0, True)))

    def test_the_verdict_and_the_fix_decision_come_from_the_last_run(self):
        code, order, ran, rounds, asked, fixer, passed, *_ = self.rounds([False, True], [(["001"], {}), ([], {})], fix=1)
        self.assertEqual((code, passed, len(ran), fixer.call_count), (0, True, 2, 0))
        code, order, ran, rounds, asked, fixer, passed, *_ = self.rounds([True, False], [(["001"], {}), ([], {})])
        self.assertEqual((code, passed, len(rounds)), (1, False, 2))

    def test_a_route_not_recorded_fails_with_exit_1_and_no_fix_round_even_when_every_stage_passed_and_a_raise_does_not_rerun(self):
        for stages, after in ([True], [([], {"001": "Linear: down"})]), ([True], [(["002"], {"001": "Linear: down"})]), ([True, True], [(["002"], {}), ([], {"001": "Linear: down"})]):
            with self.subTest(runs=len(stages)):
                code, order, ran, rounds, asked, fixer, passed, *_ = self.rounds(stages, after, fix=1)
                self.assertEqual((code, passed, len(ran), len(asked), fixer.call_count), (1, False, len(stages), len(stages), 0))
