import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, sys
from pathlib import Path
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

class FakeLinear:
    """Linear in memory: issues by identifier, each with a state and a parent. A state's id is its name."""
    TYPES = {"Todo": "unstarted", "In Progress": "started", "In Review": "started", "Done": "completed", "Canceled": "canceled"}

    def __init__(self, **issues):
        self.issues = {k.replace("_", "-"): {"state": s, "parent": p} for k, (s, p) in issues.items()}

    def __call__(self, query, variables):
        if "issueUpdate" in query:
            self.issues[variables["id"]]["state"] = variables["s"]
            return {"issueUpdate": {"success": True}}
        ident, d = variables["id"], self.issues[variables["id"]]
        st = lambda n: {"name": n, "type": self.TYPES[n]}
        return {"issue": {"id": ident, "identifier": ident, "state": st(d["state"]),
                          "team": {"states": {"nodes": [{"id": n, "name": n, "type": t} for n, t in self.TYPES.items()]}},
                          "parent": {"identifier": d["parent"]} if d["parent"] else None,
                          "children": {"nodes": [{"identifier": c, "state": st(cd["state"])} for c, cd in self.issues.items() if cd["parent"] == ident]}}}

    def states(self, *idents):
        return [self.issues[i]["state"] for i in idents]
