"""The regression test writer of a FAST task whose fix passed: its branch, brief, start, and ready line."""
import argparse
import json
import re
import time
from pathlib import Path

import fc_config, fc_core, fc_crew, fc_herdr, fc_tasks, fc_worktree


def writer_branch(proj, tid):
    """The writer's branch: the project's task prefix plus `regress-<tid>`."""
    return fc_worktree.task_prefix(fc_herdr.project_worktree(proj), proj) + f"regress-{tid}"


def writer_brief(meta, diff):
    """The writer's brief: the four instruction lines, the expected behavior, and the fix's diff."""
    lines = meta["body"].splitlines()
    head = next((n for n, l in enumerate(lines) if l == "## Reproduction"), None)
    if head is None:
        head = next(n for n, l in enumerate(lines) if l == "## Acceptance criteria")
    end = next((n for n in range(head + 1, len(lines)) if lines[n].startswith("## ")), len(lines))
    text = "\n".join(lines[head + 1:end]).strip()
    block = fc_tasks.scenarios_block(meta.get("body"))
    scenarios = f"\n\n## Scenarios\n```scenarios\n{block}\n```" if block else ""
    return (f"Write one regression test for task {meta['id']}: {meta['title']}.\n"
            "The fix below passed. Your test must fail on the base and pass on the fix.\n"
            "Change only test files, in one commit; never edit the fix.\n"
            'Then report working "test ready: <commit> <command>", where <command> runs only your test.\n'
            f"\n## Expected behavior\n{text}{scenarios}\n\n## The fix\n```diff\n{diff}\n```")


def start_writer(repo, proj, meta, result):
    """Start the `tests-<tid>` writer on its own branch and checkout off the task's tip; the checkout's path."""
    tid, branch = meta["id"], writer_branch(proj, meta["id"])
    diff = fc_core.sh(["git", "diff", "HEAD~1", "HEAD"], result["worktree"]).stdout
    fc_core.sh(["git", "branch", "-f", branch, result["branch"]], repo)
    if fc_worktree.use_treehouse():
        path = Path(fc_worktree.slot_on(repo, branch, f"compose:{proj.name}:regress-{tid}"))
        fc_worktree.slots(proj, {f"regress-{tid}": str(path)})
    else:
        path = proj / "worktrees" / f"regress-{tid}"
        fc_core.sh(["git", "worktree", "add", "-q", str(path), branch], repo)
    fc_worktree.ready_checkout(repo, path, install=fc_core.project_config(proj).get("install"))
    model = "tests" if "tests" in fc_config.CREW_MODELS else "groundwork" if "groundwork" in fc_config.CREW_MODELS else None
    fc_crew.cmd_crew(argparse.Namespace(repo=str(repo), project=proj.name, action="start", role=f"tests-{tid}", title=f"tests {tid}",
                                        brief=writer_brief(meta, diff), force=False, cwd=str(path), leased=True, model=model))
    return path


READY_RE = re.compile(r"test ready: ([0-9a-f]{7,40}) (.+)")


def parse_ready(message):
    """(commit, command) from the first ready line in `message`; None without one."""
    found = READY_RE.search(message or "")
    return (found.group(1), found.group(2).strip()) if found else None


def wait_ready(proj, tid, since=0, poll=5):
    """(commit, command) once the writer reports its test ready; None when it ends, blocks, or is gone without one."""
    name = fc_herdr.agent_name(proj.name, f"tests-{tid}")
    while True:
        reports = fc_crew.crew_reports(proj, name)[since:]
        for r in reports:
            if r["state"] in ("working", "done") and parse_ready(r["message"]):
                return parse_ready(r["message"])
        if any(r["state"] in ("blocked", "tool-bug", "contradiction", "done") for r in reports):
            return None
        path = fc_crew.crew_dir(proj) / f"{name}.json"
        if not path.exists():
            return None
        record = json.loads(path.read_text())
        if "closed" in record:
            return None
        listed = fc_herdr.herdr("agent", "list")
        if fc_crew.tab_closed(record, listed["agents"] if listed is not None else None):
            return None
        time.sleep(poll)
