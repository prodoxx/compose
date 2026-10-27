import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, subprocess, tempfile, threading, time, unittest, urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

KEY = "lin_api_SECRET_do_not_print"
STATES = [{"id": f"s-{n}", "name": n, "type": t} for n, t in
          (("Todo", "unstarted"), ("In Progress", "started"), ("In Review", "started"), ("Done", "completed"))]


class FakeLinear:
    """Linear's GraphQL API in memory: feature ENG-1 > story ENG-2 > tasks ENG-3, ENG-4."""
    def __init__(self):
        self.state = {"ENG-1": "Todo", "ENG-2": "Todo", "ENG-3": "Todo", "ENG-4": "Todo"}
        self.parent = {"ENG-2": "ENG-1", "ENG-3": "ENG-2", "ENG-4": "ENG-2"}
        self.comments, self.calls = [], 0
        self.lag, self.before = False, None  # lag: the first read after a move still shows the states from before it

    def node(self, i, seen=None):
        name = (seen or self.state)[i]
        return {"name": name, "type": next(s["type"] for s in STATES if s["name"] == name)}

    def __call__(self, query, variables):
        self.calls += 1
        if "issueUpdate" in query:
            self.before = dict(self.state) if self.lag else None
            self.state[variables["id"]] = next(s["name"] for s in STATES if s["id"] == variables["s"])
            return {"issueUpdate": {"success": True}}
        if "commentCreate" in query:
            self.comments.append((variables["id"], variables["b"]))
            return {"commentCreate": {"success": True}}
        i, seen, self.before = variables["id"], self.before, None
        return {"issue": {"id": i, "identifier": i, "state": self.node(i, seen), "team": {"states": {"nodes": STATES}},
                          "parent": {"identifier": self.parent[i]} if i in self.parent else None,
                          "children": {"nodes": [{"identifier": c, "state": self.node(c, seen)} for c, p in self.parent.items() if p == i]}}}


class LinearKey(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.cfg, self.auth = root / "config.json", root / "auth.json"
        self.cfg.write_text(json.dumps({"workspace": "work", "workspaces": {"work": {}}}))
        self.auth.write_text(json.dumps({"workspaces": {"work": {"openrouter": "sk-or-x"}}}))
        for p in (mock.patch.object(fc.fc_config, "CONFIG_PATH", self.cfg), mock.patch.object(fc.fc_config, "AUTH_PATH", self.auth),
                  mock.patch.object(fc.fc_config, "ENV_FILE", root / ".env")):
            p.start(); self.addCleanup(p.stop)

    def workspace(self, *args, stdin=""):
        a = SimpleNamespace(name=args[0] if args else None, target=args[1] if len(args) > 1 else None, linear="--linear" in args)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out):
            fc.fc_config.cmd_workspace(a)
        return out.getvalue()

    def test_the_key_comes_from_stdin_is_stored_per_workspace_and_never_printed(self):
        out = self.workspace("key", "work", "--linear", stdin=KEY + "\n")
        self.assertEqual(json.loads(self.auth.read_text())["workspaces"]["work"]["linear"], KEY)
        self.assertNotIn(KEY, out + self.cfg.read_text())
        listing = self.workspace()
        self.assertIn("linear key set", listing); self.assertNotIn(KEY, listing)

