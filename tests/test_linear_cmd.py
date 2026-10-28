import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, subprocess, tempfile, time, unittest
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
import fc_plan  # noqa: E402


KEY, URL = "lin_api_SECRET_do_not_print", "https://linear.app/acme/project/my-shop-abc123"


class LinearProject(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()  # resolved: macOS /var vs /private/var
        (self.repo / ".compose").mkdir()
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        self.calls, self.nodes, self.teams = [], None, [{"id": "t0", "name": "Design", "key": "DES"}, {"id": "t1", "name": "Engineering", "key": "ENG"}]
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_config, "WORKSPACE", "work"), (fc.fc_linear, "linear_gql", self.gql), (fc.fc_linear_cmd, "await_write", mock.Mock())):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        self.calls.append((" ".join(query.split()), variables))
        if "projectUpdate" in query or "project(id" in query:
            return {"projectUpdate": {"success": True}} if "projectUpdate" in query else {"project": {"content": self.content}}
        return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": self.teams}}] if self.nodes is None else self.nodes}}

    def args(self, action, *target, **kw):
        return SimpleNamespace(**{**dict(repo=str(self.repo), project=None, action=action, target=list(target), body=False, comments=False, json=False,
                                         append=False, description=False, any_project=False, fresh=False, label=None, parent=None, search=None, state=None,
                                         title=None, priority=None, blocked_by=None, file=None, section=None, expect=None, create=None, into=None, add_label=None, drop_label=None, add_blocked_by=None, drop_blocked_by=None), **kw})

    def fails(self, fn, *args):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as e:
            fn(*args)
        code = e.exception.code
        return (1 if isinstance(code, str) else code), ((code if isinstance(code, str) else "") + err.getvalue()).strip()

    def test_no_key_exits_naming_the_workspace_and_calls_no_verb(self):
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", None), mock.patch.object(fc.fc_linear_cmd, "verb_project", side_effect=AssertionError("verb ran without a key")):
            self.assertEqual(self.fails(fc.fc_linear_cmd.cmd_linear, self.args("project")), (1, "compose linear needs a Linear key for workspace 'work': pbpaste | compose workspace key work --linear"))
        p = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "compose"), "linear", "project", "--repo", str(self.repo)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1); self.assertIn("compose linear needs a Linear key for workspace 'default': pbpaste | compose workspace key default --linear", p.stderr)

    def test_with_a_key_it_dispatches_to_the_verb_and_wraps_its_errors(self):
        for action in ("project", "get", "doc"):
            with mock.patch.object(fc.fc_linear_cmd, f"verb_{action}") as verb:
                fc.fc_linear_cmd.cmd_linear(a := self.args(action))
            verb.assert_called_once_with(a)
        for exc in (RuntimeError("boom"), OSError("boom")):
            with mock.patch.object(fc.fc_linear_cmd, "verb_list", side_effect=exc):
                self.assertEqual(self.fails(fc.fc_linear_cmd.cmd_linear, self.args("list")), (1, "linear list: boom"))

    def test_project_reads_linear_json_and_picks_the_named_team(self):
        got = fc.fc_linear_cmd.linear_project(str(self.repo))
        self.assertEqual(got, {"id": "p1", "name": "Shop", "url": URL, "team": {"id": "t1", "name": "Engineering", "key": "ENG"}})
        self.assertEqual([v for _, v in self.calls], [{"slug": "abc123"}]); self.assertIn("projects(filter: {slugId: {eq: $slug}}) { nodes { id name url teams { nodes { id name key } } } }", self.calls[0][0])
        with redirect_stdout(io.StringIO()) as out:
            fc.fc_linear_cmd.verb_project(self.args("project"))
        self.assertEqual(out.getvalue().strip(), f"Shop on team Engineering: {URL}")

    def test_project_body_prints_the_description_and_each_body_flag_writes_it(self):
        self.content = "# Shop\n\n## Goals\n\nold\n\n## Notes\n\nkeep\n"
        written = lambda: [v for q, v in self.calls if "projectUpdate(id: $id, input: $in) { success }" in q]
        (self.repo / "body.md").write_text("new goals\n")
        hashes = ['--- section hashes (--section "<heading>" --expect <hash>)', *(f"{h} {s}" for s, h in fc_plan.section_hashes(self.content))]
        for kw, want in (({"body": True}, [f"Shop on team Engineering: {URL}", *self.content.strip().split("\n"), *hashes]), ({"section": "## Goals"}, ["updated Shop"]),
                         ({"append": True}, ["updated Shop"]), ({"description": True}, ["updated Shop"])):
            with redirect_stdout(io.StringIO()) as out:
                fc.fc_linear_cmd.verb_project(self.args("project", **({} if "body" in kw else {"file": str(self.repo / "body.md")}), **kw))
            self.assertEqual(out.getvalue().strip().split("\n"), want)
        self.assertEqual(written(), [{"id": "p1", "in": {"content": "# Shop\n\n## Goals\n\nnew goals\n\n## Notes\n\nkeep\n"}},
                                     {"id": "p1", "in": {"content": self.content + "\nnew goals\n"}}, {"id": "p1", "in": {"content": "new goals\n"}}])
        self.assertEqual([v for q, v in self.calls if "project(id: $id) { content }" in q], [{"id": "p1"}] * 4)
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_project, self.args("project", section="## Goals", description=True)), (1, "project takes one of --section, --append, --description"))
        for kw in ({}, {"body": True}):  # a body file with no body flag is never dropped silently
            self.assertEqual(self.fails(fc.fc_linear_cmd.verb_project, self.args("project", file=str(self.repo / "body.md"), **kw)),
                             (1, "project --file needs --section, --append, or --description"))
        self.assertEqual(len(written()), 3)

    def test_project_link_as_pasted_resolves_to_its_slug(self):
        for link in (f"{URL}/overview", f"{URL}/", f"{URL}/issues?layout=list", f"{URL}#top", "my-shop-abc123"):
            (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": link, "team": "Engineering"}))
            self.calls.clear()
            self.assertEqual(fc.fc_linear_cmd.linear_project(str(self.repo))["id"], "p1")
            self.assertEqual([v for _, v in self.calls], [{"slug": "abc123"}], link)

    def test_project_failures(self):
        self.teams = self.teams[:1]
        self.assertEqual(self.fails(fc.fc_linear_cmd.linear_project, str(self.repo)), (1, "project Shop is not on team Engineering"))
        self.nodes = []
        self.assertEqual(self.fails(fc.fc_linear_cmd.linear_project, str(self.repo)), (1, f"Linear has no project {URL}"))
        (self.repo / ".compose" / "linear.json").unlink()
        self.assertEqual(self.fails(fc.fc_linear_cmd.linear_project, str(self.repo)), (1, f"no .compose/linear.json in {self.repo}"))


class LinkedWorktrees(unittest.TestCase):
    """The repo's `.compose/linear.json` is one file in its main checkout, and every worktree of the repo reads it: a linked
    worktree or a subfolder of one finds the project the user wrote down once."""
    args, gql = LinearProject.args, LinearProject.gql

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()  # holds the main checkout and a linked worktree
        self.calls, self.nodes, self.teams = [], None, [{"id": "t1", "name": "Engineering", "key": "ENG"}]
        self.main, self.wt = self.repo / "main", self.repo / "wt"
        self.main.mkdir(); self.wt.mkdir()
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.gql), (fc.fc_core, "checkout", self.checkout)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def checkout(self, path):
        here = Path(path).resolve()
        top = next((t for t in (self.wt, self.main) if t == here or t in here.parents), None)
        return top and fc.fc_core.Checkout((top, self.main / ".git" / "worktrees" / top.name, self.main / ".git"))

    def link(self, checkout):
        (checkout / ".compose").mkdir(exist_ok=True)
        (checkout / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))

    def project_line(self, repo):
        with redirect_stdout(io.StringIO()) as out:
            fc.fc_linear_cmd.verb_project(self.args("project", repo=str(repo)))
        return out.getvalue().strip()

    def test_a_linked_worktree_and_its_subfolders_read_the_main_checkouts_file(self):
        self.link(self.main)
        self.assertEqual(self.project_line(self.wt), f"Shop on team Engineering: {URL}")
        (self.wt / "sub").mkdir()
        self.assertEqual(self.project_line(self.wt / "sub"), f"Shop on team Engineering: {URL}")  # from a subfolder too

    def test_the_main_checkouts_file_wins_over_an_old_copy(self):
        self.link(self.main)
        (self.wt / ".compose").mkdir()
        (self.wt / ".compose" / "linear.json").write_text(json.dumps({"project": "https://linear.app/acme/project/old-zzz999", "team": "Engineering"}))
        self.project_line(self.wt)
        self.assertEqual([v for _, v in self.calls], [{"slug": "abc123"}])

    def test_a_file_written_only_in_a_linked_worktree_is_still_read(self):
        self.link(self.wt)  # the main checkout has none
        self.assertEqual(self.project_line(self.wt), f"Shop on team Engineering: {URL}")

    def test_none_anywhere_names_the_main_checkout(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as e:
            fc.fc_linear_cmd.linear_project(str(self.wt))
        self.assertEqual(e.exception.code, f"no .compose/linear.json in {self.main.resolve()}")


class LinearGet(unittest.TestCase):
    QUERY = "issue(id: $id) { identifier title description state { name } labels { nodes { name } } project { id name } parent { identifier } inverseRelations(first: 50) { nodes { type issue { identifier state { name } } } } comments { nodes { body createdAt user { name } } } }"
    args, fails = LinearProject.args, LinearProject.fails

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir()
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        self.calls = []
        self.issue = {"identifier": "ENG-7", "title": "Fix the cart", "description": "Steps here.", "state": {"name": "In Progress"},
                      "labels": {"nodes": [{"name": "bug"}, {"name": "ui"}]}, "project": {"id": "p1", "name": "Shop"}, "parent": None,
                      "inverseRelations": {"nodes": []},
                      "comments": {"nodes": [{"body": "second", "createdAt": "2026-03-04T09:00:00.000Z", "user": {"name": "Bob"}},
                                             {"body": "first", "createdAt": "2026-01-02T23:30:00.000Z", "user": {"name": "Ann"}}]}}
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.gql)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        self.calls.append((" ".join(query.split()), variables))
        if "projects(" in query:
            return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}}
        return {"issue": self.issue}

    def get(self, *target, **kw):
        with redirect_stdout(io.StringIO()) as out:
            fc.fc_linear_cmd.verb_get(self.args("get", *target, **kw))
        return [line for line in out.getvalue().splitlines() if line.strip()]

    def test_the_same_issue_read_again_within_30_seconds_comes_from_the_cache_unless_fresh(self):
        reads = lambda: len([1 for q, v in self.calls if self.QUERY in q])
        first = self.get("ENG-7")
        self.issue["title"] = "Changed in Linear"
        with mock.patch.object(fc.fc_linear_cmd.time, "time", return_value=time.time() + 29):
            self.assertEqual(self.get("ENG-7", body=True), first + ["Steps here."])  # other flags, same read
            self.assertEqual(self.get("ENG-8"), ["ENG-7 In Progress bug Changed in Linear"])  # another issue is its own read
        self.assertEqual(reads(), 2)
        with mock.patch.object(fc.fc_linear_cmd.time, "time", return_value=time.time() + 31):
            self.assertEqual(self.get("ENG-7"), ["ENG-7 In Progress bug Changed in Linear"])
        self.assertEqual(reads(), 3)
        self.issue["title"] = "Changed again"
        self.assertEqual(self.get("ENG-7", fresh=True), ["ENG-7 In Progress bug Changed again"])
        self.assertEqual(reads(), 4)

    def test_a_write_forgets_the_cached_reads(self):
        self.get("ENG-7")
        self.issue["title"] = "Changed in Linear"
        with mock.patch.object(fc.fc_linear_cmd, "verb_comment"):
            fc.fc_linear_cmd.cmd_linear(self.args("comment", "ENG-7"))
        self.assertEqual(self.get("ENG-7"), ["ENG-7 In Progress bug Changed in Linear"])

    def test_another_linear_project_does_not_share_the_cache(self):
        self.get("ENG-7")
        other = Path(tempfile.mkdtemp()).resolve()
        (other / ".compose").mkdir(); (other / ".compose" / "linear.json").write_text(json.dumps({"project": URL + "-other", "team": "Engineering"}))
        self.issue["title"] = "Changed in Linear"
        with redirect_stdout(io.StringIO()) as out:
            fc.fc_linear_cmd.verb_get(self.args("get", "ENG-7", repo=str(other)))
        self.assertIn("Changed in Linear", out.getvalue())

    def test_blockers_follow_the_line_and_only_blocks_relations_count(self):
        line = "ENG-7 In Progress bug Fix the cart"
        rel = lambda kind, ident, state: {"type": kind, "issue": {"identifier": ident, "state": {"name": state}}}
        self.issue["inverseRelations"]["nodes"] = [rel("blocks", "ENG-2", "Todo"), rel("related", "ENG-4", "Done"), rel("blocks", "ENG-3", "Done")]
        self.assertEqual(self.get("ENG-7"), [line, "blocked by: ENG-2 (Todo), ENG-3 (Done)"])
        self.assertEqual(self.get("ENG-7", body=True), [line, "blocked by: ENG-2 (Todo), ENG-3 (Done)", "Steps here."])

    def test_body_then_comments_oldest_first(self):
        line, ann, bob = "ENG-7 In Progress bug Fix the cart", ["--- Ann 2026-01-02", "first"], ["--- Bob 2026-03-04", "second"]
        self.assertEqual(self.get("ENG-7", body=True, comments=True), [line, "Steps here."] + ann + bob)
        self.assertEqual(self.get("ENG-7", comments=True), [line] + ann + bob)
        self.assertEqual(self.get("ENG-7", body=True), [line, "Steps here."])

    def test_body_lists_each_heading_with_its_section_hash_before_the_comments(self):
        self.issue["description"] = "## Goal\n\ngoal\n\n## Notes\n\nspec line\n\n### Codebase brief\n\nwhere things live\n\n```sh\n# not a heading\n```\n"
        hash_of = lambda h: fc_plan.section_hash(self.issue["description"], h)
        got = self.get("ENG-7", body=True, comments=True)
        self.assertEqual(got[got.index("```") + 1:], ['--- section hashes (--section "<heading>" --expect <hash>)', f"{hash_of('## Goal')} ## Goal",
                                                      f"{hash_of('## Notes')} ## Notes", f"{hash_of('### Codebase brief')} ### Codebase brief",
                                                      "--- Ann 2026-01-02", "first", "--- Bob 2026-03-04", "second"])
        self.assertEqual(len({hash_of(h) for h in ("## Goal", "## Notes", "### Codebase brief")}), 3)
        self.assertRegex(hash_of("## Goal"), r"^[0-9a-f]{8}$")

    def test_out_of_scope_refuses_unless_any_project(self):
        self.issue["project"] = {"id": "p9", "name": "Shop"}
        for kw in ({}, {"json": True}):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(self.fails(fc.fc_linear_cmd.verb_get, self.args("get", "ENG-7", fresh=True, **kw)), (1, "ENG-7 is not in project Shop; pass --any-project to use it"))
            self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.get("ENG-7", any_project=True, fresh=True), ["ENG-7 In Progress bug Fix the cart"])

