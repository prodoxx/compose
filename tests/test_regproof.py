import hermetic  # first: no test sees the real home
import importlib, importlib.machinery, importlib.util, os, unittest
from pathlib import Path
import subprocess, sys
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_regproof  # noqa: E402


def proc(code, out="", err=""):
    return subprocess.CompletedProcess(["t"], code, out, err)


class Classify(unittest.TestCase):
    """A regression test's run on the base: a pass, a harness error, or a failure that proves it."""

    def test_pattern(self):
        self.assertEqual(fc_regproof.HARNESS_RE.pattern, r"ImportError|ModuleNotFoundError|Failed to import test module|ERROR collecting|errors? during collection")

    def test_exit_zero_passes_even_naming_an_import_error(self):
        for out in ("", "ImportError: x\nOK", "command not found"):
            self.assertEqual(fc_regproof.base_result(proc(0, out)), ("pass", False), out)

    def test_exit_124_is_an_error(self):
        self.assertEqual(fc_regproof.base_result(proc(124, "plain output")), ("error", None))

    def test_harness_output_is_an_error_in_stdout_or_stderr(self):
        for text in ("bash: pytest: command not found", "ImportError: no x", "ModuleNotFoundError: y", "Failed to import test module: t",
                     "ERROR collecting tests/t.py", "1 error during collection", "2 errors during collection"):
            for out, err in ((text, ""), ("", text)):
                self.assertEqual(fc_regproof.base_result(proc(1, out, err)), ("error", None), text)

    def test_other_failing_exit_fails_expected(self):
        for code in (1, 2, 5, -9):
            self.assertEqual(fc_regproof.base_result(proc(code, "AssertionError: 1 != 2")), ("fail_expected", True), code)

class Record(unittest.TestCase):
    """A proof's red and final rows go on the task's ledger."""
    META = {"id": "001", "linear": "ENG-3"}
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def setUp(self):
        self.ledger = importlib.import_module("fc_ledger")  # the foundation's module, not on every branch
        self.add = mock.Mock(return_value=True)
        for patch in (mock.patch.object(self.ledger, "add_row", self.add), mock.patch.object(self.ledger, "run_id", return_value="run-1"),
                      mock.patch.object(self.ledger, "criterion_ids", return_value=["001.1:ab", "001.2:cd"])):
            patch.start(); self.addCleanup(patch.stop)

    def proof(self, base=("fail_expected", True), tip_exit=0, **kw):
        return dict({"commit": self.SHA, "base_sha": "b" * 40, "base": base, "tip_sha": "t" * 40, "tip_exit": tip_exit}, **kw)

    def rows(self, red, final):
        e, c = self.ledger.event, ["001.1:ab", "001.2:cd"]
        return [e("run-1", "b" * 40, ["001"], c, "red", "run", red[0], red[1], "regression test 0123456789ab " + red[2] + " on the base"),
                e("run-1", "t" * 40, ["001"], c, "final", "run", final[0], None, "regression test 0123456789ab " + final[1] + " on the fix")]

    def test_rows_by_the_base_and_the_tip(self):
        for base, tip_exit, red, final in ((("fail_expected", True), 0, ("fail_expected", True, "fails"), ("pass", "passes")),
                                           (("pass", False), 0, ("pass", False, "passes"), ("pass", "passes")),
                                           (("error", None), 1, ("error", None, "errors"), ("fail", "fails")),
                                           (("fail_expected", True), 2, ("fail_expected", True, "fails"), ("fail", "fails"))):
            with self.subTest(base=base, tip_exit=tip_exit):
                self.add.reset_mock()
                fc_regproof.record_proof(Path("p"), self.META, self.proof(base, tip_exit))
                self.add.assert_called_once_with("ENG-3", self.rows(red, final))

    def test_a_failed_pick_has_an_empty_tip_sha(self):
        fc_regproof.record_proof(Path("p"), self.META, self.proof(("pass", False), 1, tip_sha=""))
        self.assertEqual(self.add.call_args[0][1][1]["commit_sha"], "")
