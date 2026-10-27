import hermetic  # first: no test sees the real home
import io, json, os, tempfile, time, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from test_linear_cmd import fc, URL, KEY
import fc_plan
os.environ["HERDR_ENV"] = ""

TODAY = time.strftime("%Y-%m-%d")
TRACKS = "## Tracks\n\n1. Sign-in: NEB-7, NEB-9\n2. Onboarding: NEB-12 (after Sign-in)\n"


def node(ident, state, *blockers, title=None, parent=None, project="p1"):
    return {"identifier": ident, "title": title or "T " + ident, "url": "u/" + ident, "description": "", "state": {"name": state},
            "parent": {"identifier": parent} if parent else None, "labels": {"nodes": [{"name": "Feature"}]}, "project": {"id": project},
            "inverseRelations": {"nodes": [{"type": "blocks", "issue": {"identifier": b, "state": {"name": s}, "labels": {"nodes": []}}} for b, s in blockers]}}


class Fake(unittest.TestCase):
    def setUp(self):
        settle = mock.patch.object(fc.fc_linear_cmd, "SETTLE_SECONDS", 0)  # a fake that keeps its document still would wait the whole settle
        settle.start(); self.addCleanup(settle.stop)
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir()
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        self.calls, self.created, self.doc_writes, self.page, self.live = [], None, [], None, False
        self.content, self.reads = TRACKS, []  # reads: the document content each later read returns, one per read
        self.docs = [("Product delivery plan", "u/doc"), ("NEB-9: delivery plan", "u/d9")]
        self.nodes = [node("NEB-7", "Done"), node("NEB-9", "Todo", ("NEB-7", "Done")), node("NEB-12", "Todo")]
        self.tracks_file = self.repo / "tracks.md"
        self.tracks_file.write_text(TRACKS)
        story = node("NEB-10", "Todo", parent="NEB-9")
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_config, "WORKSPACE", "work"), (fc.fc_linear, "linear_gql", self.gql),
                                 (fc.fc_linear, "linear_children", lambda ident: [story] if ident == "NEB-9" else [])):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def set_docs(self, docs, content):
        if docs is not None:
            self.docs = docs
        self.content = content

    def gql(self, query, variables):
        q = " ".join(query.split())
        self.calls.append((q, variables))
        if "projects(filter" in q:
            return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}}
        if "labels: {some" in q:
            return {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.nodes if self.page is None else self.page}}
        if "documents(first" in q:
            return {"project": {"documents": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                              "nodes": [{"id": "d" + str(n), "title": t, "url": u} for n, (t, u) in enumerate(self.docs)]}}}
        if "document(id" in q:
            if self.reads:
                self.content = self.reads.pop(0)
            return {"document": {"id": "d0", "title": "Product delivery plan", "url": "u/doc", "content": self.content}}
        if "documentCreate" in q:
            self.created = variables
            if self.live:
                self.docs.append(("Product delivery plan", "u/new")); self.content = variables["content"]
            return {"documentCreate": {"document": {"id": "dnew", "title": variables["title"], "url": "u/new", "content": variables["content"]}}}
        if "documentUpdate" in q:
            self.doc_writes.append(variables["content"])
            if self.live:
                self.content = variables["content"]
            return {"documentUpdate": {"success": True}}
        if "issue(id" in q:
            found = next((n for n in self.nodes if n["identifier"] == variables["id"]), None)
            if found is None:
                raise RuntimeError("Entity not found: Issue")  # what Linear answers for an id it does not know
            return {"issue": found}
        raise AssertionError(q)

    def mutations(self):
        return [q for q, _ in self.calls if q.startswith("mutation")]

    def run_verb(self, action, **kw):
        a = SimpleNamespace(**{**dict(repo=str(self.repo), action=action, file=None, quote=None, dry_run=False, json=False), **kw})
        with redirect_stdout(io.StringIO()) as out:
            fc.fc_delivery.cmd_delivery(a)
        return out.getvalue()