class LinearStates(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp())
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
        self.lin = FakeLinear()
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.lin), (fc.fc_core, "say", lambda m: None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        fc.fc_linear._linear_warned.clear()

    def result(self, status="passed", log=None):
        return {"status": status, "branch": "fast/p/001", "attempts": 2, "cost": 0.0123, "model": "pi:x/glm", "log": log or []}

    def test_the_state_table_runs_from_cli_events(self):
        self.lin.lag = True  # each roll-up reads right after its own move
        fc.fc_linear.linear_event(self.proj, {"linear": "ENG-3"}, "dispatched")
        self.assertEqual([self.lin.state[i] for i in ("ENG-3", "ENG-2", "ENG-1")], ["In Progress"] * 3)
        fc.fc_linear.linear_event(self.proj, {"linear": "ENG-3"}, "passed", self.result())
        self.assertEqual((self.lin.state["ENG-3"], self.lin.state["ENG-2"]), ("In Review", "In Progress"))  # ENG-4 not started
        fc.fc_linear.linear_event(self.proj, {"linear": "ENG-4"}, "dispatched")
        fc.fc_linear.linear_event(self.proj, {"linear": "ENG-4"}, "passed", self.result())
        self.assertEqual(self.lin.state["ENG-2"], "In Review")
        self.assertEqual(self.lin.state["ENG-1"], "In Progress")  # every story In Review, but verify has not passed
        fc.fc_linear.linear_reconcile(self.proj, verified=True)
        self.assertEqual(self.lin.state["ENG-1"], "In Review")
        self.assertIn("fast/p/001", self.lin.comments[0][1]); self.assertIn("2 attempt(s)", self.lin.comments[0][1])
        self.assertIn("$0.0123", self.lin.comments[0][1]); self.assertIn("pi:x/glm", self.lin.comments[0][1])

    def test_a_story_a_task_event_left_behind_rolls_up_after_the_run_and_at_verify_pass(self):
        """A task event rolls up from the one read after its own move, which can miss (parallel tasks, a read behind the
        write): every task In Review, the story and the feature still In Progress, as the event left them."""
        self.lin.lag = True
        self.lin.state.update({"ENG-1": "In Progress", "ENG-2": "In Progress", "ENG-3": "In Review", "ENG-4": "In Review"})
        (self.proj / "results").mkdir()
        passed = lambda repo, proj, meta, attempts: {"id": meta["id"], "title": "t", "status": "passed", "attempts": 1, "seconds": 0.0, "cost": 0.0, "branch": "-"}
        with mock.patch.object(fc.fc_implement, "run_task", passed), mock.patch.object(fc.fc_run, "preflight", lambda m: None), \
                mock.patch.object(fc.fc_herdr, "herdr_space", lambda p: None), mock.patch.object(fc.fc_run, "triage", lambda *x: None), \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            fc.fc_run.run_tasks(Path("."), self.proj, [{"id": "004", "model": "pi:x/glm", "linear": "ENG-4"}],
                                SimpleNamespace(trunk="main", parallel=1, attempts=1, no_stack=True))
        self.assertEqual((self.lin.state["ENG-2"], self.lin.state["ENG-1"]), ("In Review", "In Progress"))  # verify has not passed
        self.lin.state["ENG-2"] = "In Progress"
        fc.fc_linear.linear_reconcile(self.proj, verified=True)
        self.assertEqual((self.lin.state["ENG-2"], self.lin.state["ENG-1"]), ("In Review", "In Review"))

    def test_a_failure_stays_in_progress_with_its_cause(self):
        fc.fc_linear.linear_event(self.proj, {"linear": "ENG-3"}, "failed",
                        self.result("failed", [{"test_output": "ok a\nFAIL TestScore: got 1 want 2"}]))
        self.assertEqual(self.lin.state["ENG-3"], "In Progress")
        self.assertIn("FAIL TestScore: got 1 want 2", self.lin.comments[0][1])

    def test_no_key_skips_once_with_how_to_set_it(self):
        said = []
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", None), mock.patch.object(fc.fc_core, "say", said.append):
            fc.fc_linear.linear_event(self.proj, {"linear": "ENG-3"}, "dispatched")
            fc.fc_linear.linear_event(self.proj, {"linear": "ENG-4"}, "dispatched")
        self.assertEqual(self.lin.calls, 0)
        self.assertEqual(len(said), 1); self.assertIn("--linear", said[0])

    def test_a_linear_outage_never_stops_the_run(self):
        said = []
        with mock.patch.object(fc.fc_linear, "linear_gql", side_effect=OSError("connection refused")), mock.patch.object(fc.fc_core, "say", said.append):
            fc.fc_linear.linear_event(self.proj, {"linear": "ENG-3"}, "passed", self.result())
            fc.fc_linear.linear_reconcile(self.proj, verified=True)
        self.assertTrue(any("update failed" in s for s in said))

    def test_untracked_tasks_leave_linear_alone(self):
        fc.fc_linear.linear_event(self.proj, {}, "dispatched")
        self.assertEqual(self.lin.calls, 0)

    def done(self, only):
        (self.proj / "results").mkdir(exist_ok=True)
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [{"id": "003", "title": "a", "linear": "ENG-3"},
                                                                                {"id": "004", "title": "b", "linear": "ENG-4"}]}))
        with redirect_stdout(io.StringIO()), mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj):
            fc.fc_status.cmd_done(SimpleNamespace(repo=str(self.proj), only=only, note="finished by hand in the lead's tab"))

    def test_done_by_hand_moves_the_task_in_review_and_rolls_up(self):
        self.done("003")
        self.assertEqual([self.lin.state[i] for i in ("ENG-3", "ENG-2", "ENG-1")], ["In Review", "In Progress", "In Progress"])
        self.assertIn("finished by hand in the lead's tab", self.lin.comments[0][1])
        self.done("004")
        self.assertEqual([self.lin.state[i] for i in ("ENG-4", "ENG-2", "ENG-1")], ["In Review", "In Review", "In Progress"])

    def test_done_by_hand_records_in_review_in_the_stored_tasks_before_the_next_sync(self):
        (self.proj / "results").mkdir(exist_ok=True)
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [
            {"id": "003", "title": "a", "linear": "ENG-3", "linear_state": "Todo"}, {"id": "004", "title": "b", "linear": "ENG-4", "linear_state": "Todo"}]}))
        with redirect_stdout(io.StringIO()), mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj):
            fc.fc_status.cmd_done(SimpleNamespace(repo=str(self.proj), only="003", note="by hand"))
        self.assertEqual([m["linear_state"] for m in fc.fc_tasks.linear_cache(self.proj)["tasks"]], ["In Review", "Todo"])

    def test_done_by_hand_without_a_key_leaves_the_stored_state_alone(self):
        (self.proj / "results").mkdir(exist_ok=True)
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"tasks": [{"id": "003", "title": "a", "linear": "ENG-3", "linear_state": "Todo"}]}))
        with redirect_stdout(io.StringIO()), mock.patch.object(fc.fc_config, "LINEAR_KEY", None), mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj):
            fc.fc_status.cmd_done(SimpleNamespace(repo=str(self.proj), only="003", note="by hand"))
        self.assertEqual(fc.fc_tasks.linear_cache(self.proj)["tasks"][0]["linear_state"], "Todo")

    def test_done_by_hand_closes_the_pi_pane_its_earlier_attempts_left(self):
        with mock.patch.object(fc.fc_herdr, "herdr_close_task") as close:
            self.done("003,004")
        self.assertEqual([c.args[1] for c in close.call_args_list], ["003", "004"])

