import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, sys, unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


def task(**kv):
    kv = {"id": "001", "test": "t", **kv}
    desc = "## Goal\nDo it.\n\n```compose\n" + "\n".join(f"{k}: {v}" for k, v in kv.items()) + "\n```\n"
    return fc.fc_tasks.task_from_issue({"id": "NEB-1", "title": "T", "description": desc})


class TaskTier(unittest.TestCase):
    def test_each_of_the_four_tiers_is_kept(self):
        for tier in ("cheap", "standard", "frontier", "no-frontier"):
            with self.subTest(tier=tier):
                self.assertEqual(task(tier=tier)["tier"], tier)

    def test_a_block_without_a_tier_has_no_tier_key(self):
        self.assertNotIn("tier", task())

    def test_an_unknown_tier_exits_naming_the_issue_and_the_value(self):
        for bad in ("huge", "Frontier", "cheap,standard"):
            with self.subTest(tier=bad), self.assertRaises(SystemExit) as e:
                task(tier=bad)
            self.assertEqual(str(e.exception), f"NEB-1: tier must be cheap, standard, frontier or no-frontier, not {bad}")

class ModelAuto(unittest.TestCase):
    def test_a_block_without_model_gets_the_default_and_model_auto(self):
        meta = task()
        self.assertEqual(meta["model"], fc.fc_config.DEFAULT_MODEL)
        self.assertIs(meta["model_auto"], True)

    def test_a_block_with_model_keeps_it_and_has_no_model_auto(self):
        meta = task(model="pi:a/b")
        self.assertEqual(meta["model"], "pi:a/b")
        self.assertNotIn("model_auto", meta)

class Scenarios(unittest.TestCase):
    F = "`" * 3
    CRIT = "## Acceptance criteria\n\n* a\n* b\n\n"

    def body(self, block, pre="", post=""):
        return f"{self.CRIT}## Test plan\n\n{pre}{self.F}scenarios{post}\n{block}\n{self.F}\n\n{self.F}compose\nid: 001\n{self.F}\n"

    def test_the_block_is_the_stripped_text_of_the_scenarios_fence(self):
        self.assertEqual(fc.fc_tasks.scenarios_block(self.body("\n  id: 1\ngiven: a\n\n")), "id: 1\ngiven: a")
        self.assertEqual(fc.fc_tasks.scenarios_block(self.body("id: 1", post="  ")), "id: 1")

    def test_a_missing_fence_gives_none(self):
        only_compose = f"{self.CRIT}{self.F}compose\nid: 001\n{self.F}\n"
        for text in (None, "", self.CRIT, only_compose):
            with self.subTest(text=text):
                self.assertIsNone(fc.fc_tasks.scenarios_block(text))
                self.assertIsNone(fc.fc_tasks.parse_scenarios(text))

    def test_the_fence_stops_at_the_first_closing_line(self):
        text = f"{self.F}scenarios\nid: 1\n{self.F}\nid: 2\n{self.F}\n"
        self.assertEqual(fc.fc_tasks.parse_scenarios(text), [{"id": "1"}])

    def test_each_id_line_starts_a_scenario_in_order_with_string_values(self):
        block = "id: 1\nscenario: first\ngiven: g\nid: 2\nscenario:   second  \nid: 3"
        self.assertEqual(fc.fc_tasks.parse_scenarios(self.body(block)), [
            {"id": "1", "scenario": "first", "given": "g"}, {"id": "2", "scenario": "second"}, {"id": "3"}])

    def test_a_key_line_before_any_id_starts_the_first_scenario(self):
        self.assertEqual(fc.fc_tasks.parse_scenarios(self.body("given: g\nthen: t")), [{"given": "g", "then": "t"}])

if __name__ == "__main__":
    unittest.main()
