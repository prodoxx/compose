import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, unittest
from pathlib import Path
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_evidence, fc_plan  # noqa: E402

QUERY = "query($id: String!, $after: String) { issue(id: $id) { comments(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { body createdAt } } } }"


def row(eid, **kv): return {"event_id": eid, "run_id": "r1", "commit_sha": "", "task_ids": ["001"], "criterion_id": [], "phase": "red", "source": "run",
            "result": "fail", "fails_on_base": None, "detail": "d", **kv}
def comment(at, *rows, junk=""):
    return {"body": "Evidence: run\n\n```evidence\n" + "\n".join(map(json.dumps, rows)) + junk + "\n```", "createdAt": at}
def page(nodes, cursor=None): return {"issue": {"comments": {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes}}}


class Rows(unittest.TestCase):
    def read(self, pages, **kw):
        gql = mock.Mock(side_effect=pages)
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", "lin_api_x"), mock.patch.object(fc.fc_linear, "linear_gql", gql):
            return fc_evidence.rows("ENG-4", **kw), gql

    def test_parse_comment_reads_every_block_and_skips_other_lines(self):
        body = 'hi\n```evidence\n{"event_id": "a"}\n\nnot json\n[1]\n{"event_id": "b"}\n```\ntext\n```evidence\n{"event_id": "c"}\n```\n```python\n{"event_id": "d"}\n```'
        self.assertEqual(fc_evidence.parse_comment(body), [{"event_id": "a"}, {"event_id": "b"}, {"event_id": "c"}])

    def test_reads_every_page_once_per_event_oldest_first_with_at(self):
        a, b, c = row("a"), row("b"), row("c")
        got, gql = self.read([page([comment("2026-10-21T12:00:00.000Z", a), comment("2026-10-21T13:00:00.000Z", b)], "c1"),
                              page([comment("2026-10-21T09:00:00.000Z", c, junk="\nnot json"), comment("2026-10-21T08:00:00.000Z", a)])])
        self.assertEqual([(r["event_id"], r["at"]) for r in got], [("c", "2026-10-21T09:00:00.000Z"), ("a", "2026-10-21T12:00:00.000Z"), ("b", "2026-10-21T13:00:00.000Z")])
        self.assertEqual([c.args for c in gql.call_args_list], [(QUERY, {"id": "ENG-4", "after": None}), (QUERY, {"id": "ENG-4", "after": "c1"})])

    def test_filters_keep_matching_rows_and_empty_lists_match_any(self):
        rs = [row("a", task_ids=["001"], criterion_id=["001.1:aa"]), row("b", task_ids=["002"], criterion_id=["002.1:bb", "002.2:cc"], run_id="r2"),
              row("c", task_ids=[], criterion_id=[])]
        nodes = [comment("2026-10-21T0%d:00:00.000Z" % i, r) for i, r in enumerate(rs)]
        ids = lambda **kw: [r["event_id"] for r in self.read([page(nodes)], **kw)[0]]
        self.assertEqual(ids(), ["a", "b", "c"])
        self.assertEqual(ids(task_id="001"), ["a", "c"])
        self.assertEqual(ids(run_id="r2"), ["b"])
        self.assertEqual(ids(criterion_id="002.2:cc"), ["b", "c"])
        self.assertEqual(ids(task_id="002", criterion_id="001.1:aa"), ["c"])

    def test_empty_issue_or_no_key_reads_nothing_and_linear_errors_raise(self):
        gql = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.object(fc.fc_linear, "linear_gql", gql):
            for key, issue in (("lin_api_x", ""), (None, "ENG-4")):
                with mock.patch.object(fc.fc_config, "LINEAR_KEY", key): self.assertEqual(fc_evidence.rows(issue), [])
            gql.assert_not_called()
            with mock.patch.object(fc.fc_config, "LINEAR_KEY", "lin_api_x"), self.assertRaises(RuntimeError): fc_evidence.rows("ENG-4")


