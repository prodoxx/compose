import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, sys, unittest
from pathlib import Path
from unittest import mock
# tests/test_modules.py fails a test file without the next line
os.environ["HERDR_ENV"] = ""
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_config


class CompactConfig(unittest.TestCase):
    def setUp(self):
        saved = dict(vars(fc_config)); self.addCleanup(lambda: vars(fc_config).update(saved))

    def apply(self, compact):
        with mock.patch.object(fc_config, "read_config", return_value={"compact": compact}): fc_config.apply_config(move=False)

    def test_compact_keys_are_checked(self):
        for compact, msg in (({"minimum": 0}, "compact.minimum must be a whole number of 1 or more, not 0"),
                             ({"idle_seconds": -1}, "compact.idle_seconds must be a whole number of 0 or more, not -1"),
                             ({"judged": "no"}, "compact.judged must be true or false, not 'no'")):
            with self.assertRaises(SystemExit) as c: self.apply(compact)
            self.assertEqual(c.exception.code, f"{fc_config.CONFIG_PATH}: {msg}")
        self.apply({"ceiling": 180000})
        self.assertEqual(fc_config.COMPACT, {**fc_config.COMPACT_DEFAULTS, "ceiling": 180000})

    def test_the_env_var_turns_everything_off(self):
        with mock.patch.dict(os.environ, {"COMPOSE_NO_COMPACT": "1"}): self.assertEqual(fc_config.compact_off(), "COMPOSE_NO_COMPACT is set")
        with mock.patch.dict(os.environ, {"COMPOSE_NO_COMPACT": ""}): self.assertIsNone(fc_config.compact_off())


class FactoryConfig(unittest.TestCase):
    def setUp(self):
        saved = dict(vars(fc_config)); self.addCleanup(lambda: vars(fc_config).update(saved))  # apply_config sets module globals

    def apply(self, factory):
        with mock.patch.object(fc_config, "read_config", return_value={"factory": factory}): fc_config.apply_config(move=False)

    def test_factory_keys_must_be_whole_numbers(self):
        for factory, msg in (({"day_tickets": 0}, "factory.day_tickets must be a whole number of 1 or more, not 0"),
                             ({"turn_seconds": 5}, "factory.turn_seconds must be a whole number of 10 or more, not 5"),
                             ({"offer_snooze_minutes": 0}, "factory.offer_snooze_minutes must be a whole number of 1 or more, not 0")):
            with self.assertRaises(SystemExit) as c: self.apply(factory)
            self.assertEqual(c.exception.code, f"{fc_config.CONFIG_PATH}: {msg}")
        self.apply({"day_tickets": 3})
        self.assertEqual(fc_config.FACTORY, {**fc_config.FACTORY_DEFAULTS, "day_tickets": 3})

    def test_night_keys_are_checked(self):
        for factory, msg in (({"night_until": "7am"}, "factory.night_until must be HH:MM, not '7am'"),
                             ({"night_budget": -1}, "factory.night_budget must be a number of dollars, 0 or more, not -1"),
                             ({"night_tickets": 0}, "factory.night_tickets must be a whole number of 1 or more, not 0"),
                             ({"prep_ahead": -1}, "factory.prep_ahead must be a whole number of 0 or more, not -1")):
            with self.assertRaises(SystemExit) as c: self.apply(factory)
            self.assertEqual(c.exception.code, f"{fc_config.CONFIG_PATH}: {msg}")


if __name__ == "__main__":
    unittest.main()
