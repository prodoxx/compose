"""Night mode's morning digest: one summary for the whole product after a night - verified work waiting for a merge,
what needs the product owner's review, and what is ready to plan. Every source is read on its own: one that fails is marked unavailable."""
from __future__ import annotations
import re, time

import fc_answer, fc_change, fc_core, fc_delivery, fc_factorystate, fc_nightprep, fc_nightstate, fc_plan, fc_tasks, fc_yolo

HEADS = ("Verified and waiting for your merge", "Needs your review", "Ready to plan")
ERRORS = (RuntimeError, ValueError, TypeError, KeyError, OSError, SystemExit)
FAILED_KINDS = ("turn-failed", "judge-failed", "lead-lost", "answer-failed")
REVIEW_SECTIONS = ("Waiting on you", "Tried and failed")

def _why(e) -> str:
    return " ".join(str(e).split()) or type(e).__name__

def night_events(repo, since, until=None) -> list:
    """The event lines of the night: at or after `since`, and at or before `until` when the night has ended."""
    return [e["line"] for e in fc_factorystate.events_since(repo, 0) if e["at"] >= since and (until is None or e["at"] <= until)]

def _field(line, key) -> str:
    bare = re.sub(r'"[^"]*"', lambda q: " " * len(q.group(0)), line)  # a key inside a quoted value is not a field
    m = re.search(rf'\b{key}=', bare)
    v = re.match(r'"[^"]*"|\S*', line[m.end():]) if m else None
    return v.group(0).strip('"') if v else ""

def _kind(line) -> str:
    return _field(line, "factory")

def verified(repo, lines) -> list:
    stories = {}
    for line in lines:
        if _kind(line) == "verified" and _field(line, "story"):
            stories[_field(line, "story")] = _field(line, "project")
    if not stories:
        return []
    try:
        plan = fc_delivery.product_plan(repo)
        placed = {e["id"]: e for e in fc_plan.order_entries(plan["tracks"], plan["features"])} if plan else {}
        err = "" if plan else "no delivery plan"
    except ERRORS as e:
        placed, err = {}, _why(e)
    out = []
    for story, project in stories.items():
        e = placed.get(story)
        if e:
            out.append(f"{story} {e['title']}  (feature {e['feature']}, wave {e['wave']}, track {e['track']}; project {project})")
        else:
            out.append(f"{story}  (project {project}; placement unavailable: {err or 'not in the delivery plan'})")
    return out

def _answers(repo, since, until=None) -> list:
    out = []
    for r in fc_answer._records(repo):
        try:
            at = fc_core.report_time(r["at"])
            if r.get("error") or at < int(since) or (until is not None and at > until):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if r.get("never_set"):
            out.append(f"Waiting on you: {r.get('project')}: {r.get('question')} (never-set: {r.get('why_wait')})")
        else:
            out.append(f"To confirm: {r.get('question')} -> {r.get('answer')} (night answerer; overruling it is a plan change)")
    return out

def _projects(repo) -> list:
    """Every project of the product: one that waits on the owner shows even when nothing was dispatched to it tonight."""
    return sorted(fc_core.all_projects(repo))

def _project_items(repo, slug, since) -> tuple:
    proj = fc_core.all_projects(repo)[slug][1]
    y = fc_tasks.yolo_state(proj)
    if not y or y["mode"] != "night":  # the night's posture was lifted (a tick after the end, a stop at once): its archive keeps the items
        y = fc_nightstate.archived_posture(proj, since) or y
    if not y:
        return [], []
    confirm = []
    for d in y.get("decisions") or []:
        try:
            if fc_core.report_time(d["ts"]) >= int(since):
                confirm.append(f"To confirm: {slug} {d['text']}")
        except (KeyError, TypeError, ValueError):
            continue
    waiting = [f"{head}: {slug}: {i}" if head == "Waiting on you" else f"Failed or stuck: {slug}: {i}"
               for head, items in fc_yolo.digest_sections(repo, proj, y) if head in REVIEW_SECTIONS for i in items]
    return confirm, waiting

