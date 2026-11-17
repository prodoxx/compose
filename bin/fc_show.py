"""`compose show run|verify`: the saved output of the last run or verify, or one summary line of it. Imports only lower modules."""

import json, re

import fc_core

RUN_DONE = re.compile(r"run done\s+(\d+/\d+ passed)\s+wall (\S+)")


def _read_json(path):
    """The JSON object in `path`, or {} when it is missing, unreadable or not an object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cost(total):
    """`$<total to two places>`, or `cost not recorded` when `total` is not a number: a cost is never invented."""
    return f"${total:.2f}" if isinstance(total, (int, float)) and not isinstance(total, bool) else "cost not recorded"


def _run_log(proj):
    """The lines of results/run.log (empty when there is none)."""
    try:
        return (proj / "results" / "run.log").read_text().splitlines()
    except OSError:
        return []


def summary(proj, what):
    """One line for the last `run` or `verify` on record: counts, time and cost, or that none is on record."""
    results = proj / "results"
    if what == "run":
        done = [m for m in map(RUN_DONE.search, _run_log(proj)) if m]
        if not done:
            return "run: none finished on record"
        return f"run {done[-1][1]}, {done[-1][2]}, {_cost(_read_json(results / 'run-cost.json').get('total'))}"
    data = _read_json(results / "verify.json")
    if not data:
        return "verify: none on record"
    seconds = data.get("seconds")
    took = fc_core.fmt(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else "time not recorded"
    cost = data.get("cost")
    return f"verify {'passed' if data.get('passed') else 'failed'}, {took}, {_cost(cost.get('total') if isinstance(cost, dict) else None)}"


def cmd_show(a):
    """Print the last run (from its `run start` line) or the whole last verify output, then the summary line; with --line only the summary."""
    proj = fc_core.project_dir(a)
    if not a.line:
        if a.what == "run":
            lines = _run_log(proj)
            starts = [i for i, l in enumerate(lines) if "run start" in l]
            text = "\n".join(lines[starts[-1]:] if starts else lines)
        else:
            try:
                text = (proj / "results" / "verify.log").read_text().rstrip("\n")
            except OSError:
                text = ""
        if text:
            print(text)
    print(summary(proj, a.what))