class Refresh(unittest.TestCase):
    def setUp(self):
        self.said, self.calls, self.desc = [], [], ""
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", "lin_api_x"), (fc.fc_linear, "linear_gql", self.gql), (fc.fc_core, "say", self.said.append)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        self.calls.append((query, variables))
        if "issueUpdate" in query:
            self.desc = variables["d"]
            return {"issueUpdate": {"success": True}}
        return {"issue": {"id": "uuid-" + variables["id"], "description": self.desc}}

    def refresh(self, rows, issue="ENG-4"):
        with mock.patch.object(fc_evidence, "rows", return_value=rows): return fc_evidence.refresh(issue)

    def test_table_is_newest_first_with_the_verdict_of_each_criterions_newest_sufficiency_row(self):
        rs = [row("a", at="2026-10-21T09:05:59.000Z", fails_on_base=True, criterion_id=["001.1:aa", "001.2:bb"], detail="x | y"),
              row("b", at="2026-10-21T10:00:00.000Z", fails_on_base=False, phase="sufficiency", source="verify", result="proven", criterion_id=["001.1:aa", "001.2:bb"]),
              row("c", at="2026-10-21T11:00:00.000Z", phase="sufficiency", source="verify", result="unproven", criterion_id=["001.1:aa"])]
        head = "## Evidence\n\nVerdict: %s\n\n| id | at (UTC) | source | phase | result | fails on base | criteria | detail |\n|---|---|---|---|---|---|---|---|\n"
        self.assertEqual(fc_evidence.table([]), head % "not judged")
        self.assertEqual(fc_evidence.table(rs[:1]), head % "not judged" + "| a | 2026-10-21 09:05 | run | red | fail | yes | 001.1, 001.2 | x \\| y |\n")
        self.assertEqual(fc_evidence.table(rs), head % "001.1 unproven, 001.2 proven" + "| c | 2026-10-21 11:00 | verify | sufficiency | unproven | - | 001.1 | d |\n"
                         "| b | 2026-10-21 10:00 | verify | sufficiency | proven | no | 001.1, 001.2 | d |\n| a | 2026-10-21 09:05 | run | red | fail | yes | 001.1, 001.2 | x \\| y |\n")
        self.assertIn("| all |", fc_evidence.table([row("d", at="2026-10-21T09:00:00.000Z")]))

    def test_appends_a_missing_section_last_then_replaces_it_in_place_and_skips_an_unchanged_write(self):
        self.desc = None
        self.refresh([])
        self.assertEqual(self.desc, fc_plan.replace_section("", "## Evidence", fc_evidence.table([])))
        self.desc, self.calls[:] = "## Goal\nx\n\n```compose\nid: 1\n```\n", []
        a, b = [row("a", at="2026-10-21T09:00:00.000Z")], [row("b", at="2026-10-21T10:00:00.000Z")]
        self.assertIsNone(self.refresh(a))
        self.assertEqual(self.desc, "## Goal\nx\n\n```compose\nid: 1\n```\n\n" + fc_evidence.table(a))
        self.assertEqual(self.calls[0], ("query($id: String!) { issue(id: $id) { id description } }", {"id": "ENG-4"}))
        self.assertEqual(self.calls[1], ("mutation($id: String!, $d: String!) { issueUpdate(id: $id, input: {description: $d}) { success } }", {"id": "uuid-ENG-4", "d": self.desc}))
        self.desc += "\n## Notes\n\nkeep\n"
        self.refresh(b)
        self.assertEqual(self.desc, "## Goal\nx\n\n```compose\nid: 1\n```\n\n" + fc_evidence.table(b) + "\n## Notes\n\nkeep\n")
        self.refresh(b)
        self.assertEqual(sum("issueUpdate" in q for q, v in self.calls), 2)

    def test_does_nothing_without_an_issue_or_a_ready_workspace(self):
        self.refresh([], issue="")
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", None): self.refresh([])
        self.assertEqual(self.calls, [])

    def test_a_failed_read_write_or_row_load_says_the_line_and_raises_nothing(self):
        err = RuntimeError("e" * 300)
        line = "ledger ENG-4: Evidence section not refreshed (%s)" % ("e" * 200)
        for what in ("read", "write", "rows"):
            with self.subTest(what), mock.patch.object(fc.fc_linear, "linear_gql", side_effect=lambda q, v: (_ for _ in ()).throw(err) if what == "read" or "issueUpdate" in q else self.gql(q, v)), \
                    mock.patch.object(fc_evidence, "rows", side_effect=err if what == "rows" else None, return_value=[]):
                self.said.clear()
                self.assertIsNone(fc_evidence.refresh("ENG-4"))
                self.assertEqual(self.said, [line])


