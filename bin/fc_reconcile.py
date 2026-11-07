"""`compose reconcile list | check | show | apply`: sort every open ticket of a messy Linear project once, check the sheet, render it as one proposal, and write it on the product owner's go."""
import datetime, json, sys
from pathlib import Path

import fc_core, fc_config, fc_crew, fc_herdr, fc_fleet, fc_linear, fc_linear_cmd, fc_tasks

SORTS = ("keep", "cancel", "merge", "verify")
LABELS = ("Epic", "Feature", "Story")
RUNNING = ("In Progress", "In Review")
HEADS = {"cancel": "Canceled in the {project} reconcile ({date}), on the product owner's go.",
         "merge": "Canceled in the {project} reconcile ({date}), on the product owner's go: merged into {id} ({title}).",
         "drop-ready": "Ready label removed in the {project} reconcile ({date}): it needs shaping before it can be ready.",
         "title": 'Renamed in the {project} reconcile ({date}) from "{old}".'}
TEAM_QUERY = "query($id: String!) { team(id: $id) { name states { nodes { id name } } labels(first: 250) { nodes { id name } } } }"
CREATE_ISSUE = "mutation($in: IssueCreateInput!) { issueCreate(input: $in) { issue { id identifier } } }"
UPDATE_ISSUE = "mutation($id: String!, $in: IssueUpdateInput!) { issueUpdate(id: $id, input: $in) { success } }"
CREATE_LABEL = "mutation($name: String!, $team: String!) { issueLabelCreate(input: {name: $name, teamId: $team}) { issueLabel { id name } } }"
ANCESTORS_QUERY = "query($id: String!) { issue(id: $id) { parent { identifier parent { identifier parent { identifier parent { identifier } } } } } }"
INVENTORY_QUERY = ('query($after: String, $project: ID) { issues(first: 100, after: $after, filter: {project: {id: {eq: $project}}, '
                   'state: {type: {nin: ["completed", "canceled"]}}}) { pageInfo { hasNextPage endCursor } nodes { identifier title state { name } labels { nodes { name } } parent { identifier } } } }')

def sheet_path(project_id):
    return Path.home() / ".compose" / "reconcile" / f"{project_id}.json"

def inventory(repo):
    """The project and every issue of it that is not completed or canceled, all pages, ordered by issue number."""
    project = fc_linear_cmd.linear_project(repo)
    nodes, after = [], None
    while True:
        page = fc_linear.linear_gql(INVENTORY_QUERY, {"after": after, "project": project["id"]})["issues"]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    nodes.sort(key=lambda n: int(n["identifier"].rsplit("-", 1)[-1]))
    return project, [{"id": n["identifier"], "title": n["title"], "state": n["state"]["name"], "labels": [l["name"] for l in n["labels"]["nodes"]],
                      "parent": (n.get("parent") or {}).get("identifier")} for n in nodes]

def render_tree(issues):
    ids = {i["id"] for i in issues}
    kids = {}
    for i in issues:
        kids.setdefault(i["parent"] if i["parent"] in ids else None, []).append(i)
    out = []
    def walk(parent, depth):
        for i in kids.get(parent, []):
            out.append(f"{'  ' * depth}{i['id']} {i['state']} {','.join(i['labels']) or '-'} {i['title']}")
            walk(i["id"], depth + 1)
    walk(None, 0)
    return "\n".join(out)

def read_text_file(what, path):
    p = Path(path).expanduser()
    if not p.is_file():
        sys.exit(f"{what}: no file {path}")
    return p.read_text()

