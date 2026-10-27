import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, sys, tempfile, unittest
from contextlib import redirect_stdout; from pathlib import Path; from types import SimpleNamespace; from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_tierspend  # noqa: E402


class TestTierPins(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "p"; (self.proj / "results").mkdir(parents=True)
        route = lambda depth, parent: {"description": f"## Route\n\n* workflow: test-first\n* depth: {depth}\n", "parentId": parent}
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [], "issues": {"NEB-1": route("DEEP", "NEB-9"), "NEB-2": route("FAST", "NEB-8")}}))
        self.T, self.file = fc.fc_tierpin, self.proj / "results" / "tier.json"

    def tier(self, value, story=None):
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), redirect_stdout(io.StringIO()) as out:
            self.T.cmd_tier(SimpleNamespace(repo=".", project="p", value=value, story=story))
        return out.getvalue().splitlines()

    def meta(self, linear="NEB-1", **kw): return {"id": "001", "linear": linear, "model": fc.fc_config.DEFAULT_MODEL, "model_auto": True, **kw}

    def test_tier_sets_and_clears_only_its_own_key_and_prints_the_target(self):
        self.assertEqual(self.tier("cheap", "NEB-9"), ["tier cheap for story NEB-9"])
        self.assertEqual(self.tier("no-frontier"), ["tier no-frontier for project p"])
        self.assertEqual(json.loads(self.file.read_text()), {"project": "no-frontier", "stories": {"NEB-9": "cheap"}})
        self.assertEqual(self.tier("clear"), ["tier pin cleared for project p"])
        self.assertEqual(json.loads(self.file.read_text()), {"stories": {"NEB-9": "cheap"}})
        self.assertEqual(self.tier("clear", "NEB-9"), ["tier pin cleared for story NEB-9"])
        self.assertFalse(json.loads(self.file.read_text()).get("stories"))

    def test_pin_prefers_the_task_then_its_story_then_the_project(self):
        self.assertIsNone(self.T.pin(self.proj, self.meta()))
        self.file.write_text(json.dumps({"project": "no-frontier", "stories": {"NEB-8": "standard"}}))
        self.assertEqual(self.T.pin(self.proj, self.meta()), "no-frontier")
        self.file.write_text(json.dumps({"project": "no-frontier", "stories": {"NEB-9": "cheap"}}))
        self.assertEqual(self.T.pin(self.proj, self.meta()), "cheap")
        self.assertEqual(self.T.pin(self.proj, self.meta(tier="frontier")), "frontier")
        self.assertEqual(self.T.pin(self.proj, self.meta("NEB-404")), "no-frontier")

    def test_start_model_follows_the_route_depth_and_the_pin_only_when_auto(self):
        self.assertEqual(self.T.start_model(self.proj, self.meta()), "claude:claude-sonnet-5-5")
        self.assertEqual(self.T.start_model(self.proj, self.meta("NEB-2")), fc.fc_config.DEFAULT_MODEL)
        self.file.write_text(json.dumps({"stories": {"NEB-8": "frontier"}}))
        self.assertEqual(self.T.start_model(self.proj, self.meta("NEB-2")), "claude:claude-opus-5-5")
        self.assertEqual(self.T.start_model(self.proj, self.meta("NEB-2", model_auto=False, model="pi:a/b")), "pi:a/b")

    def test_a_project_pin_changes_the_tier_a_task_starts_on_and_a_task_pin_wins_over_it(self):
        self.assertEqual(self.T.start_model(self.proj, self.meta()), "claude:claude-sonnet-5-5")
        self.tier("cheap")
        self.assertEqual(self.T.start_model(self.proj, self.meta()), fc.fc_config.DEFAULT_MODEL)
        self.assertEqual(self.T.start_model(self.proj, self.meta(tier="frontier")), "claude:claude-opus-5-5")
        self.tier("clear")
        self.assertEqual(self.T.start_model(self.proj, self.meta()), "claude:claude-sonnet-5-5")

class TestBudgetHold(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "p"; (self.proj / "results").mkdir(parents=True)
        issue = {"description": "## Route\n\n* workflow: test-first\n* depth: DEEP\n", "parentId": "NEB-9"}
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [], "issues": {"NEB-1": issue}}))
        self.status = self.proj / "results" / "crew" / "frontier-budget.status"

    def start(self, spend, **kw):
        meta = {"id": "001", "linear": "NEB-1", "model": "pi:a/b", "model_auto": True, **kw}
        with mock.patch.object(fc_tierspend, "frontier", return_value=spend):
            return fc.fc_tierpin.start_model(self.proj, meta)

    def test_a_frontier_task_starts_at_standard_once_the_limit_is_reached_and_at_frontier_under_it(self):
        self.assertEqual(self.start(1e6, tier="frontier"), "claude:claude-sonnet-5-5")
        self.assertEqual(self.start(0.0, tier="frontier"), "claude:claude-opus-5-5")

    def test_a_story_frontier_pin_is_held_too(self):
        (self.proj / "results" / "tier.json").write_text(json.dumps({"stories": {"NEB-9": "frontier"}}))
        self.assertEqual(self.start(1e6), "claude:claude-sonnet-5-5")
        self.assertEqual(self.start(0.0), "claude:claude-opus-5-5")

    def test_a_task_without_model_auto_keeps_its_model_and_is_not_held(self):
        self.assertEqual(self.start(1e6, tier="frontier", model_auto=False), "pi:a/b")
        self.assertFalse(self.status.exists())

    def test_holding_reports_to_the_user_and_staying_under_the_limit_does_not(self):
        self.start(0.0, tier="frontier"); self.assertFalse(self.status.exists())
        self.start(1e6, tier="frontier")
        self.assertEqual(json.loads(self.status.read_text().splitlines()[-1])["state"], "needs-user")