class TaskTable(unittest.TestCase):
    def setUp(self):
        self.said, self.calls, self.desc, self.comments = [], [], "", {}
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", "lin_api_x"), (fc.fc_linear, "linear_gql", self.gql), (fc.fc_core, "say", self.said.append)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        self.calls.append((query, variables))
        if "issueUpdate" in query:
            self.desc = variables["d"]
            return {"issueUpdate": {"success": True}}
        if "comments(first: 100" in query: return page(self.comments.get(variables["id"], []))
        return {"issue": {"id": "uuid-" + variables["id"], "description": self.desc}}

    def put(self, issue, *rows_at): self.comments[issue] = [comment("2026-10-21T%02d:00:00.000Z" % h, r) for h, r in rows_at]

    def test_task_issue_rows_then_feature_rows_once_each_oldest_first_without_other_tasks(self):
        self.put("ENG-4", (10, row("t1")), (9, row("o", task_ids=["002"])), (12, row("t3", task_ids=[])))
        self.put("ENG-1", (8, row("f1")), (7, row("fo", task_ids=["002"])), (11, row("t1")), (13, row("f4", task_ids=[])))
        got = fc_evidence.task_rows("ENG-4", "ENG-1", "001")
        self.assertEqual([(r["event_id"], r["shared"]) for r in got], [("f1", True), ("t1", False), ("t3", False), ("f4", True)])
        self.assertEqual(got[1]["at"], "2026-10-21T10:00:00.000Z")

    def test_an_empty_feature_adds_nothing_and_linear_errors_raise(self):
        self.put("ENG-4", (10, row("t1")))
        self.put("", (8, row("f1")))
        for feature in ("", None): self.assertEqual([(r["event_id"], r["shared"]) for r in fc_evidence.task_rows("ENG-4", feature, "001")], [("t1", False)])
        self.assertEqual({v["id"] for q, v in self.calls}, {"ENG-4"})
        with mock.patch.object(fc.fc_linear, "linear_gql", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError): fc_evidence.task_rows("ENG-4", "ENG-1", "001")

    def test_refresh_builds_the_section_from_select_else_from_rows(self):
        seen, a, b = [], [row("s1", at="2026-10-21T09:00:00.000Z")], [row("r1", at="2026-10-21T10:00:00.000Z")]
        with mock.patch.object(fc_evidence, "rows", return_value=b):
            self.assertIsNone(fc_evidence.refresh("ENG-4", lambda i: seen.append(i) or a))
            self.assertEqual(seen, ["ENG-4"])
            self.assertIn(fc_evidence.table(a), self.desc)
            fc_evidence.refresh("ENG-4")
            self.assertIn(fc_evidence.table(b), self.desc)
            self.assertNotIn("| s1 |", self.desc)
        self.put("ENG-4", (10, row("t1")), (9, row("o", task_ids=["002"])))
        self.put("ENG-1", (8, row("f1")))
        fc_evidence.refresh("ENG-4", lambda i: fc_evidence.task_rows(i, "ENG-1", "001"))
        self.assertEqual([l.split(" | ")[0] for l in self.desc.splitlines() if l[:4] in ("| t1", "| f1", "| o ")], ["| t1", "| f1"])

    def test_a_select_that_raises_says_the_line_writes_nothing_and_raises_nothing(self):
        self.desc = "## Goal\nx\n"
        self.assertIsNone(fc_evidence.refresh("ENG-4", mock.Mock(side_effect=RuntimeError("e" * 300))))
        self.assertEqual(self.said, ["ledger ENG-4: Evidence section not refreshed (%s)" % ("e" * 200)])
        self.assertEqual(self.desc, "## Goal\nx\n")
        self.assertFalse([q for q, v in self.calls if "issueUpdate" in q])