class Product(Fake):
    def test_no_document_or_no_tracks_means_no_plan_and_order_says_how_to_start(self):
        for docs, content in (([], ""), (None, fc_plan.product_skeleton())):
            self.set_docs(docs, content)
            self.assertIsNone(fc.fc_delivery.product_plan(self.repo))
            with self.assertRaises(SystemExit) as e:
                self.run_verb("order")
            self.assertEqual(e.exception.code, fc.fc_delivery.NO_PLAN)

    def test_order_prints_one_line_per_entry_and_json_on_request(self):
        out = self.run_verb("order").splitlines()
        self.assertEqual(out[:2], ["1. NEB-10 T NEB-10 (Todo; feature NEB-9, track Sign-in, wave 1)",
                                   "2. NEB-12 T NEB-12 (Todo; feature, track Onboarding, wave 2; waits on NEB-9)"])
        self.assertEqual(json.loads(self.run_verb("order", json=True))[0]["id"], "NEB-10")

    def test_seed_lists_open_features_their_plans_and_a_guess_and_writes_nothing(self):
        self.set_docs([("NEB-9: delivery plan", "u/d9")], "")
        out = self.run_verb("seed")
        self.assertIn("No product delivery plan yet.", out)
        self.assertIn("- NEB-9 T NEB-9 (Todo, blocked by NEB-7), its plan: u/d9", out)
        self.assertIn("A first guess at the tracks, from the blocks Linear holds today; play it back as a guess:\n1. ", out)
        self.assertEqual(self.mutations(), [])

    def test_seed_starts_from_the_prd_s_product_at_a_glance(self):
        prd = {"id": "d9", "title": "PRD", "url": "u/prd", "content": "> Draft\n\n## Goals\n\nSell.\n\n## The product at a glance\n\nA shoe shop for runners.\n\n## Open questions\n\nNone.\n"}
        with mock.patch.object(fc.fc_prd, "read_prd", return_value=prd):
            out = self.run_verb("seed")
        self.assertTrue(out.startswith("The PRD's product at a glance (u/prd):\nA shoe shop for runners.\n"), out)
        self.assertNotIn("Sell.", out); self.assertNotIn("None.", out)

    def test_seed_without_a_prd_says_to_plan_it_first(self):
        with mock.patch.object(fc.fc_prd, "read_prd", return_value=None):
            out = self.run_verb("seed")
        self.assertEqual(out.splitlines()[0], fc.fc_delivery.NO_PRD)
        self.assertIn("compose prd seed", fc.fc_delivery.NO_PRD); self.assertIn("first", fc.fc_delivery.NO_PRD)

    def test_seed_goes_on_when_the_prd_lookup_fails(self):
        with mock.patch.object(fc.fc_prd, "read_prd", side_effect=RuntimeError("linear down")):
            out = self.run_verb("seed")
        self.assertIn("PRD lookup failed: linear down", out.splitlines()[0])
        self.assertIn("A first guess at the tracks", out)

    def test_a_bad_tracks_section_stops_with_its_line(self):
        self.content = "## Tracks\n\n1. Sign-in NEB-7\n"
        with self.assertRaises(SystemExit) as e:
            self.run_verb("order")
        self.assertTrue(e.exception.code.startswith('delivery order: the Tracks section does not parse: line 1: not a track: "1. Sign-in NEB-7"'))

    def test_a_track_id_outside_the_page_is_kept_only_in_this_project(self):
        self.page = [node("NEB-12", "Todo")]
        self.nodes = self.page + [node("NEB-30", "Todo", project="other"), node("NEB-31", "Todo")]
        self.content = "## Tracks\n\n1. A: NEB-12, NEB-30, NEB-31\n"
        got = fc.fc_delivery.read_product(str(self.repo))["features"]
        self.assertEqual(sorted(got), ["NEB-12", "NEB-31"])

    def test_order_reports_a_track_id_linear_does_not_know(self):
        self.content = "## Tracks\n\n1. A: NEB-7, NEB-311, NEB-9\n"
        with redirect_stderr(io.StringIO()) as err:
            self.run_verb("order")
        self.assertEqual(err.getvalue(), "NEB-311 is in a track but was not found in Linear: fix the id or drop it from the track\n")


    def test_order_and_reconcile_with_a_missing_id_report_it_and_go_on(self):
        self.content = "## Tracks\n\n1. A: NEB-7, NEB-311, NEB-12\n"
        with redirect_stderr(io.StringIO()) as err:
            out = self.run_verb("order")
        self.assertIn("NEB-311 is in a track but was not found in Linear", err.getvalue())
        self.assertIn("NEB-12 T NEB-12 (Todo; feature, track A, wave 1)", out)
        with redirect_stderr(io.StringIO()) as err, mock.patch.object(fc.fc_linear, "linear_mark_ready", return_value=[]), \
                mock.patch.object(fc.fc_linear, "linear_blocked_by"), mock.patch.object(fc.fc_linear, "linear_comment"), \
                mock.patch.object(fc.fc_linear, "linear_issue", lambda i: {"id": "uuid-" + i, "identifier": i}):
            out = self.run_verb("reconcile")
        self.assertIn("NEB-311 is in a track but was not found in Linear", err.getvalue())
        self.assertIn("+ NEB-7 blocks NEB-12", out)

    def test_any_other_linear_error_on_a_track_id_still_stops(self):
        self.content = "## Tracks\n\n1. A: NEB-311\n"
        with mock.patch.object(fc.fc_linear, "linear_gql", side_effect=lambda q, v: self.gql(q, v) if "issue(id" not in q else (_ for _ in ()).throw(RuntimeError("Entity not found: Issue; rate limited"))):
            with self.assertRaises(SystemExit) as e:
                self.run_verb("order")
        self.assertEqual(e.exception.code, "delivery order: Entity not found: Issue; rate limited")

    def test_a_value_error_that_is_not_the_tracks_is_not_called_a_parse_error(self):
        with mock.patch.object(fc.fc_linear, "linear_gql", side_effect=ValueError("Expecting value: line 1 column 1 (char 0)")):
            with self.assertRaises(SystemExit) as e:
                self.run_verb("order")
        self.assertEqual(e.exception.code, "delivery order: Expecting value: line 1 column 1 (char 0)")

