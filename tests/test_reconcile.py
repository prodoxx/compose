import hermetic  # first: no test sees the real home
import datetime, importlib.machinery, importlib.util, io, json, os, re, sys, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import ANY
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

URL = "https://linear.app/acme/project/my-shop-abc123"
TODAY = datetime.date.today().isoformat()


def issue(ident, state="Todo", labels=("Story",), parent=None):
    return {"id": ident, "title": f"{ident} title", "state": state, "labels": list(labels), "parent": parent}


def node(i):
    return {"identifier": i["id"], "title": i["title"], "state": {"name": i["state"]}, "labels": {"nodes": [{"name": l} for l in i["labels"]]}, "parent": {"identifier": i["parent"]} if i["parent"] else None}


def sheet(keep=(), verify=(), cancel_subtree=None, tickets=(), reasons=None, new=()):
    t = list(tickets) + ([{"id": cancel_subtree, "sort": "cancel", "reason": "appux", "subtree": True}] if cancel_subtree else [])
    return {"keep": list(keep), "verify": list(verify), "tickets": t, "reasons": {"appux": "App UI and UX was a mistake.", "dup": "A duplicate."} if reasons is None else reasons, "new": list(new)}


class FakeLinear:
    def __init__(self):
        self.pages, self.queries, self.mutations = [], [], []
        self.issues, self.canceled, self.created, self.comments, self.labels_made, self.fail_on, self.fail_exc = {}, [], [], {}, [], None, RuntimeError
        self.labels = [{"id": f"l-{n}", "name": n} for n in ("Epic", "Feature", "Story", "Ready", "needs-shaping", "deep-dive")]
        self.states = [{"id": "s-backlog", "name": "Backlog"}]

    def seed(self, issues):
        self.issues = {i["id"]: {"id": i["id"], "identifier": i["id"], "title": i["title"], "state": {"name": i["state"], "type": "started" if i["state"] == "In Progress" else "unstarted"}, "parent_id": i["parent"]} for i in issues}

    def comment(self, ident):
        return self.comments[ident][0]

    def comments_on(self, ident):
        return len(self.comments.get(ident, []))

    def gql(self, query, variables):
        self.queries.append(query)
        if "projects(" in query:
            return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "NEB"}]}}]}}
        if "mutation" in query:
            return self.mutate(re.search(r"\{\s*(\w+)\(", query).group(1), variables)
        if "team(id" in query:
            return {"team": {"name": "Engineering", "states": {"nodes": list(self.states)}, "labels": {"nodes": list(self.labels)}}}
        if "issue(id" in query:
            i = self.issues[variables["id"]]
            if "parent { identifier parent" in query:
                chain = {"parent": None}
                for pid in reversed(self.chain(i)):
                    chain = {"parent": {"identifier": pid, **chain}}
                return {"issue": chain}
            return {"issue": {**i, "team": {"states": {"nodes": [{"id": "s-canceled", "name": "Canceled", "type": "canceled"}]}}}}
        n = int(variables["after"][1:]) + 1 if variables["after"] else 1
        page = self.pages[n - 1]
        return {"issues": {"pageInfo": {"hasNextPage": n < len(self.pages), "endCursor": f"c{n}"}, "nodes": [node(i) for i in page]}}

    def chain(self, i):
        out = []
        while i.get("parent_id"):
            out.append(i["parent_id"])
            i = self.issues[i["parent_id"]]
        return out

    def mutate(self, name, v):
        ident = v.get("id")
        if self.fail_on == (name, ident):
            raise self.fail_exc(f"{name} failed")
        self.mutations.append((name, ident, v))
        change = v.get("in") or {"stateId": v.get("s")}  # issueUpdate sends its input whole, or just the state
        if name == "commentCreate":
            self.comments.setdefault(ident, []).append(v["b"])
        elif name == "issueUpdate" and change.get("stateId") == "s-canceled":
            self.canceled.append(ident)
            self.issues[ident]["state"] = {"name": "Canceled", "type": "canceled"}
        elif name == "issueUpdate" and "title" in change:
            self.issues[ident]["title"] = change["title"]
        elif name == "issueLabelCreate":
            self.labels_made.append(v["name"])
            self.labels.append({"id": f"l-{v['name']}", "name": v["name"]})
            return {"issueLabelCreate": {"issueLabel": {"id": f"l-{v['name']}", "name": v["name"]}}}
        elif name == "issueCreate":
            self.created.append(v["in"])
            ident = f"NEB-{100 + len(self.created)}"
            self.issues[ident] = {"id": ident, "identifier": ident, "title": v["in"]["title"], "state": {"name": "Backlog", "type": "backlog"}, "parent_id": v["in"]["parentId"]}
            self.pages[0].append(issue(ident, "Backlog", ("Feature",), v["in"]["parentId"]))  # a ticket filed is an open ticket of the project
            return {"issueCreate": {"issue": {"id": ident, "identifier": ident}}}
        return {}

    def list_query(self):
        return next(q for q in self.queries if "issues(" in q)


