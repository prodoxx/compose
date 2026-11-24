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


if __name__ == "__main__":
    unittest.main()
