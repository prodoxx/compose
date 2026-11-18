import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


class Reads(unittest.TestCase):
    """#451: the files a task's `reads:` names reach its implementer, which otherwise reads only its own files and the context pack."""

    def setUp(self):
        self.wt = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.wt, True)
        (self.wt / "lib").mkdir(); (self.wt / "lib" / "keywords.py").write_text('KEYWORDS = ["a"]\n')
        self.excluded, self.shown = [], {"940bd04:data/cases.json": b'[{"q": "one"}]\n'}
        def run(cmd, cwd=None, **kw):
            body = self.shown.get(cmd[2])
            return subprocess.CompletedProcess(cmd, 0 if body is not None else 128, body or b"", b"" if body is not None else b"fatal: path does not exist")
        for p in (mock.patch.object(fc.fc_implement.fc_reads.subprocess, "run", run), mock.patch.object(fc.fc_worktree, "exclude_paths", lambda wt, rules: self.excluded.append(rules))):
            p.start(); self.addCleanup(p.stop)

    def test_an_entry_is_a_path_of_the_clone_or_a_file_as_of_a_commit(self):
        self.assertEqual(fc.fc_implement.fc_reads.split("lib/keywords.py"), (None, "lib/keywords.py"))
        self.assertEqual(fc.fc_implement.fc_reads.split("940bd04:data/cases.json"), ("940bd04", "data/cases.json"))
        self.assertEqual(fc.fc_implement.fc_reads.split("HEAD~2:a.ts"), ("HEAD~2", "a.ts"))
        self.assertEqual(fc.fc_implement.fc_reads.split("my dir:a"), (None, "my dir:a"))  # not a revision

    def test_only_a_file_inside_the_repo_is_a_read(self):
        for ok in ("src/a.ts", "940bd04:src/a.ts", "a.py"):
            self.assertIsNone(fc.fc_implement.fc_reads.invalid(ok), ok)
        for bad in ("/etc/passwd", "../x", "a/../../x", "src/*.ts", "940bd04:../x", "940bd04:/etc/passwd", "~/x"):
            self.assertIsNotNone(fc.fc_implement.fc_reads.invalid(bad), bad)

    def test_a_path_is_readable_in_place_and_a_commits_file_is_written_into_the_clone_outside_the_repo(self):
        paths, missing = fc.fc_implement.fc_reads.resolve(self.wt, ["lib/keywords.py", "940bd04:data/cases.json"])
        root = self.wt.resolve()
        self.assertEqual((paths, missing), ([str(root / "lib" / "keywords.py"), str(root / ".reads" / "940bd04" / "data" / "cases.json")], []))
        self.assertEqual((root / ".reads" / "940bd04" / "data" / "cases.json").read_bytes(), b'[{"q": "one"}]\n')
        self.assertEqual(self.excluded, [["/.reads/"]])  # `add -A` never stages it, so it is no stray file

    def test_an_entry_that_cannot_be_read_is_reported_and_the_others_still_are(self):
        paths, missing = fc.fc_implement.fc_reads.resolve(self.wt, ["lib/none.py", "940bd04:data/none.json", "../out", "lib/keywords.py"])
        self.assertEqual(paths, [str(self.wt.resolve() / "lib" / "keywords.py")])
        self.assertEqual(len(missing), 3)
        self.assertIn("no such file", missing[0]); self.assertIn("cannot show it", missing[1]); self.assertIn("inside the repo", missing[2])
        self.assertEqual(self.excluded, [])  # nothing was written

    def test_the_guard_lets_the_implementer_read_them_and_nothing_beside_them(self):
        paths, _ = fc.fc_implement.fc_reads.resolve(self.wt, ["lib/keywords.py", "940bd04:data/cases.json"])
        g = json.loads(fc.fc_readguard.write_guard(self.wt, ["cases.py"], "", extra=paths).read_text())
        for p in ("lib/keywords.py", ".reads/940bd04/data/cases.json"):
            self.assertIsNone(fc.fc_readguard.refusal("Read", {"file_path": p}, g), p)
            self.assertIsNone(fc.fc_readguard.refusal("bash", {"command": f"head -5 {p}"}, g), p)
        self.assertIsNotNone(fc.fc_readguard.refusal("Read", {"file_path": "lib/other.py"}, g))
        self.assertIsNotNone(fc.fc_readguard.refusal("bash", {"command": "show 940bd04:data/cases.json"}, g))


