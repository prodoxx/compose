"""The evidence ledger's read side: the rows of an issue's evidence comments, and the `## Evidence` section of its
description derived from them. fc_ledger writes the rows."""
import contextlib
import json
import re

import fc_core
import fc_linear

BLOCK = re.compile(r"```evidence\n(.*?)\n```", re.S)
COMMENTS = "query($id: String!, $after: String) { issue(id: $id) { comments(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { body createdAt } } } }"

def parse_comment(body):
    """The rows of every `evidence` block in a comment body."""
    out = []
    for block in BLOCK.findall(body):
        for line in block.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out

_MEMO = {}  # issue -> its comments, while a `remembered()` block is open
_REMEMBERING = [False]

@contextlib.contextmanager
def remembered():
    """Inside the block each issue's comments are read from Linear once, however many callers ask for them: a sweep over a stack's
    tasks asks for the feature issue's comments once per task (the same pages every time), and for a task's own twice (what was
    rerouted, then the evidence rows). Only for a sweep that writes no comment on an issue before it is done reading it: what it
    wrote is not read back from the memory, which ends with the block."""
    if _REMEMBERING[0]:
        yield
        return
    _REMEMBERING[0] = True
    try:
        yield
    finally:
        _REMEMBERING[0] = False
        _MEMO.clear()

def comments(issue):
    """Every comment of the issue (`body`, `createdAt`), all pages, oldest first; a missing issue raises. Remembered inside `remembered()`."""
    if _REMEMBERING[0] and issue in _MEMO:
        return _MEMO[issue]
    nodes, cursor = [], None
    while True:
        data = fc_linear.linear_gql(COMMENTS, {"id": issue, "after": cursor})["issue"]
        if data is None:
            raise RuntimeError(f"no issue {issue}")
        nodes += data["comments"]["nodes"]
        if not data["comments"]["pageInfo"]["hasNextPage"]:
            break
        cursor = data["comments"]["pageInfo"]["endCursor"]
    if _REMEMBERING[0]:
        _MEMO[issue] = nodes
    return nodes

def rows(issue, task_id=None, run_id=None, criterion_id=None):
    """Every evidence row of the issue's comments, once each, oldest first, filtered by task, run, or criterion."""
    if not issue or not fc_linear.linear_ready():
        return []
    out, seen = [], set()
    for c in comments(issue):
        for r in parse_comment(c["body"]):
            if r["event_id"] in seen:
                continue
            seen.add(r["event_id"])
            r["at"] = c["createdAt"]
            out.append(r)
    out = [r for r in out if (not task_id or not r.get("task_ids") or task_id in r["task_ids"])
           and (not run_id or r.get("run_id") == run_id)
           and (not criterion_id or not r.get("criterion_id") or criterion_id in r["criterion_id"])]
    out.sort(key=lambda r: r["at"])
    return out

def task_rows(issue, feature, task_id):
    """The task issue's rows and the feature issue's rows naming the task or no task, once each, oldest first."""
    out = rows(issue, task_id=task_id)
    seen = {r["event_id"] for r in out}
    for r in out:
        r["shared"] = False
    if feature:
        for r in rows(feature, task_id=task_id):
            if r["event_id"] not in seen:
                r["shared"] = True
                out.append(r)
    out.sort(key=lambda r: r["at"])
    return out

def table(rows):
    """The `## Evidence` section text: the verdict line and the 60 newest rows."""
    latest = {}
    for r in rows:
        if r["phase"] == "sufficiency":
            for c in r["criterion_id"]:
                latest[c] = r
    verdict = ", ".join(f'{c.split(":")[0]} {latest[c]["result"]}' for c in latest) or "not judged"
    lines = ["## Evidence", "", "Verdict: " + verdict, "",
             "| id | at (UTC) | source | phase | result | fails on base | criteria | detail |", "|---|---|---|---|---|---|---|---|"]
    for r in rows[::-1][:60]:
        fob = {True: "yes", False: "no", None: "-"}[r["fails_on_base"]]
        crits = ", ".join(c.split(":")[0] for c in r["criterion_id"]) or "all"
        detail = r["detail"].replace("|", "\\|")
        lines.append(f"| {r['event_id']} | {r['at'][:16].replace('T', ' ')} | {r['source']} | {r['phase']} | {r['result']} | {fob} | {crits} | {detail} |")
    return "\n".join(lines) + "\n"

def refresh(issue, select=None):
    """Rewrite the issue description's `## Evidence` section from its rows, or from `select(issue)`, best effort."""
    if not issue or not fc_linear.linear_ready():
        return None
    import fc_layout, fc_plan
    try:
        d = fc_linear.linear_gql("query($id: String!) { issue(id: $id) { id description labels { nodes { name } } } }", {"id": issue})["issue"]
        new = fc_plan.replace_section(d["description"] or "", "## Evidence", table(select(issue) if select else rows(issue)))
        new = fc_layout.laid_out(new, [l["name"] for l in (d.get("labels") or {}).get("nodes") or []])
        if new != (d["description"] or ""):
            fc_linear.linear_gql("mutation($id: String!, $d: String!) { issueUpdate(id: $id, input: {description: $d}) { success } }",
                                 {"id": d["id"], "d": new})
    except Exception as e:
        fc_core.say(f"ledger {issue}: Evidence section not refreshed ({str(e)[:200]})")
