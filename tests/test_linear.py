import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, tempfile, unittest, urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_evidence, fc_ledger, fc_plan  # noqa: E402


def issue(ident, title, desc="", blocked_by=()):
    return {"id": ident, "title": title, "description": desc, "status": "Todo", "blockedBy": list(blocked_by)}


def block(**kv):
    return "## Goal\nDo it.\n\n## Acceptance criteria\n- works\n\n```compose\n" + "\n".join(f"{k}: {v}" for k, v in kv.items()) + "\n```\n"


class Sync(unittest.TestCase):
    """The team lead reads Linear through MCP and pipes the feature and its issues to `compose sync`."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"
        self.proj.mkdir()
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')

    def sync(self, issues, feature="ENG-1"):
        return fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": feature, "title": "Feature", "description": "## Plan\nThe plan."}, "issues": issues})

    def test_tasks_come_from_issue_blocks(self):
        tasks = self.sync([
            issue("ENG-2", "Story A", "story text"),
            issue("ENG-3", "First", block(id="001", files="a.go, b.go", protected="a_test.go", test="go test ./...")),
            issue("ENG-4", "Second", block(id="002", files="c.go", test="go test", model="claude"), blocked_by=[{"id": "ENG-3", "title": "First"}, "ENG-77"]),
            issue("ENG-5", "Loose task", block(id="003", files="d.go", test="true"),
                  blocked_by=["ENG-2", {"id": "ENG-80", "status": "Done"}, {"id": "ENG-81", "status": "Canceled"}]),
            issue("ENG-6", "No block: a decision ticket", "## Question\nWhich?")])
        self.assertEqual([t["id"] for t in tasks], ["001", "002", "003"])
        first, second = tasks[0], tasks[1]
        self.assertEqual(first["files"], ["a.go", "b.go"])
        self.assertEqual(first["protected"], ["a_test.go"])
        self.assertEqual(first["linear"], "ENG-3")
        self.assertEqual(first["body"], "## Goal\nDo it.\n\n## Acceptance criteria\n- works")  # the block is not part of the brief
        self.assertEqual(second["after"], "001")  # ENG-77 is outside the feature's tasks: it does not stack, it holds the task
        self.assertEqual((first["waits_on"], second["waits_on"], tasks[2]["waits_on"]), ([], ["ENG-77 (state unknown)"], ["ENG-2 (Todo)"]))
        self.assertEqual(second["model"], "claude")
        self.assertEqual(fc.fc_tasks.load_tasks(self.proj, required=True), tasks)  # the cache serves every later command
        self.assertEqual(fc.fc_tasks.linear_cache(self.proj)["feature"]["description"], "## Plan\nThe plan.")
        self.assertEqual(fc.fc_tasks.criteria_from_tasks(self.proj)[0], ("001", "works"))

    def test_story_scope_and_done_tasks(self):
        def story_task(ident, parent, tid, state="Todo"):
            return {**issue(ident, tid, block(id=tid, files=f"{tid}.go", test="t")), "parentId": parent, "status": state}
        issues = [{**issue("ENG-2", "Story A"), "parentId": "ENG-1"}, {**issue("ENG-9", "Story B"), "parentId": "ENG-1"},
                  story_task("ENG-3", "ENG-2", "001", "Done"), story_task("ENG-4", "ENG-2", "002"), story_task("ENG-5", "ENG-9", "003")]
        self.assertEqual([t["id"] for t in self.sync(issues)], ["001", "002", "003"])  # no stories set: unchanged
        (self.proj / "linear.json").write_text('{"feature": "ENG-1", "stories": ["ENG-9"]}')
        self.assertEqual([t["id"] for t in self.sync(issues)], ["003"])
        with self.assertRaises(SystemExit) as no_parent:  # a scoped project fails loudly on issues synced without parentId
            self.sync([issue("ENG-9", "Story B"), issue("ENG-5", "Task", block(id="003", files="a.go", test="t"))])
        self.assertIn("parentId", str(no_parent.exception))
        self.assertIn("ENG-5", str(no_parent.exception))
        self.assertEqual(set(fc.fc_tasks.linear_cache(self.proj)["issues"]), {"ENG-9", "ENG-5"})
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        done, todo = self.sync(issues)[:2]
        state = lambda m: fc.fc_status.task_state(self.proj, self.proj, m, "HEAD")["state"]
        self.assertEqual((state(done), state(todo)), ("merged", "pending"))
        self.assertTrue(fc.fc_tasks.linear_done(done) and not fc.fc_tasks.linear_done(todo))

    def test_a_design_task_in_review_or_done_on_linear_is_passed_whatever_this_checkout_holds(self):
        """A design task has no branch, so Linear is its record: In Review (what a passing judge or a hand accept sets) or Done is
        passed, even with no result here or a judge `failed` the designer overruled, and the task that waits on it is not blocked."""
        def design(ident, tid, state, after=()):
            return {**issue(ident, f"Design {tid}", block(id=tid, kind="design", nodes=f"P{tid}/Frame", test="t"), blocked_by=after), "status": state}
        self.sync([design("ENG-2", "001", "In Review"), design("ENG-3", "002", "In Review"), design("ENG-4", "003", "Done"),
                   design("ENG-5", "004", "Todo", ["ENG-2", "ENG-3"]), design("ENG-6", "005", "In Progress"), design("ENG-7", "006", "In Review")])
        (self.proj / "results").mkdir(exist_ok=True)
        for tid, status in (("002", "failed"), ("005", "passed"), ("006", "hand_done")):
            (self.proj / "results" / f"{tid}.json").write_text(json.dumps({"id": tid, "status": status, "model": "jev-latest"}))
        rows = {m["id"]: fc.fc_status.task_state(self.proj, self.proj, m, "HEAD") for m in fc.fc_tasks.load_tasks(self.proj)}
        self.assertEqual({t: (r["state"], r["note"]) for t, r in rows.items()},
                         {"001": ("passed", "In Review on Linear"), "002": ("passed", "In Review on Linear"), "003": ("passed", "Done on Linear"),
                          "004": ("pending", "after 001, 002"), "005": ("passed", ""), "006": ("passed (hand)", "")})
        self.assertEqual((rows["001"]["model"], rows["002"]["model"]), ("", "jev-latest"))  # never the code default model a design task does not run on

    def test_big_tasks_are_flagged(self):
        small, wide, long = self.sync([
            issue("ENG-3", "Small", block(id="001", files="a.go, a2.go", test="t")),
            issue("ENG-4", "Wide", block(id="002", files="a.go, b.go, c.go", test="t")),
            issue("ENG-5", "Long", "x" * 3000 + "\n\n" + block(id="003", files="d.go", test="t"))])
        self.assertIsNone(fc.fc_spec.too_big(small))
        self.assertIn("owns 3 files", fc.fc_spec.too_big(wide))
        self.assertIn("brief is", fc.fc_spec.too_big(long))

    def clash_warning(self, *specs):
        """What `tasks` prints about a shared file for (id, after) tasks that all own x.go."""
        tasks = [{"id": i, "title": i, "files": ["x.go"], "protected": [], "test": "t", "after": a, "linear": "", "body": ""} for i, a in specs]
        out = io.StringIO()
        with mock.patch.object(fc.fc_tasks, "load_tasks", return_value=tasks), mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), \
                mock.patch.object(fc.fc_status, "task_state", return_value={"state": "pending"}), mock.patch.object(fc.fc_core, "sh"), \
                mock.patch("sys.stdout", out), contextlib.suppress(SystemExit):  # a transitively ordered chain is a serial chain, which exits 1 after printing
            fc.fc_spec.cmd_tasks(type("A", (), {"repo": str(self.proj)})())
        return "own the same file" in out.getvalue()

    def test_shared_file_clashes_only_between_unordered_tasks(self):
        self.assertFalse(self.clash_warning(("001", ""), ("002", "001"), ("003", "002")))  # a chain, transitively ordered
        self.assertTrue(self.clash_warning(("001", ""), ("002", "")))
        self.assertTrue(self.clash_warning(("001", "002"), ("002", ""), ("003", "002")))  # 001 and 003 are siblings

    def test_criteria_in_linear_bullets(self):
        desc = "## Goal\n\nDo it.\n\n## Acceptance criteria\n\n* one\n* two\n\n## Test plan\n\n* run\n\n```compose\nid: 001\ntest: t\n```"
        self.sync([issue("ENG-3", "A", desc)])
        self.assertEqual(fc.fc_tasks.criteria_from_tasks(self.proj), [("001", "one"), ("001", "two")])

    def test_refusals(self):
        for issues, feature in (([issue("ENG-3", "A", block(id="001", test="t")), issue("ENG-4", "B", block(id="001", test="t"))], "ENG-1"),
                                ([issue("ENG-3", "A", block(files="x"))], "ENG-1"),  # no id or test
                                ([], "ENG-9")):  # another feature than linear.json names
            with self.assertRaises(SystemExit):
                self.sync(issues, feature)
        with self.assertRaises(SystemExit):
            fc.fc_tasks.sync_tasks(self.proj, {"issues": []})

class RefreshBeforeRun(unittest.TestCase):
    """`run` reads the tasks again from Linear, so an edit made since the last sync reaches the implementer."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"
        self.proj.mkdir()
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        self.payload = lambda body: {"feature": {"id": "ENG-1", "title": "F", "description": ""},
                                     "issues": [issue("ENG-3", "One", block(id="001", files="a.go", test="t").replace("Do it.", body)), issue("ENG-4", "Two", block(id="002", files="b.go", test="t"))]}
        fc.fc_tasks.sync_tasks(self.proj, self.payload("Old goal."))

    def refresh(self, key="lin_test", body="New goal."):
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", key), mock.patch.object(fc.fc_linear, "linear_sync_payload", return_value=self.payload(body)) as read, \
                mock.patch("sys.stdout", io.StringIO()) as out:
            fc.fc_linear.refresh_tasks(self.proj)
        return read, out.getvalue()

    def test_a_body_edited_on_linear_since_the_sync_replaces_the_stored_one_and_is_said(self):
        read, out = self.refresh()
        self.assertIn("New goal.", fc.fc_tasks.load_tasks(self.proj)[0]["body"])
        self.assertIn("001 body changed on Linear since the last sync: refreshed", out)
        self.assertNotIn("002 body", out)

    def test_without_a_linear_key_the_stored_tasks_stand_and_the_run_says_so(self):
        read, out = self.refresh(key=None)
        self.assertFalse(read.called)
        self.assertIn("Old goal.", fc.fc_tasks.load_tasks(self.proj)[0]["body"])
        self.assertIn("no Linear key: run uses the tasks synced at", out)

    def test_a_project_with_no_feature_keeps_its_stored_tasks_and_reads_nothing(self):
        (self.proj / "linear.json").write_text("{}")
        read, out = self.refresh()
        self.assertFalse(read.called)
        self.assertIn("Old goal.", fc.fc_tasks.load_tasks(self.proj)[0]["body"])


