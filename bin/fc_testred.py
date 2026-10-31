"""The check before a test writer's test is collected: it must fail on the stubs for the reason its task fixes, not because it cannot load."""
import json, re, sys
from pathlib import Path
import fc_core, fc_crew, fc_herdr, fc_models, fc_worktree


# A line that shows the test itself cannot run: a missing module or name, a syntax or compile error. An assertion or a stub's
# "not implemented" is the reason a test is meant to fail for, so none of them is here.
BROKEN_RE = re.compile(
    r"^\s*(?:E\s+)?(?:[\w.]+\.)?(?:ModuleNotFoundError|ImportError|SyntaxError|IndentationError|TabError|NameError|ReferenceError|LoadError)\b[:\s].*$"
    r"|^.*(?:\bCannot find module|\bFailed to resolve import|\berror TS\d+|\berror\[E\d+\]|\bcannot find symbol|\[(?:build|setup) failed\]).*$", re.M)
# A line that shows the run ran no test at all (the wrong path, a name filter no case matches, a file that failed to resolve):
# vitest, jest, playwright, pytest, go, and node's runner each say so in their own words. Such a run proves nothing about the
# behavior, whether it exits red or, as go does, green.
NO_TESTS_RE = re.compile(
    r"^.*(?:\(0 tests?\)|\bTests\s+no tests\b|\bTests:\s+0 total\b|\bNo tests? (?:files )?found\b|\bno tests ran\b|\bcollected 0 items\b|\bno tests to run\b).*$"
    r"|^\s*#\s*tests\s+0\s*$", re.M)

def no_tests(output):
    """The first line of a test run's `output` that shows it ran no test, or ""."""
    hit = NO_TESTS_RE.search(output or "")
    return hit.group(0).strip() if hit else ""

def broken(output):
    """The first line of a test run's `output` that shows the test cannot run, or ran no test, or ""."""
    hit = BROKEN_RE.search(output or "")
    return hit.group(0).strip() if hit else no_tests(output)

def run(meta, wt):
    """(exit code, output) of the task's test command in `wt`, through `compose timed`: the same slot, time limit, and scrubbing a teammate's run gets."""
    r = fc_core.sh([sys.executable, str(Path(__file__).with_name("compose.py")), "timed", str(int(meta.get("test_timeout") or fc_models.TEST_TIMEOUT)), "--", meta["test"]],
                   wt, check=False)
    return r.returncode, r.stdout + r.stderr

def sent_back(proj, update=None):
    """The test writers sent back once, by task id, with how many reports they had then (results/tests-sentback.json)."""
    f = Path(proj) / "results" / "tests-sentback.json"
    have = json.loads(f.read_text()) if f.exists() else {}
    if update:
        have.update(update)
        fc_core.atomic_write(f, json.dumps(have, indent=2))
    return have

def gate(proj, meta, writer):
    """Whether the collect of a writer that reported its test ready waits. It waits while the writer is on a turn again, since a
    correction may be pending. The test runs in the writer's checkout; one that fails
    because it cannot load (see BROKEN_RE), or that runs no test at all (NO_TESTS_RE), goes back to its writer once with the
    line that shows it, and the collect leaves it until the writer reports ready again. A test that passes, or fails any other
    way, is collected as before, and so is one that is still broken after its one send back, or whose writer cannot be
    reached: each says so."""
    tid = meta["id"]
    held = fc_worktree.slots(proj).get(f"tests-{tid}")
    wt = Path(held) if held else Path(proj) / "worktrees" / f"tests-{tid}"
    if not wt.exists() or not meta.get("test"):
        return False
    sent, before = fc_crew.crew_reports(proj, writer), sent_back(proj).get(tid)
    reports = len(sent)
    agents = {a.get("name"): a for a in fc_herdr.live_agents()}
    live = writer in agents
    last = (sent or [{}])[-1]
    if last.get("state") != "done" and (agents.get(writer) or {}).get("agent_status") == "working":
        # a correction sent with `herdr agent prompt` leaves no report, so the writer's turn is the only trace of it
        fc_core.say(f"tests-{tid} waits: its writer is on a turn since its test ready report (a correction may be pending); its branch and worktree are kept")
        return True
    if before is not None and reports <= before and live:
        fc_core.say(f"tests-{tid} waits: sent back once, it has not reported test ready again")
        return True
    code, out = run(meta, wt)
    line = broken(out) if code else no_tests(out)
    if not line:
        return False
    if before is not None or not live:
        fc_core.say(f"tests-{tid} collected although its test proves nothing ({line}): " + ("it was sent back once already" if before is not None else "its writer is not running"))
        return False
    text = "\n".join([f"Your test for task {tid} does not test the behavior the task adds: it cannot load, or it runs no test: {line}",
                      "Output of `" + meta["test"] + "`:", out[-1500:],
                      "Fix the test (a plain new commit), check it fails on the stubs for the missing behavior, and report test ready again."])
    if fc_herdr.herdr("agent", "prompt", writer, text) is None:
        fc_core.say(f"tests-{tid} collected although its test proves nothing ({line}): herdr could not reach its writer")
        return False
    sent_back(proj, {tid: reports})
    fc_core.say(f"tests-{tid} sent back to its writer: {line}")
    return True