class NextTaskId(unittest.TestCase):
    """The planner's source of a free task id: every compose block of the Linear project, whatever linear.json scopes, and results/."""
    args = LinearProject.args

    def test_it_is_one_above_the_highest_id_in_the_project_or_in_results(self):
        repo = self.repo = Path(tempfile.mkdtemp()).resolve()
        (repo / ".compose" / "shop" / "results").mkdir(parents=True)
        (repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        (repo / ".compose" / "shop" / "results" / "051.json").write_text("{}")
        block = lambda n: f"## Goal\n\nx\n\n```compose\nid: {n}\nfiles: a.css\ntest: t\n```\n"
        pages = {None: [{"description": block("048")}, {"description": "no block"}], "c1": [{"description": block("049")}]}
        calls = []
        def gql(query, variables):
            calls.append(variables)
            if "projects(" in query:
                return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}}
            after = variables.get("after")
            return {"issues": {"pageInfo": {"hasNextPage": after is None, "endCursor": "c1"}, "nodes": pages[after]}}
        with mock.patch.object(fc.fc_linear, "linear_gql", gql), mock.patch.object(fc.fc_core, "project_dir", return_value=repo / ".compose" / "shop"), redirect_stdout(io.StringIO()) as out:
            fc.fc_linear_cmd.verb_next_task_id(self.args("next-task-id", project="shop"))
        self.assertEqual(out.getvalue(), "052\n")
        self.assertEqual(calls[1]["f"], {"project": {"id": {"eq": "p1"}}, "description": {"contains": "```compose"}})