class ScopedSyncNames(unittest.TestCase):
    """A story-scoped sync missing parentId, and the sync and change code, name no Linear MCP tool."""
    def test_missing_parent_names_the_field_and_ids(self):
        proj = Path(tempfile.mkdtemp()) / "feat"
        proj.mkdir()
        (proj / "linear.json").write_text('{"feature": "ENG-1", "stories": ["ENG-9"]}')
        issues = [issue("ENG-9", "Story B"), issue("ENG-5", "Task", block(id="003", files="a.go", test="t")),
                  {**issue("ENG-6", "Task"), "parentId": "ENG-9"}, issue("ENG-7", "Other")]
        with self.assertRaises(SystemExit) as e:
            fc.fc_tasks.sync_tasks(proj, {"feature": {"id": "ENG-1", "title": "F"}, "issues": issues})
        text = str(e.exception)
        self.assertIn("(the `parentId` field of each issue)", text)
        self.assertIn("ENG-5", text)
        self.assertIn("ENG-7", text)
        self.assertNotIn("ENG-6", text)  # has its parentId
        self.assertNotIn("get_issue", text)

class HandTasks(unittest.TestCase):
    """A `kind: hand` task is built by hand: run and quick never dispatch it, and the stack, verify and `done` know it like any code task."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        self.proj.mkdir(parents=True)
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        self.tasks = fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            issue("ENG-2", "Remove tests", block(id="001", kind="hand", files="tests/a.py", test="python3 -m unittest tests.a")),
            issue("ENG-3", "Store", block(id="002", files="a.go", protected="a_test.go", test="go test"))]})
        self.by = {m["id"]: m for m in fc.fc_tasks.load_tasks(self.proj)}

    def test_sync_keeps_a_hand_task_with_its_kind_and_the_code_filter_keeps_it_for_the_stack(self):
        self.assertEqual((self.by["001"]["kind"], self.by["001"]["files"], self.by["001"]["protected"]), ("hand", ["tests/a.py"], []))
        self.assertEqual([m["id"] for m in fc.fc_tasks.code_tasks(self.tasks)], ["001", "002"])
        self.assertEqual([m["id"] for m in fc.fc_tasks.runnable(self.tasks)], ["002"])

    def test_run_never_dispatches_a_hand_task_but_the_stack_has_its_branch(self):
        (self.proj / "a_test.go").write_text("")
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_run, "run_tasks") as run, \
                mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(returncode=1, stdout="")):
            fc.fc_run.cmd_run(SimpleNamespace(repo=str(self.proj), project="p", only=None, redo=False, no_stack=False))
            with self.assertRaises(SystemExit) as e:
                fc.fc_run.cmd_run(SimpleNamespace(repo=str(self.proj), project="p", only="001", redo=False, no_stack=False))
        self.assertEqual([m["id"] for m in run.call_args.args[2]], ["002"])
        self.assertIn("no tasks on Linear under this feature carry a compose block", str(e.exception))
        with mock.patch.object(fc.fc_worktree, "task_branches", return_value={"compose/p-001": None, "compose/p-002": None}), \
                mock.patch.object(fc.fc_worktree, "task_prefix", return_value="compose/p-"):
            self.assertEqual(fc.fc_worktree.stack_order(Path("."), self.proj), ["compose/p-001", "compose/p-002"])

    def test_done_finds_a_hand_task_by_its_id(self):
        repo = self.proj.parent.parent
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc_ledger, "record_task", return_value=True), \
                mock.patch.object(fc.fc_linear, "linear_event"), mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(returncode=0, stdout="abc")):
            fc.fc_status.cmd_done(SimpleNamespace(repo=str(repo), project="p", only="001", note="removed by hand"))
        self.assertEqual(json.loads((self.proj / "results" / "001.json").read_text())["status"], "hand_done")


class DesignTasks(unittest.TestCase):
    """A `kind: design` task is built in Figma: it never clones, stacks, or is judged from files."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        self.proj.mkdir(parents=True)
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        design = lambda i, nodes: issue(f"ENG-{i}", f"Page {i}", block(id=f"00{i}", kind="design", nodes=nodes, test="t"))
        self.tasks = fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            design(1, " Checkout/Cart ,Checkout/Pay"), issue("ENG-2", "Store", block(id="002", files="a.go, b.go", protected="a_test.go", figma_nodes="7-21, 9:144", test="go test")),
            design(3, "Home/Hero"), design(4, "Checkout/Summary"), design(5, "Home/Footer")]})
        self.by = {m["id"]: m for m in fc.fc_tasks.load_tasks(self.proj)}

    def test_sync_keeps_kind_and_nodes_and_code_tasks_drops_design(self):
        self.assertEqual((self.by["001"]["kind"], self.by["001"]["nodes"]), ("design", ["Checkout/Cart", "Checkout/Pay"]))
        self.assertEqual({k: self.by["002"][k] for k in ("kind", "nodes", "files", "protected", "figma_nodes")},
                         {"kind": "code", "nodes": [], "files": ["a.go", "b.go"], "protected": ["a_test.go"], "figma_nodes": ["7-21", "9:144"]})
        self.assertEqual([m["id"] for m in fc.fc_tasks.code_tasks(self.tasks + [{"id": "009"}])], ["002", "009"])  # no kind: an older sync, code

    def test_run_stack_and_acceptance_never_see_a_design_task(self):
        (self.proj / "a_test.go").write_text("")
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_run, "run_tasks") as run, \
                mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(returncode=1, stdout="")):
            fc.fc_run.cmd_run(SimpleNamespace(repo=str(self.proj), project="p", only=None, redo=False, no_stack=False))
            with self.assertRaises(SystemExit) as e:
                fc.fc_run.cmd_run(SimpleNamespace(repo=str(self.proj), project="p", only="001,003", redo=False, no_stack=False))
        self.assertEqual([m["id"] for m in run.call_args.args[2]], ["002"])
        self.assertIn("no tasks on Linear under this feature carry a compose block", str(e.exception))
        with mock.patch.object(fc.fc_worktree, "task_branches", return_value={"compose/p-001": None, "compose/p-002": None}), \
                mock.patch.object(fc.fc_worktree, "task_prefix", return_value="compose/p-"):
            self.assertEqual(fc.fc_worktree.stack_order(Path("."), self.proj), ["compose/p-002"])
        reply = {"model": "m", "answers": {"c0": {"choice": "MET", "probabilities": {"MET": 0.9}}}}
        with mock.patch.object(fc.fc_config, "ENV_FILE", self.proj / "none.env"), mock.patch.object(fc.fc_models, "jev_ask", return_value=reply) as ask:
            fc.fc_accept.jev_acceptance({}, self.proj, "diff")
        self.assertEqual([c.args[3]["c0"]["instructions"]["task"] for c in ask.call_args_list], ["002"])

    def test_acceptance_passes_only_when_every_criterion_is_met(self):
        for choice, ok in (("MET", True), ("UNCLEAR", False), ("NOT_MET", False)):
            reply = {"model": "m", "answers": {"c0": {"choice": choice, "probabilities": {choice: 0.1}}}}
            with mock.patch.object(fc.fc_config, "ENV_FILE", self.proj / "none.env"), mock.patch.object(fc.fc_models, "jev_ask", return_value=reply), \
                    mock.patch.object(fc.fc_models, "fast_json", return_value={"confirmed": False, "evidence": ""}):
                self.assertEqual(fc.fc_accept.jev_acceptance({}, self.proj, "diff")["pass"], ok, choice)

    def test_acceptance_counts_a_criterion_the_tasks_passing_test_proves_unless_a_violation_is_confirmed(self):
        """Jev's UNCLEAR or unconfirmed NOT_MET alone is a note when the task's test passed at the tip and is on the ledger."""
        cid = fc_ledger.criterion_ids(fc.fc_tasks.load_tasks(self.proj)[1])[0]
        row = lambda eid, phase, result, ids=None: {"event_id": eid, "task_ids": ["002"], "criterion_id": [cid] if ids is None else ids, "phase": phase, "result": result}
        final, scoped_fail = row("f1", "final", "pass"), row("s1", "scoped", "fail")
        cases = [("passing final row", "UNCLEAR", False, [final], True, "f1"), ("scoped pass after final", "UNCLEAR", False, [scoped_fail, row("s2", "scoped", "pass")], True, "s2"),
                 ("row for all criteria", "NOT_MET", False, [row("f2", "final", "pass", [])], True, "f2"), ("no rows", "UNCLEAR", False, [], False, None),
                 ("test failed since", "UNCLEAR", False, [final, scoped_fail], False, None), ("other criterion", "UNCLEAR", False, [row("f3", "final", "pass", ["002.2:x"])], False, None),
                 ("checks row is not the task's test", "UNCLEAR", False, [row("c1", "checks", "pass", [])], False, None),
                 ("violation confirmed", "NOT_MET", True, [final], False, None)]
        for name, choice, confirmed, rows, ok, by in cases:
            reply = {"model": "m", "answers": {"c0": {"choice": choice, "probabilities": {choice: 0.9}}}}
            with mock.patch.object(fc.fc_config, "ENV_FILE", self.proj / "none.env"), mock.patch.object(fc.fc_models, "jev_ask", return_value=reply), \
                    mock.patch.object(fc.fc_models, "fast_json", return_value={"confirmed": confirmed, "evidence": "x"}), \
                    mock.patch.object(fc_evidence, "task_rows", return_value=rows), mock.patch.object(fc_ledger, "counted", side_effect=lambda *a: a[-1]):
                res = fc.fc_accept.jev_acceptance({}, self.proj, "diff")
            self.assertEqual((res["pass"], res["criteria"][0].get("proven_by"), len(res["unclear"]), len(res["failed"])), (ok, by, 0 if ok or confirmed else 1, int(confirmed)), name)

    def test_acceptance_re_asks_a_refuted_not_met_once_and_a_confirm_error_never(self):
        first = {"model": "m", "answers": {"c0": {"choice": "NOT_MET", "probabilities": {"NOT_MET": 0.9}}}}
        again = lambda c: {"model": "m", "answers": {"c0": {"choice": c, "probabilities": {c: 0.9}}}}
        for second, confirm, ok, calls in (("MET", {"confirmed": False, "evidence": "satisfied"}, True, 2), ("NOT_MET", {"confirmed": False, "evidence": "satisfied"}, False, 2),
                                           ("MET", "nope", False, 1)):
            with mock.patch.object(fc.fc_config, "ENV_FILE", self.proj / "none.env"), mock.patch.object(fc.fc_models, "jev_ask", side_effect=[first, again(second)]) as ask, \
                    mock.patch.object(fc.fc_models, "fast_json", return_value=confirm):
                res = fc.fc_accept.jev_acceptance({}, self.proj, "diff")
            self.assertEqual((res["pass"], ask.call_count), (ok, calls), (second, confirm))
        self.assertEqual((res["criteria"][0]["choice"]), "NOT_MET")

    def test_acceptance_state_leaves_out_lockfiles_and_stays_under_the_judge_token_limit(self):
        """TypeSafe answers HTTP 400 max_tokens_exceeded for a state of about 35k characters; a lockfile is noise, not code."""
        repo = Path(tempfile.mkdtemp())
        (repo / "bun.lock").write_text("lock\n" * 20000)
        (repo / "a.go").write_text("package a\n" * 6000)
        (repo / "b.go").write_text("package b\n")
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            issue("ENG-2", "Store", block(id="002", files="a.go, bun.lock", protected="b.go", test="go test"))]})
        reply = {"model": "m", "answers": {"c0": {"choice": "MET", "probabilities": {"MET": 0.9}}}}
        with mock.patch.object(fc.fc_config, "ENV_FILE", self.proj / "none.env"), mock.patch.object(fc.fc_models, "jev_ask", return_value=reply) as ask:
            fc.fc_accept.jev_acceptance({}, self.proj, "diff", repo=repo)
        code = ask.call_args.args[2]["code"]
        self.assertNotIn("bun.lock", code)
        self.assertLessEqual(len(code), fc.fc_accept.JEV_CODE_CAP)

    def test_a_rejected_request_reports_the_providers_error_body(self):
        err = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"detail":{"error_type":"max_tokens_exceeded"}}'))
        self.addCleanup(err.close)
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as e:
                fc.fc_models.http_json(mock.Mock(), "typesafe (Jev acceptance)")
        self.assertEqual(str(e.exception), 'typesafe (Jev acceptance): HTTP 400 Bad Request: {"detail":{"error_type":"max_tokens_exceeded"}}')
        self.assertTrue(err.fp.closed)

    def test_jev_refusing_too_many_tokens_raises_too_big(self):
        err = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"detail":{"error_type":"max_tokens_exceeded"}}'))
        self.addCleanup(err.close)
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(fc.fc_models.TooBig) as e:
                fc.fc_models.jev_ask({"TYPESAFE_API_KEY": "k"}, {}, {"s": "x"}, {}, "Jev readiness")
        self.assertIn("max_tokens_exceeded", str(e.exception))

    def test_a_refused_payment_closes_the_error_and_still_raises_payment_required(self):
        for code, reason, body in ((402, "Payment Required", b""), (403, "Forbidden", b"insufficient credits on this key")):
            err = urllib.error.HTTPError("u", code, reason, {}, io.BytesIO(body))
            with mock.patch.object(urllib.request, "urlopen", side_effect=err):
                with self.assertRaises(fc.fc_models.PaymentRequired) as e:
                    fc.fc_models.http_json(mock.Mock(), "openrouter")
            self.assertEqual((e.exception.provider, e.exception.message), ("openrouter", body.decode() or f"HTTP {code}"))
            self.assertTrue(err.fp.closed, code)

    def test_acceptance_with_no_criteria_fails_rather_than_passes(self):
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [issue("ENG-2", "Store", "```compose\nid: 002\nfiles: a.go\ntest: go test\n```\n")]})
        with mock.patch.object(fc.fc_config, "ENV_FILE", self.proj / "none.env"), mock.patch.object(fc.fc_models, "jev_ask") as ask:
            res = fc.fc_accept.jev_acceptance({}, self.proj, "diff")
        self.assertEqual((res["pass"], res["note"], ask.called), (False, "no acceptance criteria to judge", False))

    def test_acceptance_confirm_unwraps_a_list_and_reports_a_failed_confirm_as_an_error(self):
        reply = {"model": "m", "answers": {"c0": {"choice": "NOT_MET", "probabilities": {"NOT_MET": 0.9}}}}
        for confirm, failed, error in (([{"confirmed": True, "evidence": "x"}], 1, None), ("nope", 0, True)):
            with mock.patch.object(fc.fc_config, "ENV_FILE", self.proj / "none.env"), mock.patch.object(fc.fc_models, "jev_ask", return_value=reply), \
                    mock.patch.object(fc.fc_models, "fast_json", return_value=confirm):
                res = fc.fc_accept.jev_acceptance({}, self.proj, "diff")
            self.assertEqual((len(res["failed"]), "confirm call failed" in res.get("error", "") if error else "error" not in res, res["pass"]), (failed, True, False))

    def test_tasks_lists_nodes_and_warns_once_per_shared_open_page(self):
        out = io.StringIO()
        state = lambda repo, proj, m, head, facts=None: {"state": "passed" if m["id"] == "003" else "pending"}  # a design task's finish
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_status, "task_state", side_effect=state), \
                mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(returncode=0, stdout="abc\n")), mock.patch("sys.stdout", out):
            fc.fc_spec.cmd_tasks(SimpleNamespace(repo=str(self.proj), project="p"))
        text = out.getvalue()
        self.assertIn("Checkout/Cart, Checkout/Pay", text)
        self.assertIn("WARNING two design tasks own the same page (one page per task):", text)
        self.assertIn("Checkout: 001, 004\n", text)  # one id per task, though 001 owns two nodes there
        self.assertNotIn("Home:", text)  # 003 passed, so only 005 is open on Home

    def test_a_passed_design_task_stays_passed(self):
        for tid in ("001", "002"):
            (self.proj / "results" / f"{tid}.json").write_text('{"status": "passed"}')
        with mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(returncode=1, stdout="")):
            rows = [fc.fc_status.task_state(Path("."), self.proj, self.by[t], "HEAD")["state"] for t in ("001", "002")]
        self.assertEqual(rows, ["passed", "merged"])


