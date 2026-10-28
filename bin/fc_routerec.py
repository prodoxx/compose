"""The route's record: the reason the CLI composes, and the CLI-owned `## Route` section of the task issue."""
import re


def rule_source(state):
    """What the workflow rule read: the Bug label or the protected test."""
    return "its Bug label" if (state.get("bug") or {}).get("label") else "its protected test"


def answer_part(route, pin, name, state):
    """One of workflow or depth: Jev's choice with its probability and confidence, the pin or the rule that decided it, or its
    fallback with the confidence that sent it there."""
    a, value = route["answers"].get(name), route[name]
    if name == "workflow" and pin:
        return f"workflow {pin} pinned"
    if name == "workflow" and route["decided_by"].startswith("rule"):
        return f"workflow {value} from {rule_source(state)}"
    if name in route["fell_back"]:
        conf = f"confidence {a['confidence']:.2f}, " if a and a.get("confidence") is not None else "no answer, "
        return f"{name} {value} fallback ({conf}{route['fallback'].removeprefix('confidence ')})"
    return f"{name} {a['choice'].lower().replace('_', '-') if name == 'workflow' else a['choice']} (p {a['probabilities'][a['choice']]:.2f}, confidence {a['confidence']:.2f})"


def route_reason(route, state):
    """The recorded reason: Jev's answers or the fallback, the context, the Bug flags, the floor, and the policy note."""
    pin = state["pin"]
    if route.get("fell_back"):
        part1 = "Jev: " + ", ".join(answer_part(route, pin, k, state) for k in ("depth", "workflow"))
    elif route["fallback"] is None:
        w = route["answers"].get("workflow")
        if (w and w["choice"] == "CHECK_ONLY") or (pin and "depth" not in route["answers"]):
            if pin:
                part1 = f"workflow {pin} pinned"
            else:
                part1 = (f"Jev: workflow {w['choice'].lower().replace('_', '-')}"
                         f" (p {w['probabilities'][w['choice']]:.2f}, confidence {w['confidence']:.2f})")
            part1 += (", refused by the floor" if route["decided_by"] == "floor"
                      else f", depth {route['depth']} from check-only")
        else:
            d = route["answers"]["depth"]
            part1 = f"Jev: depth {d['choice']} (p {d['probabilities'][d['choice']]:.2f}, confidence {d['confidence']:.2f})"
            if pin:
                part1 += f", workflow {pin} pinned"
            elif w:
                part1 += (f", workflow {w['choice'].lower().replace('_', '-')}"
                          f" (p {w['probabilities'][w['choice']]:.2f}, confidence {w['confidence']:.2f})")
            else:
                part1 += f", workflow {route['workflow']}" + (f" from {rule_source(state)}" if route["decided_by"].startswith("rule") else "")
    else:
        part1 = f"fallback {route['depth']} {route['workflow']}"
        if pin:
            part1 += " pinned"
        part1 += f": {route['fallback']}"
    if route["retry"] is not None:
        part1 += (" after a retry with dependency analysis" if "callers" in route["retry"]
                  else " after a retry, dependency analysis unavailable")
    files = state["files"]
    tops = []
    for f in files:
        top = f["path"].split("/")[0]
        if top not in tops:
            tops.append(top)
    new = sum(1 for f in files if not f["exists"])
    shared = len({f["path"] for f in files} & set(state["other_files"]))
    n, c = len(files), len(state["criteria"])
    part2 = f"{n} file{'s' if n != 1 else ''}"
    if tops:
        part2 += f" under {', '.join(tops)}"
    part2 += f" ({new} new), {c} criteri{'on' if c == 1 else 'a'}, {shared} shared with other open tasks"
    bug = state["bug"]
    if bug["label"]:
        part3 = (f"Bug label, reproduction {'documented' if bug['documented'] else 'not documented'},"
                 f" {'confirmed' if bug['confirmed'] else 'not confirmed'}")
    else:
        part3 = "no Bug label"
    parts = [part1, part2, part3]
    if route["floor"]:
        parts.append(f"floor DEEP from {', '.join(route['floor'])}")
    if route.get("note"):
        parts.append(route["note"])
    return "; ".join(parts)


def route_section(route):
    """The `## Route` section text for one route."""
    confidence = f"{route['confidence']:.2f}" if route["confidence"] is not None else "-"
    checks = ", ".join(route["checks"]) if route["checks"] else "none"
    return "\n".join(["## Route", "",
                      f"* workflow: {route['workflow']}",
                      f"* depth: {route['depth']}",
                      f"* decided by: {route['decided_by']}",
                      f"* confidence: {confidence}",
                      f"* reason: {route['reason']}",
                      f"* verification requirements: {checks}",
                      f"* context hash: {route['hash']}"])


def parse_route(desc):
    """The Route section's bullets keyed by their labels; None without the section."""
    import fc_tasks
    text = fc_tasks.route_text(desc)
    if not text:
        return None
    out = {}
    for line in text.splitlines():
        if line.startswith("* ") or line.startswith("- "):
            label, _, value = line[2:].partition(": ")
            out[label] = value
    return out


def with_route(desc, section):
    """`desc` with its Route section replaced in place, else inserted right above the compose block."""
    import fc_tasks
    route = fc_tasks.route_text(desc)
    if route:
        return desc.replace(route, section, 1)
    m = re.search(r"^```compose", desc, re.M)
    if m:
        return desc[:m.start()].rstrip("\n") + "\n\n" + section + "\n\n" + desc[m.start():]
    return desc.rstrip("\n") + "\n\n" + section