class LinearList(unittest.TestCase):
    QUERY = "issues(first: 100, after: $after, filter: $f) { pageInfo { hasNextPage endCursor } nodes { identifier title state { name } labels { nodes { name } } } }"
    args, fails = LinearProject.args, LinearProject.fails

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir()
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        node = lambda n, state, labels: {"identifier": f"ENG-{n}", "title": f"Task {n}", "state": {"name": state}, "labels": {"nodes": [{"name": x} for x in labels]}}
        self.pages = {None: ([node(3, "Todo", ["ui"]), node(1, "Done", [])], True, "c1"), "c1": ([node(2, "In Progress", ["bug", "ui"])], True, "c2"), "c2": ([], False, "c3")}
        self.nodes = [n for page in self.pages.values() for n in page[0]]
        self.calls = []
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.gql)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        self.calls.append((" ".join(query.split()), variables))
        if "projects(" in query:
            return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}}
        nodes, more, cursor = self.pages[variables.get("after")]
        return {"issues": {"pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}}

    def run_list(self, **kw):
        with redirect_stdout(io.StringIO()) as out:
            fc.fc_linear_cmd.verb_list(self.args("list", **kw))
        return out.getvalue(), [v for q, v in self.calls if self.QUERY in q]

    def test_without_parent_or_search_it_refuses_and_queries_nothing(self):
        for kw in ({}, {"state": "Todo", "label": ["ui"]}):
            self.assertEqual(self.fails(fc.fc_linear_cmd.verb_list, self.args("list", **kw)), (1, 'list needs --parent <id> or --search "<terms>"'))
        self.assertFalse([q for q, _ in self.calls if "issues(" in q])

    def test_reads_every_page_and_prints_each_line_in_order(self):
        out, sent = self.run_list(parent="ENG-1")
        self.assertEqual(out.splitlines(), [fc.fc_linear_cmd.issue_line(n) for n in self.nodes])
        self.assertEqual([v.get("after") for v in sent], [None, "c1", "c2"])
        self.assertEqual([v["f"] for v in sent], [{"project": {"id": {"eq": "p1"}}, "parent": {"id": {"eq": "ENG-1"}}}] * 3)

    def test_filter_holds_each_flag_given(self):
        _, sent = self.run_list(search="cart total", state="todo", label=["bug", "ui"])
        self.assertEqual(sent[0]["f"], {"project": {"id": {"eq": "p1"}}, "searchableContent": {"contains": "cart total"},
                                        "state": {"name": {"eqIgnoreCase": "todo"}}, "labels": {"some": {"name": {"in": ["bug", "ui"]}}}})
        self.calls.clear()
        _, sent = self.run_list(parent="ENG-9", search="cart")
        self.assertEqual(sent[0]["f"], {"project": {"id": {"eq": "p1"}}, "parent": {"id": {"eq": "ENG-9"}}, "searchableContent": {"contains": "cart"}})

class LinearCreate(unittest.TestCase):
    TEAM, CREATE = "team(id: $id) { name states { nodes { id name } } labels(first: 250) { nodes { id name } } }", "issueCreate(input: $in) { issue { id identifier } }"
    args, fails = LinearProject.args, LinearProject.fails

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir()
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        self.calls, self.team = [], {"name": "Engineering", "states": {"nodes": [{"id": "s1", "name": "Todo"}, {"id": "s2", "name": "In Progress"}]},
                                     "labels": {"nodes": [{"id": "l1", "name": "Bug"}, {"id": "l2", "name": "ui"}]}}
        tty = mock.Mock(**{"isatty.return_value": True, "read.side_effect": AssertionError("read stdin from a terminal")})
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.gql), (sys, "stdin", tty)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        self.calls.append((" ".join(query.split()), variables))
        if "projects(" in query:
            return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}}
        if "issueCreate" in query:
            return {"issueCreate": {"issue": {"id": "new-id", "identifier": "ENG-12"}}}
        return {"team": self.team}

    def create(self, **kw):
        with mock.patch.object(fc.fc_linear, "linear_issue", return_value={"id": "parent-id"}) as issue, mock.patch.object(fc.fc_linear, "linear_blocked_by") as blocked, redirect_stdout(io.StringIO()) as out:
            fc.fc_linear_cmd.verb_create(self.args("create", title="Fix it", **kw))
        self.assertEqual(out.getvalue().strip(), "created ENG-12")
        self.assertEqual([v for q, v in self.calls if self.TEAM in q], [{"id": "t1"}])
        return [v["in"] for q, v in self.calls if self.CREATE in q], issue, blocked

    def test_add_label_or_drop_label_on_create_is_refused_and_creates_nothing(self):
        for kw in ({"add_label": ["Bug"]}, {"drop_label": ["Bug"]}):
            self.assertEqual(self.fails(fc.fc_linear_cmd.cmd_linear, self.args("create", title="Fix it", **kw)),
                             (1, "--add-label and --drop-label change an existing issue: compose linear update <id> takes them, and create takes --label"))
        self.assertEqual(self.calls, [])

    def test_read_body_takes_the_file_then_piped_stdin_then_nothing(self):
        (body := self.repo / "body.md").write_text("from file\n")
        with mock.patch("sys.stdin", io.StringIO("from stdin")):
            self.assertEqual(fc.fc_linear_cmd.read_body(self.args("create", file=str(body))), "from file\n")
            self.assertEqual(fc.fc_linear_cmd.read_body(self.args("create")), "from stdin")
        self.assertEqual(fc.fc_linear_cmd.read_body(self.args("create")), "")

    def test_state_and_labels_match_names_case_insensitively_for_the_flags_given(self):
        sl = lambda **kw: fc.fc_linear_cmd.state_and_labels(self.team, self.args("create", **kw))
        got = sl(state="in progress", label=["bug", "UI"])
        self.assertEqual((sorted(got), got["stateId"]), (["labelIds", "stateId"], "s2")); self.assertCountEqual(got["labelIds"], ["l1", "l2"])
        self.assertEqual((sl(), sl(state="TODO"), sl(label=["Ui"])), ({}, {"stateId": "s1"}, {"labelIds": ["l2"]}))
        self.assertEqual(self.fails(lambda: sl(state="Doing")), (1, "team Engineering has no state 'Doing'"))
        self.assertEqual(self.fails(lambda: sl(label=["bug", "perf"])), (1, "team Engineering has no label 'perf'; compose linear labels --create perf makes it"))

    def test_create_sends_every_flag_then_sets_the_blockers(self):
        (body := self.repo / "body.md").write_text("Steps.")
        sent, issue, blocked = self.create(file=str(body), parent="ENG-1", state="todo", label=["BUG"], blocked_by=["ENG-2,ENG-3"])
        self.assertEqual(sent, [{"teamId": "t1", "projectId": "p1", "title": "Fix it", "description": "Steps.", "parentId": "parent-id", "stateId": "s1", "labelIds": ["l1"]}])
        issue.assert_any_call("ENG-1"); blocked.assert_called_once_with({"id": "new-id"}, ["ENG-2", "ENG-3"])

    def test_a_repeated_blocked_by_keeps_every_blocker(self):
        # a planner passed --blocked-by twice to create and only the last blocker was linked
        import compose
        got = []
        argv = ["compose", "linear", "create", "--title", "T", "--blocked-by", "ENG-2", "--blocked-by", "ENG-3,ENG-4"]
        with mock.patch.object(compose.fc_linear_cmd, "cmd_linear", side_effect=lambda a: got.append(a.blocked_by)), mock.patch("sys.argv", argv), \
             mock.patch.object(compose.fc_proc, "nice_floor"), mock.patch.object(compose.fc_proc, "on_termination"):  # the parse alone, not the process setup
            compose.main()
        self.assertEqual(fc.fc_linear_cmd.ids(got[0]), ["ENG-2", "ENG-3", "ENG-4"])
        _, _, blocked = self.create(blocked_by=got[0])
        blocked.assert_called_once_with({"id": "new-id"}, ["ENG-2", "ENG-3", "ENG-4"])

    def test_priority_takes_a_name_or_linears_number_on_create_and_update(self):
        for given, want in (("urgent", 1), ("High", 2), ("medium", 3), ("low", 4), ("none", 0), ("0", 0), ("4", 4)):
            with self.subTest(given=given):
                self.calls = []
                with mock.patch("sys.stdin", io.StringIO("")):
                    self.assertEqual(self.create(priority=given)[0][0]["priority"], want)
        for bad in ("5", "p1", "-1"):
            with self.subTest(bad=bad):
                self.calls = []
                self.assertEqual(self.fails(fc.fc_linear_cmd.verb_create, self.args("create", title="Fix it", priority=bad)),
                                 (1, f"--priority is one of none, urgent, high, medium, low or 0-4, not {bad!r}"))
                self.assertFalse([q for q, _ in self.calls if self.CREATE in q])

    def test_refuses_without_a_title_or_with_an_unknown_name_and_creates_nothing(self):
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_create, self.args("create")), (1, "create needs --title"))
        self.assertIn("has no label 'perf'", self.fails(fc.fc_linear_cmd.verb_create, self.args("create", title="Fix it", label=["perf"]))[1])
        self.assertFalse([q for q, _ in self.calls if "issueCreate" in q])


