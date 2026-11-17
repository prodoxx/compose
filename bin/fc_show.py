"""`compose show run|verify`: the saved output of the last run or verify, or one summary line of it. Imports only lower modules."""

import json, re

import fc_core, fc_tasks, fc_history

RUN_DONE = re.compile(r"run done\s+(\d+/\d+ passed)\s+wall (\S+)")
RUN_START = "] run start project="  # compose's own line, as fc_core.say stamps it: a teammate's line that quotes the phrase is no start


def _read_json(path):
    """The JSON object in `path`, or {} when it is missing, unreadable or not an object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _number(x):
    """True for an int or a float that is not a bool."""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _cost(total):
    """`$<total to two places>`, or `cost not recorded` when `total` is not a number: a cost is never invented."""
    return f"${total:.2f}" if _number(total) else "cost not recorded"


def _took(seconds):
    """The time a run or verify took, or `time not recorded`."""
    return fc_core.fmt(seconds) if _number(seconds) else "time not recorded"


def _run_log(proj):
    """The lines of the newer of results/run.log and results/resume.log (a resume writes its own log); empty when there is neither."""
    logs = []
    for name in ("run.log", "resume.log"):
        try:
            logs.append(((proj / "results" / name).stat().st_mtime, proj / "results" / name))
        except OSError:
            pass
    try:
        return max(logs)[1].read_text(errors="replace").splitlines() if logs else []
    except OSError:
        return []


def _last_run(proj, repo):
    """The project's newest run-start or run-end line in the run history (a run and a resume each write a start, and an end unless they
    died); the history is appended in order, so a start after the last end is a run that has not finished. None when there is none."""
    runs = [e for e in fc_history.read(repo, proj.name, 0) if e.get("kind") in ("run-start", "run-end")] if repo is not None else []
    return runs[-1] if runs else None


def summary(proj, what, repo=None):
    """One line for the last `run` or `verify` on record: counts, time and cost, or that none is on record. A run is read from the newest
    run-end of the run history of `repo`, so the count, the time and the cost are of one run; the log is read only when the history has none."""
    results = proj / "results"
    if what == "run":
        if fc_tasks.run_alive(proj):
            return "run: still running"
        if end := _last_run(proj, repo):
            if end["kind"] == "run-start":
                return "run: did not finish"
            count = f"{end['passed']}/{end['tasks']} passed" if _number(end.get("passed")) and _number(end.get("tasks")) else "count not recorded"
            return f"run {count}, {_took(end.get('seconds'))}, {_cost(end.get('cost'))}"
        lines = _run_log(proj)
        done = [m for m in map(RUN_DONE.search, lines) if m]
        starts = [i for i, l in enumerate(lines) if RUN_START in l]
        if starts and not any(RUN_DONE.search(l) for l in lines[starts[-1]:]):
            return "run: did not finish"
        if not done:
            return "run: none finished on record"
        return f"run {done[-1][1]}, {done[-1][2]}, {_cost(_read_json(results / 'run-cost.json').get('total'))}"
    data = _read_json(results / "verify.json")
    if not data:
        return "verify: none on record"
    cost, passed = data.get("cost"), data.get("passed")
    outcome = ("verify passed" if passed else "verify failed") if isinstance(passed, bool) else "verify: outcome not recorded"
    return f"{outcome}, {_took(data.get('seconds'))}, {_cost(cost.get('total') if isinstance(cost, dict) else None)}"


def cmd_show(a):
    """Print the last run (from compose's last `run start` line, of the newer of the run and the resume log) or the whole last verify
    output, then the summary line; with --line only the summary."""
    proj = fc_core.project_dir(a)
    if not a.line:
        if a.what == "run":
            lines = _run_log(proj)
            starts = [i for i, l in enumerate(lines) if RUN_START in l]
            text = "\n".join(lines[starts[-1]:] if starts else lines)
        else:
            try:
                text = (proj / "results" / "verify.log").read_text(errors="replace").rstrip("\n")
            except OSError:
                text = ""
        if text:
            print(text)
    print(summary(proj, a.what, getattr(a, "repo", None)))
