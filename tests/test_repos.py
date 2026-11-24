import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_repos  # noqa: E402


class Names(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        link = Path(self.repo) / ".compose" / "linear.json"
        link.parent.mkdir()
        link.write_text(json.dumps({"project": "https://linear.app/acme/project/my-shop-abc123", "team": "Engineering", "repo": "fork/shop"}))

    def test_every_url_form_of_one_repo_gives_one_name(self):
        for url in ("git@github.com:jobscanco/jobscan.git", "https://github.com/jobscanco/jobscan",
                    "https://github.com/jobscanco/jobscan.git/", "ssh://git@github.com:22/jobscanco/jobscan.git",
                    "https://user:token@github.com/jobscanco/jobscan.git"):
            self.assertEqual(fc_repos.name_from_url(url), "jobscanco/jobscan", url)

    def test_a_subgroup_keeps_its_whole_path_and_case_is_kept(self):
        self.assertEqual(fc_repos.name_from_url("git@gitlab.com:Acme/web/Shop.git"), "Acme/web/Shop")

    def test_a_url_with_no_owner_gives_none(self):
        for url in ("", "   ", "/tmp/bare.git", "file:///tmp/bare.git", "https://github.com/onlyone"):
            self.assertIsNone(fc_repos.name_from_url(url), url)

    def test_the_recorded_name_wins_over_origin(self):
        with mock.patch.object(fc_repos, "origin_url", return_value="git@github.com:acme/shop.git") as asked:
            self.assertEqual(fc_repos.repo_name(self.repo), "fork/shop")
        asked.assert_not_called()

    def test_without_a_recorded_name_origin_names_the_repo(self):
        (Path(self.repo) / ".compose" / "linear.json").write_text(json.dumps({"project": "https://linear.app/acme/project/my-shop-abc123"}))
        with mock.patch.object(fc_repos, "origin_url", return_value="git@github.com:acme/shop.git"):
            self.assertEqual(fc_repos.repo_name(self.repo), "acme/shop")

    def test_no_link_file_and_no_origin_give_none(self):
        (Path(self.repo) / ".compose" / "linear.json").unlink()
        with mock.patch.object(fc_repos, "origin_url", return_value=""):
            self.assertIsNone(fc_repos.repo_name(self.repo))

    def test_origin_url_is_the_one_git_call_and_empty_without_a_remote(self):
        with mock.patch.object(fc_repos.fc_core, "sh", return_value=mock.Mock(returncode=0, stdout="git@github.com:acme/shop.git\n")) as sh:
            self.assertEqual(fc_repos.origin_url(self.repo), "git@github.com:acme/shop.git")
        self.assertEqual(sh.call_args.args[0], ["git", "remote", "get-url", "origin"])
        with mock.patch.object(fc_repos.fc_core, "sh", return_value=mock.Mock(returncode=2, stdout="")):
            self.assertEqual(fc_repos.origin_url(self.repo), "")

    def test_the_link_reader_gives_the_repo_only_once_it_is_listed(self):
        link = Path(self.repo) / ".compose" / "linear.json"
        self.assertEqual((fc.fc_core.recorded_repo(self.repo), fc.fc_core.linked_repo(self.repo)), ("fork/shop", None))
        link.write_text(json.dumps({"project": "x", "repo": "fork/shop", "listed": "x"}))
        self.assertEqual(fc.fc_core.linked_repo(self.repo), "fork/shop")
        for junk in ("{not json", "[]", '{"listed": "x", "project": "x"}', '{"repo": "", "listed": "x", "project": "x"}', '{"repo": "a/b", "listed": true, "project": "x"}',
                     '{"repo": "a/b", "listed": "x", "project": "y"}', '{"repo": "a/b", "listed": ""}', '{"repo": "a/b", "listed": null, "project": null}'):
            link.write_text(junk)
            self.assertIsNone(fc.fc_core.linked_repo(self.repo), junk)
        link.unlink()
        self.assertEqual((fc.fc_core.recorded_repo(self.repo), fc.fc_core.linked_repo(self.repo)), (None, None))

    def test_names_compare_without_case(self):
        self.assertTrue(fc_repos.same("JobscanCo/Jobscan", "jobscanco/jobscan"))
        self.assertFalse(fc_repos.same("acme/api", "acme/web"))


class RepoList(unittest.TestCase):
    CONTENT = "Intro line.\n\n## Repos\n- acme/api: the API\n- acme/web: the web app\n\n## Links\n- a link\n"

    def test_it_reads_names_and_descriptions(self):
        self.assertEqual(fc_repos.parse_list(self.CONTENT), [("acme/api", "the API"), ("acme/web", "the web app")])

    def test_no_section_and_no_content_read_as_empty(self):
        self.assertEqual(fc_repos.parse_list("Intro only."), [])
        self.assertEqual(fc_repos.parse_list(None), [])

    def test_a_hand_edited_section_still_parses(self):
        text = "### Repos\n\n* `acme/api` : the API\n\n- acme/web\n- not a repo line\n"
        self.assertEqual(fc_repos.parse_list(text), [("acme/api", "the API"), ("acme/web", "")])

    def test_bullets_outside_the_section_do_not_count(self):
        self.assertEqual(fc_repos.parse_list("- other/thing: before\n## Repos\n- acme/api: x\n## Links\n- acme/web: y\n"), [("acme/api", "x")])

    def test_adding_writes_the_section_at_the_end_when_missing(self):
        self.assertEqual(fc_repos.with_repo("Intro only.", "acme/api", "the API"), "Intro only.\n\n## Repos\n- acme/api: the API\n")
        self.assertEqual(fc_repos.with_repo(None, "acme/api", ""), "## Repos\n- acme/api\n")

    def test_adding_keeps_every_other_line_and_is_idempotent(self):
        new = fc_repos.with_repo(self.CONTENT, "acme/engine", "the engine")
        self.assertEqual(new, self.CONTENT.replace("- acme/web: the web app\n", "- acme/web: the web app\n- acme/engine: the engine\n"))
        self.assertEqual(fc_repos.with_repo(new, "ACME/engine", "other words"), new)

    def test_adding_to_an_empty_section_goes_under_its_heading(self):
        self.assertEqual(fc_repos.with_repo("## Repos\n\n## Links\n", "acme/api", "the API"), "## Repos\n- acme/api: the API\n\n## Links\n")

    def test_dropping_removes_only_that_bullet(self):
        self.assertEqual(fc_repos.without_repo(self.CONTENT, "acme/api"), self.CONTENT.replace("- acme/api: the API\n", ""))
        self.assertEqual(fc_repos.without_repo(self.CONTENT, "ACME/WEB"), self.CONTENT.replace("- acme/web: the web app\n", ""))

    def test_dropping_what_is_not_listed_changes_nothing(self):
        self.assertEqual(fc_repos.without_repo(self.CONTENT, "acme/none"), self.CONTENT)
        self.assertEqual(fc_repos.without_repo(None, "acme/none"), "")

    def test_a_crlf_content_reads_like_an_lf_one(self):
        text = self.CONTENT.replace("\n", "\r\n")
        self.assertEqual(fc_repos.parse_list(text), [("acme/api", "the API"), ("acme/web", "the web app")])
        self.assertEqual(fc_repos.parse_list("## Repos\r\n- acme/web\r\n"), [("acme/web", "")])

    def test_a_closed_or_indented_heading_is_still_the_section(self):
        for heading in ("## Repos ##", "## Repos ##\r", "   ## Repos", "  ## Repos  ##"):
            self.assertEqual(fc_repos.parse_list(f"{heading}\n- acme/api: the API\n"), [("acme/api", "the API")], heading)
        self.assertEqual(fc_repos.parse_list("    ## Repos\n- acme/api: the API\n"), [])
        self.assertEqual(fc_repos.parse_list("## Repos#\n- acme/api: the API\n"), [])

    def test_a_crlf_content_is_written_back_with_every_other_line_as_it_was(self):
        text = self.CONTENT.replace("\n", "\r\n")
        new = fc_repos.with_repo(text, "acme/engine", "the engine")
        self.assertEqual(new, text.replace("- acme/web: the web app\r\n", "- acme/web: the web app\r\n- acme/engine: the engine\r\n"))
        self.assertEqual(fc_repos.with_repo(new, "ACME/Engine", ""), new)
        self.assertEqual(fc_repos.without_repo(text, "ACME/web"), text.replace("- acme/web: the web app\r\n", ""))
        self.assertEqual(fc_repos.with_repo("Intro.\r\n", "acme/api", ""), "Intro.\r\n\r\n## Repos\r\n- acme/api\r\n")

    def test_a_closed_or_indented_heading_is_written_into_not_duplicated(self):
        for heading in ("## Repos ##", "  ## Repos"):
            text = f"{heading}\n- acme/api: the API\n\n## Links\n"
            self.assertEqual(fc_repos.with_repo(text, "acme/web", "the web"), f"{heading}\n- acme/api: the API\n- acme/web: the web\n\n## Links\n")
            self.assertEqual(fc_repos.without_repo(text, "acme/api"), f"{heading}\n\n## Links\n")


TASK = "## Goal\nBuild it.\n\n```compose\nid: 001\nfiles: a.py\ntest: pytest a\n```\n"


class TaskRepo(unittest.TestCase):
    def test_a_task_that_names_a_repo_and_a_second_block_are_left_alone(self):
        named = TASK.replace("id: 001\n", "id: 001\nrepo: acme/other\n")
        self.assertIsNone(fc_repos.with_task_repo(named, "acme/api"))
        two = TASK + "\n```compose\nid: 009\ntest: x\n```\n"
        self.assertEqual(fc_repos.with_task_repo(two, "acme/api").count("repo: acme/api"), 1)
        self.assertIsNone(fc_repos.with_task_repo("no block", "acme/api"))

    def test_a_block_whose_first_line_is_not_id_gets_the_repo_after_its_id_line(self):
        odd = "```compose\nfiles: a.py\nid: 001\ntest: x\n```\n"
        self.assertEqual(fc_repos.with_task_repo(odd, "acme/api"), "```compose\nfiles: a.py\nid: 001\nrepo: acme/api\ntest: x\n```\n")

    def test_the_repo_line_follows_the_id_line_s_indent_and_a_key_with_spaces_counts(self):
        self.assertEqual(fc_repos.with_task_repo("```compose\n  id: 001\ntest: x\n```\n", "acme/api"), "```compose\n  id: 001\n  repo: acme/api\ntest: x\n```\n")
        self.assertIsNone(fc_repos.with_task_repo("```compose\nid: 001\n repo : acme/web\n```\n", "acme/api"))
        self.assertIsNone(fc_repos.with_task_repo("```compose\ntest: x\n```\n", "acme/api"))
        self.assertIsNone(fc_repos.with_task_repo(None, "acme/api"))


class PathMap(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        for p in (mock.patch.object(fc.fc_core, "main_checkout", lambda path: Path(path)),
                  mock.patch.object(fc.fc_config, "CONFIG_PATH", Path(tempfile.mkdtemp()).resolve() / ".compose" / "config.json")):
            p.start(); self.addCleanup(p.stop)

    def test_a_recorded_repo_is_found_under_any_case(self):
        fc_repos.record_path("acme/api", self.repo)
        self.assertEqual(fc_repos.path_of("ACME/api"), self.repo)
        self.assertEqual(fc_repos.paths(), {"acme/api": str(self.repo)})

    def test_the_map_sits_beside_the_config_and_follows_it(self):
        self.assertEqual(fc_repos.PATHS_FILE(), fc.fc_config.CONFIG_PATH.parent / "repos.json")

    def test_recording_in_another_case_replaces_the_entry(self):
        fc_repos.record_path("acme/api", self.repo)
        other = self.repo / "other"
        other.mkdir()
        fc_repos.record_path("Acme/API", other)
        self.assertEqual(fc_repos.paths(), {"acme/api": str(other)})  # the name as first seen, the path as last recorded

    def test_the_main_checkout_is_what_is_stored(self):
        main = self.repo / "main"
        main.mkdir()
        with mock.patch.object(fc.fc_core, "main_checkout", return_value=main) as asked:
            fc_repos.record_path("acme/api", self.repo / "worktree")
        asked.assert_called_once_with(self.repo / "worktree")
        self.assertEqual(fc_repos.paths(), {"acme/api": str(main)})

    def test_each_workspace_has_its_own_map(self):
        fc_repos.record_path("acme/api", self.repo)
        with mock.patch.object(fc.fc_config, "WORKSPACE", "work"):
            self.assertIsNone(fc_repos.path_of("acme/api"))
            fc_repos.record_path("acme/web", self.repo)
            self.assertEqual(fc_repos.paths(), {"acme/web": str(self.repo)})
        self.assertEqual(json.loads(fc_repos.PATHS_FILE().read_text()),
                         {"default": {"acme/api": str(self.repo)}, "work": {"acme/web": str(self.repo)}})

    def test_a_folder_that_is_gone_reads_as_unknown_and_prune_drops_it(self):
        fc_repos.record_path("acme/api", self.repo)
        fc_repos.record_path("acme/web", self.repo / "missing")
        self.assertIsNone(fc_repos.path_of("acme/web"))
        self.assertEqual(fc_repos.prune_paths(), ["acme/web"])
        self.assertEqual(fc_repos.paths(), {"acme/api": str(self.repo)})

    def test_prune_keeps_the_other_workspaces_and_writes_nothing_when_all_is_live(self):
        fc_repos.record_path("acme/api", self.repo)
        with mock.patch.object(fc.fc_config, "WORKSPACE", "work"):
            fc_repos.record_path("acme/web", self.repo / "missing")
        before = fc_repos.PATHS_FILE().read_text()
        self.assertEqual(fc_repos.prune_paths(), [])
        self.assertEqual(fc_repos.PATHS_FILE().read_text(), before)

    def test_no_file_reads_as_empty_and_prune_creates_none(self):
        self.assertEqual((fc_repos.paths(), fc_repos.path_of("acme/api"), fc_repos.prune_paths()), ({}, None, []))
        self.assertFalse(fc_repos.PATHS_FILE().exists())

    def test_an_unreadable_file_reads_as_empty_and_is_rewritten_whole(self):
        fc_repos.PATHS_FILE().parent.mkdir(parents=True, exist_ok=True)
        for junk in ("{not json", "[]", '{"default": ["x"]}', '{"default": {"acme/api": 3}}'):
            fc_repos.PATHS_FILE().write_text(junk)
            self.assertEqual(fc_repos.paths(), {}, junk)
        fc_repos.PATHS_FILE().write_text("{not json")
        fc_repos.record_path("acme/api", self.repo)
        self.assertEqual(json.loads(fc_repos.PATHS_FILE().read_text()), {"default": {"acme/api": str(self.repo)}})


if __name__ == "__main__":
    unittest.main()