class ReadyRule(unittest.TestCase):
    @staticmethod
    def node(ident, state, *blockers, labels=()):
        rels = [{"type": t, "issue": {"identifier": i, "state": {"name": s}, "labels": {"nodes": [{"name": n} for n in ls]}}} for t, i, s, ls in blockers]
        return {"identifier": ident, "title": ident, "state": {"name": state}, "labels": {"nodes": [{"name": n} for n in labels]},
                "inverseRelations": {"nodes": rels}}

    def ready(self, *nodes):
        return fc.fc_linear.ready_ids(list(nodes))

    def test_open_states_without_blockers_are_ready_in_input_order(self):
        nodes = [self.node(i, s) for i, s in (("NEB-9", "Todo"), ("NEB-3", "backlog"), ("NEB-5", "In Progress"), ("NEB-1", "TODO"),
                                              ("NEB-2", "In Review"), ("NEB-4", "Done"), ("NEB-6", "Canceled"), ("NEB-7", "BACKLOG"))]
        self.assertEqual(self.ready(*nodes), ["NEB-9", "NEB-3", "NEB-1", "NEB-7"])
        self.assertEqual(self.ready(), [])

    def test_a_ticket_labeled_needs_shaping_is_never_ready(self):
        self.assertEqual(self.ready(self.node("NEB-2", "Todo", labels=("Feature", "Needs-Shaping")), self.node("NEB-3", "Todo")), ["NEB-3"])

    def test_ready_only_when_every_blocker_is_finished(self):
        done, canceled = ("blocks", "NEB-1", "Done", ()), ("blocks", "NEB-2", "Canceled", ())
        self.assertEqual(self.ready(self.node("NEB-3", "Todo", done, canceled)), ["NEB-3"])
        for open_ in ("Todo", "Backlog", "In Progress", "In Review"):
            self.assertEqual(self.ready(self.node("NEB-3", "Todo", done, ("blocks", "NEB-4", open_, ()))), [], open_)

    def test_a_blocker_in_review_is_finished_only_with_the_design_label(self):
        design = lambda state, *ls: ("blocks", "NEB-1", state, ls)
        self.assertEqual(self.ready(self.node("NEB-2", "Todo", design("In Review", "Design")),
                                    self.node("NEB-3", "Backlog", design("In Review", "bug", "design")),
                                    self.node("NEB-4", "Todo", design("In Review", "Bug")),
                                    self.node("NEB-5", "Todo", design("In Progress", "Design")),
                                    self.node("NEB-6", "Todo", design("In Review"), labels=("Design",))), ["NEB-2", "NEB-3"])

    def test_a_sibling_in_review_under_the_same_parent_frees_it(self):
        # NEB-2184: story 1 was verified (In Review, waiting for the merge) and story 2, blocked by it in the same feature, never went Ready
        sib = lambda state, parent="NEB-1": {"type": "blocks", "issue": {"identifier": "NEB-2", "state": {"name": state}, "labels": {"nodes": []},
                                                                           "parent": {"identifier": parent} if parent else None}}
        node = lambda *rels: {**self.node("NEB-3", "Todo"), "inverseRelations": {"nodes": list(rels)}}
        self.assertEqual(fc.fc_linear.ready_ids([node(sib("In Review"))], "NEB-1"), ["NEB-3"])
        self.assertEqual(fc.fc_linear.ready_ids([node(sib("In Review"))]), [])  # no parent given: only finished blockers free it
        for held in (sib("In Progress"), sib("Todo"), sib("In Review", "NEB-9"), sib("In Review", None)):
            self.assertEqual(fc.fc_linear.ready_ids([node(held)], "NEB-1"), [], held)

    def test_a_task_held_by_a_sibling_storys_task_in_review_is_freed(self):
        held = lambda state, feature: {**self.node("NEB-3", "Todo"), "children": {"nodes": [
            {"identifier": "NEB-31", "state": {"name": "Todo"}, "inverseRelations": {"nodes": [{"type": "blocks", "issue": {
                "identifier": "NEB-21", "state": {"name": state}, "labels": {"nodes": []}, "parent": {"identifier": "NEB-2", "parent": {"identifier": feature}}}}]}}]}}
        self.assertEqual(fc.fc_linear.task_holds(held("In Review", "NEB-1"), "NEB-1"), [])
        self.assertEqual(fc.fc_linear.task_holds(held("In Review", "NEB-9"), "NEB-1"), ["NEB-2"])
        self.assertEqual(fc.fc_linear.task_holds(held("In Progress", "NEB-1"), "NEB-1"), ["NEB-2"])
        self.assertEqual(fc.fc_linear.task_holds(held("In Review", "NEB-1")), ["NEB-2"])

    def test_a_non_open_issue_is_never_ready_even_with_finished_blockers(self):
        done = ("blocks", "NEB-1", "Done", ())
        self.assertEqual(self.ready(*(self.node("NEB-2", s, done) for s in ("In Progress", "In Review", "Done", "Canceled"))), [])

    def test_relations_of_other_types_are_ignored(self):
        rels = [(t, "NEB-1", "In Progress", ()) for t in ("related", "duplicate", "similar")]
        self.assertEqual(self.ready(self.node("NEB-2", "Todo", *rels), self.node("NEB-3", "Todo", rels[0], ("blocks", "NEB-4", "Todo", ()))),
                         ["NEB-2"])