def review(repo, state, lines) -> list:
    night = state.get("night") or {}
    since, until = night.get("since") or 0.0, (night.get("ended") or {}).get("at")
    out, confirm, waiting, paused = [], [], [], []
    def guard(label, fn, default=None):
        try:
            return fn()
        except ERRORS as e:
            out.append(f"{label} unavailable: {_why(e)}")
            return [] if default is None else default
    answers = guard("night answers", lambda: _answers(repo, since, until))
    slugs = guard("projects", lambda: _projects(repo))
    for slug in slugs:
        c, w = guard(f"digest of {slug}", lambda: _project_items(repo, slug, since), ([], []))
        confirm += c; waiting += w
    def reconcile():
        got = fc_delivery.last_reconcile(repo)
        return [f"Proposed fix: {p}" for p in (got or {}).get("proposed") or []]
    replan = [f"Re-plan: {_field(l, 'story')} {_kind(l)}" + (f" ({_field(l, 'why')})" if _field(l, "why") else "") for l in lines if _kind(l) in ("held", "changed")]
    for slug in slugs:
        def read(slug=slug):
            proj = fc_core.all_projects(repo)[slug][1]
            return [f"Paused: {slug} {tid} ({p.get('reason')})" for tid, p in fc_change.paused_tasks(proj).items()]
        paused += guard(f"paused tasks of {slug}", read)
    failed = [f"Failed or stuck: {_field(l, 'why') or _field(l, 'question') or _field(l, 'story')}" for l in lines if _kind(l) in FAILED_KINDS]
    proposals = guard("last reconcile", reconcile)
    return answers + confirm + proposals + replan + paused + waiting + failed + out

def _sections(content) -> dict:
    """{ticket id: (title, number of numbered questions)} from the prep document's `## <id>: <title>` sections."""
    found, cur = {}, None
    for line in (content or "").split("\n"):
        m = re.match(r"## ([^:]+): (.*)$", line)
        if m:
            cur = m.group(1).strip()
            found[cur] = [m.group(2).strip(), 0]
        elif cur and line.startswith("Questions:"):
            found[cur][1] = 0
        elif cur and re.match(r"\d+\. ", line):
            found[cur][1] += 1
    return {k: tuple(v) for k, v in found.items()}

PREP_LINES = {"cut": "prep was cut off when the night ended; it runs again next night", "failed": "prep failed; it is tried once more next night",
              "gave-up": "prep failed twice; see .compose/night-prep.log", "running": "still being prepared"}

def ready_to_plan(repo, state, now=None) -> list:
    """The prepared tickets and how each other prep ended. The status comes from the worker outcomes, not from a tick that may not have run yet."""
    now = time.time() if now is None else now
    out, recs = [], {i: r for i, r in (state.get("prep") or {}).items() if r.get("print") is not None or r.get("gave_up")}
    if recs:
        try:
            doc = fc_nightprep.find_doc(repo)  # find only: the digest changes nothing
            found, err = _sections(doc["content"]), "" if doc["id"] else "document missing"
        except ERRORS as e:
            doc, found, err = {}, {}, _why(e)
        for ident, rec in recs.items():
            layer = rec.get("layer", "story")
            try:
                status = fc_nightprep.prep_status(repo, ident, rec, now)
            except ERRORS as e:
                out.append(f"{ident} ({layer}): prep status unavailable ({_why(e)})")
                continue
            if status in PREP_LINES:
                out.append(f"{ident} ({layer}): {PREP_LINES[status]}")
            elif status != "prepared":
                continue
            elif ident in found:
                title, n = found[ident]
                out.append(f"{ident} {title} ({layer}): {n} question{'' if n == 1 else 's'} prepared")
            elif err:
                out.append(f"{ident} ({layer}): prepared questions unavailable ({err})")
            else:
                out.append(f"{ident} ({layer}): prepared, but its section is not in the document")
        if out and doc.get("id") and doc.get("url"):
            out.insert(0, f"Prep document: {doc['url']}")
    night = state.get("night") or {}
    for held in night.get("morning_msgs") or []:
        slug, _, message = held.partition("|")
        out.append(f"Planning waits: {slug}: {message}")
    return out

def _span(seconds) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m" if seconds >= 3600 else f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"

def render(repo, state, now) -> str:
    night = state.get("night") or {}
    clock = lambda t: time.strftime("%H:%M", time.localtime(t))
    ended = night.get("ended")
    head = (f"morning digest for {repo.name}: night {clock(night.get('since') or now)} to {clock((ended or {}).get('at') or now)}, "
            f"ended: {(ended or {}).get('state') or 'still on'}")
    out = [head, f"spend ${night.get('spend') or 0:.2f} of ${night.get('budget') or 0:.2f}; usage waits: {night.get('waits') or 0} ({_span(night.get('waited') or 0)})"]
    try:
        lines = night_events(repo, night.get("since") or 0.0, (ended or {}).get("at"))
        events_err = ""
    except ERRORS as e:
        lines, events_err = [], _why(e)
    def part(fn, *args):
        try:
            return fn(*args)
        except ERRORS as e:
            return [f"unavailable: {_why(e)}"]
    items = [part(verified, repo, lines), part(review, repo, state, lines), part(ready_to_plan, repo, state, now)]
    if events_err:
        items[0] = items[1] = [f"night events unavailable: {events_err}"]
    for h, its in zip(HEADS, items):
        out += ["", h] + [f"- {i}" for i in its or ["none"]]
    return "\n".join(out + ["", f"Night notes: {fc_nightstate.notes_path(repo)}"]) + "\n"
