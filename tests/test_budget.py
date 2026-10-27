import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_tierspend  # noqa: E402


class Budget(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        self.report = self.proj / "results" / "crew" / "frontier-budget.status"

    def hold(self, spend, tier="frontier"):
        with mock.patch.object(fc_tierspend, "frontier", return_value=spend):
            return fc.fc_budget.hold(self.proj, tier)

    def set_limit(self, dollars):
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), redirect_stdout(io.StringIO()) as out:
            fc.fc_budget.cmd_budget(SimpleNamespace(repo=".", project="p", tier="frontier", dollars=dollars))
        return out.getvalue()

    def test_limit_defaults_to_25_and_a_broken_file_does_too(self):
        self.assertEqual(fc.fc_budget.limit(self.proj), 25.0)
        (self.proj / "results" / "budget.json").write_text("{not json")
        self.assertEqual(fc.fc_budget.limit(self.proj), 25.0)

    def test_a_limit_that_is_negative_or_not_a_number_exits_naming_the_file_and_key(self):
        f = self.proj / "results" / "budget.json"
        for bad in ("-1", '"ten"', "null", "true", "[5]", "NaN", "Infinity", "-0.01"):
            with self.subTest(bad=bad):
                f.write_text('{"frontier": %s}' % bad)
                with self.assertRaises(SystemExit) as e: fc.fc_budget.limit(self.proj)
                self.assertRegex(str(e.exception), rf"^{f}: frontier must be a number of dollars, 0 or more, not ")
        f.write_text("[25]")
        with self.assertRaises(SystemExit) as e: fc.fc_budget.limit(self.proj)
        self.assertEqual(str(e.exception), f'{f}: must be an object such as {{"frontier": 25}}, not [25]')

    def test_the_budget_command_refuses_a_negative_or_non_finite_limit_and_writes_nothing(self):
        for bad in (-5.0, float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as e: self.set_limit(bad)
            self.assertRegex(str(e.exception), r"^compose budget: frontier must be a number of dollars, 0 or more")
            self.assertFalse((self.proj / "results" / "budget.json").exists())

    def test_budget_command_sets_the_limit_and_prints_it(self):
        self.assertEqual(self.set_limit(40.0), "frontier limit $40.00 for p\n")
        self.assertEqual(fc.fc_budget.limit(self.proj), 40.0)
        self.assertEqual(json.loads((self.proj / "results" / "budget.json").read_text()), {"frontier": 40.0})

    def test_frontier_at_the_limit_runs_at_standard_and_reports_needs_user(self):
        self.assertEqual(self.hold(25.0), "standard")
        self.assertEqual(self.hold(30.5), "standard")
        first, second = [json.loads(l) for l in self.report.read_text().splitlines()]
        self.assertEqual(first["state"], "needs-user")
        self.assertEqual(first["message"], "frontier spend $25.00 reached the $25.00 limit of p: new frontier work runs at standard; raise it with compose budget --project p frontier <dollars>")
        self.assertIn("$30.50", second["message"])
        self.assertRegex(first["at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_frontier_under_the_limit_and_other_tiers_return_unchanged_with_no_report(self):
        self.assertEqual(self.hold(24.99), "frontier")
        for tier in ("standard", "cheap", None): self.assertEqual(self.hold(99.0, tier), tier)
        self.assertFalse(self.report.exists())