PLAN = "## Tracks\n\n1. Sign-in: NEB-7, NEB-9\n2. Onboarding: NEB-12 (after Sign-in)\n3. Old: NEB-5\n"


class Reconcile(Fake):
    def setUp(self):
        super().setUp()
        self.content = PLAN
        self.nodes = [node("NEB-7", "Done"), node("NEB-9", "Todo", ("NEB-7", "Done")), node("NEB-12", "Todo", ("NEB-5", "Todo"), ("NEB-40", "Todo")),
                      node("NEB-5", "Todo"), node("NEB-30", "Todo")]
        self.comments = []
        self.mark = mock.Mock(side_effect=lambda ident: [{"id": "NEB-10", "title": "T NEB-10"}] if ident == "NEB-9" else [])
        self.blocked_by = mock.Mock()
        for name, value in (("linear_issue", self.issue), ("linear_blocked_by", self.blocked_by), ("linear_mark_ready", self.mark),
                            ("linear_comment", lambda issue, body: self.comments.append((issue["identifier"], body)))):
            p = mock.patch.object(fc.fc_linear, name, value); p.start(); self.addCleanup(p.stop)

    def issue(self, ident):
        return {"id": "uuid-" + ident, "identifier": ident}

    def apply_writes(self):
        for (issue, want), _ in self.blocked_by.call_args_list:
            n = next(n for n in self.nodes if n["identifier"] == issue["identifier"])
            n["inverseRelations"]["nodes"] = [{"type": "blocks", "issue": {"identifier": b, "state": {"name": "Todo"}, "labels": {"nodes": []}}} for b in want]
        if self.doc_writes:
            self.content = self.doc_writes[-1]

    def test_reconcile_adds_and_removes_feature_blocks_comments_and_marks_ready(self):
        r = fc.fc_delivery.reconcile(self.repo)
        self.assertEqual((r["added"], r["removed"], r["ready"]), ([["NEB-9", "NEB-12"]], [["NEB-5", "NEB-12"]], ["NEB-10"]))
        self.blocked_by.assert_called_once_with(self.issue("NEB-12"), ["NEB-9", "NEB-40"])
        self.assertEqual(self.comments, [("NEB-12", "Product delivery plan reconcile: now blocked by NEB-9; no longer blocked by NEB-5. "
                                          "The plan wins over Linear; change the plan to change this: u/doc")])
        self.assertEqual([c.args[0] for c in self.mark.call_args_list], ["NEB-9", "NEB-5", "NEB-12"])  # open plan features, in plan order
        self.assertIn("NEB-30 T NEB-30 is an open feature the plan does not name: add it to a track, or cancel it.", r["proposed"])
        self.assertEqual(json.loads(fc.fc_delivery.state_path(self.repo).read_text())["removed"], [["NEB-5", "NEB-12"]])
        self.assertEqual(fc.fc_delivery.last_reconcile(self.repo)["added"], [["NEB-9", "NEB-12"]])
        self.assertIn("## Start today\n\n1. NEB-10", self.doc_writes[-1])
        self.assertFalse([q for q in self.mutations() if any(w in q for w in ("issueDelete", "issueArchive", "parentId", "stateId"))])  # destructive fixes are only proposed

    def test_a_second_reconcile_writes_nothing(self):
        fc.fc_delivery.reconcile(self.repo)
        self.apply_writes()  # the fake now holds the relations and document the first run wrote
        before = len(self.mutations()); self.blocked_by.reset_mock(); self.comments.clear()
        fc.fc_delivery.reconcile(self.repo)
        self.assertEqual((self.mutations()[before:], self.blocked_by.call_count, self.comments), ([], 0, []))

    def test_a_document_linear_only_reformatted_is_not_written_again(self):
        fc.fc_delivery.reconcile(self.repo)
        self.apply_writes()
        self.assertIn("\n- ", self.content)
        self.content = self.content.replace("\n- ", "\n* ")  # how Linear stores the bullets it was sent
        writes = len(self.doc_writes)
        fc.fc_delivery.reconcile(self.repo)
        self.assertEqual(len(self.doc_writes), writes)

    def test_no_reconcile_yet_reads_as_none(self):
        self.assertIsNone(fc.fc_delivery.last_reconcile(self.repo))

    def test_dry_run_prints_what_it_would_do_and_writes_nothing(self):
        out = self.run_verb("reconcile", dry_run=True).splitlines()
        self.assertEqual(out[:3], ["product delivery plan (dry run, nothing written): 1 blocks added, 1 removed, ready not checked, 1 proposed",
                                   "  + NEB-9 blocks NEB-12", "  - NEB-5 no longer blocks NEB-12"])
        self.assertEqual(self.mutations(), []); self.mark.assert_not_called(); self.blocked_by.assert_not_called()
        self.assertFalse(fc.fc_delivery.state_path(self.repo).exists())

    def test_the_summary_lists_ready_and_proposed_lines(self):
        out = self.run_verb("reconcile").splitlines()
        self.assertEqual(out[0], "product delivery plan: 1 blocks added, 1 removed, 1 ready, 1 proposed")
        self.assertIn("  ready: NEB-10", out)
        self.assertIn("  proposed: NEB-30 T NEB-30 is an open feature the plan does not name: add it to a track, or cancel it.", out)

    def test_a_bad_plan_stops_before_any_write(self):
        self.content = "## Tracks\n\n1. A: NEB-7 (after B)\n2. B: NEB-9 (after A)\n"
        with self.assertRaises(SystemExit):
            self.run_verb("reconcile")
        self.assertEqual((self.mutations(), self.blocked_by.call_count), ([], 0))

    def test_a_linear_failure_says_to_run_it_again(self):
        self.blocked_by.side_effect = RuntimeError("boom")
        with self.assertRaises(SystemExit) as e:
            self.run_verb("reconcile")
        self.assertEqual(e.exception.code, "delivery reconcile: boom; run it again: it only writes what is still out of step")


    def test_an_unknown_id_as_blocked_or_blocker_writes_no_relation_and_is_proposed(self):
        for text in ("1. A: NEB-7, NEB-311\n", "1. A: NEB-311, NEB-12\n"):
            with self.subTest(text):
                self.content = "## Tracks\n\n" + text
                self.blocked_by.reset_mock()
                with redirect_stderr(io.StringIO()) as err:
                    out = self.run_verb("reconcile", dry_run=True)
                self.assertNotIn("NEB-311 blocks", out)
                self.assertNotIn("blocks NEB-311", out)
                self.assertIn("NEB-311 is in a track but was not found in Linear", err.getvalue())
                self.assertIn("proposed: NEB-311 is in track A but Linear has no issue NEB-311", out)
                with redirect_stderr(io.StringIO()):
                    r = fc.fc_delivery.reconcile(self.repo)
                self.assertFalse([p for p in r["added"] + r["removed"] if "NEB-311" in p])
                for call in self.blocked_by.call_args_list:
                    self.assertNotIn("NEB-311", call.args[1])

    def test_a_second_run_with_an_unknown_id_writes_nothing(self):
        self.content = "## Tracks\n\n1. A: NEB-311, NEB-12\n"
        with redirect_stderr(io.StringIO()):
            fc.fc_delivery.reconcile(self.repo)
            self.apply_writes()
            before = len(self.mutations()); self.blocked_by.reset_mock(); self.comments.clear()
            fc.fc_delivery.reconcile(self.repo)
        self.assertEqual((self.mutations()[before:], self.blocked_by.call_count, self.comments), ([], 0, []))

    def test_a_canceled_feature_or_a_story_gets_no_relations_and_is_reported(self):
        story = node("NEB-50", "Todo"); story["labels"] = {"nodes": [{"name": "Story"}]}
        self.nodes += [node("NEB-60", "Canceled"), story]
        self.content = "## Tracks\n\n1. A: NEB-60, NEB-12, NEB-50\n"
        with redirect_stderr(io.StringIO()) as err:
            r = fc.fc_delivery.reconcile(self.repo)
        self.assertFalse([p for p in r["added"] if {"NEB-60", "NEB-50"} & set(p)])
        self.assertIn("NEB-60 is in a track but is Canceled", err.getvalue())
        self.assertIn("NEB-50 is in a track but is not a feature", err.getvalue())
        self.assertTrue([p for p in r["proposed"] if p.startswith("NEB-60") or p.startswith("NEB-50")])


    def test_a_canceled_or_unknown_middle_feature_still_holds_the_order(self):
        self.nodes += [node("NEB-60", "Canceled")]
        for text in ("1. A: NEB-9, NEB-60, NEB-12\n", "1. A: NEB-9, NEB-311, NEB-12\n", "1. A: NEB-9, NEB-60\n2. B: NEB-12 (after A)\n",
                     "1. A: NEB-9, NEB-311\n2. B: NEB-12 (after A)\n"):
            with self.subTest(text):
                self.content = "## Tracks\n\n" + text
                with redirect_stderr(io.StringIO()):
                    r = fc.fc_delivery.reconcile(self.repo, dry_run=True)
                    order = fc.fc_delivery.delivery_order(self.repo)
                self.assertIn(["NEB-9", "NEB-12"], r["added"])
                self.assertEqual([e["waits_on"] for e in order if e["id"] == "NEB-12"], [["NEB-9"]])

    def test_each_changed_feature_is_read_once(self):
        issue = mock.Mock(side_effect=self.issue)
        with mock.patch.object(fc.fc_linear, "linear_issue", issue):
            fc.fc_delivery.reconcile(self.repo)
        self.assertEqual([c.args[0] for c in issue.call_args_list], ["NEB-12"])

    def test_an_edit_made_during_the_run_is_kept(self):
        edited = PLAN + "\n## Why this order\n\nAuth first, edited by hand.\n"
        self.reads = [PLAN, edited]  # the run reads PLAN, then someone edits the document before it writes
        fc.fc_delivery.reconcile(self.repo)
        self.assertEqual(len(self.doc_writes), 1)
        self.assertIn("Auth first, edited by hand.", self.doc_writes[0])

    def test_a_document_that_keeps_changing_is_not_written(self):
        self.reads = [PLAN, PLAN + "\nx\n", PLAN + "\ny\n"]
        with self.assertRaises(SystemExit) as e:
            self.run_verb("reconcile")
        self.assertEqual(self.doc_writes, [])
        self.assertIn("kept changing", e.exception.code)

    def test_missing_computed_sections_go_where_the_skeleton_puts_them(self):
        self.content = PLAN + "\n## History\n\n- 2026-01-01: first\n"
        fc.fc_delivery.reconcile(self.repo)
        heads = [l for l in self.doc_writes[0].splitlines() if l.startswith("## ")]
        self.assertEqual(heads, ["## Tracks", "## Waves", "## Start today", "## Proposed fixes", "## History"])

