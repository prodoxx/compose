import hermetic  # first: no test sees the real home
import io, os, shutil, subprocess, sys, tempfile, time, types, unittest
from pathlib import Path
from unittest import mock
from cleanup_kit import fc
os.environ["HERDR_ENV"] = ""


class HermeticCleanup(unittest.TestCase):
    """Every temp dir a test makes is gone when the test is: a suite run by many teammates at once must not fill the disk's temp folder."""

    def run_case(self, body):
        made = {}
        class Case(unittest.TestCase):
            def test_it(self):
                body(self, made)
        unittest.TextTestRunner(stream=io.StringIO()).run(unittest.defaultTestLoader.loadTestsFromTestCase(Case))
        return made

    def test_a_tests_mkdtemp_dirs_are_removed_after_it(self):
        made = self.run_case(lambda case, made: made.update(a=tempfile.mkdtemp(), b=Path(tempfile.mkdtemp(prefix="x-")) / "sub"))
        self.assertEqual([k for k, d in made.items() if Path(d).exists()], [])

    def test_a_tests_temp_home_is_removed_after_it(self):
        made = self.run_case(lambda case, made: made.update(home=os.environ["HOME"]))
        self.assertTrue(made["home"].startswith(tempfile.gettempdir()) or "/compose-home-" in made["home"], made["home"])
        self.assertFalse(Path(made["home"]).exists(), made["home"])

    def test_stubs_with_the_same_body_share_one_executable(self):
        """macOS scans every new executable file on its first run (about 150 ms of XprotectService each); a stub body
        seen before is linked to the file already scanned instead of being a new file."""
        def body(case, made):
            for n in ("a", "b"):
                d = Path(tempfile.mkdtemp()); (d / "gh").write_text("#!/bin/sh\necho 1.0.0\n"); (d / "gh").chmod(0o755)
                made[n] = d / "gh"
            made["out"] = subprocess.run([str(made["a"])], capture_output=True, text=True).stdout
            made["links"] = (made["a"].is_symlink(), made["b"].is_symlink())
            made["same"] = made["a"].resolve() == made["b"].resolve()
        made = self.run_case(body)
        self.assertEqual((made["out"], made["links"], made["same"]), ("1.0.0\n", (True, True), True))


class HermeticMachine(unittest.TestCase):
    """No test touches the real machine: lsof and herdr on PATH are fakes, and a process a test registers dies with it."""

    def process(self, cwd):
        p = subprocess.Popen(["sleep", "300"], cwd=cwd)
        p.cwd = cwd
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def alive(self, p):
        deadline = time.time() + 5
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        return p.poll() is None

    def test_lsof_and_herdr_on_the_path_are_the_fakes(self):
        self.assertEqual(Path(shutil.which("lsof")).parent, hermetic.guard_dir())
        self.assertEqual(Path(shutil.which("herdr")).parent, hermetic.guard_dir())
        self.assertNotEqual(os.environ.get("HERDR_ENV"), "1")  # the suite is no pane of the user's herdr
        for var in ("HERDR_SOCKET_PATH", "HERDR_SESSION", "HERDR_BIN_PATH", "HERDR_TAB_ID"):
            self.assertNotIn(var, os.environ)

    def run_case(self, body):
        class Case(unittest.TestCase):
            def test_it(self):
                body(self)
        return unittest.TextTestRunner(stream=io.StringIO()).run(unittest.defaultTestLoader.loadTestsFromTestCase(Case))

    def test_a_test_that_starts_a_herdr_agent_starts_none_and_fails(self):
        def body(case):
            with mock.patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/nowhere/herdr.sock"}):
                case.assertIsNone(fc.fc_herdr.herdr("agent", "start", "p-tester", "--cwd", tempfile.mkdtemp()))
        result = self.run_case(body)
        self.assertEqual(len(result.failures), 1, result.errors)
        self.assertIn("real herdr", result.failures[0][1])
        self.assertIn("herdr agent start p-tester", result.failures[0][1])

    def test_a_process_a_test_registers_is_killed_when_the_test_ends(self):
        made = {}
        def body(case):
            made["p"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
            hermetic.listen(made["p"].pid, 9300, Path.cwd())
        self.run_case(body)
        self.addCleanup(lambda: (made["p"].kill(), made["p"].wait()))
        self.assertFalse(self.alive(made["p"]))


class HermeticModules(unittest.TestCase):
    """Test files reload every fc_* module at import, so the last file loaded owns sys.modules while the others run."""

    def test_a_test_runs_with_the_fc_modules_its_file_loaded(self):
        probes, seen, cases = [types.ModuleType("fc_probe"), types.ModuleType("fc_probe")], [], []
        self.addCleanup(sys.modules.pop, "fc_probe", None)
        for n, probe in zip("ab", probes):  # file a loads first and file b after it: only b's copy is left in sys.modules
            sys.modules["fc_probe"] = probe
            cases.append(type("Case", (unittest.TestCase,), {"__module__": "probe_file_" + n, "test_it": lambda case: seen.append(sys.modules["fc_probe"])}))
            self.addCleanup(hermetic._world.pop, "probe_file_" + n, None)
        unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in cases))
        self.assertEqual(seen, probes)
        self.assertIs(sys.modules["fc_probe"], probes[1])  # and what sys.modules held is back after each test


if __name__ == "__main__":
    unittest.main()