def load_sheet(path):
    """The sheet normalized, with every reason and body path read into its text."""
    try:
        raw = json.loads(read_text_file("sheet", path))
    except ValueError as e:
        sys.exit(f"sheet {path}: not valid JSON ({e})")
    if not isinstance(raw, dict):
        sys.exit(f"sheet {path}: expected a JSON object")
    for key in ("keep", "verify", "tickets", "new"):
        if not isinstance(raw.get(key) or [], list):
            sys.exit(f"sheet: {key} must be a list")
    if not isinstance(raw.get("reasons") or {}, dict):
        sys.exit("sheet: reasons must map a reason name to the path of its text file")
    for key in ("keep", "verify"):
        for entry in raw.get(key) or []:
            if not (isinstance(entry, str) and entry.strip()):
                sys.exit(f"sheet: a {key} entry must be a ticket id like NEB-12: {entry!r}")
    for entry in raw.get("tickets") or []:
        if not isinstance(entry, dict):
            sys.exit(f"sheet: a tickets entry must be an object with an id: {entry!r}")
        if not entry.get("id"):
            sys.exit(f"sheet: a tickets entry has no id: {entry}")
    for name, path in (raw.get("reasons") or {}).items():
        if not (isinstance(path, str) and path):
            sys.exit(f"sheet: reason {name} must be the path of a text file: {path!r}")
    for entry in raw.get("new") or []:
        if not (isinstance(entry, dict) and entry.get("key")):
            sys.exit(f"sheet: a new entry must be an object with a key: {entry!r}")
        if entry.get("body") is not None and not isinstance(entry["body"], str):
            sys.exit(f"sheet: new {entry['key']} body must be the path of a text file: {entry['body']!r}")
    return {"keep": list(raw.get("keep") or []), "verify": list(raw.get("verify") or []), "tickets": [dict(t) for t in raw.get("tickets") or []],
            "reasons": {k: read_text_file(f"reason {k}", p) for k, p in (raw.get("reasons") or {}).items()},
            "new": [{**n, "body": read_text_file(f"new ticket {n['key']} body", n["body"]) if n.get("body") else ""} for n in raw.get("new") or []]}

def explicit(sheet):
    """Every entry the sheet sorts by hand, in order: the keep and verify lists first, then the tickets."""
    return [{"id": i, "sort": "keep"} for i in sheet["keep"]] + [{"id": i, "sort": "verify"} for i in sheet["verify"]] + list(sheet["tickets"])

def expand(sheet, issues):
    """id to its effective entry; a subtree cancel reaches each open descendant reachable without passing a ticket sorted on its own, marked with the root it came from."""
    out = {}
    for e in explicit(sheet):
        out.setdefault(e["id"], e)
    kids = {}
    for i in issues:
        kids.setdefault(i["parent"], []).append(i["id"])
    for e in explicit(sheet):
        if e["sort"] == "cancel" and e.get("subtree"):
            todo = list(kids.get(e["id"], []))
            while todo:
                c = todo.pop(0)
                if c not in out:  # a ticket sorted on its own, and everything under it, is not this cancel's to take
                    out[c] = {"id": c, "sort": "cancel", "reason": e.get("reason"), "under": e["id"]}
                    todo += kids.get(c, [])
    return out

def entry_problems(e, by_id, got, sheet):
    i, sort = e["id"], e.get("sort")
    if sort not in SORTS:
        return [f"{i} has sort {sort!r}, not one of {' '.join(SORTS)}"]
    out = []
    if sort == "verify" and any(e.get(k) for k in ("title", "drop_ready", "deep_dive")):
        out.append(f"{i} is sorted verify: it takes no changes")
    elif sort != "keep" and any(e.get(k) for k in ("title", "drop_ready", "deep_dive")):
        out.append(f"{i} is sorted {sort}: title, drop_ready and deep_dive belong to keep")
    if sort == "cancel" and not e.get("reason"):
        out.append(f"{i} is canceled without a reason")
    if e.get("reason") and not sheet["reasons"].get(e["reason"], "").strip():
        out.append(f"reason {e['reason']} has no text")
    if sort == "keep" and e.get("drop_ready"):
        if "Ready" not in by_id.get(i, {}).get("labels", []):
            out.append(f"{i} drops Ready but does not carry it")
        if not e.get("reason"):
            out.append(f"{i} drops Ready without a reason")
    if sort == "merge":
        into = e.get("into")
        if not into:
            out.append(f"{i} is merged without a ticket to merge into")
        elif into == i:
            out.append(f"{i} merges into itself")
        elif got.get(into, {}).get("sort") == "cancel":
            out.append(f"{i} merges into {into}, which this sheet cancels")
        elif got.get(into, {}).get("sort") not in ("keep", "verify"):
            out.append(f"{i} merges into {into}, which this sheet does not keep or verify")
    return out

