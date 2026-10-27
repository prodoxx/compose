"""The run history: one JSON line per run start, run end, task result and verify round, appended to history.jsonl beside the
user's config. It lives outside every worktree, so it outlives `crew stop`, a returned worktree and a rerun that overwrites
results/ and run.log. A line carries numbers and an outcome word, never a prompt, a log or a secret."""

import json
import time
from pathlib import Path

import fc_config
import fc_core

FILE = "history.jsonl"
FIELDS = ("task", "model", "attempts", "seconds", "cost", "outcome", "tasks", "passed", "round", "rounds", "parallel", "providers")  # a line keeps nothing else
SHOWN = 40  # lines `status --history` prints when it is not told how many


def path():
    return fc_config.CONFIG_PATH.parent / FILE


def main_repo(repo):
    """The repository's main checkout: the same whichever worktree a run started in."""
    try:
        return str(fc_core.main_checkout(repo))
    except (OSError, IndexError, RuntimeError):
        return str(repo)


def providers(snapshot):
    """The calls and tokens per provider of a ledger snapshot, which `cost` totals across runs."""
    return {p: {k: r.get(k, 0) for k in ("calls", "tokens_in", "tokens_out")} for p, r in snapshot.items() if p != "total" and isinstance(r, dict)}


def record(proj, repo, kind, **fields):
    """Append one line: when, project, repo, kind (run-start, run-end, task, verify-round, verify-end), then the known fields that are set.
    A history that cannot be written never fails the run it describes."""
    line = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "project": Path(proj).name, "repo": main_repo(repo), "kind": kind,
            **{k: fields[k] for k in FIELDS if fields.get(k) is not None}}
    try:
        f = path()
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a") as out:
            out.write(fc_core.scrub(json.dumps(line)) + "\n")
    except OSError:
        pass


def read(repo, project=None, last=SHOWN):
    """The newest `last` lines of this repository's history (one project's with `project`), oldest first; a line that does not parse is skipped."""
    try:
        text = path().read_text()
    except OSError:
        return []
    here, out = main_repo(repo), []
    for raw in text.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("repo") == here and project in (None, e.get("project")):
            out.append(e)
    return out[-last:] if last else out


def render(entries):
    """One line per entry, then the cost of the lines shown."""
    if not entries:
        return "history  none"
    rows = [f"{e['at']}  {e['project']}  {e['kind']:12} {e.get('task', ''):4} {e.get('model', ''):28} "
            f"{'x' + str(e['attempts']) if 'attempts' in e else '':3} {fc_core.fmt(e['seconds']) if 'seconds' in e else '':>9} "
            f"{'$' + format(e['cost'], '.4f') if 'cost' in e else '':>9}  {e.get('outcome', '')}".rstrip() for e in entries]
    return "\n".join(rows + [f"total cost ${sum(e.get('cost') or 0 for e in entries if e['kind'] in ('task', 'verify-end')):.4f} (task and verify-end lines)"])
