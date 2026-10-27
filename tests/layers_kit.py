import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os
from pathlib import Path
import sys
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

def unit(name, lines):
    return {"name": name, "commits": [], "lines": lines, "generated": {}}

WRITTEN = ("# Keep the toggle's state across reloads\n\n## Why\nThe toggle forgot its state on every reload.\nThis stores it.\n\n"
           "## What changed\n- Store the toggle's state\n- Read it back on load\n")