class MarkReady(unittest.TestCase):
    UPDATE = "mutation($id: String!, $labels: [String!]!) { issueUpdate(id: $id, input: {labelIds: $labels}) { success } }"

    def setUp(self):
        self.reads, self.updates, self.said, self.parent, self.feature_rels = [], [], [], None, []
        self.team = [{"id": "l-bug", "name": "Bug"}, {"id": "l-ready", "name": "Ready"}]
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", "k"), (fc.fc_linear, "linear_gql", self.gql), (fc.fc_core, "say", self.said.append)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        if query == fc.fc_linear.LINEAR_READY_TREE:
            self.reads.append(variables)
            return {"issue": {"identifier": getattr(self, "ident", None), "team": {"labels": {"nodes": self.team}}, "children": {"nodes": self.children}, "parent": self.parent,
                              "inverseRelations": {"nodes": self.feature_rels}}}
        self.assertEqual(query, self.UPDATE)
        self.updates.append(variables)
        return {"issueUpdate": {"success": True}}

    @staticmethod
    def node(ident, state, *labels, blocked=False, tasks=("Todo",), desc=""):
        rels = [{"type": "blocks", "issue": {"identifier": "ENG-99", "state": {"name": "Todo"}, "labels": {"nodes": []}}}] if blocked else []
        return {"id": "id-" + ident, "identifier": ident, "title": "T " + ident, "state": {"name": state},
                "labels": {"nodes": [{"id": "l-" + n.lower(), "name": n} for n in labels]}, "inverseRelations": {"nodes": rels},
                "children": {"nodes": [{"state": {"name": t}} for t in tasks]}, "description": desc}

    @staticmethod
    def rel(kind, ident, state):
        return {"type": kind, "issue": {"identifier": ident, "state": {"name": state}, "labels": {"nodes": []}}}

    def mark(self, *expected):
        self.assertEqual(fc.fc_linear.linear_mark_ready("ENG-1"), [{"id": i, "title": "T " + i} for i in expected])
        self.assertEqual(self.reads, [{"id": "ENG-1"}])

    def test_adds_removes_and_leaves_the_label_keeping_other_labels(self):
        self.children = [self.node("ENG-2", "Todo", "Bug", "Area"), self.node("ENG-3", "Todo", "Bug", "Ready", "Area", blocked=True),
                         self.node("ENG-4", "Todo", "Ready"), self.node("ENG-5", "Done", "Bug"), self.node("ENG-6", "Backlog")]
        self.mark("ENG-2", "ENG-4", "ENG-6")
        self.assertCountEqual(self.updates, [{"id": "id-ENG-2", "labels": ["l-bug", "l-area", "l-ready"]},
                                             {"id": "id-ENG-3", "labels": ["l-bug", "l-area"]}, {"id": "id-ENG-6", "labels": ["l-ready"]}])
        self.assertCountEqual(self.said, ["linear ENG-2 -> Ready", "linear ENG-3 -> not Ready", "linear ENG-6 -> Ready"])

    def test_a_story_whose_tasks_wait_on_another_storys_tasks_is_not_ready(self):
        # NEB-2188: every task of the last story waited on tasks of earlier stories, with no story-level block, and it went Ready
        task = lambda ident, *rels: {"identifier": ident, "state": {"name": "Todo"}, "inverseRelations": {"nodes": list(rels)}}
        outside = lambda ident, state, story: {"type": "blocks", "issue": {"identifier": ident, "state": {"name": state}, "labels": {"nodes": []},
                                                                          "parent": {"identifier": story}}}
        held, inside, done = self.node("ENG-2", "Todo"), self.node("ENG-3", "Todo"), self.node("ENG-4", "Todo")
        held["children"]["nodes"] = [task("ENG-21", outside("ENG-31", "In Progress", "ENG-3")), task("ENG-22")]
        inside["children"]["nodes"] = [task("ENG-31"), task("ENG-32", outside("ENG-31", "Todo", "ENG-3"))]  # a block inside the story holds nothing
        done["children"]["nodes"] = [task("ENG-41", outside("ENG-31", "Done", "ENG-3"), outside("ENG-91", "Canceled", "ENG-9"))]
        self.children = [held, inside, done]
        self.mark("ENG-3", "ENG-4")
        self.assertEqual(fc.fc_linear.task_holds(held), ["ENG-3"])

    def test_a_story_blocked_by_a_verified_sibling_goes_ready(self):
        self.ident = "ENG-1"
        sibling = {"type": "blocks", "issue": {"identifier": "ENG-2", "state": {"name": "In Review"}, "labels": {"nodes": []}, "parent": {"identifier": "ENG-1"}}}
        later = self.node("ENG-3", "Todo")
        later["inverseRelations"]["nodes"] = [sibling]
        self.children = [self.node("ENG-2", "In Review"), later]
        self.mark("ENG-3")

    def test_a_child_labeled_needs_shaping_loses_ready_and_keeps_the_label(self):
        self.team.append({"id": "l-needs-shaping", "name": "needs-shaping"})
        self.children = [self.node("ENG-2", "Todo", "Ready", "needs-shaping")]
        self.mark()
        self.assertEqual(self.updates, [{"id": "id-ENG-2", "labels": ["l-needs-shaping"]}])
        self.assertEqual(self.said, ["linear ENG-2 -> not Ready"])

    def test_a_feature_that_waits_on_an_unbuilt_feature_has_no_ready_story(self):
        self.children = [self.node("ENG-2", "Todo"), self.node("ENG-3", "Todo", "Ready")]
        self.feature_rels = [self.rel("blocks", "ENG-90", "In Progress"), self.rel("related", "ENG-91", "Todo")]
        self.mark()
        self.assertEqual(self.updates, [{"id": "id-ENG-3", "labels": []}])

    def test_a_verified_base_feature_lets_its_dependents_start(self):
        self.children = [self.node("ENG-2", "Todo")]
        self.feature_rels = [self.rel("blocks", "ENG-90", "In Review")]
        self.mark("ENG-2")

    def test_an_issue_with_open_questions_is_not_ready(self):
        asked = "## Open questions\n\n* Where does ENG-2 stop? (readiness check, 2026-10-26)\n"
        self.children = [self.node("ENG-2", "Todo", "Ready", desc=asked), self.node("ENG-3", "Todo", desc="## Open questions\n\nNone.\n")]
        self.mark("ENG-3")
        self.assertCountEqual(self.updates, [{"id": "id-ENG-2", "labels": []}, {"id": "id-ENG-3", "labels": ["l-ready"]}])
        self.assertRegex(fc.fc_linear.LINEAR_READY_TREE, r"fragment ReadyNode on Issue \{[^}]*\bdescription\b")

    def test_a_story_without_a_live_task_is_not_ready(self):
        self.children = [self.node("ENG-2", "Todo", tasks=()), self.node("ENG-3", "Backlog", "Ready", tasks=("Canceled", "canceled")),
                         self.node("ENG-4", "Todo", tasks=("Canceled", "Todo"))]
        self.mark("ENG-4")
        self.assertCountEqual(self.updates, [{"id": "id-ENG-3", "labels": []}, {"id": "id-ENG-4", "labels": ["l-ready"]}])

    def test_the_epic_children_follow_the_feature_children(self):
        self.team = [{"id": "l-ready", "name": "READY"}]
        self.children = [self.node("ENG-9", "Todo")]
        self.parent = {"children": {"nodes": [self.node("ENG-1", "Todo", "READY", tasks=()), self.node("ENG-5", "In Progress", "READY"), self.node("ENG-3", "Todo", tasks=())]}}  # features need no story to be ready
        self.mark("ENG-9", "ENG-1", "ENG-3")
        self.assertCountEqual(self.updates, [{"id": "id-ENG-9", "labels": ["l-ready"]}, {"id": "id-ENG-5", "labels": []},
                                             {"id": "id-ENG-3", "labels": ["l-ready"]}])
        self.assertCountEqual(self.said, ["linear ENG-9 -> Ready", "linear ENG-5 -> not Ready", "linear ENG-3 -> Ready"])

    def test_no_ready_label_on_the_team_raises_before_any_update(self):
        self.team = [{"id": "l-bug", "name": "Bug"}, {"id": "l-not", "name": "Not Ready"}]
        self.children = [self.node("ENG-2", "Todo"), self.node("ENG-3", "Done", "Ready")]
        with self.assertRaises(RuntimeError) as e:
            fc.fc_linear.linear_mark_ready("ENG-1")
        self.assertEqual(str(e.exception), "team has no label 'Ready'; compose linear labels --create Ready makes it")
        self.assertEqual(self.updates, [])


