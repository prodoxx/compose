import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

C, T = fc.fc_config, fc.fc_tiers
HAIKU, SONNET, OPUS = "claude:claude-haiku-5-5", "claude:claude-sonnet-5-5", "claude:claude-opus-5-5"


class Tiers(unittest.TestCase):
    def config(self, tiers):
        self.addCleanup(setattr, C, "TIERS", C.TIERS)
        d = Path(tempfile.mkdtemp())
        (d / "config.json").write_text(json.dumps({"models": {"tiers": tiers}}))
        with mock.patch.object(C, "CONFIG_PATH", d / "config.json"), mock.patch.object(C, "PROBE_CACHE", d / "probe.json"):
            try: C.apply_config()
            finally: self.path = d / "config.json"

    def test_defaults_and_per_tier_config_with_a_missing_tier_taken_from_the_defaults(self):
        self.assertEqual(C.TIER_NAMES, ("cheap", "standard", "frontier"))
        self.assertEqual(C.TIERS["standard"], [(SONNET, None)])
        self.assertEqual(C.TIERS["frontier"][0], (OPUS, "high"))
        self.config({"cheap": ["pi:a/b"], "standard": [{"model": "pi:c/d", "effort": "low"}]})
        self.assertEqual((C.TIERS["cheap"], C.TIERS["standard"]), ([("pi:a/b", None)], [("pi:c/d", "low")]))
        self.assertEqual(C.TIERS["frontier"][0], (OPUS, "high"))

    def test_an_unknown_tier_name_exits(self):
        with self.assertRaises(SystemExit) as e: self.config({"huge": ["pi:a/b"]})
        self.assertEqual(str(e.exception), f"{self.path}: models.tiers.huge: tiers are cheap, standard, frontier")

    def test_tier_for_and_start_tier_follow_every_pin_rule(self):
        for pin in ("cheap", "standard", "frontier"): self.assertEqual((T.tier_for("cheap", pin), T.tier_for(None, pin), T.start_tier("DEEP", pin), T.start_tier("FAST", pin)), (pin,) * 4)
        self.assertEqual([T.tier_for(c, "no-frontier") for c in (None, "frontier", "standard", "cheap")], ["standard", "standard", "standard", "cheap"])
        self.assertEqual([T.tier_for(c) for c in ("frontier", "cheap", None)], ["frontier", "cheap", None])
        self.assertEqual([T.start_tier(d) for d in ("DEEP", "STANDARD", "FAST")], ["standard", "cheap", "cheap"])
        self.assertEqual(T.start_tier("DEEP", "no-frontier"), "standard")

    def test_chain_is_a_ceiling(self):
        h, s, o, p = (HAIKU, None), (SONNET, None), (OPUS, "high"), ("pi:a/b", None)
        self.assertEqual(T.chain("standard", [h, s, o]), [h, s])
        self.assertEqual(T.chain("cheap", [o]), list(C.TIERS["cheap"]))
        self.assertEqual(T.chain("cheap", [o, p]), [p])
        self.assertEqual(T.chain("frontier", [o, h]), [o, h])

    def test_implementer_model_and_escalate(self):
        self.assertEqual((T.implementer_model("cheap"), T.implementer_model("frontier")), (C.DEFAULT_MODEL, OPUS))
        with mock.patch.object(C, "DEFAULT_MODEL", "pi:a/b"): self.assertEqual(T.implementer_model("frontier"), "pi:a/b")
        self.assertEqual((T.escalate(C.DEFAULT_MODEL), T.escalate(HAIKU)), (SONNET, SONNET))
        self.assertEqual([T.escalate(m) for m in (SONNET, OPUS, "pi:a/b")], [None] * 3)
