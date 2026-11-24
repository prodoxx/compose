import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, tempfile, unittest
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
import fc_repolist, fc_repos  # noqa: E402


KEY, URL = "lin_api_SECRET_do_not_print", "https://linear.app/acme/project/my-shop-abc123"
TASK = "## Goal\nBuild it.\n\n```compose\nid: 001\nfiles: a.py\ntest: pytest a\n```\n"
NOT_HERE = " (not on this machine: run compose linear project in its checkout)"


class Fake(unittest.TestCase):
    """A Linear project `Shop` whose content and issues the fake serves and the writes change, one issue to a page so the paging is read."""
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir()
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        self.calls, self.content, self.issues = [], "", {}
        self.overwrite, self.refuse = None, set()  # another writer's content served after a project write; issues whose next write fails
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_linear, "linear_gql", self.gql), (fc.fc_linear_cmd, "await_write", mock.Mock()),
                                 (fc.fc_core, "main_checkout", lambda path: Path(path)), (fc_repos, "origin_url", lambda repo: "git@github.com:acme/api.git"),
                                 (fc_repolist, "about", lambda repo: "the API"),
                                 (fc.fc_config, "CONFIG_PATH", Path(tempfile.mkdtemp()).resolve() / ".compose" / "config.json")):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        q = " ".join(query.split())
        self.calls.append((q, variables))
        if "projectUpdate" in q:
            self.content = variables["in"]["content"] if self.overwrite is None else self.overwrite(variables["in"]["content"])
            return {"projectUpdate": {"success": True}}
        if "issueUpdate" in q:
            if variables["id"] in self.refuse:
                self.refuse.discard(variables["id"])
                raise RuntimeError(f"Linear refused {variables['id']}")
            self.issues[variables["id"]] = variables["in"]["description"]
            return {"issueUpdate": {"success": True}}
        if "project(id" in q:
            return {"project": {"content": self.content}}
        if "issue(id" in q:
            return {"issue": {"description": self.issues[variables["id"]]}}
        if "issues(" in q:
            keys, at = list(self.issues), int(variables["after"] or 0)
            return {"issues": {"pageInfo": {"hasNextPage": at + 1 < len(keys), "endCursor": str(at + 1)},
                               "nodes": [{"id": k, "identifier": k, "description": self.issues[k]} for k in keys[at:at + 1]]}}
        return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}}

    def link(self):
        return json.loads((self.repo / ".compose" / "linear.json").read_text())

    def args(self, action, *target, **kw):
        return SimpleNamespace(**{**dict(repo=str(self.repo), project=None, action=action, target=list(target), body=False, comments=False, json=False,
                                         append=False, description=False, any_project=False, fresh=False, label=None, parent=None, search=None, state=None,
                                         title=None, priority=None, blocked_by=None, file=None, section=None, expect=None, create=None, into=None, add_label=None, drop_label=None, add_blocked_by=None, drop_blocked_by=None, subsections=None, quote=None,
                                         add=None, about=None, drop=None), **kw})

    def run_cmd(self, a):
        out = io.StringIO()
        with redirect_stdout(out):
            fc_repolist.cmd_linear(a)
        return out.getvalue().splitlines()

    def fails(self, fn, *args):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            fn(*args)
        return str(e.exception.code)