class LinearUpdate(unittest.TestCase):
    READ, UPDATE = "issue(id: $id) { id identifier description project { id name } team { name states { nodes { id name } } labels(first: 250) { nodes { id name } } } }", "issueUpdate(id: $id, input: $in) { success }"
    args, fails = LinearProject.args, LinearProject.fails

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir(); (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        self.calls, self.issue = [], {"id": "iid", "identifier": "ENG-7", "description": "# Cart\n\n## Plan\n\nold\n\n## Notes\n\nkeep\n", "project": {"id": "p1", "name": "Shop"},
                                      "team": {"name": "Engineering", "states": {"nodes": [{"id": "s1", "name": "Todo"}, {"id": "s2", "name": "In Progress"}]}, "labels": {"nodes": [{"id": "l1", "name": "Bug"}]}}}
        tty = mock.Mock(**{"isatty.return_value": True, "read.side_effect": AssertionError("read stdin from a terminal")})
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.gql), (sys, "stdin", tty), (fc.fc_linear_cmd, "await_write", mock.Mock()), (fc.fc_core, "worktree_branches", lambda repo: [(self.repo, "main")])):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        self.calls.append((" ".join(query.split()), variables))
        if "projects(" in query:
            return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}}
        return {"issueUpdate": {"success": True}} if "issueUpdate" in query else {"issue": self.issue}

    def update(self, body=None, **kw):
        self.calls.clear()
        if body is not None:
            (self.repo / "body.md").write_text(body); kw["file"] = str(self.repo / "body.md")
        with mock.patch.object(fc.fc_linear, "linear_issue", return_value={"id": "parent-id"}) as issue, mock.patch.object(fc.fc_linear, "linear_blocked_by", return_value=[]) as blocked, redirect_stdout(io.StringIO()) as out:
            fc.fc_linear_cmd.verb_update(self.args("update", "ENG-7", **kw))
        self.assertEqual(out.getvalue().strip(), "updated ENG-7")
        self.assertEqual([v for q, v in self.calls if self.READ in q], [{"id": "ENG-7"}])
        sent = [v for q, v in self.calls if self.UPDATE in q]  # at most one; none when only the blockers change
        self.assertLessEqual(len(sent), 1); self.assertTrue(all(s["id"] in ("iid", "ENG-7") for s in sent))
        return (sent[0]["in"] if sent else None), issue, blocked

    def test_replace_section_swaps_only_that_section_or_appends_it(self):
        rs, body, want = fc_plan.replace_section, "# Cart\n\n## Plan\n\nold\n\n### Steps\n\n1. a\n\n## Notes\n\nkeep\n", "# Cart\n\n## Plan\n\nnew\n\n## Notes\n\nkeep\n"
        self.assertEqual((rs(body, "## Plan", "  new \n\n"), rs(body, "## Plan", "## Plan\n\nnew\n")), (want, want))
        self.assertEqual(rs("# Cart\n\n## Plan\nold\n", "## Plan", "new"), "# Cart\n\n## Plan\n\nnew\n")
        self.assertEqual(rs("# Cart\n\n## Planning\n\nx\n\n\n", "## Plan", "new\n"), "# Cart\n\n## Planning\n\nx\n\n## Plan\n\nnew\n")

    def test_replace_section_renames_on_a_leading_heading_and_removes_on_an_empty_body(self):
        rs, body = fc_plan.replace_section, "# Cart\n\n## Plan\n\nold\n\n## Notes\n\nkeep\n"
        self.assertEqual(rs(body, "## Plan", "## Stories\n\nnew\n"), "# Cart\n\n## Stories\n\nnew\n\n## Notes\n\nkeep\n")
        self.assertEqual(rs(body, "## Notes", "## Later\nkept\n"), "# Cart\n\n## Plan\n\nold\n\n## Later\n\nkept\n")
        self.assertEqual((rs(body, "## Plan", ""), rs(body, "## Notes", " \n")), ("# Cart\n\n## Notes\n\nkeep\n", "# Cart\n\n## Plan\n\nold\n"))
        self.assertEqual((rs(body, "## Gone", ""), rs("## Plan\n\nold\n", "## Plan", "")), (body, ""))

    def test_replace_section_matches_the_heading_only_as_a_whole_line_wherever_it_sits(self):
        rs = fc_plan.replace_section
        self.assertEqual(rs("# Cart\n\nSee ## Plan.\n\n## Planned\nx", "## Plan", "new"), "# Cart\n\nSee ## Plan.\n\n## Planned\nx\n\n## Plan\n\nnew\n")
        self.assertEqual(rs("## Plan\nold\n## Notes\nkeep", "## Plan", "new"), "## Plan\n\nnew\n\n## Notes\nkeep\n")

    def test_fields_come_from_the_issue_team_and_the_parent(self):
        sent, issue, blocked = self.update(title="New", parent="ENG-1", state="in progress", label=["bug"])
        self.assertEqual(sent, {"title": "New", "parentId": "parent-id", "stateId": "s2", "labelIds": ["l1"]})
        issue.assert_any_call("ENG-1"); blocked.assert_not_called()

    def test_each_body_flag_sets_the_description(self):
        self.assertEqual(self.update("new plan", section="## Plan")[0], {"description": "# Cart\n\n## Plan\n\nnew plan\n\n## Notes\n\nkeep\n"})
        with mock.patch("sys.stdin", io.StringIO("\n more \n")):
            self.assertEqual(self.update(append=True)[0], {"description": "# Cart\n\n## Plan\n\nold\n\n## Notes\n\nkeep\n\nmore\n"})
        self.assertEqual(self.update("whole\n\n", description=True, title="T")[0], {"description": "whole\n\n", "title": "T"})

    def test_priority_alone_is_a_change_and_a_bad_one_reads_nothing(self):
        self.assertEqual(self.update(priority="high")[0], {"priority": 2})
        self.calls = []
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7", priority="soon")),
                         (1, "--priority is one of none, urgent, high, medium, low or 0-4, not 'soon'"))
        self.assertEqual(self.calls, [])

    def test_blockers_are_set_on_the_read_issue(self):
        sent, _, blocked = self.update(blocked_by=["ENG-2,ENG-3"])
        self.assertEqual((sent, blocked.call_args.args), (None, (self.issue, ["ENG-2", "ENG-3"])))  # no empty issueUpdate before them
        (blocked := self.update(blocked_by=[""])[2]).assert_called_once(); self.assertEqual(blocked.call_args.args[0], self.issue); self.assertFalse([x for x in blocked.call_args.args[1] if x])
        self.assertEqual(self.update(blocked_by=["ENG-2"], title="T")[0], {"title": "T"})

    def test_a_body_file_without_a_body_flag_refuses_and_writes_nothing(self):
        (self.repo / "brief.md").write_text("## Goal\nnew brief\n")
        for kw in ({}, {"title": "T"}, {"blocked_by": ["ENG-2"]}):
            self.assertEqual(self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7", file=str(self.repo / "brief.md"), **kw)),
                             (1, "update --file needs --section, --append, or --description"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.update("## Goal\nnew brief\n", description=True)[0], {"description": "## Goal\nnew brief\n"})

    def test_add_and_drop_label_keep_the_other_labels(self):
        self.issue["team"]["labels"]["nodes"] += [{"id": "l9", "name": "Ready"}, {"id": "l8", "name": "needs-shaping"}]
        sent, _, _ = self.update(drop_label=["ready"], add_label=["needs-shaping"])
        self.assertEqual(sent, {"removedLabelIds": ["l9"], "addedLabelIds": ["l8"]})

    def test_label_and_add_or_drop_together_refuse_and_an_unknown_name_names_the_create_line(self):
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7", label=["Bug"], drop_label=["Bug"])),
                         (1, "update takes --label (the whole list) or --add-label/--drop-label, not both"))
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7", add_label=["deep-dive"])),
                         (1, "team Engineering has no label 'deep-dive'; compose linear labels --create deep-dive makes it"))

    def test_nothing_to_change_refuses_before_reading_the_issue(self):
        want = (1, "update needs --title, --state, --parent, --label, --add-label, --drop-label, --priority, --blocked-by, --add-blocked-by, --drop-blocked-by, "
                   "or a body: --section, --append, or --description with --file or stdin")
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7")), want)
        self.assertEqual(self.calls, [])

    def test_replace_section_ends_a_section_at_the_next_heading_of_its_level_or_higher(self):
        rs, body = fc_plan.replace_section, "# Cart\n\n## Notes\n\n### A\n\na\n\n### B\n\nb\n\n## Plan\n\nplan\n\n# Appendix\n\nz\n"
        self.assertEqual(rs(body, "### A", "### A\n\nnew a"), "# Cart\n\n## Notes\n\n### A\n\nnew a\n\n### B\n\nb\n\n## Plan\n\nplan\n\n# Appendix\n\nz\n")
        self.assertEqual(rs(body, "### B", "new b"), "# Cart\n\n## Notes\n\n### A\n\na\n\n### B\n\nnew b\n\n## Plan\n\nplan\n\n# Appendix\n\nz\n")
        self.assertEqual(rs(body, "### A", "### C\n\nc"), "# Cart\n\n## Notes\n\n### C\n\nc\n\n### B\n\nb\n\n## Plan\n\nplan\n\n# Appendix\n\nz\n")  # renames
        self.assertEqual(rs(body, "## Plan", "p"), "# Cart\n\n## Notes\n\n### A\n\na\n\n### B\n\nb\n\n## Plan\n\np\n\n# Appendix\n\nz\n")
        self.assertEqual(rs(body, "## Notes", "n"), "# Cart\n\n## Notes\n\nn\n\n## Plan\n\nplan\n\n# Appendix\n\nz\n")  # an H2 still takes its H3s

    def test_replace_section_reads_no_heading_inside_fenced_code(self):
        rs, body = fc_plan.replace_section, "## Plan\n\n```sh\n# install\n## Notes\nmake\n```\n\n~~~\n### Steps\n~~~\n\n## Notes\n\nkeep\n"
        self.assertEqual(rs(body, "## Plan", "new"), "## Plan\n\nnew\n\n## Notes\n\nkeep\n")
        self.assertEqual(rs(body, "## Notes", "kept"), "## Plan\n\n```sh\n# install\n## Notes\nmake\n```\n\n~~~\n### Steps\n~~~\n\n## Notes\n\nkept\n")
        self.assertEqual([h for h, _ in fc_plan.section_hashes(body)], ["## Plan", "## Notes"])

    def test_a_section_write_with_a_stale_hash_refuses_and_writes_nothing(self):
        read = "# Cart\n\n## Notes\n\nspec line\n"  # what the team lead read
        seen = fc_plan.section_hash(read, "## Notes")
        self.issue["description"] = "# Cart\n\n## Notes\n\nspec line\n\n### Codebase brief\n\nwhere things live\n"  # explore wrote in between
        (self.repo / "notes.md").write_text("## Notes\n\nspec line\nmockup line\n")
        code, msg = self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7", section="## Notes", expect=seen, file=str(self.repo / "notes.md")))
        self.assertEqual((code, msg), (1, f"## Notes changed since you read it (hash {seen}): nothing written; read it again, keep what changed, "
                                          "and write it back with the hash it shows now"))
        self.assertFalse([q for q, _ in self.calls if "issueUpdate" in q])
        self.issue["description"] = "# Cart\n"
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7", section="## Notes", expect=seen, file=str(self.repo / "notes.md"))),
                         (1, f"## Notes is gone since you read it (hash {seen}): nothing written; read the body again and write from it"))
        self.assertFalse([q for q, _ in self.calls if "issueUpdate" in q])

    def test_a_section_write_with_the_current_hash_goes_through(self):
        now = fc_plan.section_hash(self.issue["description"], "## Plan")
        self.assertEqual(self.update("new plan", section="## Plan", expect=now)[0], {"description": "# Cart\n\n## Plan\n\nnew plan\n\n## Notes\n\nkeep\n"})

    def test_expect_needs_a_section_and_a_section_needs_a_heading_line(self):
        for kw in ({"description": True}, {"append": True}, {"title": "T"}):
            self.assertEqual(self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7", expect="abcd1234", **kw)),
                             (1, "update --expect needs --section: it checks that section's hash"))
        for heading in ("Notes", "##Notes", "####### Deep"):
            self.assertEqual(self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7", section=heading)),
                             (1, f'update --section takes a heading line such as "## Notes", not {heading!r}'))
        self.assertEqual(self.calls, [])

    def test_refuses_two_body_flags_or_another_project_and_updates_nothing(self):
        for kw in ({"section": "## Plan", "append": True}, {"append": True, "description": True}, {"section": "## Plan", "description": True}):
            self.assertEqual(self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7", **kw)), (1, "update takes one of --section, --append, --description"))
        self.issue["project"] = {"id": "p9", "name": "Shop"}
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7", title="T")), (1, "ENG-7 is not in project Shop; pass --any-project to use it"))
        self.assertFalse([q for q, _ in self.calls if "issueUpdate" in q])
        self.assertEqual(self.update(title="T", any_project=True)[0], {"title": "T"})


class LinearStaleRead(unittest.TestCase):
    """Linear serves an issue's description from before the last write for a few seconds: a second write must not start from that read."""
    args, fails = LinearProject.args, LinearProject.fails
    STALE_READS = 3

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir(); (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        self.stored, self.served, self.stale, self.clock = "# Cart\n\n## Plan\n\nold\n\n## Notes\n\nkeep\n", "# Cart\n\n## Plan\n\nold\n\n## Notes\n\nkeep\n", 0, [0.0]
        tty = mock.Mock(**{"isatty.return_value": True, "read.side_effect": AssertionError("read stdin from a terminal")})
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.gql), (sys, "stdin", tty),
                                 (fc.fc_linear_cmd.time, "sleep", lambda s: self.clock.__setitem__(0, self.clock[0] + s)), (fc.fc_linear_cmd.time, "monotonic", lambda: self.clock[0])):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        if "projects(" in query:
            return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}}
        if "issueUpdate" in query:
            self.stored, self.stale = variables["in"]["description"], self.STALE_READS
            return {"issueUpdate": {"success": True}}
        if self.stale:
            self.stale -= 1
        else:
            self.served = self.stored
        return {"issue": {"id": "iid", "identifier": "ENG-7", "description": self.served, "project": {"id": "p1", "name": "Shop"},
                          "team": {"name": "Engineering", "states": {"nodes": []}, "labels": {"nodes": []}}}}

    def update(self, text, **kw):
        (self.repo / "body.md").write_text(text)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            fc.fc_linear_cmd.verb_update(self.args("update", "ENG-7", file=str(self.repo / "body.md"), **kw))
        return err.getvalue()

    def test_section_writes_back_to_back_keep_every_earlier_write(self):
        self.update("new plan", section="## Plan")
        self.update("new notes", section="## Notes")
        self.assertEqual(self.stored, "# Cart\n\n## Plan\n\nnew plan\n\n## Notes\n\nnew notes\n")

    def test_a_write_that_never_shows_warns_after_the_wait_and_still_ends_as_updated(self):
        self.STALE_READS = 10 ** 6
        err = self.update("new plan", section="## Plan")
        self.assertIn("still serves the old text", err)
        self.assertEqual(self.clock[0], fc.fc_linear_cmd.SETTLE_SECONDS)
        self.assertEqual(10 ** 6 - self.stale, 7)  # the reads back off: seven requests over the wait, not one every half second