class Reconcile(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir()
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        self.tmp = Path(tempfile.mkdtemp())
        self.linear = FakeLinear()
        self.issues = [issue("NEB-1", labels=("Epic",)), issue("NEB-2", labels=("Feature",), parent="NEB-1"), issue("NEB-3", parent="NEB-2"),
                       issue("NEB-4", parent="NEB-2"), issue("NEB-5", parent="NEB-2")]
        self.issues[2]["labels"] = ["Story", "Ready"]
        self.linear.pages = [self.issues]
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", "lin_api_x"), (fc.fc_linear, "linear_gql", self.linear.gql)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(Path, "home", return_value=self.tmp); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(fc.fc_core, "worktree_branches", return_value=[(self.repo, "")]); p.start(); self.addCleanup(p.stop)  # the one checkout git would list

    def args(self, action, **kw):
        return SimpleNamespace(repo=str(self.repo), action=action, file=kw.get("file"), go=kw.get("go"), project=kw.get("project"))

    def out(self, action, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            fc.fc_reconcile.cmd_reconcile(self.args(action, **kw))
        return out.getvalue().splitlines()

    def fails(self, action, **kw):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as e:
            fc.fc_reconcile.cmd_reconcile(self.args(action, **kw))
        code = e.exception.code
        return (1 if isinstance(code, str) else code), ((code if isinstance(code, str) else "") + err.getvalue()).strip()

    def project_dir(self, name):
        d = self.repo / ".compose" / name
        (d / "results").mkdir(parents=True, exist_ok=True)
        (d / "verify.json").write_text("{}")
        return d

    def attempt(self, action, go=None, project=None):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            try:
                fc.fc_reconcile.cmd_reconcile(SimpleNamespace(repo=str(self.repo), action=action, file=None, go=go, project=project))
            except SystemExit:
                pass
        return out.getvalue().splitlines()

    def checked(self, extra=0):
        """A sheet that renames and relabels NEB-3, merges NEB-4 into it, cancels NEB-2 with its subtree, files one new ticket: 9 actions."""
        self.issues += [issue(f"NEB-{n}", parent="NEB-1") for n in range(10, 10 + extra)]
        self.linear.pages = [self.issues]
        self.linear.seed(self.issues)
        raw = {"keep": ["NEB-1"] + [f"NEB-{n}" for n in range(10, 10 + extra)], "verify": [], "reasons": {"appux": self.write("appux.md", "App UI and UX was a mistake."), "dup": self.write("dup.md", "Same slice.")},
               "tickets": [{"id": "NEB-3", "sort": "keep", "title": "RS1. Chat with the Guide", "drop_ready": True, "deep_dive": True, "reason": "dup"},
                           {"id": "NEB-4", "sort": "merge", "into": "NEB-3", "reason": "dup"}, {"id": "NEB-2", "sort": "cancel", "reason": "appux", "subtree": True}],
               "new": [{"key": "N1", "title": "So far: the draft report", "parent": "NEB-1", "label": "Feature", "body": self.write("b.md", "Body.")}]}
        self.linear.issues["NEB-5"]["title"] = "NEB-5 title"
        self.raw = raw
        self.out("check", file=self.sheet(raw))

    def write(self, name, text):
        p = self.tmp / name
        p.write_text(text)
        return str(p)

    def sheet(self, raw=None):
        raw = raw or {"keep": ["NEB-1"], "verify": ["NEB-3"],
                      "tickets": [{"id": "NEB-2", "sort": "cancel", "reason": "appux", "subtree": True}, {"id": "NEB-4", "sort": "merge", "into": "NEB-3", "reason": "dup"}],
                      "reasons": {"appux": self.write("appux.md", "App UI and UX was a mistake."), "dup": self.write("dup.md", "A duplicate.")},
                      "new": [{"key": "N1", "title": "So far: the draft report and its evidence", "parent": "NEB-1", "label": "Feature", "body": self.write("b.md", "Body.")}]}
        return self.write("sheet.json", json.dumps(raw))

    def stored(self, in_progress=None):
        self.issues = [dict(i, state="In Progress" if i["id"] == in_progress else i["state"]) for i in self.issues]
        s = sheet(keep=["NEB-1"], verify=["NEB-3"],
                  tickets=[{"id": "NEB-4", "sort": "merge", "into": "NEB-3", "reason": "dup"}], cancel_subtree="NEB-2",
                  new=[{"key": "N1", "title": "So far: the draft report and its evidence", "parent": "NEB-1", "label": "Feature", "body": "Body.", "deep_dive": False}])
        s["tickets"].append({"id": "NEB-3", "sort": "keep", "title": "RS1. Chat with the Guide", "drop_ready": True, "reason": "dup", "deep_dive": True})
        s["verify"] = ["NEB-6"]
        self.issues = self.issues + [issue("NEB-6", parent="NEB-1")]
        return {**s, "issues": self.issues}

    def test_list_prints_every_open_ticket_as_a_tree_from_every_page(self):
        self.linear.pages = [[issue("NEB-1", labels=("Epic",)), issue("NEB-3", parent="NEB-2")], [issue("NEB-2", labels=("Feature",), parent="NEB-1")]]
        self.assertEqual(self.out("list"), ["NEB-1 Todo Epic NEB-1 title", "  NEB-2 Todo Feature NEB-2 title", "    NEB-3 Todo Story NEB-3 title"])
        self.assertIn('state: {type: {nin: ["completed", "canceled"]}}', self.linear.list_query())

    def test_a_good_sheet_is_stored_with_its_reason_texts_and_the_check_says_so(self):
        out = self.out("check", file=self.sheet())
        self.assertEqual(out[-1], "checked: 5 open tickets sorted (1 kept, 1 to verify, 2 canceled, 1 merged), 1 new; compose reconcile show prints the proposal")
        stored = json.loads(fc.fc_reconcile.sheet_path("p1").read_text())
        self.assertEqual((stored["reasons"]["appux"], stored["applied"], stored["go"], stored["project"]["name"]), ("App UI and UX was a mistake.", {}, [], "Shop"))

    def test_every_open_ticket_is_sorted_exactly_once(self):
        problems = fc.fc_reconcile.check_sheet(sheet(keep=["NEB-1", "NEB-1"]), self.issues)
        self.assertIn("NEB-1 is sorted twice", problems)
        self.assertIn("NEB-4 is open and not sorted", problems)

    def test_a_subtree_cancel_stops_at_a_descendant_sorted_on_its_own(self):
        got = fc.fc_reconcile.expand(sheet(cancel_subtree="NEB-2", verify=["NEB-3"]), self.issues)
        self.assertEqual((got["NEB-3"]["sort"], got["NEB-5"]["sort"], got["NEB-5"]["under"]), ("verify", "cancel", "NEB-2"))

    def test_a_subtree_cancel_does_not_walk_past_a_kept_or_verified_ticket(self):
        issues = self.issues + [issue("NEB-6", parent="NEB-3"), issue("NEB-7", parent="NEB-4")]
        s = sheet(keep=["NEB-1", "NEB-3"], verify=["NEB-4"], cancel_subtree="NEB-2")
        got = fc.fc_reconcile.expand(s, issues)
        self.assertEqual((got["NEB-5"]["sort"], "NEB-6" in got, "NEB-7" in got), ("cancel", False, False))
        problems = fc.fc_reconcile.check_sheet(s, issues)
        self.assertIn("NEB-6 is open and not sorted", problems)
        self.assertIn("NEB-7 is open and not sorted", problems)

    def test_a_sheet_that_is_not_json_or_has_an_entry_without_an_id_exits_with_a_message(self):
        self.assertEqual(self.fails("check", file=self.write("bad.json", "{nope")), (1, f"sheet {self.tmp / 'bad.json'}: not valid JSON (Expecting property name enclosed in double quotes: line 1 column 2 (char 1))"))
        raw = {"tickets": [{"sort": "keep"}]}
        self.assertEqual(self.fails("check", file=self.write("noid.json", json.dumps(raw))), (1, "sheet: a tickets entry has no id: {'sort': 'keep'}"))

    def test_a_sheet_of_the_wrong_shape_exits_with_a_message_not_a_traceback(self):
        for raw, msg in (({"keep": "NEB-1"}, "sheet: keep must be a list"), ({"verify": {"a": 1}}, "sheet: verify must be a list"),
                         ({"tickets": "NEB-1"}, "sheet: tickets must be a list"), ({"keep": [3]}, "sheet: a keep entry must be a ticket id like NEB-12: 3"),
                         ({"verify": [["NEB-1"]]}, "sheet: a verify entry must be a ticket id like NEB-12: ['NEB-1']"),
                         ({"tickets": ["NEB-1"]}, "sheet: a tickets entry must be an object with an id: 'NEB-1'"),
                         ({"reasons": ["x"]}, "sheet: reasons must map a reason name to the path of its text file"),
                         ({"reasons": {"x": 4}}, "sheet: reason x must be the path of a text file: 4"),
                         ({"new": ["N1"]}, "sheet: a new entry must be an object with a key: 'N1'"),
                         ({"new": [{"title": "t"}]}, "sheet: a new entry must be an object with a key: {'title': 't'}"),
                         ({"new": [{"key": "N1", "body": 5}]}, "sheet: new N1 body must be the path of a text file: 5")):
            self.assertEqual(self.fails("check", file=self.write("s.json", json.dumps(raw))), (1, msg))

    def test_check_refuses_bad_entries_and_stores_nothing(self):
        bad = {"tickets": [{"id": "NEB-9", "sort": "keep"}, {"id": "NEB-4", "sort": "merge", "into": "NEB-2", "reason": "dup"},
                           {"id": "NEB-1", "sort": "cancel"}, {"id": "NEB-5", "sort": "verify", "title": "x"}, {"id": "NEB-3", "sort": "keep", "drop_ready": True, "reason": "r"}]}
        problems = fc.fc_reconcile.check_sheet(sheet(**bad, cancel_subtree="NEB-2"), self.issues)
        for p in ("NEB-9 is not an open ticket of this project", "NEB-4 merges into NEB-2, which this sheet cancels",
                  "NEB-1 is canceled without a reason", "NEB-5 is sorted verify: it takes no changes", "reason r has no text"):
            self.assertIn(p, problems)
        self.assertEqual(self.fails("check", file=self.sheet({**bad, "reasons": {}, "cancel": []})), (1, "\n".join(fc.fc_reconcile.check_sheet(sheet(**bad, reasons={}), self.issues))))
        self.assertFalse(fc.fc_reconcile.sheet_path("p1").exists())

    def test_drop_ready_needs_the_ready_label_and_new_tickets_need_a_real_parent_and_label(self):
        self.issues[2]["labels"] = ["Story"]
        s = sheet(keep=["NEB-1", "NEB-2", "NEB-4", "NEB-5"], tickets=[{"id": "NEB-3", "sort": "keep", "drop_ready": True, "reason": "dup"}],
                  new=[{"key": "N1", "title": "t", "parent": "NEB-4", "label": "Task"}, {"key": "N2", "title": "t", "parent": "N1", "label": "Story"}])
        s["new"][0]["parent"] = "NEB-9"
        problems = fc.fc_reconcile.check_sheet(s, self.issues)
        self.assertIn("NEB-3 drops Ready but does not carry it", problems)
        self.assertTrue(any(p.startswith("new N1 has parent NEB-9") for p in problems))
        self.assertTrue(any(p.startswith("new N1 has label 'Task'") for p in problems))
        self.assertFalse(any(p.startswith("new N2") for p in problems))

    def test_a_missing_reason_file_exits_naming_it_and_a_half_applied_sheet_is_not_replaced(self):
        raw = {"keep": [], "tickets": [], "reasons": {"x": "/nope/x.md"}}
        self.assertEqual(self.fails("check", file=self.write("s.json", json.dumps(raw)))[1], "reason x: no file /nope/x.md")
        self.out("check", file=self.sheet())
        path = fc.fc_reconcile.sheet_path("p1")
        stored = json.loads(path.read_text()); stored["applied"] = {"NEB-2": "x"}; path.write_text(json.dumps(stored))
        code, msg = self.fails("check", file=self.sheet())
        self.assertTrue(msg.startswith("a reconcile is half applied (1 of 4 done): finish it with compose reconcile apply --go"), msg)

    def test_proposal_sections_in_order_with_reasons_grouped_and_running_work_named(self):
        text = fc.fc_reconcile.render_proposal(self.stored(in_progress="NEB-5"))
        self.assertEqual([l for l in text.splitlines() if l.startswith("## ")],
                         ["## Cancel", "## Merge", "## Running work this stops", "## Rename", "## Ready removed (needs shaping)",
                          "## Label deep-dive", "## New tickets", "## Left for you to verify", "## Keep"])
        self.assertTrue(text.startswith("6 open tickets: 2 kept, 1 left for you to verify, 2 canceled, 1 merged; 1 new.\n"))
        self.assertIn("App UI and UX was a mistake.\n\n- NEB-2 NEB-2 title, and the 1 open ticket under it", text)
        self.assertIn("- NEB-4 NEB-4 title, into NEB-3 NEB-3 title", text)
        self.assertIn("- NEB-5 NEB-5 title (In Progress)", text)
        self.assertIn('- NEB-3: "NEB-3 title" becomes "RS1. Chat with the Guide"', text)
        self.assertIn("- N1 So far: the draft report and its evidence (Feature under NEB-1, needs shaping)", text)
        self.assertNotIn(chr(0x2014), text)

    def test_a_project_hears_changes_under_its_feature_and_above_it_only(self):
        features = {"rec": "NEB-1733", "appux": "NEB-1890", "billing": "NEB-1732"}
        chains = {"NEB-1789": ["NEB-1778", "NEB-1733", "NEB-1731"], "NEB-1890": ["NEB-1731"], "NEB-1731": [], "NEB-1733": ["NEB-1731"], "NEB-1732": ["NEB-1731"]}
        changes = {"NEB-1789": "NEB-1789 ChatMessage shows the Guide: Canceled", "NEB-1890": "NEB-1890 App UI and UX, and the 37 open tickets under it: Canceled",
                   "NEB-1731": 'NEB-1731 renamed "DecodeThisText investigation platform" (was "subscription app")'}
        got = fc.fc_reconcile.touched(features, chains, changes)
        self.assertEqual(got["rec"], [changes["NEB-1731"], changes["NEB-1789"]])
        self.assertEqual(got["appux"], [changes["NEB-1731"], changes["NEB-1890"]])
        self.assertEqual(got["billing"], [changes["NEB-1731"]])

    def team_leads(self, features, live):
        """Compose projects of the repo under the given features, with the named leads live in herdr."""
        for name, feature in features.items():
            (self.project_dir(name) / "linear.json").write_text(json.dumps({"feature": feature}))
            crew = self.project_dir(name) / "results" / "crew"
            crew.mkdir(parents=True, exist_ok=True)
            (crew / f"{name}-orchestrator.json").write_text(json.dumps({"name": f"{name}-orchestrator", "space": f"w-{name}"}))
        agents = [{"name": f"{n}-orchestrator", "workspace_id": f"w-{n}"} for n in live]
        p = mock.patch.object(fc.fc_herdr, "live_agents", return_value=agents); p.start(); self.addCleanup(p.stop)

    def test_live_leads_find_a_cut_lead_name_and_return_the_numbered_agent(self):
        for slug, rec in (("onboarding-checklist-for", {"name": "onboarding-checklis-orchestrator", "space": "w1"}),
                          ("rec", {"name": "rec-orchestrator", "agent": "rec-orchestrator-2", "space": "w2"})):
            crew = self.project_dir(slug) / "results" / "crew"; crew.mkdir(parents=True, exist_ok=True)
            (self.project_dir(slug) / "linear.json").write_text(json.dumps({"feature": "NEB-3"}))
            (crew / f"{rec['name']}.json").write_text(json.dumps(rec))
        agents = [{"name": "onboarding-checklis-orchestrator", "workspace_id": "w1"}, {"name": "rec-orchestrator-2", "workspace_id": "w2"}]
        with mock.patch.object(fc.fc_herdr, "live_agents", return_value=agents):
            self.assertEqual(fc.fc_reconcile.live_leads(self.repo), {"onboarding-checklist-for": "onboarding-checklis-orchestrator", "rec": "rec-orchestrator-2"})

    def test_notices_name_each_touched_project_and_whether_its_team_lead_is_live(self):
        self.assertEqual(self.fails("notices")[1], 'nothing applied yet: compose reconcile apply --go "<their words>" first')
        self.checked(extra=1)
        self.raw["verify"], self.raw["keep"] = ["NEB-10"], ["NEB-1"]
        self.out("check", file=self.sheet(self.raw))
        self.team_leads({"rec": "NEB-3", "appux": "NEB-2", "calm": "NEB-10"}, live=["rec"])
        self.linear.seed(self.issues)
        self.attempt("apply", go="yes")
        out = self.attempt("notices")
        self.assertEqual(out[0], "rec: tell rec-orchestrator (live)")
        self.assertIn("- NEB-10 NEB-10 title: left for the product owner to verify", out)
        self.assertIn('- NEB-3 NEB-3 title: renamed "RS1. Chat with the Guide" (was "NEB-3 title"); Ready label removed (needs shaping); labeled deep-dive', out)
        self.assertIn("appux: no live team lead; its next session reads Linear", out)
        self.assertIn("- NEB-2 NEB-2 title, and the 1 open ticket under it: Canceled", out)
        self.assertEqual(out[out.index("calm: no live team lead; its next session reads Linear") + 1:], ["- NEB-10 NEB-10 title: left for the product owner to verify"])  # nothing else is under or above NEB-10 but the verify
        self.assertFalse([l for l in out if l.startswith("- NEB-5")])

    def test_apply_needs_the_go_in_their_words_and_refuses_under_away_or_yolo(self):
        self.checked()
        self.assertEqual(self.fails("apply"), (1, 'apply needs the product owner\'s go, verbatim: --go "<their words>"'))
        (self.project_dir("shop") / "results" / "yolo.json").write_text('{"mode": "away"}')
        self.assertEqual(self.fails("apply", go="yes, do it"),
                         (1, "shop is under away: a reconcile changes Linear only on the product owner's own go; apply it once away is off"))
        self.assertEqual(self.linear.mutations, [])

    def test_apply_writes_each_change_in_order_with_its_comment(self):
        self.checked()
        out = self.attempt("apply", go="yes, do it")
        self.assertEqual([m[0] for m in self.linear.mutations if m[0] != "issueLabelCreate"][:3], ["issueUpdate", "commentCreate", "issueUpdate"])  # rename, its comment, labels
        self.assertEqual(self.linear.comment("NEB-5"), f"Canceled in the Shop reconcile ({TODAY}), on the product owner's go.\nApp UI and UX was a mistake.")
        self.assertEqual(self.linear.comment("NEB-4"), f"Canceled in the Shop reconcile ({TODAY}), on the product owner's go: merged into NEB-3 (RS1. Chat with the Guide).\nSame slice.")
        self.assertEqual(self.linear.comments["NEB-3"], [f'Renamed in the Shop reconcile ({TODAY}) from "NEB-3 title".',
                                                         f"Ready label removed in the Shop reconcile ({TODAY}): it needs shaping before it can be ready.\nSame slice."])
        self.assertLess(self.linear.canceled.index("NEB-5"), self.linear.canceled.index("NEB-2"))  # children before their parent
        self.assertEqual((self.linear.created[0]["labelIds"], self.linear.created[0]["stateId"], self.linear.created[0]["parentId"]), (["l-Feature", "l-needs-shaping"], "s-backlog", "NEB-1"))
        self.assertEqual(out[-1], "applied 7 of 7: 0 skipped, 0 failed; compose reconcile notices lists what each team lead must hear")
        self.assertEqual(json.loads(fc.fc_reconcile.sheet_path("p1").read_text())["go"], [{"quote": "yes, do it", "at": ANY}])

    def test_a_second_apply_after_a_failure_continues_and_comments_nothing_twice(self):
        self.checked()
        self.linear.fail_on = ("commentCreate", "NEB-2")
        self.assertIn("NEB-2:cancel FAILED: commentCreate failed", "\n".join(self.attempt("apply", go="yes")))
        self.linear.fail_on = None
        self.attempt("apply", go="yes")
        self.assertEqual((self.linear.comments_on("NEB-5"), self.linear.comments_on("NEB-2")), (1, 1))
        self.assertEqual(self.attempt("apply", go="yes"), ["nothing left to apply"])

    def test_a_ticket_that_changed_since_the_check_is_skipped_never_forced(self):
        self.checked()
        self.linear.issues["NEB-5"]["state"] = {"name": "Done", "type": "completed"}
        self.linear.issues["NEB-3"]["state"] = {"name": "In Progress", "type": "started"}  # a kept ticket: its rename still applies
        out = self.attempt("apply", go="yes")
        self.assertIn("NEB-5:cancel skipped: it is Done now, not Todo as at the check; sort it again", out)
        self.assertIn("NEB-3:title done", out)
        self.assertNotIn("NEB-5", self.linear.canceled)

    def test_a_missing_label_is_made_once_and_an_already_canceled_ticket_is_skipped(self):
        self.checked()
        self.linear.labels = [l for l in self.linear.labels if l["name"] not in ("needs-shaping", "deep-dive")]
        self.linear.issues["NEB-4"]["state"] = {"name": "Canceled", "type": "canceled"}
        out = self.attempt("apply", go="yes")
        self.assertIn("NEB-4:merge skipped: already Canceled", out)
        self.assertEqual(sorted(self.linear.labels_made), ["deep-dive", "needs-shaping"])

    def test_a_new_ticket_under_a_new_ticket_gets_the_identifier_the_first_was_given(self):
        self.checked()
        path = fc.fc_reconcile.sheet_path("p1")
        stored = json.loads(path.read_text())
        stored["new"].append({"key": "N2", "title": "Child", "parent": "N1", "label": "Story", "body": "", "deep_dive": True})
        path.write_text(json.dumps(stored))
        self.attempt("apply", go="yes")
        self.assertEqual((self.linear.created[1]["parentId"], self.linear.created[1]["labelIds"]), ("NEB-101", ["l-Story", "l-needs-shaping", "l-deep-dive"]))

    def test_apply_refuses_while_any_project_is_under_away_whatever_project_is_named(self):
        self.checked()
        self.project_dir("other")
        (self.project_dir("shop") / "results" / "yolo.json").write_text('{"mode": "yolo"}')
        for name in ("other", "shpo"):
            self.assertEqual(self.fails("apply", go="yes", project=name)[1], "shop is under yolo: a reconcile changes Linear only on the product owner's own go; apply it once yolo is off")
        self.assertEqual(self.linear.mutations, [])

    def test_a_recheck_after_a_skipped_cancel_never_files_a_new_ticket_twice(self):
        self.checked()
        self.linear.issues["NEB-5"]["state"] = {"name": "Done", "type": "completed"}
        self.attempt("apply", go="yes")
        self.out("check", file=self.sheet({**self.raw, "keep": self.raw["keep"] + ["NEB-101"]}))
        self.attempt("apply", go="yes")
        self.assertEqual(len(self.linear.created), 1)
        self.assertEqual(self.linear.comments_on("NEB-3"), 2)  # the rename and the Ready drop, once each

    def test_a_ticket_filed_is_never_filed_again_after_its_title_is_edited_in_the_sheet(self):
        self.checked()
        self.linear.issues["NEB-5"]["state"] = {"name": "Done", "type": "completed"}
        self.attempt("apply", go="yes")
        edited = {**self.raw, "keep": self.raw["keep"] + ["NEB-101"], "new": [{**self.raw["new"][0], "title": "A better title"}]}
        self.out("check", file=self.sheet(edited))
        out = self.attempt("apply", go="yes")
        self.assertEqual(len(self.linear.created), 1)
        self.assertIn("N1 already filed as NEB-101; edit it in Linear", out)

    def test_a_merge_half_written_refuses_a_recheck_until_apply_finishes_it(self):
        self.linear.seed(self.issues)
        raw = {"keep": ["NEB-1", "NEB-2", "NEB-3", "NEB-5"], "tickets": [{"id": "NEB-4", "sort": "merge", "into": "NEB-3", "reason": "dup"}],
               "reasons": {"dup": self.write("dup.md", "Same slice.")}}
        self.out("check", file=self.sheet(raw))
        self.linear.fail_on = ("issueUpdate", "NEB-4")
        self.attempt("apply", go="yes")
        self.linear.fail_on = None
        raw["tickets"][0]["into"] = "NEB-5"
        code, msg = self.fails("check", file=self.sheet(raw))
        self.assertTrue(msg.startswith("a reconcile is half applied (0 of 1 done; NEB-4:merge has its comment and relation written): finish it with compose reconcile apply --go"), msg)
        self.attempt("apply", go="yes")
        self.assertEqual([m[2]["o"] for m in self.linear.mutations if m[0] == "issueRelationCreate"], ["NEB-3"])
        self.assertEqual((self.linear.comments_on("NEB-4"), "NEB-4" in self.linear.canceled), (1, True))
        self.assertIn("merged into NEB-3", self.linear.comment("NEB-4"))

    def test_a_merge_whose_target_was_canceled_since_the_check_is_skipped_and_allows_a_recheck(self):
        self.checked()
        self.linear.issues["NEB-3"]["state"] = {"name": "Canceled", "type": "canceled"}
        out = self.attempt("apply", go="yes")
        self.assertIn("NEB-4:merge skipped: NEB-3 is Canceled now; sort it again", out)
        self.assertNotIn("NEB-4", self.linear.canceled)
        self.assertTrue(self.out("check", file=self.sheet({**self.raw, "keep": self.raw["keep"] + ["NEB-101"]}))[-1].startswith("checked: "))

    def test_a_recheck_is_allowed_when_only_skipped_actions_are_left_however_many_tickets_are_kept(self):
        self.checked(extra=10)
        self.linear.issues["NEB-5"]["state"] = {"name": "Done", "type": "completed"}
        self.attempt("apply", go="yes")
        self.assertTrue(self.out("check", file=self.sheet({**self.raw, "keep": self.raw["keep"] + ["NEB-101"]}))[-1].startswith("checked: "))

    def test_a_recheck_is_refused_while_a_failed_action_is_pending(self):
        self.checked()
        self.linear.fail_on = ("commentCreate", "NEB-2")
        self.attempt("apply", go="yes")
        code, msg = self.fails("check", file=self.sheet(self.raw))
        self.assertTrue(msg.startswith("a reconcile is half applied (6 of 7 done): finish it with compose reconcile apply --go"), msg)

    def test_a_cancel_that_fails_after_its_comment_comments_once_in_total(self):
        self.checked()
        self.linear.fail_on = ("issueUpdate", "NEB-2")
        self.attempt("apply", go="yes")
        self.linear.fail_on = None
        self.attempt("apply", go="yes")
        self.assertEqual((self.linear.comments_on("NEB-2"), "NEB-2" in self.linear.canceled), (1, True))

    def test_a_merge_that_fails_after_its_relation_neither_comments_nor_relates_again(self):
        self.checked()
        self.linear.fail_on = ("issueUpdate", "NEB-4")
        self.attempt("apply", go="yes")
        self.linear.fail_on = None
        self.attempt("apply", go="yes")
        self.assertEqual((self.linear.comments_on("NEB-4"), [m[0] for m in self.linear.mutations].count("issueRelationCreate")), (1, 1))

    def test_an_unexpected_error_in_one_action_is_recorded_and_the_rest_go_on(self):
        self.checked()
        self.linear.fail_on, self.linear.fail_exc = ("issueUpdate", "NEB-3"), KeyError
        out = self.attempt("apply", go="yes")
        self.assertTrue(any(l.startswith("NEB-3:title FAILED: ") for l in out), out)
        self.assertEqual(len(self.linear.created), 1)
        self.assertTrue(out[-1].endswith("compose reconcile notices lists what each team lead must hear"))

    def test_check_refuses_a_new_key_listed_twice_an_untitled_new_ticket_and_a_key_that_is_a_ticket_id(self):
        s = sheet(keep=["NEB-1", "NEB-2", "NEB-3", "NEB-4", "NEB-5"],
                  new=[{"key": "N1", "title": "A", "parent": "NEB-1", "label": "Feature"}, {"key": "N1", "title": "B", "parent": "NEB-1", "label": "Feature"},
                       {"key": "N2", "parent": "NEB-1", "label": "Feature"}, {"key": "N3", "title": "  ", "parent": "NEB-1", "label": "Feature"},
                       {"key": "NEB-3", "title": "C", "parent": "NEB-1", "label": "Story"}])
        problems = fc.fc_reconcile.check_sheet(s, self.issues)
        for p in ("new N1 is listed twice", "new N2 has no title", "new N3 has no title", "new NEB-3 has the id of an open ticket; give it a key like N1"):
            self.assertIn(p, problems)

    def test_show_never_crashes_on_a_stored_sheet_with_an_untitled_new_ticket(self):
        self.out("check", file=self.sheet())
        path = fc.fc_reconcile.sheet_path("p1")
        stored = json.loads(path.read_text()); stored["new"] = [{"key": "N1", "body": ""}]; path.write_text(json.dumps(stored))
        self.assertIn("- N1 (untitled) (? under ?, needs shaping)", self.out("show"))

    def test_a_team_without_a_backlog_state_fails_each_new_ticket_and_the_rest_go_on_to_the_summary(self):
        self.checked()
        self.linear.states = [{"id": "s-todo", "name": "Todo"}]
        out = self.attempt("apply", go="yes")
        self.assertIn("N1:create FAILED: team Engineering has no Backlog state", out)
        self.assertIn("NEB-2:cancel done", out)
        self.assertEqual(out[-1], "applied 6 of 7: 0 skipped, 1 failed; compose reconcile notices lists what each team lead must hear")

    def test_a_recheck_keeps_the_record_of_the_earlier_go(self):
        self.checked()
        self.linear.issues["NEB-5"]["state"] = {"name": "Done", "type": "completed"}
        self.attempt("apply", go="yes, do it")
        self.out("check", file=self.sheet({**self.raw, "keep": self.raw["keep"] + ["NEB-101"]}))
        self.assertEqual(json.loads(fc.fc_reconcile.sheet_path("p1").read_text())["go"], [{"quote": "yes, do it", "at": ANY}])

if __name__ == "__main__":
    unittest.main()