def check_sheet(sheet, issues):
    """Every problem with the sheet against the open tickets, empty when it is good."""
    by_id = {i["id"]: i for i in issues}
    problems, seen = [], set()
    for e in explicit(sheet):
        if e["id"] in seen:
            problems.append(f"{e['id']} is sorted twice")
        elif e["id"] not in by_id:
            problems.append(f"{e['id']} is not an open ticket of this project")
        seen.add(e["id"])
    got = expand(sheet, issues)
    for e in explicit(sheet):
        if e["id"] in by_id:
            problems += entry_problems(e, by_id, got, sheet)
    problems += [f"{i['id']} is open and not sorted" for i in issues if i["id"] not in got]
    kept, keys = {i for i, e in got.items() if e["sort"] == "keep"}, set()
    for n in sheet["new"]:
        if n.get("key") in keys:
            problems.append(f"new {n.get('key')} is listed twice")
        if n.get("key") in by_id:
            problems.append(f"new {n.get('key')} has the id of an open ticket; give it a key like N1")
        if not str(n.get("title") or "").strip():
            problems.append(f"new {n.get('key')} has no title")
        if n.get("parent") not in kept and n.get("parent") not in keys:
            problems.append(f"new {n.get('key')} has parent {n.get('parent')}, which is neither a kept open ticket nor an earlier new ticket")
        if n.get("label") not in LABELS:
            problems.append(f"new {n.get('key')} has label {n.get('label')!r}, not one of {', '.join(LABELS)}")
        keys.add(n.get("key"))
    return problems

def counts(got):
    return {s: sum(1 for e in got.values() if e["sort"] == s) for s in SORTS}

def summary(got, sheet):
    c = counts(got)
    return f"{len(got)} open tickets sorted ({c['keep']} kept, {c['verify']} to verify, {c['cancel']} canceled, {c['merge']} merged), {len(sheet['new'])} new"

def grouped(entries, reasons, line):
    """Bullets grouped by reason, each group under its reason text."""
    groups = {}
    for e in entries:
        groups.setdefault(e.get("reason"), []).append(e)
    return "\n\n".join((reasons[k].strip() + "\n\n" if k in reasons else "") + "\n".join(line(e) for e in es) for k, es in groups.items())

def render_proposal(stored):
    sheet, issues = stored, stored["issues"]
    by_id = {i["id"]: i for i in issues}
    got = expand(sheet, issues)
    t = lambda i: f"{i} {by_id.get(i, {}).get('title', '')}".rstrip()
    of = lambda sort: [e for e in got.values() if e["sort"] == sort]
    under = lambda root: sum(1 for e in got.values() if e.get("under") == root)
    def cancel_line(e):
        n = under(e["id"])
        return f"- {t(e['id'])}" + (f", and the {n} open ticket{'s' if n != 1 else ''} under it" if n else "")
    stops = [e for e in of("cancel") + of("merge") if by_id[e["id"]]["state"] in RUNNING]
    keeps = of("keep")
    sections = [
        ("Cancel", grouped([e for e in of("cancel") if "under" not in e], sheet["reasons"], cancel_line)),
        ("Merge", grouped(of("merge"), sheet["reasons"], lambda e: f"- {t(e['id'])}, into {t(e['into'])}")),
        ("Running work this stops", "\n".join(f"- {t(e['id'])} ({by_id[e['id']]['state']})" for e in stops)),
        ("Rename", "\n".join(f'- {e["id"]}: "{by_id[e["id"]]["title"]}" becomes "{e["title"]}"' for e in keeps if e.get("title"))),
        ("Ready removed (needs shaping)", grouped([e for e in keeps if e.get("drop_ready")], sheet["reasons"], lambda e: f"- {t(e['id'])}")),
        ("Label deep-dive", "\n".join(f"- {t(e['id'])}" for e in keeps if e.get("deep_dive"))),
        ("New tickets", "\n".join(f"- {n['key']} {n.get('title') or '(untitled)'} ({n.get('label') or '?'} under {n.get('parent') or '?'}, needs shaping" + (", deep-dive" if n.get("deep_dive") else "") + ")" for n in sheet["new"])),
        ("Left for you to verify", "\n".join(f"- {t(e['id'])}" for e in of("verify"))),
        ("Keep", "\n".join(f"- {t(e['id'])}" for e in keeps if not (e.get("title") or e.get("drop_ready") or e.get("deep_dive")))),
    ]
    c = counts(got)
    head = f"{len(got)} open tickets: {c['keep']} kept, {c['verify']} left for you to verify, {c['cancel']} canceled, {c['merge']} merged; {len(sheet['new'])} new."
    return "\n\n".join([head] + [f"## {name}\n\n{body}" for name, body in sections if body]) + "\n"

