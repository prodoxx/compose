"""What each route cost: the task's workflow and depth from its Route section, and its attempts, escalations, fix rounds and dollars."""

import json

import fc_tasks
import fc_routerec


def route(proj, meta):
    """`(workflow, depth)` from the Route section of the task's issue in the sync cache, each `-` when missing or unknown."""
    rec = fc_routerec.parse_route((fc_tasks.linear_cache(proj).get("issues", {}).get(meta.get("linear")) or {}).get("description") or "")
    wf = rec.get("workflow") if rec else None
    dp = rec.get("depth") if rec else None
    return (wf if wf in fc_tasks.WORKFLOWS else "-", dp if dp in fc_tasks.DEPTHS else "-")


def rounds(proj):
    """The `rounds` of `results/verify.json`; [] when it is missing or unreadable."""
    try:
        return json.loads((proj / "results" / "verify.json").read_text())["rounds"]
    except (OSError, ValueError, TypeError, KeyError):
        return []


def fix_tasks(tasks, files):
    """Ids of the runnable tasks (never a design or hand task: nothing dispatches those, so no fix round is charged to them) whose `files` or `protected` hold one of `files`, in task order."""
    changed = set(files)
    return [t["id"] for t in fc_tasks.runnable(tasks)
            if changed & (set(t.get("files", [])) | set(t.get("protected", [])))]


def counts(result, rounds, tid):
    """The task's `attempts`, `escalations`, `fix_rounds` and `cost`."""
    return {"attempts": result.get("attempts", 0),
            "escalations": sum(1 for entry in result.get("log", []) if "fallback" in entry),
            "fix_rounds": sum(1 for round_ in rounds if tid in round_.get("fix_tasks", [])),
            "cost": result.get("cost", 0.0)}


def summary(rows):
    """The status block of cost per workflow and per depth; [] for no rows."""
    if not rows:
        return []
    out = [f"  {'route cost':34} {'tasks':>5} {'att':>4} {'esc':>4} {'fix':>4} {'cost':>9}"]
    for kind, values in (("workflow", fc_tasks.WORKFLOWS), ("depth", fc_tasks.DEPTHS)):
        for value in (*values, "-"):
            rs = [r for r in rows if r[kind] == value]
            if rs:
                out.append(f"  {kind + ' ' + value:34} {len(rs):>5} {sum(r['attempts'] for r in rs):>4} {sum(r['escalations'] for r in rs):>4} {sum(r['fix_rounds'] for r in rs):>4} {'$' + format(sum(r['cost'] for r in rs), '.4f'):>9}")
    return out