class Grounding(unittest.TestCase):
    """What an implementer has to see to write a test the way the repo does. 8 tasks of one project missed on a guessed test setup: jsdom
    where the repo has happy-dom, `@testing-library/react` and `@storybook/react` that are not installed, JSX in a `.mjs` test; two said the
    read guard blocked package.json."""

    def setUp(self):
        self.wt = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.wt, True)
        self.root = self.wt.resolve()
        for f, body in {"package.json": "{}", "web/package.json": "{}", "web/src/card.tsx": "x", "web/src/card.stories.tsx": "s\n" * 5, "web/src/button.stories.tsx": "s\n" * 3,
                        "web/tests/card.test.mjs": "t\n" * 9, "web/tests/card-row.test.mjs": "t\n" * 4, "web/tests/panel.test.mjs": "t\n" * 2, "web/tests/helpers.mjs": "h",
                        "api/pyproject.toml": "", "api/tests/test_auth.py": "t\n", "api/tests/conftest.py": "c", "api/server/auth.py": "a",
                        "php/composer.json": "{}", "php/tests/Feature/AuthControllerTest.php": "t"}.items():
            (self.wt / f).parent.mkdir(parents=True, exist_ok=True); (self.wt / f).write_text(body)
        self.reads = fc.fc_implement.fc_reads

    def test_the_package_files_above_a_tasks_files_are_readable(self):
        self.assertEqual(self.reads.package_files(self.wt, ["web/src/new.tsx", "web/tests/new.test.mjs"]), [str(self.root / "package.json"), str(self.root / "web" / "package.json")])
        self.assertEqual(self.reads.package_files(self.wt, ["api/server/auth.py"]), [str(self.root / "api" / "pyproject.toml"), str(self.root / "package.json")])
        self.assertEqual(self.reads.package_files(self.wt, ["php/tests/Feature/NewTest.php"]), [str(self.root / "package.json"), str(self.root / "php" / "composer.json")])
        self.assertEqual(self.reads.package_files(self.wt, []), [str(self.root / "package.json")])  # the root's own, whatever the task touches
        shutil.rmtree(self.wt / "web"); (self.wt / "package.json").unlink()
        self.assertEqual(self.reads.package_files(self.wt, ["web/src/new.tsx"]), [])

    def test_a_new_test_or_story_gets_an_existing_one_of_its_kind_to_write_it_like(self):
        ex = lambda files: self.reads.examples(self.wt, files)
        # the nearest name in the same directory with the same ending; an existing file needs none: it shows its own setup
        self.assertEqual(ex(["web/tests/card-list.test.mjs", "web/tests/panel.test.mjs"]), {"web/tests/card-list.test.mjs": str(self.root / "web/tests/card-row.test.mjs")})
        self.assertEqual(ex(["web/tests/zebra.test.mjs"]), {"web/tests/zebra.test.mjs": str(self.root / "web/tests/panel.test.mjs")})  # no name near it: the shortest file
        self.assertEqual(ex(["web/src/menu.stories.tsx"]), {"web/src/menu.stories.tsx": str(self.root / "web/src/button.stories.tsx")})
        self.assertEqual(ex(["api/tests/test_drafts.py"]), {"api/tests/test_drafts.py": str(self.root / "api/tests/test_auth.py")})  # a test_ file, never conftest.py
        self.assertEqual(ex(["php/tests/Feature/SessionTest.php"]), {"php/tests/Feature/SessionTest.php": str(self.root / "php/tests/Feature/AuthControllerTest.php")})
        # a plain source file is not a kind, and a kind the directory does not hold yet has no example
        self.assertEqual(ex(["web/src/menu.tsx", "web/tests/helpers2.mjs", "web/src/menu.test.tsx", "api/server/test_x.py"]), {})
        # a file the same task creates is never the example of another
        self.assertEqual(ex(["web/tests/a.spec.mjs", "web/tests/b.spec.mjs"]), {})

    def test_the_prompt_names_them_and_the_guard_lets_the_implementer_read_them(self):
        paths, note = self.reads.grounding(self.wt, ["web/src/menu.tsx", "web/tests/menu.test.mjs"], may_install=False)
        self.assertEqual(paths, [str(self.root / "package.json"), str(self.root / "web/package.json"), str(self.root / "web/tests/panel.test.mjs")])
        self.assertIn(f"{self.root / 'package.json'}, {self.root / 'web/package.json'}", note)
        self.assertIn("is not installed", note); self.assertIn("CONTRADICTION", note)
        self.assertIn(f"{self.root / 'web/tests/menu.test.mjs'} like {self.root / 'web/tests/panel.test.mjs'}", note)
        self.assertTrue(note.endswith("\n"))
        g = json.loads(fc.fc_readguard.write_guard(self.wt, ["web/src/menu.tsx", "web/tests/menu.test.mjs"], "", extra=paths).read_text())
        for p in ("package.json", "web/package.json", "web/tests/panel.test.mjs"):
            self.assertIsNone(fc.fc_readguard.refusal("Read", {"file_path": p}, g), p)
        self.assertIsNotNone(fc.fc_readguard.refusal("Read", {"file_path": "web/tests/card.test.mjs"}, g))
        # a task that may install adds what it needs instead of stopping
        _, note = self.reads.grounding(self.wt, ["web/package.json", "web/src/menu.tsx"], may_install=True)
        self.assertIn("add it with the package manager command below", note); self.assertNotIn("CONTRADICTION", note)
        # nothing to name: no line at all
        shutil.rmtree(self.wt / "web"); (self.wt / "package.json").unlink()
        self.assertEqual(self.reads.grounding(self.wt, ["web/src/menu.tsx"], may_install=False), ([], ""))


if __name__ == "__main__":
    unittest.main()