class LinearCommentLabelsDoc(unittest.TestCase):
    ISSUE, LABELS, DOC = "issue(id: $id) { id identifier project { id name } }", "team(id: $id) { labels(first: 250) { nodes { id name } } }", "document(id: $id) { title content url }"
    NEW_LABEL, NEW_DOC, DOC_URL = "issueLabelCreate(input: {name: $name, teamId: $team}) { success }", "documentCreate(input: $in) { document { url } }", "https://linear.app/acme/document/research-d1"
    args, fails = LinearProject.args, LinearProject.fails

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir(); (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        self.calls, self.issue, self.doc = [], {"id": "iid", "identifier": "ENG-7", "project": {"id": "p1", "name": "Shop"}}, None
        self.sent = lambda query: [v for q, v in self.calls if query in q]
        tty = mock.Mock(**{"isatty.return_value": True, "read.side_effect": AssertionError("read stdin from a terminal")})
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.gql), (sys, "stdin", tty), (fc.fc_linear, "linear_comment", mock.Mock()), (fc.fc_linear_cmd, "await_write", mock.Mock())):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        self.calls.append((" ".join(query.split()), variables))
        replies = {"projects(": {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t0", "name": "Design", "key": "DES"}, {"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}},
                   "issueLabelCreate": {"issueLabelCreate": {"success": True}}, "documentCreate": {"documentCreate": {"document": {"url": self.DOC_URL}}},
                   "documentUpdate(": {"documentUpdate": {"success": True}}, "documents(": lambda: {"project": {"documents": self.pages.pop(0)}},
                   "document(": {"document": self.doc or {"title": "Research", "content": "Line one.\nLine two.", "url": self.DOC_URL}},
                   "team(": {"team": {"labels": {"nodes": [{"id": "l2", "name": "ui"}, {"id": "l1", "name": "bug"}, {"id": "l3", "name": "perf"}]}}}, "issue(": {"issue": self.issue}}
        reply = next(v for k, v in replies.items() if k in query)
        return reply() if callable(reply) else reply

    def a(self, verb, *target, body=None, **kw):
        if body is not None: (self.repo / "body.md").write_text(body); kw["file"] = str(self.repo / "body.md")
        return self.args(verb, *target, **kw)

    def run_verb(self, verb, *target, **kw):
        with redirect_stdout(io.StringIO()) as out:
            getattr(fc.fc_linear_cmd, f"verb_{verb}")(self.a(verb, *target, **kw))
        return [line for line in out.getvalue().splitlines() if line.strip()]

    def test_comment_posts_the_stripped_body_from_a_file_or_stdin(self):
        self.assertEqual(self.run_verb("comment", "ENG-7", body="\n  Looks good.\n\n"), ["commented on ENG-7"])
        with mock.patch("sys.stdin", io.StringIO("piped note\n")):
            self.run_verb("comment", "ENG-7")
        self.assertEqual([c.args for c in fc.fc_linear.linear_comment.call_args_list], [(self.issue, "Looks good."), (self.issue, "piped note")])
        self.assertEqual(self.sent(self.ISSUE), [{"id": "ENG-7"}] * 2)

    def test_comment_refuses_words_after_the_id_and_never_waits_on_a_stdin_that_does_not_end(self):
        for kw in ({}, {"body": "from the file"}):  # stdin is a terminal here: reading it would fail the test
            self.assertEqual(self.fails(fc.fc_linear_cmd.verb_comment, self.a("comment", "ENG-7", "Looks", "good.", **kw)),
                             (1, "comment takes the body from --file <path> or stdin, not from the command line: compose linear comment ENG-7 --file <path>"))
        fc.fc_linear.linear_comment.assert_not_called()
        r, w = os.pipe()  # a pipe whose writer stays open: what an agent's shell can hand a command
        self.addCleanup(os.close, w)
        with os.fdopen(r) as held, mock.patch("sys.stdin", held), mock.patch.object(fc.fc_linear_cmd, "STDIN_SECONDS", 0.2):
            self.assertEqual(self.fails(fc.fc_linear_cmd.read_body, self.a("comment", "ENG-7")),
                             (1, "no body on stdin after 0.2s: pass --file <path>, or pipe the text in"))
        r, w2 = os.pipe()
        os.write(w2, b"piped note\n")
        os.close(w2)
        with os.fdopen(r) as ended, mock.patch("sys.stdin", ended):
            self.assertEqual(fc.fc_linear_cmd.read_body(self.a("comment", "ENG-7")), "piped note\n")

    def test_comment_refuses_an_empty_body_or_another_project(self):
        for kw in ({"body": " \n\n"}, {}):
            self.assertEqual(self.fails(fc.fc_linear_cmd.verb_comment, self.a("comment", "ENG-7", **kw)), (1, "comment needs a body: --file <path>, or stdin"))
        self.issue["project"] = {"id": "p9", "name": "Shop"}
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_comment, self.a("comment", "ENG-7", body="hi")), (1, "ENG-7 is not in project Shop; pass --any-project to use it"))
        fc.fc_linear.linear_comment.assert_not_called()
        self.assertEqual(self.run_verb("comment", "ENG-7", body="hi", any_project=True), ["commented on ENG-7"])

    def test_labels_lists_the_repo_team_sorted_or_creates_one(self):
        self.assertEqual((self.run_verb("labels"), self.sent(self.LABELS)), (["bug", "perf", "ui"], [{"id": "t1"}]))
        self.assertEqual((self.run_verb("labels", create="docs")[-1], self.sent(self.NEW_LABEL)), ("created label docs", [{"name": "docs", "team": "t1"}]))

    def test_doc_create_sends_the_body_as_read_and_get_prints_title_then_content(self):
        self.assertEqual(self.run_verb("doc", "create", title="Research", body="Findings.\n"), [self.DOC_URL])
        self.assertEqual([v["in"] for v in self.sent(self.NEW_DOC)], [{"title": "Research", "content": "Findings.\n", "projectId": "p1"}])
        self.assertEqual((self.fails(fc.fc_linear_cmd.verb_doc, self.args("doc", "create")), len(self.sent("documentCreate"))), ((1, "doc create needs --title"), 1))
        self.assertEqual((self.run_verb("doc", "get", "d1"), self.sent(self.DOC)), (["Research", "Line one.", "Line two."], [{"id": "d1"}]))

    def test_doc_refuses_any_other_target_and_sends_nothing(self):
        for target in ([], ["get"], ["update"], ["list", "extra"], ["create", "extra"]):
            self.assertEqual(self.fails(fc.fc_linear_cmd.verb_doc, self.args("doc", *target, title="T")), (1, "doc takes create, list, get <id>, or update <id>"))
        self.assertFalse([q for q, _ in self.calls if "document" in q])

    def test_doc_list_prints_every_page_of_the_project_documents(self):
        self.pages = [{"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [{"id": "d1", "title": "PRD", "updatedAt": "2026-10-18T17:15:38.008Z"}]},
                      {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": "d2", "title": "Architecture", "updatedAt": "2026-10-17T09:00:00.000Z"}]}]
        self.assertEqual(self.run_verb("doc", "list"), ["d1 2026-10-18 PRD", "d2 2026-10-17 Architecture"])
        self.assertEqual(self.sent("project(id: $id) { documents(first: 100, after: $after)"), [{"id": "p1", "after": None}, {"id": "p1", "after": "c1"}])

    def doc_pages(self):
        return [{"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [{"id": "d1", "title": "PRD", "updatedAt": "2026-10-18T17:15:38.008Z", "url": "https://linear.app/d1"}]},
                {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": "d2", "title": "Architecture", "updatedAt": "2026-10-17T09:00:00.000Z", "url": "https://linear.app/d2"}]}]

    def test_doc_list_json_prints_one_list_with_every_page_and_the_url(self):
        self.pages = self.doc_pages()
        with redirect_stdout(io.StringIO()) as out:
            fc.fc_linear_cmd.verb_doc(self.a("doc", "list", json=True))
        self.assertEqual(json.loads(out.getvalue()), [{"id": "d1", "updatedAt": "2026-10-18T17:15:38.008Z", "title": "PRD", "url": "https://linear.app/d1"},
                                                      {"id": "d2", "updatedAt": "2026-10-17T09:00:00.000Z", "title": "Architecture", "url": "https://linear.app/d2"}])

    def test_doc_update_sets_the_title_and_each_body_flag_on_the_read_content(self):
        content, sent = "# PRD\n\n## Plan\n\nold\n\n## Notes\n\nkeep\n", lambda: [v["in"] for v in self.sent("documentUpdate(id: $id, input: $in) { success }")]
        self.doc = {"id": "did", "title": "PRD", "content": content, "url": self.DOC_URL, "project": {"id": "p1", "name": "Shop"}}
        self.assertEqual(self.run_verb("doc", "update", "d1", body="new plan", section="## Plan"), [f"updated {self.DOC_URL}"])
        self.run_verb("doc", "update", "d1", body="\n more \n", append=True, title="PRD v2")
        self.run_verb("doc", "update", "d1", body="whole\n", description=True)
        self.run_verb("doc", "update", "d1", title="Only the title")
        self.assertEqual(sent(), [{"content": "# PRD\n\n## Plan\n\nnew plan\n\n## Notes\n\nkeep\n"}, {"title": "PRD v2", "content": content + "\nmore\n"},
                                  {"content": "whole\n"}, {"title": "Only the title"}])
        self.assertEqual(([v["id"] for v in self.sent("documentUpdate(")], self.sent("document(id: $id) { id title content url project { id name } }")), (["did"] * 4, [{"id": "d1"}] * 4))
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_doc, self.a("doc", "update", "d1", section="## Plan", append=True)), (1, "doc update takes one of --section, --append, --description"))
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_doc, self.a("doc", "update", "d1")), (1, "doc update needs --title, --section, --append, or --description"))
        for kw in ({}, {"title": "PRD v3"}):
            self.assertEqual(self.fails(fc.fc_linear_cmd.verb_doc, self.a("doc", "update", "d1", body="x", **kw)), (1, "doc update --file needs --section, --append, or --description"))
        stale = fc_plan.section_hash("# PRD\n\n## Plan\n\nolder\n", "## Plan")
        self.assertIn("## Plan changed since you read it", self.fails(fc.fc_linear_cmd.verb_doc, self.a("doc", "update", "d1", body="x", section="## Plan", expect=stale))[1])
        self.assertEqual(self.run_verb("doc", "get", "d1"), ["PRD", *(l for l in content.split("\n") if l), '--- section hashes (--section "<heading>" --expect <hash>)',
                                                          *(f"{h} {s}" for s, h in fc_plan.section_hashes(content))])
        self.doc["project"] = None
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_doc, self.a("doc", "update", "d1", body="x", description=True)), (1, "document PRD is not in project Shop; pass --any-project to use it"))
        self.assertEqual(len(sent()), 4)
        self.run_verb("doc", "update", "d1", body="x", description=True, any_project=True)
        self.assertEqual(sent()[-1], {"content": "x"})


    def test_doc_update_section_writes_through_fc_plan_replace_section(self):
        content = "# PRD\n\n## Plan\n\nold\n"
        self.doc = {"id": "did", "title": "PRD", "content": content, "url": self.DOC_URL, "project": {"id": "p1", "name": "Shop"}}
        with mock.patch.object(fc_plan, "replace_section", return_value="via plan\n") as rs:
            self.assertEqual(self.run_verb("doc", "update", "d1", body="new plan", section="## Plan"), [f"updated {self.DOC_URL}"])
        rs.assert_called_once_with(content, "## Plan", "new plan")
        self.assertEqual([v["in"] for v in self.sent("documentUpdate(")], [{"content": "via plan\n"}])

