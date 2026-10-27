import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, unittest
from pathlib import Path
import sys
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""

CLI = str(Path(__file__).resolve().parents[1] / "bin" / "compose")
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(CLI).with_name("compose.py"))))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


class NoTrace(unittest.TestCase):
    """The delivered branch carries no trace of the tool: no commit message names it."""
    def test_task_commit_message_is_the_change(self):
        self.assertEqual(fc.fc_layers.commit_message({"title": "001 SQLite store: open, schema"}), "SQLite store: open, schema")
        self.assertEqual(fc.fc_layers.commit_message({"title": "Add a favicon"}), "Add a favicon")
        self.assertEqual(fc.fc_layers.commit_message({"title": "007", "files": ["a.go"]}), "Update a.go")


if __name__ == "__main__":
    unittest.main()
