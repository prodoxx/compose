import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py"))); fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc); import fc_policy  # noqa: E402


def task(ident, tid, status="Todo", **kv):
    block = "\n".join(f"{k}: {v}" for k, v in {"id": tid, "files": f"bin/{tid}.py", "test": "t", **kv}.items())
    return {"id": ident, "title": "T", "description": f"## Goal\nx\n\n```compose\n{block}\n```\n", "status": status, "blockedBy": [], "parentId": "ENG-2"}


class Quick(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve(); self.proj = self.repo / ".compose" / "p"; self.proj.mkdir(parents=True); (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        issues = [{"id": "ENG-2", "title": "S", "description": "## Goal\ns", "status": "Todo", "blockedBy": [], "parentId": "ENG-1"}, task("ENG-3", "001"), task("ENG-4", "002", protected="tests/t2.py"), task("ENG-5", "003", "In Progress", protected="tests/t3.py"),
                  task("ENG-6", "004", files="bin/secret4.py"), task("ENG-7", "005", route="test-first")]
        self.issues = issues
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": issues})
        self.live, self.calls, self.cache = {i["id"]: i["description"] for i in issues}, [], self.proj / "results" / "tasks.json"; self.addCleanup(mock.patch.stopall)
        def gql(q, v):
            self.calls.append((q, v))
            return {"issue": {"id": v["id"], "description": self.live[v["id"]]}} if "issue(id" in q else {"issueUpdate": {"success": True}}
        for target, name, kw in [(fc.fc_core, "project_dir", {"return_value": self.proj}), (fc.fc_config, "LINEAR_KEY", {"new": "lin_test"}), (fc.fc_linear, "linear_gql", {"side_effect": gql}),
                                 (fc.fc_linear, "linear_comment", {}), (fc_policy, "read_policies", {"return_value": ([{"name": "Secrets", "statement": "Keys are checked.", "globs": ["bin/secret*.py"], "checks": []}], "")}), (fc_policy, "glob_matches", {"side_effect": lambda pols, files: [p for p in pols if any(f.startswith("bin/secret") for f in files)]}),
                                 (fc.fc_route, "route_tasks", {"return_value": ({}, {})})]:
            setattr(self, name, mock.patch.object(target, name, **kw).start())

    def quick(self, *ids):
        with mock.patch("sys.stdout", io.StringIO()) as out:
            fc.fc_quick.cmd_quick(SimpleNamespace(repo=str(self.repo), project="p", ids=list(ids)))
        return out.getvalue().splitlines()

    def test_ids_expand_once_in_id_order_and_skips_print_in_order(self):
        lines = self.quick("ENG-1", "005", "ENG-3", "ENG-2", "nope")
        self.assertEqual(["nope skipped: no such task" in lines] + [l for l in lines if l[:3].isdigit()], [True, "001 pinned check-only", "002 skipped: protected test tests/t2.py", "003 skipped: already dispatched (In Progress)",
                                                                  "004 skipped: policy Secrets applies", "005 pinned check-only"])

    def test_a_hand_task_is_never_pinned_whether_named_or_reached_through_its_feature_or_story(self):
        self.issues.append(task("ENG-8", "006", kind="hand"))
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": self.issues})
        lines = self.quick("ENG-1", "ENG-2", "ENG-8", "006")
        self.assertEqual([l for l in lines if l.startswith("006")], ["006 skipped: no such task"])
        self.assertNotIn("ENG-8", [v["id"] for q, v in self.calls])

    def test_kept_tasks_are_pinned_on_linear_and_in_the_cache_then_routed_once(self):
        self.assertEqual(self.quick("ENG-4"), ["002 skipped: protected test tests/t2.py"])
        self.assertEqual((self.calls, self.route_tasks.call_count), ([], 0))  # nothing pinned: no write, no route call
        self.quick("ENG-3", "ENG-7", "ENG-4")
        sent = {v["id"]: v["in"]["description"] for q, v in self.calls if "mutation" in q}
        self.assertEqual(sorted(sent), ["ENG-3", "ENG-7"])
        self.assertEqual([sent[i].count("route:") for i in sent] + ["test-first" in sent["ENG-7"], sent["ENG-3"].rstrip().endswith("route: check-only\n```")], [1, 1, False, True])
        cache = json.loads(self.cache.read_text())
        self.assertEqual({t["id"]: t.get("route") for t in cache["tasks"] if t["id"] in "001 002 005"}, {"001": "check-only", "002": None, "005": "check-only"})
        self.assertEqual([cache["issues"][i]["description"] for i in sent], list(sent.values()))
        args, kwargs = self.route_tasks.call_args
        self.assertEqual((self.route_tasks.call_count, [m["id"] for m in args[2]], kwargs.get("recalculate", args[3] if len(args) > 3 else None)), (1, ["001", "005"], True))

    def test_without_a_linear_key_it_exits_and_writes_nothing(self):
        before = self.cache.read_text()
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", ""), self.assertRaises(SystemExit) as e:
            self.quick("ENG-3")
        self.assertEqual((e.exception.code, self.cache.read_text(), self.calls, self.route_tasks.call_count), ("quick: no Linear key for this workspace, so no pin was written", before, [], 0))

    def fails(self, *ids):
        with self.assertRaises(SystemExit) as e:
            self.quick(*ids)
        self.assertEqual((type(e.exception.code), "\n" in e.exception.code), (str, False))  # one line, so no traceback
        return e.exception.code

    def test_an_issue_without_a_compose_block_or_a_failing_linear_call_exits_with_one_line(self):
        self.live["ENG-3"] = "## Goal\nx\n"
        self.assertEqual(self.fails("ENG-3", "ENG-7"), "quick: ENG-3 has no compose block on Linear; nothing was pinned")
        self.assertEqual((self.route_tasks.call_count, [q for q, v in self.calls if "mutation" in q]), (0, []))
        self.linear_gql.side_effect = RuntimeError("Linear rate limit")
        self.assertEqual(self.fails("ENG-3"), "quick: Linear failed reading ENG-3 (Linear rate limit); nothing was pinned")
        self.linear_gql.side_effect = OSError("HTTP Error 500")
        self.assertIn("(HTTP Error 500)", self.fails("ENG-3"))

    def test_a_failure_after_a_pin_keeps_that_pin_cached_and_says_what_to_route(self):
        self.live["ENG-7"] = "## Goal\nx\n"
        self.assertEqual(self.fails("ENG-3", "ENG-7"), "quick: ENG-7 has no compose block on Linear; pinned before it: 001, not routed yet (`compose route --only 001`)")
        self.assertEqual(([t.get("route") for t in json.loads(self.cache.read_text())["tasks"] if t["id"] == "001"], self.route_tasks.call_count), (["check-only"], 0))
        self.live["ENG-7"] = task("ENG-7", "005")["description"]
        def gql(q, v):
            if "mutation" in q: raise RuntimeError("down")
            return {"issue": {"id": v["id"], "description": self.live[v["id"]]}}
        self.linear_gql.side_effect = gql
        self.assertEqual(self.fails("ENG-7"), "quick: Linear failed writing the pin on ENG-7 (down); nothing was pinned")