class LinearCancel(unittest.TestCase):
    args, fails = LinearProject.args, LinearProject.fails

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir(); (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        self.states = {"s-todo": {"id": "s-todo", "name": "Todo", "type": "unstarted"}, "s-done": {"id": "s-done", "name": "Done", "type": "completed"},
                       "s-canceled": {"id": "s-canceled", "name": "Canceled", "type": "canceled"}, "s-duplicate": {"id": "s-duplicate", "name": "Duplicate", "type": "canceled"}}
        self.issue = {"id": "iid", "identifier": "ENG-7", "title": "Slice", "project": {"id": "p1", "name": "Shop"}, "state": {"name": "Todo", "type": "unstarted"}, "parent": None, "children": {"nodes": []}}
        self.other = {"id": "oid", "identifier": "ENG-3", "title": "Cart total", "project": {"id": "p1", "name": "Shop"}, "state": {"name": "Todo", "type": "unstarted"}, "parent": None, "children": {"nodes": []}}
        self.calls = []
        tty = mock.Mock(**{"isatty.return_value": True, "read.side_effect": AssertionError("read stdin from a terminal")})
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.gql), (sys, "stdin", tty), (fc.fc_linear_cmd, "await_write", mock.Mock()), (fc.fc_core, "worktree_branches", lambda repo: [(self.repo, "main")])):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        self.calls.append((" ".join(query.split()), variables))
        if "projects(" in query:
            return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}}
        if "mutation" in query:
            return {"ok": {"success": True}}
        item = {"ENG-7": self.issue, "ENG-3": self.other}[variables["id"]]
        return {"issue": {**item, "team": {"states": {"nodes": list(self.states.values())}}}}

    def a(self, verb, *target, body=None, **kw):
        if body is not None: (self.repo / "body.md").write_text(body); kw["file"] = str(self.repo / "body.md")
        return self.args(verb, *target, **kw)

    def run_verb(self, verb, *target, **kw):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            fc.fc_linear_cmd.cmd_linear(self.a(verb, *target, **kw))
        return out.getvalue().strip().split("\n")

    def writes(self): return [(q, v) for q, v in self.calls if q.startswith("mutation")]
    def order(self): return [q.split("{")[1].split("(")[0].strip() for q, _ in self.writes()]
    def comment_body(self): return next(v["b"] for q, v in self.writes() if "commentCreate" in q)
    def moved_to(self): return next(v["s"] for q, v in self.writes() if "issueUpdate" in q)
    def relation_query(self): return next(q for q, _ in self.writes() if "issueRelationCreate" in q)

    def test_cancel_comments_first_then_moves_to_the_canceled_state_never_duplicate(self):
        self.assertEqual(self.run_verb("cancel", "ENG-7", body="No longer relevant.\n"), ["canceled ENG-7"])
        self.assertEqual(self.order(), ["commentCreate", "issueUpdate"])
        self.assertEqual((self.comment_body(), self.moved_to()), ("No longer relevant.", "s-canceled"))

    def test_a_team_that_spells_it_cancelled_still_gets_it(self):
        self.states["s-canceled"]["name"] = "Cancelled"
        self.run_verb("cancel", "ENG-7", body="x")
        self.assertEqual(self.moved_to(), "s-canceled")

    def test_into_writes_the_merged_line_and_the_duplicate_relation(self):
        self.run_verb("cancel", "ENG-7", body="Same slice.", into="ENG-3")
        self.assertEqual(self.comment_body(), "Merged into ENG-3 (Cart total).\nSame slice.")
        self.assertIn("type: duplicate", self.relation_query())
        self.assertEqual(self.order(), ["commentCreate", "issueRelationCreate", "issueUpdate"])

    def test_already_canceled_writes_nothing_and_done_or_empty_refuses(self):
        self.issue["state"] = {"name": "Canceled", "type": "canceled"}
        self.assertEqual(self.run_verb("cancel", "ENG-7", body="x"), ["ENG-7 is already Canceled: nothing written"])
        self.issue["state"] = {"name": "Done", "type": "completed"}
        self.assertEqual(self.fails(fc.fc_linear_cmd.cmd_linear, self.a("cancel", "ENG-7", body="x")), (1, "linear cancel: ENG-7 is Done: a finished ticket is never canceled"))
        self.issue["state"] = {"name": "Todo", "type": "unstarted"}
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_cancel, self.a("cancel", "ENG-7", body="  \n")), (1, "cancel needs a reason: --file <path>, or stdin"))
        self.assertEqual(self.writes(), [])

    def test_another_project_refuses_unless_any_project_and_so_does_into(self):
        self.issue["project"] = {"id": "p2", "name": "Other"}
        self.assertEqual(self.fails(fc.fc_linear_cmd.cmd_linear, self.a("cancel", "ENG-7", body="x")), (1, "ENG-7 is not in project Shop; pass --any-project to use it"))
        self.assertEqual(self.run_verb("cancel", "ENG-7", body="x", any_project=True), ["canceled ENG-7"])
        self.issue["project"] = {"id": "p1", "name": "Shop"}; self.other["project"] = {"id": "p2", "name": "Other"}; self.calls.clear()
        self.assertEqual(self.fails(fc.fc_linear_cmd.cmd_linear, self.a("cancel", "ENG-7", body="x", into="ENG-3")), (1, "ENG-3 is not in project Shop; pass --any-project to use it"))
        self.assertEqual(self.writes(), [])

    def test_into_itself_or_into_a_canceled_ticket_is_refused_and_writes_nothing(self):
        self.assertEqual(self.fails(fc.fc_linear_cmd.cmd_linear, self.a("cancel", "ENG-7", body="x", into="ENG-7")), (1, "linear cancel: ENG-7 cannot be merged into itself"))
        self.other["state"] = {"name": "Cancelled", "type": "canceled"}
        self.assertEqual(self.fails(fc.fc_linear_cmd.cmd_linear, self.a("cancel", "ENG-7", body="x", into="ENG-3")),
                         (1, "linear cancel: ENG-3 is Cancelled: a ticket is merged only into an open or finished one"))
        self.assertEqual(self.writes(), [])

    def test_cancel_refuses_while_any_project_of_the_repo_is_under_away_or_yolo(self):
        for mode in ("away", "yolo"):
            d = self.repo / ".compose" / "shop" / "results"; d.mkdir(parents=True, exist_ok=True)
            (d.parent / "verify.json").write_text("{}"); (d / "yolo.json").write_text(json.dumps({"mode": mode}))
            self.assertEqual(self.fails(fc.fc_linear_cmd.cmd_linear, self.a("cancel", "ENG-7", body="x")),
                             (1, f"shop is under {mode}: a cancel changes Linear only on the product owner's own go; cancel it once {mode} is off"))
        self.assertEqual(self.writes(), [])