class ReadyCommand(unittest.TestCase):
    ITEMS = [{"id": "ENG-3", "title": "Login form"}, {"id": "ENG-2", "title": "Sign up"}]

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-7"}))
        self.stored, self.mark = self.proj / "results" / "ready.json", mock.Mock(return_value=self.ITEMS)
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", "k"), (fc.fc_config, "WORKSPACE", "acme"), (fc.fc_linear, "linear_mark_ready", self.mark),
                                 (fc.fc_linear, "linear_gql", mock.Mock(side_effect=AssertionError("Linear called"))),
                                 (fc.fc_core, "project_dir", mock.Mock(return_value=self.proj)), (fc.fc_linear, "linear_plan", mock.Mock())):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def ready(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            fc.fc_linear.cmd_ready(SimpleNamespace(repo=".", project="p"))
        return out.getvalue().splitlines()

    def test_the_line_lists_each_item_in_order_or_says_nothing(self):
        line = fc.fc_linear.ready_line
        self.assertEqual(line([]), "can start now: nothing")
        self.assertEqual(line([{"id": "NEB-1", "title": "A"}]), "can start now: NEB-1 A")
        self.assertEqual(line([{"id": "NEB-1", "title": "A"}, {"id": "NEB-2", "title": "B"}]), "can start now: NEB-1 A, NEB-2 B")
        self.assertEqual(line([{"id": "NEB-9", "title": "Sign up form"}, {"id": "NEB-2", "title": "B"}]), "can start now: NEB-9 Sign up form, NEB-2 B")

    def test_marks_the_project_feature_replaces_the_stored_list_and_prints_the_line(self):
        self.stored.write_text(json.dumps([{"id": "ENG-1", "title": "stale"}]))
        self.assertIn("can start now: ENG-3 Login form, ENG-2 Sign up", self.ready())
        self.mark.assert_called_once_with("ENG-7")
        self.assertEqual(json.loads(self.stored.read_text()), self.ITEMS)
        self.mark.return_value = []
        self.assertIn("can start now: nothing", self.ready())
        self.assertEqual(json.loads(self.stored.read_text()), [])

    def test_without_a_key_exits_before_marking(self):
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", None), self.assertRaises(SystemExit) as e:
            self.ready()
        self.assertEqual(e.exception.code, "ready needs a Linear key for workspace 'acme': pbpaste | compose workspace key acme --linear")
        self.mark.assert_not_called()
        self.assertFalse(self.stored.exists())

    def test_a_label_error_exits_with_its_message_and_stores_nothing(self):
        self.mark.side_effect = RuntimeError("team has no label 'Ready'")
        with self.assertRaises(SystemExit) as e:
            self.ready()
        self.assertEqual(e.exception.code, "ready: team has no label 'Ready'")
        self.assertFalse(self.stored.exists())

class ReadyRefresh(unittest.TestCase):
    def setUp(self):
        self.proj, self.said, self.items = Path(tempfile.mkdtemp()) / "p", [], [{"id": "ENG-3", "title": "Login form"}]
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
        self.mark = mock.Mock(return_value=self.items)
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", "k"), (fc.fc_config, "WORKSPACE", "work"), (fc.fc_core, "say", self.said.append),
                                 (fc.fc_linear, "linear_mark_ready", self.mark), (fc.fc_linear, "linear_gql", mock.Mock(side_effect=AssertionError("Linear called"))),
                                 (fc.fc_linear, "linear_plan", mock.Mock())):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        fc.fc_linear._linear_warned.clear()

    def test_marks_and_stores_skips_without_a_feature_or_key_and_says_errors(self):
        fc.fc_linear.refresh_ready(self.proj)
        fc.fc_linear.refresh_ready(self.proj, "ENG-9")
        self.assertEqual(self.mark.mock_calls, [mock.call("ENG-1"), mock.call("ENG-9")])
        self.assertEqual((self.proj / "results" / "ready.json").read_text(), json.dumps(self.items, indent=2))
        (self.proj / "results").rename(self.proj / "old")
        fc.fc_linear.refresh_ready(self.proj)  # no results directory: marks, writes nothing
        self.assertEqual((self.mark.call_count, self.said, (self.proj / "results").exists()), (3, [], False))
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", None):
            (self.proj / "linear.json").write_text("{}"); fc.fc_linear.refresh_ready(self.proj)
            (self.proj / "linear.json").unlink(); fc.fc_linear.refresh_ready(self.proj)
            self.assertEqual(self.said, [])  # no feature: not even the no-key line
            fc.fc_linear.refresh_ready(self.proj, "ENG-1")
        self.assertEqual((self.mark.call_count, len(self.said)), (3, 1)); self.assertTrue(self.said.pop().startswith("linear skipped: "))
        self.mark.side_effect = RuntimeError("x" * 300)
        fc.fc_linear.refresh_ready(self.proj, "ENG-1")
        self.assertEqual(self.said, ["linear ready: " + "x" * 200])

    def test_roll_up_reconcile_and_sync_from_linear_refresh_once(self):
        gql = lambda q, v: {"issue": {"id": v["id"], "identifier": v["id"], "state": {"name": "Todo", "type": "unstarted"}, "team": {"states": {"nodes": []}},
                                      "parent": {"identifier": "ENG-1"} if v["id"] == "ENG-2" else None, "children": {"nodes": []}}}
        for run in (lambda: fc.fc_linear.linear_roll_up(self.proj, "ENG-2"), lambda: fc.fc_linear.linear_roll_up(self.proj, "ENG-5"),
                    lambda: fc.fc_linear.linear_reconcile(self.proj, verified=True)):  # exits at the feature, past the top, and through reconcile
            with mock.patch.object(fc.fc_linear, "linear_gql", gql), mock.patch.object(fc.fc_linear, "refresh_ready") as r:
                run()
            r.assert_called_once_with(self.proj)
        data = {"feature": {"id": "ENG-1", "title": "F"}, "issues": [issue("ENG-3", "A", block(id="001", test="t"))]}
        for stdin in (True, False):
            calls = mock.Mock()
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))), \
                    mock.patch.object(fc.fc_linear, "linear_sync_payload", return_value=data), mock.patch.object(fc.fc_linear, "linear_feature_after"), \
                    mock.patch("builtins.print", calls.print), mock.patch.object(fc.fc_linear, "refresh_ready", calls.refresh_ready):
                fc.fc_linear.cmd_sync(SimpleNamespace(stdin=stdin, repo=str(self.proj)))
            self.assertEqual(calls.refresh_ready.mock_calls, [] if stdin else [mock.call(self.proj)])
            self.assertEqual(calls.mock_calls[-1][0], "print" if stdin else "refresh_ready")  # after the task list

    def test_clean_refreshes_the_linked_feature_after_moving_merged_work(self):
        import subprocess
        r = Path(tempfile.mkdtemp()).resolve()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        g = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True, env=env)
        g("init", "-q", "-b", "main"); g("commit", "-q", "--allow-empty", "-m", "init"); g("checkout", "-q", "-b", "compose/p")
        proj = r / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        (proj / "verify.json").write_text("{}"); (proj / "linear.json").write_text(json.dumps({"feature": "ENG-7"}))
        calls = mock.Mock()
        with mock.patch.object(fc.fc_linear, "linear_merged", calls.linear_merged), mock.patch.object(fc.fc_linear, "refresh_ready", calls.refresh_ready), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch.dict(os.environ, {"COMPOSE_NO_TREEHOUSE": "1"}):
            fc.fc_clean.clean_project(r, "p", r, proj, explicit=True)
        self.assertEqual(calls.mock_calls[-2:], [mock.call.linear_merged("ENG-7", [], False, scope=None), mock.call.refresh_ready(proj, "ENG-7")])