class SyncFromLinear(unittest.TestCase):
    """Feature ENG-1 > stories ENG-2, ENG-3 > tasks; ENG-2 has 3 tasks served 2 per page."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp())
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
        self.queries = []
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.gql), (fc.fc_core, "say", lambda m: None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def node(self, i, parent, blocked=()):
        task = parent in ("ENG-2", "ENG-22")
        desc = f"brief {i}\n\n```compose\nid: 00{i[-1]}\ntest: t\n```" if task else f"text {i}"
        return {"identifier": i, "title": f"T{i}", "description": desc, "state": {"name": "Todo"}, "parent": {"identifier": parent} if parent else None,
                "labels": {"nodes": [{"name": "Task" if task else "Story"}] if parent else []},
                "inverseRelations": {"nodes": [{"type": "blocks", "issue": {"identifier": b, "state": {"name": state}, "labels": {"nodes": [{"name": l} for l in labels]}}}
                                               for b, state, labels in blocked] + [{"type": "related", "issue": {"identifier": "ENG-99"}}]}}

    def gql(self, query, variables):
        self.queries.append(query)
        kids = {"ENG-1": ["ENG-2", "ENG-3"], "ENG-2": ["ENG-4", "ENG-5", "ENG-6"], "ENG-3": [],
                "ENG-20": ["ENG-21"], "ENG-21": ["ENG-22"], "ENG-22": ["ENG-23"]}  # ENG-20: a design-system epic > feature > story > task
        if "children" not in query:
            return {"issue": self.node("ENG-1", None)}
        ids, after = kids[variables["id"]], variables["after"]
        start = int(after) if after else 0
        chunk = ids[start:start + 2] if variables["id"] == "ENG-2" else ids
        blocked = {"ENG-5": [("ENG-4", "Todo", [])], "ENG-6": [("ENG-90", "In Review", ["Design"]), ("ENG-91", "In Progress", ["Design"])]}
        nodes = [self.node(i, variables["id"], blocked.get(i, ())) for i in chunk]
        more = start + len(chunk) < len(ids)
        return {"issue": {"children": {"pageInfo": {"hasNextPage": more, "endCursor": str(start + 2)}, "nodes": nodes}}}

    def sync(self, stdin=False):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj):
            fc.fc_linear.cmd_sync(SimpleNamespace(stdin=stdin, repo=str(self.proj)))
        return out.getvalue()

    def fails(self, gql=None):
        """The exit text of a sync from Linear, with linear_gql answering by `gql` when given; it never holds the key."""
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(fc.fc_linear, "linear_gql", gql or self.gql), self.assertRaises(SystemExit) as e:
            self.sync()
        code = e.exception.code
        text = (code if isinstance(code, str) else "") + err.getvalue()
        self.assertEqual(1 if isinstance(code, str) else code, 1); self.assertNotIn(KEY, text)
        return text

    def test_payload_is_the_stdin_shape_with_every_page(self):
        data = fc.fc_linear.linear_sync_payload(self.proj)
        self.assertEqual(data["feature"], {"id": "ENG-1", "title": "TENG-1", "description": "text ENG-1"})
        self.assertEqual([i["id"] for i in data["issues"]], ["ENG-2", "ENG-4", "ENG-5", "ENG-6", "ENG-3"])
        task = data["issues"][2]
        self.assertEqual((task["parentId"], task["status"], task["blockedBy"]), ("ENG-2", "Todo", [{"id": "ENG-4", "status": "Todo", "labels": []}]))
        self.assertEqual((task["labels"], data["issues"][0]["labels"]), (["Task"], ["Story"]))
        self.assertIn("```compose", task["description"])
        self.assertFalse(any(w in q for q in self.queries for w in ("mutation", KEY)))  # read-only

    def test_sync_stores_the_tasks_with_their_blockers(self):
        self.assertIn("3 task(s) synced", self.sync())
        tasks = json.loads((self.proj / "results" / "tasks.json").read_text())["tasks"]
        self.assertEqual([(t["id"], t["after"], t["waits_on"]) for t in tasks],
                         [("004", "", []), ("005", "004", []), ("006", "", ["ENG-91 (In Progress)"])])  # a design at In Review is finished
        self.assertEqual(json.loads((self.proj / "results" / "tasks.json").read_text())["issues"]["ENG-5"]["labels"], ["Task"])

    def test_stories_scope_skips_other_stories_children(self):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "stories": ["ENG-3"]}))
        self.assertEqual([i["id"] for i in fc.fc_linear.linear_sync_payload(self.proj)["issues"]], ["ENG-3"])

    def test_design_epic_tasks_are_read_with_the_features_tasks(self):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "design_epic": "ENG-20"}))
        data = fc.fc_linear.linear_sync_payload(self.proj)
        self.assertEqual([i["id"] for i in data["issues"]], ["ENG-2", "ENG-4", "ENG-5", "ENG-6", "ENG-3", "ENG-21", "ENG-22", "ENG-23"])
        self.assertIn("4 task(s) synced", self.sync())
        self.assertEqual([t["linear"] for t in json.loads((self.proj / "results" / "tasks.json").read_text())["tasks"]],
                         ["ENG-23", "ENG-4", "ENG-5", "ENG-6"])

    def test_missing_feature_and_linear_failures_exit_and_store_nothing(self):
        self.assertIn("sync: Linear has no issue ENG-1", self.fails(lambda q, v: {"issue": None}))
        for err, first in ((RuntimeError("boom"), True), (OSError("boom"), False)):  # on the feature, or while paging its children
            def gql(q, v, err=err, first=first):
                if first or "children" in q:
                    raise err
                return self.gql(q, v)
            with self.subTest(err=repr(err)):
                self.assertIn(f"sync: Linear failed (boom); compose sync --stdin still works ({fc.fc_tasks.SYNC_SHAPE})", self.fails(gql))
        self.assertFalse((self.proj / "results" / "tasks.json").exists())

    def test_cli_takes_stdin_and_rejects_from_linear(self):
        def run(flag):
            return subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "compose"), "sync", flag, "--project", "x",
                                   "--repo", str(self.proj)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        old = run("--from-linear")
        self.assertEqual(old.returncode, 2); self.assertIn("--from-linear", old.stderr)
        new = run("--stdin")
        self.assertNotEqual(new.returncode, 2); self.assertNotIn("unrecognized arguments", new.stderr)

class LinearErrors(unittest.TestCase):
    """linear_gql with urlopen faked: an HTTP error whose body holds Linear's `errors` gives the text a 200 reply gives."""
    def setUp(self):
        p = mock.patch.object(fc.fc_config, "LINEAR_KEY", KEY); p.start(); self.addCleanup(p.stop)

    def gql(self, reply):
        """urlopen raises `reply` when it is an exception, else answers with its bytes."""
        r = mock.MagicMock()
        r.__enter__.return_value = None if isinstance(reply, Exception) else io.BytesIO(reply)
        with mock.patch("urllib.request.urlopen", side_effect=reply if isinstance(reply, Exception) else None, return_value=r):
            return fc.fc_linear.linear_gql("query { viewer { id } }", {})

    def http(self, body, code=400):
        return urllib.error.HTTPError(fc.fc_linear.LINEAR_URL, code, "Bad Request", {}, io.BytesIO(body))

    def rejected(self, reply):
        with self.assertRaises(RuntimeError) as e:
            self.gql(reply)
        self.assertNotIn(KEY, str(e.exception))
        return str(e.exception)

    def test_an_http_error_with_json_errors_raises_linears_messages(self):
        self.assertEqual(self.rejected(self.http(b'{"errors": [{"message": "a"}, {"message": "b"}]}')), "a; b")

    def test_the_text_is_cut_to_300_and_is_what_a_200_reply_gives(self):
        cases = (([{"message": "x" * 200}, {"message": "y" * 200}], ("x" * 200 + "; " + "y" * 200)[:300]),
                 ([{"message": "Unknown field 'nope'"}, {"extensions": {}}], "Unknown field 'nope'; ?"))
        for errors, text in cases:
            with self.subTest(text=text[:20]):
                body = json.dumps({"errors": errors}).encode()
                self.assertEqual(self.rejected(self.http(body, 401)), text)
                self.assertEqual(self.rejected(body), text)

    def test_an_http_error_without_json_errors_is_raised_unchanged(self):
        for body, code in ((b"<html>502 Bad Gateway</html>", 502), (b"", 401), (b'{"data": null}', 400), (b'["errors"]', 400)):
            with self.subTest(body=body):
                err = self.http(body, code)
                with self.assertRaises(urllib.error.HTTPError) as e:
                    self.gql(err)
                self.assertIs(e.exception, err)
                self.assertNotIn(KEY, str(e.exception))

    def test_a_rate_limited_call_waits_as_long_as_linear_says_and_runs_again(self):
        limited = json.dumps({"errors": [{"message": "Rate limit exceeded", "extensions": {"code": "RATELIMITED"}}]}).encode()
        reset = str(int(time.time()) + 40)
        cases = (({"Retry-After": "7"}, 7), ({"X-RateLimit-Complexity-Reset": reset}, 40), ({"X-RateLimit-Complexity-Reset": reset + "000"}, 40), ({}, 10))
        for headers, wait in cases:
            with self.subTest(headers=headers):
                ok = mock.MagicMock(); ok.__enter__.return_value = io.BytesIO(b'{"data": {"viewer": {"id": "u1"}}}')
                err = urllib.error.HTTPError(fc.fc_linear.LINEAR_URL, 400, "Bad Request", headers, io.BytesIO(limited))
                with mock.patch("urllib.request.urlopen", side_effect=[err, ok]) as call, mock.patch("time.sleep") as nap, \
                        mock.patch("sys.stderr", io.StringIO()) as said:
                    self.assertEqual(fc.fc_linear.linear_gql("query { viewer { id } }", {}), {"viewer": {"id": "u1"}})
                self.assertEqual(call.call_count, 2)
                self.assertAlmostEqual(nap.call_args.args[0], wait, delta=2)
                self.assertIn(f"waiting {int(nap.call_args.args[0])}s", said.getvalue())

    def test_a_rate_limit_that_stays_or_names_a_wait_past_the_time_left_fails_with_the_time(self):
        body = json.dumps({"errors": [{"message": "Rate limit exceeded", "extensions": {"code": "RATELIMITED"}}]}).encode()
        for headers, tries in (({}, 4), ({"Retry-After": "250"}, 1)):  # the doubling pauses 10, 20, 40 fit in 120s, the 80 after them does not
            with self.subTest(headers=headers):
                errs = [urllib.error.HTTPError(fc.fc_linear.LINEAR_URL, 429, "Too Many Requests", headers, io.BytesIO(body)) for _ in range(tries)]
                now = [0.0]
                nap = lambda s: now.__setitem__(0, now[0] + s)
                with mock.patch("urllib.request.urlopen", side_effect=errs) as call, mock.patch("time.sleep", nap), mock.patch("time.monotonic", lambda: now[0]), \
                        mock.patch("sys.stderr", io.StringIO()):
                    with self.assertRaises(RuntimeError) as e:
                        fc.fc_linear.linear_gql("query { viewer { id } }", {})
                self.assertEqual(call.call_count, tries)
                self.assertLessEqual(now[0], fc.fc_linear.LINEAR_CALL_SECONDS)
                self.assertIn("Rate limit exceeded", str(e.exception)); self.assertIn("run it again then", str(e.exception))

    def test_a_reply_that_never_ends_fails_naming_the_time_instead_of_hanging(self):
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **kw: stuck.wait(30)), mock.patch.object(fc.fc_linear, "LINEAR_CALL_SECONDS", 0.2):
            with self.assertRaises(RuntimeError) as e:
                fc.fc_linear.linear_gql("mutation { x }", {})
        self.assertIn("Linear gave no answer within 0.2s", str(e.exception))
        self.assertIn("read the issue before repeating it", str(e.exception))