class LinearNoProject(unittest.TestCase):
    REFUSAL = (1, "ENG-7 is not in project Shop; pass --any-project to use it")
    args, fails = LinearProject.args, LinearProject.fails

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir(); (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        (self.repo / "body.md").write_text("a note\n")
        self.calls, self.issue = [], {"id": "iid", "identifier": "ENG-7", "title": "Fix the cart", "description": "Steps.", "state": {"name": "Todo"}, "labels": {"nodes": []},
                                      "project": None, "parent": None, "comments": {"nodes": []}, "team": {"name": "Engineering", "states": {"nodes": []}, "labels": {"nodes": []}}}
        tty = mock.Mock(**{"isatty.return_value": True, "read.side_effect": AssertionError("read stdin from a terminal")})
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.gql), (sys, "stdin", tty)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        self.calls.append((" ".join(query.split()), variables))
        if "projects(" in query:
            return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}}
        return {"issueUpdate": {"success": True}} if "issueUpdate" in query else {"commentCreate": {"success": True}} if "commentCreate" in query else {"issue": self.issue}

    def writes(self):
        return [w for q, _ in self.calls for w in ("issueUpdate", "commentCreate") if w in q]

    def verbs(self):
        return (("get", {}), ("get", {"json": True, "body": True, "fresh": True}), ("update", {"title": "T"}), ("comment", {"file": str(self.repo / "body.md")}))

    def test_in_scope_refuses_no_project_or_another_unless_any_project(self):
        self.assertEqual(self.fails(fc.fc_linear_cmd.in_scope, self.issue, self.args("get", "ENG-7")), self.REFUSAL)
        fc.fc_linear_cmd.in_scope(self.issue, self.args("get", "ENG-7", any_project=True))
        fc.fc_linear_cmd.in_scope({**self.issue, "project": {"id": "p1", "name": "Shop"}}, self.args("get", "ENG-7"))
        self.assertEqual(self.fails(fc.fc_linear_cmd.in_scope, {**self.issue, "project": {"id": "p9", "name": "Shop"}}, self.args("get", "ENG-7")), self.REFUSAL)

    def test_each_verb_refuses_it_through_the_command_printing_and_writing_nothing(self):
        for action, kw in self.verbs():
            with self.subTest(action=action, **kw):
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(self.fails(fc.fc_linear_cmd.cmd_linear, self.args(action, "ENG-7", **kw)), self.REFUSAL)
                self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.writes(), [])

    def test_any_project_lets_each_verb_use_it(self):
        with redirect_stdout(io.StringIO()) as out:
            for action, kw in self.verbs()[:1] + self.verbs()[2:]:
                fc.fc_linear_cmd.cmd_linear(self.args(action, "ENG-7", any_project=True, **kw))
        self.assertEqual([line for line in out.getvalue().splitlines() if line.strip()], ["ENG-7 Todo - Fix the cart", "updated ENG-7", "commented on ENG-7"])
        self.assertEqual(self.writes(), ["issueUpdate", "commentCreate"])

    def test_add_and_drop_blocked_by_keep_the_other_blockers_and_blocked_by_says_what_it_removed(self):
        # a planner ran update --blocked-by twice to add two blockers, and only the second stayed
        self.issue["project"] = {"id": "p1", "name": "Shop"}
        have = [{"id": "r2", "type": "blocks", "issue": {"id": "u2", "identifier": "ENG-2"}}, {"id": "r3", "type": "blocks", "issue": {"id": "u3", "identifier": "ENG-3"}}]
        writes = []
        def gql(query, variables):
            if "issueRelationCreate" in query or "issueRelationDelete" in query:
                writes.append(("create", variables["a"]) if "Create" in query else ("delete", variables["id"]))
                return {}
            return {"issue": {"inverseRelations": {"nodes": have}}} if "inverseRelations" in query else self.gql(query, variables)
        def run(**kw):
            writes.clear()
            with mock.patch.object(fc.fc_linear, "linear_gql", gql), mock.patch.object(fc.fc_linear, "linear_issue", lambda i: {"id": "u-" + i}), \
                    redirect_stdout(io.StringIO()) as out:
                fc.fc_linear_cmd.verb_update(self.args("update", "ENG-7", **kw))
            return writes[:], out.getvalue().splitlines()
        self.assertEqual(run(add_blocked_by=["ENG-4", "ENG-2,ENG-5"]), ([("create", "u-ENG-4"), ("create", "u-ENG-5")], ["updated ENG-7"]))
        self.assertEqual(run(drop_blocked_by=["ENG-3"]), ([("delete", "r3")], ["updated ENG-7"]))
        self.assertEqual(run(blocked_by=["ENG-4"]), ([("create", "u-ENG-4"), ("delete", "r2"), ("delete", "r3")],
                                                   ["updated ENG-7", "no longer blocked by: ENG-2, ENG-3 (--add-blocked-by adds and keeps the rest)"]))
        self.assertEqual(self.fails(fc.fc_linear_cmd.verb_update, self.args("update", "ENG-7", blocked_by=["ENG-4"], add_blocked_by=["ENG-5"])),
                         (1, "update takes --blocked-by (the whole list) or --add-blocked-by/--drop-blocked-by, not both"))
        self.assertEqual(self.fails(fc.fc_linear_cmd.cmd_linear, self.args("create", title="T", add_blocked_by=["ENG-5"])),
                         (1, "--add-blocked-by and --drop-blocked-by change an existing issue: compose linear update <id> takes them, and create takes --blocked-by"))

    def test_update_blocked_by_drops_empty_ids(self):
        self.issue["project"] = {"id": "p1", "name": "Shop"}
        for given, want in (("", []), ("NEB-1,,NEB-2", ["NEB-1", "NEB-2"]), (",NEB-3,", ["NEB-3"])):
            with self.subTest(given=given):
                with mock.patch.object(fc.fc_linear, "linear_blocked_by", return_value=[]) as blocked, redirect_stdout(io.StringIO()):
                    fc.fc_linear_cmd.verb_update(self.args("update", "ENG-7", blocked_by=[given]))
                blocked.assert_called_once_with(self.issue, want)