class DeliveryPlan(unittest.TestCase):
    flat = staticmethod(lambda v: [x for y in (v.values() if isinstance(v, dict) else v) for x in DeliveryPlan.flat(y)] if isinstance(v, (dict, list)) else [v])

    def setUp(self):
        self.sent, self.sections = [], mock.Mock(return_value={"## Start today": "x"})
        for mod, name, value in ((fc.fc_linear, "linear_gql", self.gql), (fc_plan, "plan_sections", self.sections)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        self.sent.append((next((k for k in ("documentCreate", "issueUpdate", "documentUpdate") if k in query), query), variables))
        doc = {"id": "d-new", "title": "NEB-7: delivery plan", "url": "https://linear.app/x/document/d-new", "content": fc_plan.plan_skeleton()}
        return {"issue": self.tree} if query == fc.fc_linear.LINEAR_PLAN_TREE else {self.sent[-1][0]: {"success": True, "document": doc}}

    writes = lambda self, name: [self.flat(v) for k, v in self.sent if k == name]

    @staticmethod
    def node(ident, state="Todo", labels=(), rels=(), kids=(), docs=(), parent=None):
        return {"id": "id-" + ident, "identifier": ident, "title": "T " + ident, "url": "u/" + ident, "description": "## Goal\nG.\n", "state": {"name": state},
                "labels": {"nodes": [{"id": "l-" + n, "name": n} for n in labels]}, "children": {"nodes": list(kids)}, "parent": parent,
                "inverseRelations": {"nodes": [{"type": t, "issue": {"identifier": i, "state": {"name": s}, "labels": {"nodes": [{"name": n} for n in ls]}}}
                                               for t, i, s, ls in rels]}, "project": {"id": "p-" + ident, "documents": {"nodes": list(docs)}}}

    def test_a_feature_map_gets_its_plan_created_and_linked_once(self):
        other = {"id": "d-x", "title": "NEB-1: delivery plan", "content": "old", "url": "u/d-x"}  # the parent is no epic, so not the map
        self.tree = self.node("NEB-7", labels=["Feature"], docs=[other], parent=self.node("NEB-1", labels=["Initiative"]), kids=[
            self.node("NEB-8", "Todo", ["Story"], [("blocks", "NEB-9", "Done", ["Design"]), ("related", "NEB-5", "Todo", [])], [self.node("NEB-20")]),
            self.node("NEB-9", "In Review", ["Design"])])
        fc.fc_linear.linear_plan("NEB-7")
        self.assertEqual([self.sent[0]] + [k for k, _ in self.sent[1:]], [(fc.fc_linear.LINEAR_PLAN_TREE, {"id": "NEB-7"}), "documentCreate", "issueUpdate", "documentUpdate"])
        story = lambda i, s, ls, b: {"id": i, "title": "T " + i, "url": "u/" + i, "state": s, "labels": ls, "blockers": b}
        self.sections.assert_called_once_with([{"id": "NEB-7", "title": "T NEB-7", "url": "u/NEB-7", "stories": [
            story("NEB-8", "Todo", ["Story"], [{"id": "NEB-9", "state": "Done", "labels": ["Design"]}]), story("NEB-9", "In Review", ["Design"], [])]}])
        skeleton, title = fc_plan.plan_skeleton(), "NEB-7: delivery plan"
        self.assertTrue({skeleton, title, "p-NEB-7"} <= set(self.writes("documentCreate")[0]))
        self.assertIn(fc_plan.with_plan_link("## Goal\nG.\n", title, "https://linear.app/x/document/d-new"), self.writes("issueUpdate")[0])
        self.assertIn(fc_plan.replace_section(skeleton, "## Start today", "x"), self.writes("documentUpdate")[0])

    def test_an_epic_map_has_a_lane_per_feature_and_only_its_data_sections_are_rewritten(self):
        old = "\n\n".join(f"{h}\n\nold {h[3:]}." for h in fc_plan.PLAN_SECTIONS) + "\n"; doc = {"id": "d-1", "title": "NEB-1: delivery plan", "content": old, "url": "u/d-1"}
        self.tree = self.node("NEB-7", labels=["Feature"], kids=[self.node("NEB-8", "Backlog")], parent=self.node("NEB-1", labels=["epic"], docs=[doc], kids=[
            self.node("NEB-7", labels=["Feature"], kids=[self.node("NEB-8", "Backlog")]), self.node("NEB-6", labels=["Feature"]),
            self.node("NEB-5", labels=["Design"], kids=[self.node("NEB-30")])]))
        self.sections.return_value = {"## Start today": "1. new", "## Lanes": "lanes"}
        fc.fc_linear.linear_plan("NEB-7")
        self.assertEqual([(l["id"], l["url"], [s["id"] for s in l["stories"]]) for l in self.sections.call_args.args[0]], [("NEB-7", "u/NEB-7", ["NEB-8"]), ("NEB-6", "u/NEB-6", [])])
        new = fc_plan.replace_section(fc_plan.replace_section(old, "## Start today", "1. new"), "## Lanes", "lanes")
        self.assertEqual([k for k, _ in self.sent[1:]], ["documentUpdate"]); self.assertIn(new, self.writes("documentUpdate")[0])
        self.assertTrue(all(f"{h}\n\nold {h[3:]}." in new for h in fc_plan.PLAN_SECTIONS[2:]))
        doc["content"] = new; fc.fc_linear.linear_plan("NEB-7")  # nothing changed: no write
        self.assertEqual(len(self.writes("documentUpdate")), 1)

    def test_ready_and_every_refresh_plan_after_storing_the_list_and_a_plan_failure_keeps_it(self):
        proj = Path(tempfile.mkdtemp()) / "p"; (proj / "results").mkdir(parents=True); stored = proj / "results" / "ready.json"
        (proj / "linear.json").write_text(json.dumps({"feature": "ENG-7"})); order, said, out = [], [], io.StringIO()
        plan = mock.Mock(side_effect=lambda f: order.append(("plan", f, stored.exists(), out.getvalue().splitlines())))
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", "k"), (fc.fc_core, "project_dir", mock.Mock(return_value=proj)), (fc.fc_core, "say", said.append), (sys, "stdout", out),
                                 (fc.fc_linear, "linear_mark_ready", lambda f: order.append(("mark", f)) or [{"id": "ENG-3", "title": "A"}]), (fc.fc_linear, "linear_plan", plan)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        ready = lambda: fc.fc_linear.cmd_ready(SimpleNamespace(repo=".", project="p"))
        ready(); stored.unlink(); fc.fc_linear.refresh_ready(proj)
        self.assertEqual(order, [("mark", "ENG-7"), ("plan", "ENG-7", True, ["can start now: ENG-3 A"])] * 2)
        plan.side_effect = RuntimeError("x"); stored.unlink()
        with self.assertRaises(SystemExit) as e:
            ready()
        self.assertEqual((e.exception.code, stored.exists()), ("ready: x", True))
        plan.side_effect = RuntimeError("x" * 300); stored.unlink(); fc.fc_linear.refresh_ready(proj, "ENG-9")
        self.assertEqual((said, stored.exists(), plan.call_args), (["linear plan: " + "x" * 200], True, mock.call("ENG-9")))


class PlanStoredNewline(unittest.TestCase):
    setUp, gql, writes, flat = DeliveryPlan.setUp, DeliveryPlan.gql, DeliveryPlan.writes, DeliveryPlan.__dict__["flat"]
    DATA = {h: f"{h[3:]} now." for h in fc_plan.PLAN_SECTIONS[:4]}

    def plan(self, sections, content):
        """Runs linear_plan on a feature map whose plan Linear stores as content; returns the names of the writes it sent."""
        self.doc = {"id": "d-1", "title": "NEB-7: delivery plan", "content": content, "url": "u/d-1"}
        self.tree = DeliveryPlan.node("NEB-7", labels=["Feature"], docs=[self.doc], kids=[DeliveryPlan.node("NEB-8")])
        self.sections.return_value, start = sections, len(self.sent)
        fc.fc_linear.linear_plan("NEB-7")
        return [k for k, _ in self.sent[start + 1:]]

    def stored(self, data):  # every section in place, as Linear keeps it: no final newline
        return "\n\n".join(f"{h}\n\n{data.get(h, 'Kept ' + h[3:] + '.')}" for h in fc_plan.PLAN_SECTIONS)

    def test_a_current_plan_stored_without_its_final_newline_gets_no_write(self):
        self.assertEqual(self.plan(self.DATA, self.stored(self.DATA)), [])
        self.assertEqual(self.plan({"## Start today": self.DATA["## Start today"]}, self.stored(self.DATA)), [])

    def test_a_changed_section_is_written_once_and_its_stored_copy_is_not_rewritten(self):
        old, sections = self.stored(self.DATA), {**self.DATA, "## Waves": "Waves changed."}
        self.assertEqual(self.plan(sections, old), ["documentUpdate"])
        new = fc_plan.replace_section(old, "## Waves", "Waves changed.")
        self.assertIn(new, self.writes("documentUpdate")[0])
        self.assertTrue("Waves changed." in new and all(f"Kept {h[3:]}." in new for h in fc_plan.PLAN_SECTIONS[4:]))
        self.assertEqual(self.plan(sections, new.rstrip("\n")), [])  # Linear dropped the final newline of what was sent
        self.assertEqual(self.plan(sections, new), [])


class FeatureAfterLink(unittest.TestCase):
    MAKE = "mutation($a: String!, $b: String!) { issueRelationCreate(input: {issueId: $a, relatedIssueId: $b, type: blocks}) { success } }"

    def setUp(self):
        self.root, self.said, self.calls, self.blockers = Path(tempfile.mkdtemp()), [], [], {"ENG-1": ["ENG-5"]}
        self.proj, base, bare = self.root / "p", self.root / "base", self.root / "bare"
        for d, link in ((self.proj, {"feature": "ENG-1"}), (base, {"feature": "ENG-9"}), (bare, {})):
            d.mkdir(); (d / "linear.json").write_text(json.dumps(link))
        self.projects = mock.Mock(return_value={"base": (self.root, base), "bare": (self.root, bare)})
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", "k"), (fc.fc_linear, "linear_gql", self.gql), (fc.fc_core, "say", self.said.append),
                                 (fc.fc_core, "all_projects", self.projects)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        """Answers every read with the union of LINEAR_ISSUE and LINEAR_BLOCKERS, keyed by identifier or node id; a link it makes shows up after."""
        self.calls.append((query, variables))
        if "mutation" in query:
            self.blockers[variables["b"].removeprefix("id-")].append(variables["a"].removeprefix("id-"))
            return {"issueRelationCreate": {"success": True}}
        ident = variables["id"].removeprefix("id-")
        rels = [{"id": f"r-{b}", "type": "blocks", "issue": {"id": f"id-{b}", "identifier": b}} for b in self.blockers.get(ident, [])]
        rels.append({"id": "r-rel", "type": "related", "issue": {"id": "id-ENG-9", "identifier": "ENG-9"}})  # related is not blocked by
        return {"issue": {"id": f"id-{ident}", "identifier": ident, "state": {"name": "Todo", "type": "unstarted"}, "team": {"states": {"nodes": []}},
                          "parent": None, "children": {"nodes": []}, "inverseRelations": {"nodes": rels}}}

    def link(self, verify, times=1):
        (self.proj / "verify.json").write_text(json.dumps(verify))
        self.calls.clear(); self.said.clear()
        for _ in range(times):
            fc.fc_linear.linear_feature_after(self.root, self.proj)
        return [c for c in self.calls if "mutation" in c[0]]

    def test_adds_a_missing_link_once_and_keeps_other_blockers(self):
        for verify in ({"after": "base"}, {"review_base": "compose/base"}):
            self.blockers["ENG-1"] = ["ENG-5"]
            self.assertEqual(self.link(verify, times=2), [(self.MAKE, {"a": "id-ENG-9", "b": "id-ENG-1"})])
            self.assertEqual(self.said, ["linear ENG-1 blocked by ENG-9"])
            self.projects.assert_called_with(self.root)
        self.assertEqual(self.link({"after": "base"}), [])  # already blocked by ENG-9: no mutation, ENG-5 kept

    def test_sync_from_linear_links_after_refreshing_and_stdin_does_not(self):
        data = {"feature": {"id": "ENG-1", "title": "F"}, "issues": [issue("ENG-3", "A", block(id="001", test="t"))]}
        for stdin in (True, False):
            calls = mock.Mock()
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))), \
                    mock.patch.object(fc.fc_linear, "linear_sync_payload", return_value=data), mock.patch("sys.stdout", io.StringIO()), \
                    mock.patch.object(fc.fc_linear, "refresh_ready", calls.refresh_ready), mock.patch.object(fc.fc_linear, "linear_feature_after", calls.after):
                fc.fc_linear.cmd_sync(SimpleNamespace(stdin=stdin, repo=str(self.root / "p" / "..")))
            self.assertEqual(calls.mock_calls[-1:], [] if stdin else [mock.call.after(self.root.resolve(), self.proj)])  # last: after refresh_ready


