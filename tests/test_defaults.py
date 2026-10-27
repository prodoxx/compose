import hermetic  # first: no test sees the real home
import copy, json, sys, unittest
from pathlib import Path
from unittest import mock
import os
os.environ["HERDR_ENV"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
import compose as fc, fc_config, fc_defaults as D

OPUS = {"model": "claude:claude-opus-5-5", "effort": "high"}
ASTRA = {"model": "pi:openai/gpt-6-astra", "effort": "high"}
CREW = {"groundwork": "claude:claude-opus-5-5", "default": "claude:claude-sonnet-5-5", "small": "claude:claude-haiku-5-5",
        "planner": {**OPUS, "fallback": ASTRA},
        "architect": {"model": "claude:claude-fable-5-1", "effort": "high", "fallback": [ASTRA, OPUS]},
        "builder-check": OPUS,
        **{r: OPUS for r in ("orchestrator", "designer", "designer-plan", "explore", "research", "perf", "toolsmith")}}
MODELS = {"implementer": "pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast", "fallback": None, "crew": CREW, "tiers": fc_config.DEFAULT_TIERS}


class TestTiersDefault(unittest.TestCase):
    def test_a_missing_tiers_is_added_with_its_line_and_a_second_merge_adds_nothing(self):
        cfg, lines = D.merge_models({})
        self.assertEqual(cfg["models"]["tiers"], fc_config.DEFAULT_TIERS)
        self.assertEqual([l for l in lines if l.startswith("models.tiers = ")], [f"models.tiers = {json.dumps(fc_config.DEFAULT_TIERS)}"])
        self.assertEqual(D.merge_models(cfg)[1], [])

    def test_an_existing_partial_tiers_is_kept_without_a_tiers_line_and_the_rest_is_still_added(self):
        mine = {"cheap": ["claude:claude-haiku-5-5"]}
        cfg, lines = D.merge_models({"models": {"tiers": copy.deepcopy(mine)}})
        self.assertEqual(cfg["models"]["tiers"], mine)
        self.assertFalse([l for l in lines if "models.tiers" in l])
        self.assertEqual(cfg["models"]["crew"], CREW)

class Defaults(unittest.TestCase):
    def test_empty_config_gets_the_whole_block_and_a_second_merge_adds_nothing(self):
        cfg, lines = D.merge_models({"workspaces": {"w": {"target": "x"}}})
        self.assertEqual(cfg, {"workspaces": {"w": {"target": "x"}}, "models": MODELS})
        self.assertTrue(lines)
        cfg["models"]["crew"]["planner"]["effort"] = "low"  # the result is a copy, never DEFAULT_MODELS itself
        self.assertEqual(D.DEFAULT_MODELS, MODELS)
        self.assertEqual(D.merge_models(D.merge_models({})[0]), (D.merge_models({})[0], []))

    def test_partial_config_keeps_its_values_and_nulls_and_adds_one_line_per_missing_role(self):
        mine = {"implementer": "pi:mine", "fallback": "pi:other", "crew": {"planner": None, "small": "claude:x"}}
        cfg, lines = D.merge_models({"models": copy.deepcopy(mine)})
        self.assertEqual(cfg["models"], {**mine, "crew": {**CREW, "planner": None, "small": "claude:x"}, "tiers": fc_config.DEFAULT_TIERS})
        self.assertEqual(len(lines), len(CREW) - 2 + 1)
        for role in set(CREW) - {"planner", "small"}:
            self.assertTrue(any(role in l for l in lines), role)
        cfg, lines = D.merge_models({"models": {"implementer": None, "fallback": None, "crew": dict(CREW, perf=None), "tiers": {}}})
        self.assertEqual((cfg["models"]["implementer"], cfg["models"]["crew"]["perf"], lines), (None, None, []))

    def test_preset_steps_name_the_preset_its_providers_and_where_to_change_the_model(self):
        text = "\n".join(D.PRESET_STEPS)
        for part in ("glm-5-3-flash-fast", "https://openrouter.ai/settings/presets", "z-ai/glm-5.3-flash",
                     "models.implementer", "~/.compose/config.json"):
            self.assertIn(part, text)
        self.assertLess(text.index("Together"), text.index("CoreWeave"))

    def test_probe_passes_with_one_line_on_the_configured_implementer(self):
        with mock.patch.object(fc.fc_models, "model_available", return_value=(True, "answered")) as m:
            ok, lines = D.probe_implementer({"models": {"implementer": "pi:openrouter/some/model"}})
        m.assert_called_once_with("pi:openrouter/some/model")
        self.assertTrue(ok)
        self.assertEqual(len(lines), 1)

    def test_probe_failure_gives_the_providers_message_then_the_preset_steps(self):
        with mock.patch.object(fc.fc_models, "model_available", return_value=(False, "No endpoints found for preset")):
            self.assertEqual(D.probe_implementer({"models": MODELS}), (False, ["No endpoints found for preset", *D.PRESET_STEPS]))