def read_stored(project):
    p = sheet_path(project["id"])
    return json.loads(p.read_text()) if p.exists() else None

def verb_list(a):
    print(render_tree(inventory(a.repo)[1]))

def landed(stored):
    """What a half-applied action already wrote to Linear, for the refusal: '; NEB-4:merge has its comment and relation written'."""
    by_act = {}
    for k in stored.get("steps", {}):
        act, _, x = k.rpartition(":")
        if act not in stored["applied"]:
            by_act.setdefault(act, []).append(x)
    return "".join(f"; {act} has its {' and '.join(xs)} written" for act, xs in by_act.items())

def verb_check(a):
    if not a.file:
        sys.exit("reconcile check needs --file <sheet>")
    project, issues = inventory(a.repo)
    sheet = load_sheet(a.file)
    stored = read_stored(project)
    started = stored and (stored["applied"] or stored.get("steps"))  # a comment or relation that landed is Linear written too
    if started and any(a["key"] not in stored["applied"] and a["key"] not in stored.get("skipped", {}) for a in actions(stored)):
        sys.exit(f"a reconcile is half applied ({len(stored['applied'])} of {len(actions(stored))} done{landed(stored)}): "
                 f'finish it with compose reconcile apply --go "<their words>" first')
    problems = check_sheet(sheet, issues)
    if problems:
        sys.exit("\n".join(problems))
    path = sheet_path(project["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = {**sheet, "project": {"id": project["id"], "name": project["name"], "team": project["team"]["id"]}, "checked_at": fc_core.now(),
             "issues": issues, "go": [], "applied": {}, "steps": {}, "skipped": {}, "created": {}}
    if stored:
        fresh["go"] = list(stored.get("go", []))  # the product owner's earlier words stay with what they let through
        carry_done(stored, fresh)
    save(path, fresh)
    print(f"checked: {summary(expand(sheet, issues), sheet)}; compose reconcile show prints the proposal")

def carry_done(old, fresh):
    """A re-check keeps what the last apply already wrote: each applied action the new sheet still holds unchanged, its finished steps, and the tickets it filed."""
    was = {a["key"]: a for a in actions(old)}
    fresh["changed_after_filing"] = []
    for a in actions(fresh):
        same = was.get(a["key"]) == a
        if a["key"] in old["applied"] and same:
            fresh["applied"][a["key"]] = old["applied"][a["key"]]
        if same:
            fresh["steps"].update({k: v for k, v in old.get("steps", {}).items() if k.rsplit(":", 1)[0] == a["key"] and a["key"] not in fresh["applied"]})
        if a["do"] == "create" and a["new"]["key"] in old["created"]:  # a key filed stays filed, whatever the sheet now says of it
            fresh["created"][a["new"]["key"]] = old["created"][a["new"]["key"]]
            fresh["applied"][a["key"]] = old["applied"].get(a["key"]) or old["created"][a["new"]["key"]]
            if not same:
                fresh["changed_after_filing"].append(a["new"]["key"])

def verb_show(a):
    project = fc_linear_cmd.linear_project(a.repo)
    stored = read_stored(project)
    if not stored:
        sys.exit(f"no checked reconcile for {project['name']}: compose reconcile check --file <sheet> first")
    print(render_proposal(stored), end="")

def depth(i, by_id):
    n = 0
    while by_id.get(i, {}).get("parent") in by_id:
        i, n = by_id[i]["parent"], n + 1
    return n

def actions(stored):
    """Every change the sheet makes, in the order they are written: renames, label changes, cancels and merges deepest first, new tickets parents first."""
    by_id = {i["id"]: i for i in stored["issues"]}
    got = expand(stored, stored["issues"])
    keeps = [e for e in got.values() if e["sort"] == "keep"]
    out = [{"key": f"{e['id']}:title", "do": "title", "id": e["id"], "title": e["title"]} for e in keeps if e.get("title")]
    out += [{"key": f"{e['id']}:drop-ready", "do": "drop-ready", "id": e["id"], "reason": e.get("reason")} for e in keeps if e.get("drop_ready")]
    out += [{"key": f"{e['id']}:deep-dive", "do": "deep-dive", "id": e["id"]} for e in keeps if e.get("deep_dive")]
    ends = sorted((e for e in got.values() if e["sort"] in ("cancel", "merge")), key=lambda e: -depth(e["id"], by_id))
    out += [{"key": f"{e['id']}:{e['sort']}", "do": e["sort"], "id": e["id"], "reason": e.get("reason"), "into": e.get("into")} for e in ends]
    out += [{"key": f"{n['key']}:create", "do": "create", "new": n} for n in stored["new"]]
    return out

def comment_for(stored, act, today):
    """The comment an action leaves on its ticket: the head, then the reason text on the next line."""
    by_id = {i["id"]: i for i in stored["issues"]}
    into = act.get("into")
    renamed = {e["id"]: e["title"] for e in explicit(stored) if e.get("title")}
    head = HEADS[act["do"]].format(project=stored["project"]["name"], date=today, old=by_id.get(act["id"], {}).get("title"), id=into,
                                   title=renamed.get(into) or by_id.get(into, {}).get("title"))
    reason = stored["reasons"].get(act.get("reason"), "").strip()
    return head + ("\n" + reason if reason else "")

class Team:
    """The project's team labels and states, read once; a label the cleanup needs is created the first time it is asked for."""
    def __init__(self, team_id):
        self.id, self.team = team_id, fc_linear.linear_gql(TEAM_QUERY, {"id": team_id})["team"]

    def label(self, name):
        found = next((l for l in self.team["labels"]["nodes"] if l["name"].lower() == name.lower()), None)
        if not found:
            found = fc_linear.linear_gql(CREATE_LABEL, {"name": name, "team": self.id})["issueLabelCreate"]["issueLabel"]
            self.team["labels"]["nodes"].append({"id": found["id"], "name": name})
        return found["id"]

def stale(issue, was):
    """Why a cancel or merge is not written, or None: the ticket is already canceled, or moved since the check."""
    if issue["state"]["type"] == "canceled":
        return f"already {issue['state']['name']}"
    if issue["state"]["name"] != was:
        return f"it is {issue['state']['name']} now, not {was} as at the check; sort it again"

def do_action(stored, act, team, today, path):
    """Write one action to Linear; returns why it was skipped, or None when done."""
    if act["do"] == "create":
        return create_ticket(stored, act["new"], team)
    by_id = {i["id"]: i for i in stored["issues"]}
    issue = fc_linear.linear_issue(act["id"])
    if act["do"] in ("cancel", "merge"):
        why = stale(issue, by_id[act["id"]]["state"])
        if why:
            return why
        into = fc_linear.linear_issue(act["into"]) if act.get("into") else None
        if into and into["state"]["type"] == "canceled":
            return f"{into['identifier']} is {into['state']['name']} now; sort it again"
        done = tuple(x for x in ("comment", "relation") if f"{act['key']}:{x}" in stored["steps"])
        fc_linear.linear_cancel(act["id"], comment_for(stored, act, today), act.get("into"), done, lambda x: step(stored, path, f"{act['key']}:{x}"))
        return None
    if act["do"] == "title":
        fc_linear.linear_gql(UPDATE_ISSUE, {"id": issue["id"], "in": {"title": act["title"]}})
    elif act["do"] == "drop-ready":
        fc_linear.linear_gql(UPDATE_ISSUE, {"id": issue["id"], "in": {"removedLabelIds": [team.label("Ready")], "addedLabelIds": [team.label(fc_linear.NEEDS_SHAPING)]}})
    else:
        fc_linear.linear_gql(UPDATE_ISSUE, {"id": issue["id"], "in": {"addedLabelIds": [team.label("deep-dive")]}})
    if act["do"] != "deep-dive":
        fc_linear.linear_comment(issue, comment_for(stored, act, today))

def create_ticket(stored, n, team):
    """A new ticket in Backlog under its parent, labeled and marked needs-shaping, the way `compose linear create` makes one."""
    names = [n["label"], fc_linear.NEEDS_SHAPING] + (["deep-dive"] if n.get("deep_dive") else [])
    backlog = next((s for s in team.team["states"]["nodes"] if s["name"].lower() == "backlog"), None)
    if not backlog:
        raise RuntimeError(f"team {team.team['name']} has no Backlog state")
    inp = {"teamId": team.id, "projectId": stored["project"]["id"], "title": n.get("title") or "", "description": n.get("body") or "",
           "stateId": backlog["id"], "labelIds": [team.label(name) for name in names]}
    parent = stored["created"].get(n["parent"], n["parent"])
    if n["parent"] in {x["key"] for x in stored["new"]} and n["parent"] not in stored["created"]:
        raise RuntimeError(f"its parent {n['parent']} was not created")
    inp["parentId"] = fc_linear.linear_issue(parent)["id"]
    stored["created"][n["key"]] = fc_linear.linear_gql(CREATE_ISSUE, {"in": inp})["issueCreate"]["issue"]["identifier"]

def apply_sheet(stored, path, quote):
    """Write every action not yet applied, saving the sheet after each success; one line per action."""
    acts = [a for a in actions(stored) if a["key"] not in stored["applied"]]
    notes = [f"{k} already filed as {stored['created'][k]}; edit it in Linear" for k in stored.pop("changed_after_filing", [])]
    if not acts:
        save(path, stored)
        return notes
    stored["go"].append({"quote": quote, "at": datetime.datetime.now().isoformat(timespec="seconds")})
    save(path, stored)
    stored["skipped"], stored["steps"] = {}, stored.get("steps", {})
    team, today, lines = Team(stored["project"]["team"]), datetime.date.today().isoformat(), notes
    for act in acts:
        try:
            why = do_action(stored, act, team, today, path)
        except Exception as e:  # one action's failure is reported, never the end of the others
            lines.append(f"{act['key']} FAILED: {e}")
            continue
        if why:
            lines.append(f"{act['key']} skipped: {why}")
            stored["skipped"][act["key"]] = why
            continue
        stored["applied"][act["key"]] = datetime.datetime.now().isoformat(timespec="seconds")
        save(path, stored)
        lines.append(f"{act['key']} done")
    save(path, stored)
    return lines

def step(stored, path, key):
    """Record a finished sub-step of an action, so a retry after a failure does not write it again."""
    stored["steps"][key] = datetime.datetime.now().isoformat(timespec="seconds")
    save(path, stored)

def save(path, stored):
    fc_core.atomic_write(path, json.dumps(stored, indent=1))

def verb_apply(a):
    go = (a.go or "").strip()
    if not go:
        sys.exit('apply needs the product owner\'s go, verbatim: --go "<their words>"')
    if posture := fc_linear_cmd.under_posture(a.repo):
        sys.exit(f"{posture[0]} is under {posture[1]}: a reconcile changes Linear only on the product owner's own go; apply it once {posture[1]} is off")
    project = fc_linear_cmd.linear_project(a.repo)
    stored = read_stored(project)
    if not stored:
        sys.exit(f"no checked reconcile for {project['name']}: compose reconcile check --file <sheet> first")
    lines = apply_sheet(stored, sheet_path(project["id"]), go)
    if not lines and all(x["key"] in stored["applied"] for x in actions(stored)):
        print("nothing left to apply")
        return
    print("\n".join(lines))
    total, failed = len(actions(stored)), sum(1 for l in lines if " FAILED: " in l)
    skipped = sum(1 for l in lines if " skipped: " in l)
    print(f"applied {len(stored['applied'])} of {total}: {skipped} skipped, {failed} failed; compose reconcile notices lists what each team lead must hear")
    if failed:
        sys.exit(1)

def ancestors(ident):
    """The ticket's parent, grandparent and so on, up to four levels, in one Linear read."""
    out, node = [], fc_linear.linear_gql(ANCESTORS_QUERY, {"id": ident})["issue"]
    while node.get("parent"):
        node = node["parent"]
        out.append(node["identifier"])
    return out

def touched(features, chains, changes):
    """For each project (slug to its feature), the change lines of every changed ticket that is its feature, under it, or above it."""
    num = lambda t: int(t.rsplit("-", 1)[-1])
    return {slug: [changes[t] for t in sorted(changes, key=num) if t == f or f in chains.get(t, []) or t in chains.get(f, [])] for slug, f in features.items()}

def live_leads(repo):
    """Project slug to its team lead's name, for each project whose crew record matches a live herdr agent by name and workspace."""
    return leads_among(repo, fc_fleet.live_agents())

def leads_among(repo, agents):
    """live_leads over a given herdr agent list."""
    out = {}
    for slug, (_, proj) in sorted(fc_core.all_projects(Path(repo).resolve()).items()):
        c = fc_crew.crew_record(proj, fc_herdr.agent_name(slug, "orchestrator"))
        if c and any(ag.get("name") == c.get("agent", c["name"]) and ag.get("workspace_id") == c.get("space") for ag in agents):
            out[slug] = c.get("agent", c["name"])
    return out

def change_lines(stored):
    """One line per ticket the applied cleanup changed, or left for the product owner to verify; a subtree's members fold into their root."""
    by_id = {i["id"]: i for i in stored["issues"]}
    got = expand(stored, stored["issues"])
    tails, name = {}, lambda i: f"{i} {by_id[i]['title']}"
    for a in actions(stored):
        if a["key"] not in stored["applied"]:
            continue
        if a["do"] == "create":
            ident = stored["created"].get(a["new"]["key"]) or stored["applied"][a["key"]]
            tails[ident] = (f"{ident} {a['new']['title']}", [f"new ticket filed under {stored['created'].get(a['new']['parent'], a['new']['parent'])}"])
            continue
        if got.get(a["id"], {}).get("under"):
            continue
        n = sum(1 for e in got.values() if e.get("under") == a["id"])
        head = name(a["id"]) + (f", and the {n} open ticket{'s' if n != 1 else ''} under it" if n else "")
        tail = {"title": f'renamed "{a.get("title")}" (was "{by_id[a["id"]]["title"]}")', "drop-ready": "Ready label removed (needs shaping)", "deep-dive": "labeled deep-dive",
                "cancel": "Canceled", "merge": f"Canceled, merged into {a.get('into')}"}[a["do"]]
        tails.setdefault(a["id"], (head, []))[1].append(tail)
    for e in got.values():
        if e["sort"] == "verify":
            tails[e["id"]] = (name(e["id"]), ["left for the product owner to verify"])
    return {i: f"{h}: {'; '.join(t)}" for i, (h, t) in tails.items()}

def verb_notices(a):
    project = fc_linear_cmd.linear_project(a.repo)
    stored = read_stored(project)
    if not stored or not stored["applied"]:
        sys.exit('nothing applied yet: compose reconcile apply --go "<their words>" first')
    changes = change_lines(stored)
    features = {}
    for slug, (_, proj) in sorted(fc_core.all_projects(Path(a.repo).resolve()).items()):
        f = proj / "linear.json"
        if f.exists() and (feature := json.loads(f.read_text()).get("feature")):
            features[slug] = feature
    chains = {t: ancestors(t) for t in {*changes, *features.values()}}
    heard, leads = touched(features, chains, changes), live_leads(a.repo)
    for slug in sorted(heard, key=lambda s: (s not in leads, s)):
        if heard[slug]:
            print(f"{slug}: tell {leads[slug]} (live)" if slug in leads else f"{slug}: no live team lead; its next session reads Linear")
            print("\n".join(f"- {l}" for l in heard[slug]))

def cmd_reconcile(a):
    if not fc_config.LINEAR_KEY:
        sys.exit(fc_config.needs_linear_key("compose reconcile"))
    verb = globals().get(f"verb_{a.action}")
    if not verb:
        sys.exit(f"reconcile {a.action}: not available yet")
    try:
        verb(a)
    except (RuntimeError, OSError) as e:
        sys.exit(f"reconcile {a.action}: {e}")