class ReadyTreeQuery(unittest.TestCase):
    FRAGMENT = """fragment ReadyNode on Issue { id identifier title description state { name } labels { nodes { id name } }
  children { nodes { identifier state { name } inverseRelations(first: 50) { nodes { type issue { identifier state { name } labels { nodes { name } } parent { identifier parent { identifier } } } } } } }
  inverseRelations(first: 250) { nodes { type issue { identifier state { name } labels { nodes { name } } parent { identifier } } } } }"""

    @staticmethod
    def norm(text):
        return " ".join(text.replace("{", " { ").replace("}", " } ").split())

    def fields(self, body):
        """Each top-level selection of body with its block, normalized; text left after the last block is its own item."""
        out, depth, cur = [], 0, ""
        for ch in body:
            cur += ch
            depth += {"{": 1, "}": -1}.get(ch, 0)
            if ch == "}" and depth == 0:
                out.append(self.norm(cur)); cur = ""
        return out + ([self.norm(cur)] if cur.strip() else [])

    def inner(self, field):
        return field[field.index("{") + 1:field.rindex("}")]

    def test_braces_balance_and_no_prefix_closes_more_than_it_opens(self):
        depth = 0
        for i, ch in enumerate(fc.fc_linear.LINEAR_READY_TREE):
            depth += {"{": 1, "}": -1}.get(ch, 0)
            self.assertGreaterEqual(depth, 0, f"closes more than it opens at {i}")
        self.assertEqual(depth, 0)

