import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_testred as tr  # noqa: E402

WRITER = "p-tests-001"
PY = f"{sys.executable} -c"


class Broken(unittest.TestCase):
    """What shows a test cannot run, against what shows it fails for the behavior it checks."""
    def test_a_test_that_cannot_load_is_named_by_its_line(self):
        for output, line in (("Traceback (most recent call last):\nModuleNotFoundError: No module named 'x'\n", "ModuleNotFoundError: No module named 'x'"),
                             ("E   ImportError: cannot import name 'f' from 'm'\n", "E   ImportError: cannot import name 'f' from 'm'"),
                             ("  File \"t.py\", line 3\n    def (:\nSyntaxError: invalid syntax\n", "SyntaxError: invalid syntax"),
                             ("E       NameError: name 'client' is not defined\n", "E       NameError: name 'client' is not defined"),
                             ("ReferenceError: describe is not defined\n", "ReferenceError: describe is not defined"),
                             ("Error: Cannot find module './a'\n", "Error: Cannot find module './a'"),
                             ("src/a.ts(3,1): error TS2307: Cannot find module 'b'.\n", "src/a.ts(3,1): error TS2307: Cannot find module 'b'."),
                             ("FAIL\tpkg [build failed]\n", "FAIL\tpkg [build failed]"),
                             ("error[E0432]: unresolved import `crate::a`\n", "error[E0432]: unresolved import `crate::a`")):
            self.assertEqual(tr.broken(output), line.strip())

    def test_a_test_that_fails_on_the_missing_behavior_is_not_broken(self):
        for output in ("E   AssertionError: assert 0 == 3\n", "NotImplementedError: not implemented\n", "FAIL: TestSum (0.00s)\n    a_test.go:9: got 0, want 3\n",
                       "AssertionError: expected 'SyntaxError: bad' in the message\n", "", " Tests  2 failed | 1 passed (3)\n", "# tests 3\n# fail 1\n",
                       "collected 4 items\n\ntest_a.py F...\n"):
            self.assertEqual(tr.broken(output), "")

    def test_a_run_that_ran_no_test_is_broken_and_named_by_its_line(self):
        """A red run that ran zero tests (the wrong path, a filter no case matches, a file that failed to resolve) proves nothing about the behavior."""
        for output, line in ((" ❯ src/InvestigationPage.test.tsx (0 test)\n\n Test Files  1 failed (1)\n      Tests  no tests\n", "❯ src/InvestigationPage.test.tsx (0 test)"),
                             ("No test files found, exiting with code 1\n", "No test files found, exiting with code 1"),
                             ("No tests found, exiting with code 1\nRun with `--passWithNoTests`\n", "No tests found, exiting with code 1"),
                             ("============ no tests ran in 0.01s ============\n", "============ no tests ran in 0.01s ============"),
                             ("collected 0 items\n", "collected 0 items"),
                             ("testing: warning: no tests to run\nPASS\n", "testing: warning: no tests to run"),
                             ("# tests 0\n# pass 0\n", "# tests 0"),
                             ("Error: No tests found\n", "Error: No tests found")):
            self.assertEqual(tr.broken(output), line)


class Gate(unittest.TestCase):
    """A writer whose test cannot load is sent back once; everything else is collected as before."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "worktrees" / "tests-001").mkdir(parents=True)
        self.reports = []
        self.sent = []
        self.says = io.StringIO()
        for p in (mock.patch.object(fc.fc_crew, "crew_reports", lambda proj, name: self.reports), mock.patch.object(fc.fc_herdr, "live_agents", lambda: [{"name": WRITER}]),
                  mock.patch.object(fc.fc_herdr, "herdr", self.herdr), mock.patch.object(fc.fc_core, "say", lambda m: self.says.write(m + "\n"))):
            p.start(); self.addCleanup(p.stop)
        self.answer = {"ok": 1}

    def herdr(self, *args, **kw):
        self.sent.append(args)
        return self.answer

    def meta(self, code):
        return {"id": "001", "test": f"{PY} {json.dumps(code)}"}

    def test_a_test_that_cannot_load_goes_back_to_its_writer_with_the_error(self):
        self.reports = [{"state": "working", "message": "test ready: abc"}]
        self.assertTrue(tr.gate(self.proj, self.meta("import no_such_module_xyz"), WRITER))
        (_, _, name, text), = self.sent
        self.assertEqual(name, WRITER)
        self.assertIn("ModuleNotFoundError: No module named 'no_such_module_xyz'", text)
        self.assertIn("report test ready again", text)

    def test_the_collect_waits_until_the_writer_reports_again_then_takes_what_it_wrote(self):
        meta = self.meta("import no_such_module_xyz")
        self.reports = [{"state": "working", "message": "test ready: abc"}]
        self.assertTrue(tr.gate(self.proj, meta, WRITER))
        self.assertTrue(tr.gate(self.proj, meta, WRITER))  # no new report: it waits, and sends nothing more
        self.assertEqual(len(self.sent), 1)
        self.reports.append({"state": "working", "message": "test ready: def"})
        self.assertFalse(tr.gate(self.proj, meta, WRITER))  # still broken after its one send back: collected, with a warning
        self.assertEqual(len(self.sent), 1)
        self.assertIn("sent back once already", self.says.getvalue())

    def test_a_test_that_runs_no_case_goes_back_to_its_writer(self):
        self.reports = [{"state": "working", "message": "test ready: abc"}]
        self.assertTrue(tr.gate(self.proj, self.meta("print('collected 0 items'); raise SystemExit(5)"), WRITER))
        (_, _, name, text), = self.sent
        self.assertEqual(name, WRITER)
        self.assertIn("collected 0 items", text)

    def test_a_run_of_no_case_that_exits_green_goes_back_too(self):
        """go exits 0 on `no tests to run`: a green that proves nothing is as useless as a red that does."""
        self.reports = [{"state": "working", "message": "test ready: abc"}]
        self.assertTrue(tr.gate(self.proj, self.meta("print('testing: warning: no tests to run'); print('PASS')"), WRITER))
        (_, _, _, text), = self.sent
        self.assertIn("no tests to run", text)

    def test_a_test_that_fails_on_the_behavior_or_passes_is_collected(self):
        self.reports = [{"state": "working", "message": "test ready: abc"}]
        for code in ("assert 1 == 2", "pass"):
            self.assertFalse(tr.gate(self.proj, self.meta(code), WRITER))
        self.assertEqual(self.sent, [])

    def test_a_writer_that_cannot_be_reached_is_collected_with_a_warning(self):
        self.reports = [{"state": "working", "message": "test ready: abc"}]
        self.answer = None
        self.assertFalse(tr.gate(self.proj, self.meta("import no_such_module_xyz"), WRITER))
        self.assertIn("could not reach", self.says.getvalue())
        with mock.patch.object(fc.fc_herdr, "live_agents", lambda: []):
            self.assertFalse(tr.gate(self.proj, self.meta("import no_such_module_xyz"), WRITER))
        self.assertIn("is not running", self.says.getvalue())

    def test_a_writer_without_a_checkout_is_collected(self):
        (self.proj / "worktrees" / "tests-001").rmdir()
        self.assertFalse(tr.gate(self.proj, self.meta("import no_such_module_xyz"), WRITER))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
