import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, sys, unittest
from unittest import mock
from pathlib import Path
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_config, fc_linear, fc_linear_cmd, fc_policy  # noqa: E402

DOC = """# Verification policies
Intro text.
## Auth
Auth changes need a security review.
Keys too.
globs: bin/auth*.py, bin/keys/*
checks: security review, auth tests
## Migrations
A change to stored data runs a dry run first.
checks: migration dry run
"""
AUTH = {"name": "Auth", "statement": "Auth changes need a security review. Keys too.", "globs": ["bin/auth*.py", "bin/keys/*"], "checks": ["security review", "auth tests"]}
MIGRATIONS = {"name": "Migrations", "statement": "A change to stored data runs a dry run first.", "globs": [], "checks": ["migration dry run"]}


class Parse(unittest.TestCase):
    def test_brief_example(self):
        self.assertEqual(fc_policy.parse_policies(DOC), [AUTH, MIGRATIONS])

    def test_holdout_spacing_empty_parts_and_preamble(self):
        text = "globs: *\nchecks: early\n##   Spaced name  \n\n   first line   \n\t\n  globs: a/*, , b ,\n### not a heading\n  checks:  ,x,\n## Empty\n"
        self.assertEqual(fc_policy.parse_policies(text), [
            {"name": "Spaced name", "statement": "first line ### not a heading", "globs": ["a/*", "b"], "checks": ["x"]},
            {"name": "Empty", "statement": "", "globs": [], "checks": []}])
        self.assertEqual(fc_policy.parse_policies("# Title\nno policies here\n"), [])

    def test_glob_matches(self):
        policies = [AUTH, MIGRATIONS]
        self.assertEqual(fc_policy.glob_matches(policies, ["bin/auth_keys.py"]), [AUTH])
        self.assertEqual(fc_policy.glob_matches(policies, ["bin/keys/deep/k.pem"]), [AUTH])
        self.assertEqual(fc_policy.glob_matches(policies, ["bin/other.py", "migrations/1.sql"]), [])
        self.assertEqual(fc_policy.glob_matches(policies, []), [])

    def test_holdout_once_each_in_document_order(self):
        a = {"name": "A", "statement": "", "globs": ["x/*", "*.md"], "checks": []}
        b = {"name": "B", "statement": "", "globs": ["y/*"], "checks": []}
        plain = {"name": "P", "statement": "", "globs": [], "checks": ["c"]}
        files = ["y/1.py", "x/2.py", "x/README.md"]
        self.assertEqual(fc_policy.glob_matches([a, plain, b], files), [a, b])


PROJECT = {"id": "P1", "name": "Proj", "url": "u", "team": {"id": "T1", "name": "Team", "key": "NEB"}}
TITLE = "Verification policies"


class Read(unittest.TestCase):
    def read(self, pages, project=(PROJECT,), fail_get=()):
        """read_policies with linear_project giving `project` in turn and linear_gql serving `pages` of (id, title) documents,
        each get raising the next of `fail_get` before answering with DOC."""
        fails = list(fail_get)
        def gql(query, variables):
            if "documents(first: 100, after: $after)" in query and "nodes { id title updatedAt }" in query:
                self.assertEqual(variables["id"], "P1")
                i = int(variables["after"] or 0)
                return {"project": {"documents": {"pageInfo": {"hasNextPage": i + 1 < len(pages), "endCursor": str(i + 1)},
                                                  "nodes": [{"id": d, "title": t, "updatedAt": "2026-10-21T00:00:00Z"} for d, t in pages[i]]}}}
            self.assertIn("document(id: $id) { title content url }", query)
            if fails:
                raise fails.pop(0)
            return {"document": {"title": TITLE, "content": DOC, "url": "u"}}
        with mock.patch.object(fc_config, "LINEAR_KEY", "lin_test"), mock.patch.object(fc_linear, "linear_gql", side_effect=gql) as self.gql, \
                mock.patch.object(fc_linear_cmd, "linear_project", side_effect=list(project)) as self.project:
            return fc_policy.read_policies("/repo")

    def test_no_document_and_holdout_exact_title(self):
        self.assertEqual(self.read([[("D1", "Verification policies (old)"), ("D2", "verification policies")]]), ([], "no Verification policies document"))

    def test_one_document_parsed_holdout_on_a_later_page(self):
        self.assertEqual(self.read([[("D7", "Other")], [("D1", TITLE)]]), ([AUTH, MIGRATIONS], ""))
        self.assertEqual(self.gql.call_args.args[1], {"id": "D1"})
        self.project.assert_called_once_with("/repo")

    def test_duplicates_raise_naming_every_id_across_pages_without_retry(self):
        with self.assertRaises(RuntimeError) as e:
            self.read([[("D1", TITLE), ("D2", "Other")], [("D3", TITLE)]])
        self.assertEqual(str(e.exception), "2 documents titled Verification policies: D1, D3")
        self.assertEqual(self.project.call_count, 1)

    def test_system_exit_is_retried_once_then_succeeds(self):
        self.assertEqual(self.read([[("D1", TITLE)]], project=(SystemExit("no .compose/linear.json in /repo"), PROJECT)), ([AUTH, MIGRATIONS], ""))
        self.assertEqual(self.project.call_count, 2)

    def test_second_failure_raises_unreadable_holdout_retry_from_the_start(self):
        with self.assertRaises(RuntimeError) as e:
            self.read([[("D1", TITLE)]], project=(PROJECT, SystemExit("Linear has no project x")), fail_get=(ValueError("timed out"),))
        self.assertEqual(str(e.exception), "policies unreadable: Linear has no project x; check the link with `compose linear project --repo /repo`; or set COMPOSE_NO_POLICIES=1 to run without the project's policies")
        self.assertEqual(self.project.call_count, 2)

    def test_a_missing_link_names_the_file_to_write_and_the_offline_override(self):
        with mock.patch.object(fc_policy.fc_core, "repo_link", return_value=Path("/repo/.compose/linear.json")), self.assertRaises(RuntimeError) as e:
            self.read([], project=(SystemExit("no .compose/linear.json in /repo"),) * 2)
        self.assertEqual(str(e.exception), 'policies unreadable: no .compose/linear.json in /repo; write /repo/.compose/linear.json as {"project": "<Linear project URL>", "team": "Engineering"}, '
                                           "then check it with `compose linear project --repo /repo`; or set COMPOSE_NO_POLICIES=1 to run without the project's policies")

    def test_compose_no_policies_reads_nothing_and_says_so(self):
        with mock.patch.dict(os.environ, {"COMPOSE_NO_POLICIES": "1"}):
            self.assertEqual(self.read([[("D1", TITLE)]]), ([], "policies skipped: COMPOSE_NO_POLICIES is set"))
        self.assertEqual((self.project.call_count, self.gql.call_count), (0, 0))


if __name__ == "__main__":
    unittest.main()
