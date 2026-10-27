import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_tierspend, fc_cost  # noqa: E402


def row(model, role="planner", input=1e6, output=0): return {"name": "p-" + role, "role": role, "model": model, "turns": 1, "input": input, "cache_created": 0, "cache_read": 0, "output": output, "note": ""}


class TierSpend(unittest.TestCase):
    def config(self, models, **extra):
        d = Path(tempfile.mkdtemp()); self.path = d / "config.json"
        self.path.write_text(json.dumps({"models": models, **extra}))
        for k in ("PRICES", "TIERS"): self.addCleanup(setattr, fc.fc_config, k, getattr(fc.fc_config, k))
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", self.path), mock.patch.object(fc.fc_config, "PROBE_CACHE", d / "probe.json"):
            fc.fc_config.apply_config()
        for k in ("CREW_MODELS", "CREW_EFFORT", "CREW_FALLBACK"): self.addCleanup(setattr, fc.fc_config, k, {})

    def test_ref_dollars_prices_four_numbers_two_numbers_and_unpriced(self):
        self.assertAlmostEqual(fc_tierspend.ref_dollars("claude-opus-5-5", 1e6, 1e6, 1e6, 1e6), 29.2)
        self.assertAlmostEqual(fc_tierspend.ref_dollars("claude:claude-opus-5-5", 1e6, 0, 0, 0), 4)
        self.assertIsNone(fc_tierspend.ref_dollars("pi:x/y", 1e6, 0, 0, 0))
        self.config({}, prices={"pi:a/b": [1, 2], "claude:claude-opus-5-5": [1, 1]})
        self.assertAlmostEqual(fc_tierspend.ref_dollars("pi:a/b", 1e6, 1e6, 1e6, 1e6), 5)
        self.assertAlmostEqual(fc_tierspend.ref_dollars("claude-opus-5-5", 1e6, 0, 0, 1e6), 2)
        self.assertAlmostEqual(fc_tierspend.ref_dollars("claude-haiku-5-5", 1e6, 0, 0, 0), 0.10)

    def test_a_prices_value_that_is_not_an_object_exits_naming_the_file_and_key(self):
        for bad in ([1, 2], "cheap", 3, None):
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as e: self.config({}, prices=bad)
            self.assertEqual(str(e.exception), f"{self.path}: prices must be an object of model id to 2 or 4 numbers, not {bad!r}")

    def test_spend_and_lines_sum_teammates_and_implementers_into_tiers(self):
        self.config({"tiers": {"cheap": ["pi:a/b"]}}, prices={"pi:a/b": [1, 2]})
        proj = Path(tempfile.mkdtemp()) / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        for n, r in {"001": ("claude:claude-sonnet-5-5", 0.0, 0.5), "002": ("claude:claude-opus-5-5", 0.1, None), "003": ("pi:a/b", 0.25, None)}.items():
            (proj / "results" / f"{n}.json").write_text(json.dumps({"id": n, "status": "passed", "model": r[0], "cost": r[1], "reference_cost": r[2]}))
        (proj / "results" / "budget.json").write_text('{"frontier": 9}')
        rows = [row("claude-opus-5-5"), row("pi:a/b", "tests", 1e6, 1e6), row("claude-opus-5-5", "run", 5e6), row("claude-opus-5-5", "x", None)]
        with mock.patch.object(fc_cost, "rows", return_value=rows):
            got = fc_tierspend.spend(proj)
            self.assertEqual(list(got), list(fc.fc_tiers.ORDER))
            for tier, (real, ref, flag) in {"cheap": (3.25, 0, False), "standard": (0, 0.5, False), "frontier": (0.1, 4.0, True)}.items():
                self.assertAlmostEqual(got[tier]["real"], real); self.assertAlmostEqual(got[tier]["ref"], ref); self.assertIs(got[tier]["unpriced"], flag)
            self.assertAlmostEqual(fc_tierspend.frontier(proj), 4.1)
            self.assertEqual(fc_tierspend.lines(proj, 25.0), [f"  {'tier spend':12} {'real $':>9} {'ref $':>9}", f"  {'cheap':12} {3.25:>9.4f} {0.0:>9.4f}",
                f"  {'standard':12} {0.0:>9.4f} {0.5:>9.4f}", f"  {'frontier':12} {0.1:>9.4f} {4.0:>9.4f}+?  limit 25.00"])
            self.assertEqual(fc_tierspend.lines(proj)[-1], f"  {'frontier':12} {0.1:>9.4f} {4.0:>9.4f}+?")