class LinearUrl(unittest.TestCase):
    DEFAULT = "https://api.linear.app/graphql"

    def setUp(self):
        p = mock.patch.object(fc.fc_config, "LINEAR_KEY", KEY); p.start(); self.addCleanup(p.stop)

    def url(self, **env):
        r = mock.MagicMock(); r.__enter__.return_value = io.BytesIO(b'{"data": {}}')
        with mock.patch.dict(os.environ, env), mock.patch("urllib.request.urlopen", return_value=r) as op:
            fc.fc_linear.linear_gql("query { viewer { id } }", {})
        return op.call_args.args[0].full_url

    def test_the_variable_is_read_on_every_call_and_the_constant_stays(self):
        with mock.patch.dict(os.environ, {"LINEAR_URL": "http://127.0.0.1:1/a"}):
            self.assertEqual(self.url(), "http://127.0.0.1:1/a")
            os.environ["LINEAR_URL"] = "http://127.0.0.1:2/b"
            self.assertEqual(self.url(), "http://127.0.0.1:2/b")
            del os.environ["LINEAR_URL"]
            self.assertEqual(self.url(), self.DEFAULT)
        self.assertEqual(fc.fc_linear.LINEAR_URL, self.DEFAULT)


if __name__ == "__main__":
    unittest.main()