class Ensure(Fake):
    def setUp(self):
        super().setUp()
        p = mock.patch("sys.stdout", io.StringIO()); p.start(); self.addCleanup(p.stop)

    def ens(self):
        """The lines one ensure prints; what it returns is kept in self.known."""
        out = io.StringIO()
        with redirect_stdout(out):
            self.known = fc_repolist.ensure(self.repo)
        return out.getvalue().splitlines()

    def test_a_project_with_no_list_gets_it_and_its_open_tasks_get_the_repo(self):
        self.content, self.issues = "Intro.", {"ENG-1": TASK, "ENG-2": "## A story\nNo block."}
        lines = self.ens()
        self.assertEqual(self.content, "Intro.\n\n## Repos\n- acme/api: the API\n")
        self.assertIn("```compose\nid: 001\nrepo: acme/api\nfiles: a.py\n", self.issues["ENG-1"])
        self.assertEqual(self.issues["ENG-2"], "## A story\nNo block.")
        self.assertEqual(lines, ["repos: added acme/api to Shop", "repos: wrote repo: acme/api on 1 open task(s): ENG-1"])
        link = self.link()
        self.assertEqual((link["repo"], link["listed"], link["project"]), ("acme/api", URL, URL))
        self.assertEqual(fc_repos.path_of("acme/api"), self.repo)

    def test_a_second_run_makes_no_write_and_no_open_issues_read_and_returns_what_it_read(self):
        self.content, self.issues = "", {"ENG-1": TASK}
        self.ens(); self.calls.clear()
        before = (self.repo / ".compose" / "linear.json").read_text()
        self.assertEqual(self.ens(), [])
        self.assertEqual([q for q, _ in self.calls if "Update" in q or "issues(" in q], [])
        self.assertEqual(self.known, fc_repos.Known("acme/api", [("acme/api", "the API")], {"acme/api": str(self.repo)}))
        self.assertEqual((self.repo / ".compose" / "linear.json").read_text(), before)

    def test_a_second_repo_is_added_and_no_task_is_touched(self):
        self.content, self.issues = "## Repos\n- acme/web: the web app\n", {"ENG-1": TASK}
        self.assertEqual(self.ens(), ["repos: added acme/api to Shop"])
        self.assertEqual(self.issues["ENG-1"], TASK)

    def test_a_run_that_died_after_the_list_was_written_still_fills_the_tasks(self):
        self.content, self.issues = "## Repos\n- acme/api: the API\n", {"ENG-1": TASK}
        self.assertEqual(self.ens(), ["repos: wrote repo: acme/api on 1 open task(s): ENG-1"])

    def test_no_task_is_named_when_an_open_task_already_names_another_repo(self):
        # the section was renamed or reformatted by hand so it reads as no list: the project has another repo all the same
        self.content = "## Our repos\n- acme/web: the web app\n"
        self.issues = {"ENG-1": TASK, "ENG-2": TASK.replace("id: 001\n", "id: 002\nrepo: acme/web\n"), "ENG-3": TASK.replace("id: 001\n", "id: 003\nrepo: ACME/Api\n"),
                       "ENG-4": TASK.replace("id: 001\n", "id: 004\nrepo: acme/engine\n")}
        self.assertEqual(self.ens(), ["repos: added acme/api to Shop", "repos: wrote no repo: on open tasks, because some already name another repo (acme/engine, acme/web)"])
        self.assertEqual([q for q, _ in self.calls if "issueUpdate" in q], [])
        self.assertEqual((self.link()["listed"], self.ens()), (URL, []))  # settled for this project: said once

    def test_a_repo_with_no_link_file_or_no_project_in_it_is_told_that_first(self):
        path = self.repo / ".compose" / "linear.json"
        with mock.patch.object(fc_repos, "origin_url", return_value=""):  # a fresh local repo: no origin either
            path.write_text(json.dumps({"repo": "acme/api", "team": "Engineering"}))
            with self.assertRaises(SystemExit) as e:
                self.ens()
            self.assertEqual(e.exception.code, f'{path} has no "project": add the Linear project URL')
            path.unlink()
            with self.assertRaises(SystemExit) as e:
                self.ens()
            self.assertEqual(e.exception.code, f"no .compose/linear.json in {self.repo}")
        self.assertEqual(self.calls, [])

    def test_a_repo_with_no_name_stops_and_says_where_to_record_one(self):
        with mock.patch.object(fc_repos, "origin_url", return_value=""), self.assertRaises(SystemExit) as e:
            self.ens()
        self.assertIn('add "repo": "<owner>/<name>" to ' + str(self.repo / ".compose" / "linear.json"), str(e.exception.code))
        self.assertEqual(self.calls, [])

    def test_known_names_this_repo_the_list_and_the_paths_on_this_machine(self):
        self.content, self.issues = "## Repos\n- acme/web: the web app\n", {}
        self.ens()
        k = self.known
        self.assertEqual((k.own, [n for n, _ in k.listed], k.paths), ("acme/api", ["acme/web", "acme/api"], {"acme/api": str(self.repo)}))
        self.assertEqual(fc_repolist.known(self.repo), k)  # the list as it is, with no add: what `repos` prints

    def test_every_page_of_open_tasks_is_read_and_only_open_ones_are_asked_for(self):
        self.issues = {f"ENG-{i}": TASK.replace("001", f"00{i}") for i in range(1, 4)}
        self.assertEqual(self.ens()[-1], "repos: wrote repo: acme/api on 3 open task(s): ENG-1, ENG-2, ENG-3")
        self.assertTrue(all("repo: acme/api" in d for d in self.issues.values()))
        asked = [(q, v) for q, v in self.calls if "issues(" in q]
        self.assertEqual([v["after"] for _, v in asked], [None, "1", "2"])
        self.assertIn('filter: {project: {id: {eq: $p}}, state: {type: {nin: ["completed", "canceled"]}}}', asked[0][0])
        self.assertEqual({v["p"] for _, v in asked}, {"p1"})

    def test_each_write_waits_for_linear_to_serve_it_back(self):
        self.content, self.issues = "Intro.", {"ENG-1": TASK}
        self.ens()
        waits = fc.fc_linear_cmd.await_write.call_args_list
        self.assertEqual([(c.args[1], c.args[2]) for c in waits], [("Intro.", self.content), (TASK, self.issues["ENG-1"])])
        self.assertEqual([c.args[0]() for c in waits], [self.content, self.issues["ENG-1"]])

    def test_a_task_that_already_names_a_repo_is_not_written(self):
        named = TASK.replace("id: 001\n", "id: 001\nrepo: acme/other\n")
        self.issues = {"ENG-1": named}
        self.assertEqual(self.ens(), ["repos: added acme/api to Shop", "repos: wrote no repo: on open tasks, because some already name another repo (acme/other)"])
        self.assertEqual(([q for q, _ in self.calls if "issueUpdate" in q], self.issues["ENG-1"]), ([], named))

    def test_the_list_in_another_case_counts_as_listed_and_the_link_keeps_its_other_keys(self):
        self.content = "## Repos\n- ACME/Api: the API\n"
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering", "feature": "ENG-9"}))
        self.assertEqual(self.ens(), [])
        self.assertEqual([q for q, _ in self.calls if "projectUpdate" in q], [])
        self.assertEqual(self.link(), {"project": URL, "team": "Engineering", "feature": "ENG-9", "repo": "ACME/Api", "listed": URL})  # as the list first wrote it
        self.assertEqual(fc_repos.paths(), {"ACME/Api": str(self.repo)})

    def test_a_null_content_gets_the_list(self):
        self.content = None
        self.ens()
        self.assertEqual(self.content, "## Repos\n- acme/api: the API\n")

    def test_a_failed_write_leaves_the_link_unmarked_so_the_next_run_does_it_again(self):
        self.content, real = "Intro.", self.gql
        def refuse(query, variables):
            if "projectUpdate" in query:
                return {"projectUpdate": {"success": False}}
            return real(query, variables)
        with mock.patch.object(fc.fc_linear, "linear_gql", refuse), self.assertRaises(RuntimeError):
            self.ens()
        self.assertNotIn("listed", self.link())
        self.assertEqual(self.ens()[0], "repos: added acme/api to Shop")
        self.assertEqual(self.link()["listed"], URL)

    def test_a_marked_link_whose_repo_left_the_list_adds_it_back_and_names_no_task_and_an_old_true_mark_names_them(self):
        # a hand edit, or the write race two repos listing themselves at once can still lose, dropped this repo from the list
        self.content, self.issues = "Intro.", {"ENG-1": TASK}
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering", "repo": "acme/api", "listed": URL}))
        self.assertEqual(self.ens(), ["repos: added acme/api to Shop"])
        self.assertEqual((self.content, self.issues["ENG-1"]), ("Intro.\n\n## Repos\n- acme/api: the API\n", TASK))
        self.assertEqual([q for q, _ in self.calls if "issues(" in q], [])  # the mark: the open tasks were seen to from here once already
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering", "repo": "acme/api", "listed": True}))
        self.assertEqual(self.ens(), ["repos: wrote repo: acme/api on 1 open task(s): ENG-1"])
        self.assertEqual(self.link()["listed"], URL)

    def test_a_link_pointed_at_another_project_after_marking_lists_the_repo_there(self):
        self.ens()
        other = "https://linear.app/acme/project/the-right-one-def456"
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({**self.link(), "project": other}))
        self.content, self.calls[:] = "Other project.", []
        self.assertEqual(self.ens(), ["repos: added acme/api to Shop"])
        self.assertEqual([v for _, v in self.calls][0], {"slug": "def456"})
        self.assertEqual((self.content, self.link()["listed"]), ("Other project.\n\n## Repos\n- acme/api: the API\n", other))

    def test_a_list_write_another_writer_replaced_leaves_the_link_unmarked_and_names_no_task(self):
        self.content, self.issues, self.overwrite = "Intro.", {"ENG-1": TASK}, lambda new: "Intro.\n\n## Repos\n- acme/web: the web app\n"
        with self.assertRaises(RuntimeError) as e:
            self.ens()
        self.assertIn("Shop", str(e.exception))
        self.assertEqual((self.issues["ENG-1"], "listed" in self.link(), fc_repos.paths()), (TASK, False, {"acme/api": str(self.repo)}))  # where it is holds anyway

    def test_a_fresh_read_that_shows_a_second_repo_names_no_task(self):
        self.content, self.issues = "Intro.", {"ENG-1": TASK}
        self.overwrite = lambda new: new.replace("- acme/api: the API\n", "- acme/web: the web app\n- acme/api: the API\n")
        self.assertEqual(self.ens(), ["repos: added acme/api to Shop"])
        self.assertEqual((self.issues["ENG-1"], self.link()["listed"]), (TASK, URL))

    def test_every_run_records_where_this_repo_is(self):
        # after `compose doctor` pruned it, a lost repos.json or another workspace: the fix compose prints is to run it here again
        self.content = "## Repos\n- acme/api: the API\n"
        self.ens()
        for gone in (lambda: fc_repos.PATHS_FILE().unlink(), lambda: fc_repos.write_all({"default": {"acme/api": str(self.repo / "moved")}})):
            gone()
            self.assertIsNone(fc_repos.path_of("acme/api"))
            self.ens()
            self.assertEqual(fc_repos.path_of("acme/api"), self.repo)
        with mock.patch.object(fc.fc_config, "WORKSPACE", "work"):
            self.ens()
            self.assertEqual(fc_repos.paths(), {"acme/api": str(self.repo)})

    def test_a_name_linear_serves_back_escaped_is_listed_once_and_stays_listed(self):
        self.content, self.overwrite = "Intro.", lambda new: new.replace("_", "\\_")  # as Linear stores a bullet
        with mock.patch.object(fc_repos, "origin_url", return_value="git@github.com:acme/my_repo.git"):
            self.ens()
            self.assertEqual(self.content, "Intro.\n\n## Repos\n- acme/my\\_repo: the API\n")
            self.ens()
        self.assertEqual([q for q, _ in self.calls if "projectUpdate" in q], ["mutation($id: String!, $in: ProjectUpdateInput!) { projectUpdate(id: $id, input: $in) { success } }"])
        self.assertEqual(self.link()["repo"], "acme/my_repo")

    def test_a_task_write_that_fails_midway_is_finished_by_the_next_run(self):
        self.issues, self.refuse = {f"ENG-{i}": TASK.replace("001", f"00{i}") for i in range(1, 4)}, {"ENG-2"}
        out = io.StringIO()
        with mock.patch.object(fc.fc_linear_cmd, "drop_get_cache") as dropped, redirect_stdout(out), self.assertRaises(RuntimeError):
            fc_repolist.ensure(self.repo)
        dropped.assert_called_once()  # ENG-1 was written, so a cached `get` of it is stale
        self.assertEqual(out.getvalue().splitlines()[-1], "repos: wrote repo: acme/api on 1 open task(s): ENG-1")  # said before the error, so it can be moved
        self.assertNotIn("listed", self.link())
        self.assertIn("id: 001\nrepo: acme/api\n", self.issues["ENG-1"])
        self.assertNotIn("repo:", self.issues["ENG-3"])
        self.calls.clear()
        self.assertEqual(self.ens(), ["repos: wrote repo: acme/api on 2 open task(s): ENG-2, ENG-3"])
        self.assertEqual([v["id"] for q, v in self.calls if "issueUpdate" in q], ["ENG-2", "ENG-3"])
        self.assertEqual([d.count("repo: acme/api") for d in self.issues.values()], [1, 1, 1])
        self.assertEqual(self.link()["listed"], URL)


