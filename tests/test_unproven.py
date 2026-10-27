import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, unittest
from pathlib import Path
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_unproven as up  # noqa: E402
import json, tempfile  # noqa: E402
PROJ = Path("/x/.compose/p")
def proof(base="fail_expected", tip=0, base_out="base out\n", tip_out="tip out\n"):
    return {"base": (base, base == "fail_expected"), "tip_exit": tip, "proven": False, "base_output": base_out, "tip_output": tip_out}

class Reprompt(unittest.TestCase):
    """Why a regression test is not proof, the re-prompt that says so, and the writer it goes to."""
    def test_reason_first_rule_that_holds(self):
        self.assertEqual(up.reason(None), "the writer stopped without reporting a test ready")
        self.assertEqual(up.reason(None, saved=False), "the writer stopped without reporting a test ready")
        self.assertEqual(up.reason(proof("pass", 1), saved=False), "the proof's evidence was not saved to Linear")
        self.assertEqual(up.reason(proof("pass", 1)), "the regression test passes on the base, so it proves nothing")
        self.assertEqual(up.reason(proof("error", 1, base_out="x\nImportError: no mod\n\n  \n")), "the base run was a harness error, not proof: ImportError: no mod")
        self.assertEqual(up.reason(proof(tip=2)), "the regression test fails on the fix (exit 2)")
        self.assertEqual(up.reason(proof()), "")
        self.assertEqual(up.reason(proof(), saved=False), "the proof's evidence was not saved to Linear")
    def test_prompt_text_quotes_the_failing_run(self):
        last = "Make it fail on the base and pass on the fix, commit it, and report test ready: <commit> <command> again."
        for p, quoted, other in ((proof("pass"), "base out", "tip out"), (proof("error", base_out="boom\n"), "boom", "tip out"), (proof(tip=3), "tip out", "base out")):
            lines = up.prompt_text(p).splitlines()
            self.assertEqual(lines[0], "Your regression test is not proof yet: %s." % up.reason(p))
            self.assertEqual(lines[1], "Output of that run:")
            self.assertEqual(lines[-1], last)
            self.assertIn(quoted, "\n".join(lines[2:-1]))
            self.assertNotIn(other, "\n".join(lines[2:-1]))
    def test_restartable_needs_unproven_and_kept_clone(self):
        root = Path(tempfile.mkdtemp()); proj = root / "p"; (proj / "results").mkdir(parents=True); (root / "wt").mkdir()
        self.assertIsNone(up.restartable(proj, "040"))
        for status, wt, want in (("unproven", root / "wt", True), ("unproven", root / "gone", False), ("passed", root / "wt", False)):
            res = {"id": "040", "status": status, "worktree": str(wt)}
            (proj / "results" / "040.json").write_text(json.dumps(res))
            self.assertEqual(up.restartable(proj, "040"), res if want else None)