class Tracks(Fake):
    def setUp(self):
        super().setUp()
        self.live = True
        self.nodes = [node("NEB-7", "Done"), node("NEB-9", "Todo"), node("NEB-12", "Todo")]
        self.mark = mock.Mock(return_value=[])
        for name, value in (("linear_issue", lambda ident: {"id": "uuid-" + ident, "identifier": ident}), ("linear_blocked_by", mock.Mock()),
                            ("linear_mark_ready", self.mark), ("linear_comment", mock.Mock())):
            p = mock.patch.object(fc.fc_linear, name, value); p.start(); self.addCleanup(p.stop)

    def test_tracks_need_the_product_owners_words(self):  # there is no away or yolo variant
        with self.assertRaises(SystemExit) as e:
            self.run_verb("tracks", file=self.tracks_file)
        self.assertEqual(e.exception.code, 'delivery tracks needs --quote "<the product owner\'s words>": the order across features is theirs to set')
        self.assertEqual(self.mutations(), [])

    def test_first_tracks_create_the_document_and_record_who_set_them(self):
        self.set_docs([], "")
        out = self.run_verb("tracks", file=self.tracks_file, quote="auth first,\nsearch whenever")
        self.assertEqual((self.created["title"], self.created["projectId"]), ("Product delivery plan", "p1"))
        body = self.doc_writes[0]
        self.assertIn("## Tracks\n\n1. Sign-in: NEB-7, NEB-9", body)
        self.assertIn(f'## History\n\n- {TODAY}: tracks set; the product owner said: "auth first, search whenever"', body)
        self.assertIn(f"## Why this order\n\n{fc_plan.PRODUCT_NEXT}", body)
        self.assertTrue(out.splitlines()[0].startswith("tracks set on u/doc"))
        self.assertIn("product delivery plan: ", out)  # the reconcile ran

    def test_new_tracks_replace_the_old_ones_and_add_a_history_line(self):
        self.content = fc_plan.with_history(PLAN, "- 2026-01-01: first")
        self.run_verb("tracks", file=self.tracks_file, quote="x")
        self.assertIsNone(self.created)
        self.assertIn("## Tracks\n\n1. Sign-in: NEB-7, NEB-9\n2. Onboarding", self.doc_writes[0])
        self.assertNotIn("3. Old", self.doc_writes[0])
        self.assertIn(f'- 2026-01-01: first\n- {TODAY}: tracks set', self.doc_writes[0])

    def test_an_unknown_id_or_a_bad_line_writes_nothing(self):
        for text, msg in (("1. A: NEB-311\n", "delivery tracks: NEB-311 is no issue in this project; nothing written"),
                          ("1. A NEB-7\n", 'delivery tracks: the Tracks section does not parse: line 1: not a track: "1. A NEB-7"')):
            Path(self.tracks_file).write_text(text)
            with self.subTest(text), self.assertRaises(SystemExit) as e:
                self.run_verb("tracks", file=self.tracks_file, quote="x")
            self.assertTrue(e.exception.code.startswith(msg))
        self.assertEqual(self.mutations(), [])

    def test_an_empty_tracks_text_is_refused_and_writes_nothing(self):
        for text in ("", "\n", "## Tracks\n\n", "### Tracks\n\n1. A: NEB-7\n", "## Why this order\n\nx\n"):
            Path(self.tracks_file).write_text(text)
            with self.subTest(text), self.assertRaises(SystemExit) as e:
                self.run_verb("tracks", file=self.tracks_file, quote="x")
            self.assertTrue(e.exception.code.startswith("delivery tracks: "), e.exception.code)
        self.assertEqual(self.mutations(), [])

    def test_a_file_with_other_sections_writes_only_the_tracks_section(self):
        self.tracks_file.write_text(TRACKS + "\n## Why this order\n\nAuth first because x.\n")
        self.run_verb("tracks", file=self.tracks_file, quote="x")
        self.assertNotIn("Auth first because x.", self.doc_writes[0])
        self.assertIn("## Tracks\n\n1. Sign-in: NEB-7, NEB-9\n2. Onboarding: NEB-12 (after Sign-in)\n", self.doc_writes[0])
        fc_plan.parse_tracks(fc.fc_delivery._tracks_text(self.doc_writes[0]))

    def test_a_document_without_a_tracks_heading_gets_it_first(self):
        self.content = "## Why this order\n\nAuth first.\n\n## History\n\n- 2026-01-01: first\n"
        self.run_verb("tracks", file=self.tracks_file, quote="x")
        heads = [l for l in self.doc_writes[0].splitlines() if l.startswith("## ")]
        self.assertEqual(heads, ["## Tracks", "## Why this order", "## History"])

    def test_new_tracks_survive_linear_serving_the_old_document_for_a_while_after_the_write(self):
        # delivery tracks while the factory ran: the reconcile read the plan from before the write and put the old tracks back,
        # and a read that flipped between old and new gave "kept changing while it was being written"
        self.content = PLAN
        write = self.gql
        def lagging(query, variables):
            old = self.content
            out = write(query, variables)
            if "documentUpdate" in query:
                self.reads = [old, old, old, self.content]  # Linear serves the text from before the write for a few reads
            return out
        self.gql = lagging
        with mock.patch.object(fc.fc_linear, "linear_gql", lagging), mock.patch.object(fc.fc_linear_cmd, "SETTLE_SECONDS", 30), mock.patch.object(fc.fc_linear_cmd, "SETTLE_POLL", 0):
            self.run_verb("tracks", file=self.tracks_file, quote="x")
        self.assertNotIn("3. Old", self.content)
        self.assertIn("## Tracks\n\n1. Sign-in: NEB-7, NEB-9\n2. Onboarding: NEB-12 (after Sign-in)\n", self.content)
        self.assertIn("## Waves", self.content)
        self.assertIn("## Start today", self.content)

    def test_a_reconcile_waits_for_another_process_writing_the_plan(self):
        import fcntl, subprocess, sys as _sys
        lock = fc.fc_delivery.lock_path(self.repo)
        lock.parent.mkdir(parents=True, exist_ok=True)
        held = subprocess.Popen([_sys.executable, "-c", "import fcntl, sys, time; f = open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); "
                                 "print('held', flush=True); time.sleep(1)", str(lock)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(held.wait)
        self.assertEqual(held.stdout.readline().strip(), "held")
        start = time.monotonic()
        self.content = PLAN
        self.run_verb("reconcile")
        self.assertGreater(time.monotonic() - start, 0.5)

    def test_the_same_tracks_again_write_nothing_and_add_no_history(self):
        self.content = PLAN
        self.run_verb("tracks", file=self.tracks_file, quote="x")
        before = len(self.doc_writes)
        self.run_verb("tracks", file=self.tracks_file, quote="y")
        self.assertEqual(len(self.doc_writes), before)
        self.assertEqual(self.content.count("tracks set;"), 1)