class EvidenceBrief(unittest.TestCase):
    """The CLI-owned Evidence section after the compose block is not part of the implementer's brief."""
    setUp, sync = Sync.setUp, Sync.sync
    SECTION = "\n## Evidence\n\nVerdict: not judged\n\n| id | at | detail |\n|---|---|---|\n| abc123 | 2026-10-21 10:00 | a |\n"
    BRIEF = "## Goal\nDo it.\n\n## Acceptance criteria\n- works"

    def task(self, desc):
        return self.sync([issue("ENG-3", "First", desc)])[0]

    def test_the_section_and_its_table_are_cut_from_the_body_and_the_other_fields_stay(self):
        meta = self.task(block(id="001", files="a.go", protected="a_test.go", test="go test") + self.SECTION)
        self.assertEqual(meta["body"], self.BRIEF)
        self.assertEqual((meta["id"], meta["files"], meta["protected"], meta["test"]), ("001", ["a.go"], ["a_test.go"], "go test"))
        self.assertEqual(self.task(block(id="001", test="t"))["body"], self.BRIEF)  # no section: as before

    def test_trailing_blanks_still_cut_but_a_mention_or_a_longer_heading_does_not(self):
        self.assertEqual(self.task(block(id="001", test="t") + self.SECTION.replace("## Evidence", "## Evidence \t"))["body"], self.BRIEF)
        for line in ("See ## Evidence below", "### Evidence", "## Evidence log", " ## Evidence"):
            desc = block(id="001", test="t") + f"\nNote:\n{line}\nmore\n"
            self.assertEqual(self.task(desc)["body"], self.BRIEF + f"\nNote:\n{line}\nmore", line)


class RoutePin(unittest.TestCase):
    """A task may pin its workflow with `route:`; the Route section the CLI writes into a description is never part of the brief."""
    ROUTE = "## Route\n\n* workflow: test-first\n### Why\n* depth: STANDARD"

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "feat"
        self.proj.mkdir()
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')

    def task(self, desc):
        return fc.fc_tasks.task_from_issue(issue("ENG-3", "T", desc))

    def test_route_text_ends_at_the_next_section_or_the_block(self):
        fence = "```compose\nid: 001\ntest: t\n```\n"
        self.assertEqual(fc.fc_tasks.route_text(f"## Goal\nDo it.\n\n{self.ROUTE}\n\n\n{fence}"), self.ROUTE)
        self.assertEqual(fc.fc_tasks.route_text(f"## Goal\nDo it.\n\n{self.ROUTE}\n\n## Acceptance criteria\n- works\n"), self.ROUTE)
        for desc in (block(id="001", test="t"), "## Routes\n* workflow: test-first\n\n" + fence, "### Route\n* workflow: test-first\n"):
            self.assertEqual(fc.fc_tasks.route_text(desc), "", desc)

    def test_the_body_leaves_the_route_section_out(self):
        plain = self.task(block(id="001", test="t"))["body"]
        above_block = block(id="001", test="t").replace("\n\n```compose", f"\n\n{self.ROUTE}\n\n```compose")
        between = block(id="001", test="t").replace("## Acceptance", f"{self.ROUTE}\n\n\n## Acceptance")
        first = f"{self.ROUTE}\n\n" + block(id="001", test="t")
        for desc in (above_block, between, first):
            self.assertIn(self.ROUTE, desc)
            self.assertEqual(self.task(desc)["body"], plain, desc)

    def test_a_valid_pin_is_kept_and_no_pin_means_no_route_key(self):
        for workflow in fc.fc_tasks.WORKFLOWS:
            self.assertEqual(self.task(block(id="001", test="t", route=workflow))["route"], workflow)
        self.assertNotIn("route", self.task(block(id="001", test="t")))
        tasks = fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""},
                                                   "issues": [issue("ENG-3", "T", block(id="001", test="t", route="implement-then-test"))]})
        self.assertEqual(tasks[0]["route"], "implement-then-test")

    def test_a_check_only_pin_is_kept_through_sync_and_stored(self):
        self.assertEqual(self.task(block(id="001", test="t", route="check-only"))["route"], "check-only")
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""},
                                           "issues": [issue("ENG-3", "T", block(id="001", test="t", route="check-only"))]})
        self.assertEqual(fc.fc_tasks.load_tasks(self.proj)[0]["route"], "check-only")
        self.assertEqual(json.loads((self.proj / "results" / "tasks.json").read_text())["tasks"][0]["route"], "check-only")

    def test_sync_exits_on_a_route_that_is_not_a_workflow(self):
        for value in ("Test-First", "TEST_FIRST", "DEEP", "quick", "check_only", "Check-Only"):
            with self.assertRaises(SystemExit) as e:
                fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""},
                                                   "issues": [issue("ENG-7", "T", block(id="001", test="t", route=value))]})
            self.assertEqual(e.exception.code,
                             f"ENG-7: route must be test-first, implement-then-test, fix-then-regression-test or check-only, not {value}")


class BlockedByOrder(unittest.TestCase):
    def test_new_blockers_are_created_before_old_ones_are_deleted(self):
        calls = []
        def gql(query, variables):
            calls.append("delete" if "issueRelationDelete" in query else "create" if "issueRelationCreate" in query else "read")
            return {"issue": {"inverseRelations": {"nodes": [{"id": "r1", "type": "blocks", "issue": {"id": "u5", "identifier": "ENG-5"}}]}}}
        with mock.patch.object(fc.fc_linear, "linear_gql", gql), mock.patch.object(fc.fc_linear, "linear_issue", lambda i: {"id": "u-" + i}):
            fc.fc_linear.linear_blocked_by({"id": "u12"}, ["ENG-9"])
        self.assertEqual(calls, ["read", "create", "delete"])  # a failed create leaves the old blocker in place


class OpenQuestions(unittest.TestCase):
    def test_open_questions_reads_star_bullets_and_treats_none_as_empty(self):
        self.assertEqual(fc.fc_linear.open_questions("## Open questions\n\n* a\n- b\n\n## Notes\n\nx"), ["a", "b"])
        self.assertEqual(fc.fc_linear.open_questions("## Open questions\n\nNone.\n"), [])
        self.assertEqual(fc.fc_linear.open_questions(""), [])

    def test_open_questions_reads_only_the_h2_section_and_its_h3_children(self):
        self.assertEqual(fc.fc_linear.open_questions("## Notes\n\n### Open questions\n\n- mine\n"), [])
        self.assertEqual(fc.fc_linear.open_questions("## Open questions:\n\n- a\n\n### Detail\n\n- b\n\n## After\n\n- c\n"), ["a", "b"])
