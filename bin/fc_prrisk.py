"""A layer's `## Risk` (sections/4.md): how much its change could break, read from the route compose chose for each of its tasks
(the task issue's `## Route`, cached in results/tasks.json), so a reviewer sees the call that set how deep the task was built and checked."""
import re

import fc_routerec

LEVEL = {"FAST": "Low", "STANDARD": "Medium", "DEEP": "High"}
MEANS = {"FAST": "little relies on what it changes and how to build it was clear",  # fc_routejev's depth criteria, in a reviewer's words
         "STANDARD": "it changes existing behavior its own tests cover",
         "DEEP": "much relies on what it changes, or how to build it was unclear"}
FLOOR = re.compile(r"\bfloor DEEP from ([^;]+)")
LEVEL_LINE = re.compile(r"^-\s+\**(?:low|medium|high)\b", re.I)  # a written bullet that states the level: compose's to state

def task_line(meta, route):
    """One task's bullet: its title, its level, and why: the policies that set its floor, else what its depth means."""
    depth = route["depth"]
    floor = FLOOR.search(route.get("reason") or "")
    if floor:
        names = [n.strip() for n in floor.group(1).split(",") if n.strip()]
        checks = (route.get("verification requirements") or "none").strip()
        why = (f"the {' and '.join(names)} {'policy covers' if len(names) == 1 else 'policies cover'} its files"
               + (f" and {'requires' if len(names) == 1 else 'require'} {checks}" if checks != "none" else ""))
    else:
        why = MEANS[depth]
    return f"- {meta['title']}: {LEVEL[depth]}, {why}"

def risk_lines(tasks, issues, written=()):
    """The `## Risk` lines of a layer whose task metas are `tasks`: `**<level>**` of its riskiest routed task, one bullet per routed
    task, then the team lead's bullets (`written`); [] when no task carries a route and nothing is written."""
    routed = []
    for m in tasks:
        route = fc_routerec.parse_route((issues.get(m.get("linear")) or {}).get("description") or "")
        if route and route.get("depth") in LEVEL:
            routed.append((m, route))
    if not routed:
        return list(written)
    top = max((r["depth"] for _, r in routed), key=list(LEVEL).index)
    return [f"**{LEVEL[top]}**", *(task_line(m, r) for m, r in routed), *written]

def written_problem(lines):
    """Why the team lead's `## Risk` lines are refused, or None: each is a `- ` bullet, and none states the level."""
    if any(x.strip() and not x.startswith("- ") for x in lines):
        return "every line of `## Risk` must be a `- ` bullet"
    if any(LEVEL_LINE.match(x) for x in lines):
        return "`## Risk` states a level; compose adds the level from each task's route, so say what could break and where to look"
    return None
