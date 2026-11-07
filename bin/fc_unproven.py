"""Why a regression test is not proof yet, the re-prompt that says so, and the writer it goes to."""
import json
from pathlib import Path

import fc_herdr, fc_fleet


def reason(proof, saved=True):
    """One line saying why `proof` is not proof; "" when it is."""
    if proof is None:
        return "the writer stopped without reporting a test ready"
    if not saved:
        return "the proof's evidence was not saved to Linear"
    result = proof["base"][0]
    if result == "pass":
        return "the regression test passes on the base, so it proves nothing"
    if result == "error":
        return "the base run was a harness error, not proof: %s" % [line for line in proof["base_output"].splitlines() if line.strip()][-1]
    if proof["tip_exit"] != 0:
        return "the regression test fails on the fix (exit %s)" % proof["tip_exit"]
    return ""


def prompt_text(proof):
    """The re-prompt for the writer, quoting the failing run's output."""
    output = ""
    if proof is not None:
        output = proof["base_output"] if proof["base"][0] in ("pass", "error") else proof["tip_output"]
    return "\n".join([
        "Your regression test is not proof yet: %s." % reason(proof),
        "Output of that run:",
        output,
        "Make it fail on the base and pass on the fix, commit it, and report test ready: <commit> <command> again.",
    ])


def writer_live(proj, tid):
    """True when the task's `tests-<tid>` writer is live in the project's herdr space."""
    name = fc_herdr.agent_name(proj.name, f"tests-{tid}")
    return any(a["name"] == name and a["workspace_id"] == fc_fleet.project_space(proj) for a in fc_fleet.live_agents())


def reprompt(proj, tid, text):
    """Send `text` to the task's writer; whether herdr answered."""
    return fc_fleet.prompt(fc_herdr.agent_name(proj.name, f"tests-{tid}"), text)[1] is None


def restartable(proj, tid):
    """The saved unproven result whose fix clone is kept, else None."""
    path = proj / "results" / f"{tid}.json"
    if not path.exists():
        return None
    result = json.loads(path.read_text())
    if result["status"] == "unproven" and Path(result["worktree"]).exists():
        return result
    return None