class Unread(Fake):
    def test_the_list_is_read_with_a_key_and_a_linked_project_and_the_reason_is_named_without_either(self):
        self.assertIsNone(fc_repolist.unread(self.repo))
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", ""):
            self.assertEqual(fc_repolist.unread(self.repo), "no Linear key")
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"team": "Engineering"}))
        self.assertEqual(fc_repolist.unread(self.repo), "no Linear project linked")


class About(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()

    def test_the_github_description_comes_first_on_one_line(self):
        with mock.patch.object(fc.fc_core, "sh", return_value=mock.Mock(returncode=0, stdout="The  API\nof the shop\n")) as sh:
            self.assertEqual(fc_repolist.about(self.repo), "The API of the shop")
        self.assertEqual(sh.call_args.args[0], ["gh", "repo", "view", "--json", "description", "-q", ".description"])

    def readme(self, text):
        (self.repo / "README.md").write_text(text)
        with mock.patch.object(fc.fc_core, "sh", return_value=mock.Mock(returncode=1, stdout="")):
            return fc_repolist.about(self.repo)

    def test_without_one_the_readme_s_first_paragraph_cut_to_120_characters_on_a_word(self):
        words = "The shop's API:\norders " + " ".join(["and payments"] * 20)
        (self.repo / "README.md").write_text("# Shop\n\n" + words + "\n\nMore.\n")
        for sh in (mock.Mock(returncode=1, stdout=""), mock.Mock(returncode=0, stdout="\n"), mock.Mock(side_effect=OSError("no gh"))):
            with mock.patch.object(fc.fc_core, "sh", sh):
                got = fc_repolist.about(self.repo)
            self.assertEqual(got, "The shop's API: orders " + " ".join(["and payments"] * 7) + " and")
            self.assertLessEqual(len(got), 120)
        self.assertEqual(self.readme("x" * 200 + "\n"), "x" * 120)  # one word longer than the line is cut where the line ends
        self.assertEqual(self.readme("y" * 118 + " z\n"), "y" * 118 + " z")  # a last word that ends on the 120th character stays

    def test_badges_images_and_html_are_not_the_readme_s_paragraph(self):
        for junk in ("[![CI](https://x/badge.svg)](https://x)", "![logo](logo.png)", '<h1 align="center">Shop</h1>', "  <p>\n<img src=x>\n</p>"):
            self.assertEqual(self.readme(f"{junk}\n\n# Shop\n\nThe shop's API.\n"), "The shop's API.", junk)
        self.assertEqual(self.readme("[the docs](docs.md) say what it is.\n"), "[the docs](docs.md) say what it is.")

    def test_with_neither_it_is_empty(self):
        with mock.patch.object(fc.fc_core, "sh", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(fc_repolist.about(self.repo), "")


class Command(Fake):
    def test_project_adds_this_repo_first_and_prints_the_list(self):
        self.content = "## Repos\n- acme/web: the web app\n"
        web = Path(tempfile.mkdtemp()).resolve()
        fc_repos.record_path("acme/web", web)  # acme/web is checked out here too, so its line carries no mark
        with mock.patch.object(fc.fc_repos, "origin_url", return_value="git@github.com:acme/api.git"), mock.patch.object(fc_repolist, "about", return_value="the API"):
            out = io.StringIO()
            with redirect_stdout(out):
                fc_repolist.cmd_linear(self.args("project"))
            out = out.getvalue()
        self.assertEqual(out.splitlines()[-3:], ["repos:", "  acme/web: the web app", "  acme/api: the API (this repo)"])
        self.assertEqual(out.splitlines()[:2], ["repos: added acme/api to Shop", f"Shop on team Engineering: {URL}"])

    def test_a_listed_repo_with_no_path_here_says_where_to_run(self):
        self.content = "## Repos\n- acme/web: the web app\n- acme/docs\n"
        self.assertEqual(self.run_cmd(self.args("project"))[-4:], ["repos:", "  acme/web: the web app" + NOT_HERE, "  acme/docs" + NOT_HERE, "  acme/api: the API (this repo)"])

    def test_repos_add_and_drop_change_only_the_section(self):
        self.content = before = "Intro.\n\n## Repos\n- acme/web: the web app\n\n## Links\n- a link\n"
        self.assertEqual(self.run_cmd(self.args("repos", add="acme/engine", about="the engine")), ["repos: added acme/engine to Shop"])
        self.assertEqual(self.content, before.replace("the web app\n", "the web app\n- acme/engine: the engine\n"))
        self.assertEqual(self.run_cmd(self.args("repos", drop="acme/engine")), ["repos: dropped acme/engine from Shop"])
        self.assertEqual(self.content, before)
        self.assertNotIn("listed", self.link())

    def test_repos_prints_the_list_and_writes_nothing(self):
        self.content = "## Repos\n- acme/web: the web app\n- acme/api: the API\n"
        self.assertEqual(self.run_cmd(self.args("repos")), ["repos:", "  acme/web: the web app" + NOT_HERE, "  acme/api: the API (this repo)"])
        self.content = "Intro."
        self.assertEqual(self.run_cmd(self.args("repos")), ["repos: none listed in Shop"])
        self.assertEqual([q for q, _ in self.calls if "Update" in q], [])

    def test_repos_add_or_drop_of_what_is_already_so_writes_nothing(self):
        self.content = "## Repos\n- acme/web: the web app\n"
        self.assertEqual(self.run_cmd(self.args("repos", add="ACME/web")), ["repos: acme/web is already listed in Shop"])
        self.assertEqual(self.run_cmd(self.args("repos", drop="acme/none")), ["repos: acme/none is not listed in Shop"])
        self.assertEqual([q for q, _ in self.calls if "Update" in q], [])

    def test_repos_refuses_dropping_this_repo_and_a_wrong_flag_mix(self):
        self.content = "## Repos\n- acme/api: the API\n"
        self.assertEqual(self.fails(fc_repolist.cmd_linear, self.args("repos", drop="ACME/api")), "this repo is linked to the project: unlink it before dropping it from the list")
        self.assertEqual(self.fails(fc_repolist.cmd_linear, self.args("repos", add="acme/x", drop="acme/y")), "repos takes one of --add, --drop")
        self.assertEqual(self.fails(fc_repolist.cmd_linear, self.args("repos", about="words")), "--about goes with --add")
        for bad in ("engine", "a//b", "/a/b", "a/b/", "acme/my engine", "acme/\tx"):
            self.assertEqual(self.fails(fc_repolist.cmd_linear, self.args("repos", add=bad)), f"--add takes <owner>/<name>, such as acme/api, not {bad!r}", bad)
        self.assertEqual(self.fails(fc_repolist.cmd_linear, self.args("repos", drop="a//b")), "--drop takes <owner>/<name>, such as acme/api, not 'a//b'")
        self.assertEqual(self.fails(fc_repolist.cmd_linear, self.args("get", "ENG-1", add="acme/x")), "--add, --about and --drop belong to compose linear repos")
        self.assertEqual([q for q, _ in self.calls if "Update" in q], [])

    def test_repos_wraps_linear_errors(self):
        with mock.patch.object(fc.fc_linear, "linear_gql", side_effect=RuntimeError("boom")):
            self.assertEqual(self.fails(fc_repolist.cmd_linear, self.args("repos")), "linear repos: boom")
            self.assertEqual(self.fails(fc_repolist.cmd_linear, self.args("project")), "linear project: boom")

    def test_get_delegates_and_never_ensures(self):
        with mock.patch.object(fc_repolist, "ensure", side_effect=AssertionError("ensured")), mock.patch.object(fc.fc_linear_cmd, "cmd_linear") as cmd:
            fc_repolist.cmd_linear(a := self.args("get", "ENG-1"))
        cmd.assert_called_once_with(a)

    def test_no_key_never_ensures(self):
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", None), mock.patch.object(fc_repolist, "ensure", side_effect=AssertionError("ensured")):
            for action in ("project", "repos"):
                self.assertIn("compose linear needs a Linear key", self.fails(fc_repolist.cmd_linear, self.args(action)))
        self.assertEqual(self.calls, [])

    def test_a_project_body_write_never_ensures(self):
        self.content = "# Shop\n\n## Goals\n\nold\n"
        (self.repo / "body.md").write_text("new goals\n")
        with mock.patch.object(fc_repolist, "ensure", side_effect=AssertionError("ensured")):
            self.assertEqual(self.run_cmd(self.args("project", file=str(self.repo / "body.md"), section="## Goals")), ["updated Shop"])
        self.assertEqual(self.content, "# Shop\n\n## Goals\n\nnew goals\n")


if __name__ == "__main__":
    unittest.main()
