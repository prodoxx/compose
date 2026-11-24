"""`compose linear <verb>`: read and write Linear with the workspace key, printing compact text."""
import hashlib, json, os, re, select, sys, time, urllib.parse
from pathlib import Path

import fc_core, fc_config, fc_labels, fc_layout, fc_linear, fc_plan, fc_tasks


def issue_line(issue):
    labels = issue["labels"]["nodes"]
    return f"{issue['identifier']} {issue['state']['name']} {labels[0]['name'] if labels else '-'} {issue['title']}"

STDIN_SECONDS = 30  # a piped body that has not ended by then is refused: a stdin nobody closes must not hold the command silently

def read_stdin():
    """All of stdin, or exit naming the time when it neither ends nor sends anything for STDIN_SECONDS."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):  # not a file descriptor: a StringIO has nothing to wait on
        return sys.stdin.read()
    chunks = []
    while True:
        if not select.select([fd], [], [], STDIN_SECONDS)[0]:
            sys.exit(f"no body on stdin after {STDIN_SECONDS}s: pass --file <path>, or pipe the text in")
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks).decode(errors="replace")
        chunks.append(chunk)

def read_body(a):
    if a.file:
        return Path(a.file).read_text()
    return "" if sys.stdin.isatty() else read_stdin()

def project_link(repo):
    """(linear.json, the project's slug id from its URL)."""
    path = fc_core.repo_link(repo)
    if not path.exists():
        sys.exit(f"no .compose/linear.json in {path.parent.parent}")
    link = json.loads(path.read_text())
    name = urllib.parse.urlsplit(link["project"]).path.split("/project/", 1)[-1].strip("/").split("/")[0]
    return link, name.rsplit("-", 1)[-1]

def linear_project(repo):
    link, slug = project_link(repo)
    p = fc_linear.linear_gql("query($slug: String!) { projects(filter: {slugId: {eq: $slug}}) { nodes { id name url teams { nodes { id name key } } } } }",
                             {"slug": slug})["projects"]["nodes"]
    if not p:
        sys.exit(f"Linear has no project {link['project']}")
    p = p[0]
    team = next((t for t in p["teams"]["nodes"] if t["name"] == link["team"]), None)
    if not team:
        sys.exit(f"project {p['name']} is not on team {link['team']}")
    return {"id": p["id"], "name": p["name"], "url": p["url"], "team": {"id": team["id"], "name": team["name"], "key": team["key"]}}

def in_scope(item, a, name=None):
    if a.any_project:
        return
    p = linear_project(a.repo)
    if (item["project"] or {}).get("id") != p["id"]:
        sys.exit(f"{name or item['identifier']} is not in project {p['name']}; pass --any-project to use it")

def missing(e):
    """Exit with a Missing label's message and the `labels --create` line that makes it."""
    name = f'"{e.create}"' if fc_labels.SEP in e.create else e.create
    sys.exit(f"{e}; compose linear labels --create {name} makes it")

def resolved(team, names, types=None):
    """The labels `names` mean on `team` (fc_labels.resolve), printing a line for each name that resolved to another label than
    its own: a `Type` group's child, or what the types map names. Exits naming the create line for a name the team lacks."""
    out = []
    for name in names:
        try:
            label, note = fc_labels.resolve(team["labels"]["nodes"], name, types, team["name"])
        except fc_labels.Missing as e:
            missing(e)
        if note:
            print(note)
        out.append(label)
    return out

def label_ids(team, names, types=None):
    return [l["id"] for l in resolved(team, names, types)]

def state_and_labels(team, a, types=None):
    out = {}
    if a.state:
        s = next((s for s in team["states"]["nodes"] if s["name"].lower() == a.state.lower()), None)
        if not s:
            sys.exit(f"team {team['name']} has no state '{a.state}'")
        out["stateId"] = s["id"]
    if a.label:
        out["labelIds"] = label_ids(team, a.label, types)
    return out

def label_changes(team, a, have=(), types=None):
    """{"addedLabelIds": [...], "removedLabelIds": [...]} for --add-label and --drop-label, by the same case-insensitive lookup
    as --label (fc_labels). A bare --drop-label takes off every label the name could mean; Linear ignores dropping one the issue
    lacks. Each added label also takes off what single-select allows only one of (fc_labels.single_select) from `have`, the
    issue's labels, in the same update, saying so."""
    if not (a.add_label or a.drop_label):
        return {}
    if a.label:
        sys.exit("update takes --label (the whole list) or --add-label/--drop-label, not both")
    added = resolved(team, a.add_label or (), types)
    try:
        out, notes = fc_labels.changes(team["labels"]["nodes"], have, added, a.drop_label or (), types, team["name"])
    except fc_labels.Missing as e:
        missing(e)
    for note in notes:
        print(note)
    return out

PRIORITIES = {"none": 0, "urgent": 1, "high": 2, "medium": 3, "low": 4}  # Linear's own numbers

def priority(a):
    """{"priority": n} for --priority given as a name or as Linear's 0-4, else {}."""
    if a.priority is None:
        return {}
    n = PRIORITIES.get(a.priority.lower(), int(a.priority) if a.priority.isdigit() else None)
    if n not in PRIORITIES.values():
        sys.exit(f"--priority is one of {', '.join(PRIORITIES)} or 0-4, not {a.priority!r}")
    return {"priority": n}

def print_body(body):
    """The body, then each heading's section hash, which a section write passes back to --expect, and a line for what a hash list
    does not show: a section's hash covers its subsections, and the task's compose block belongs to no section."""
    print((body or "").rstrip("\n"))
    lines = (body or "").split("\n")
    marks = fc_plan.section_hashes(body or "")
    if marks:
        print('--- section hashes (--section "<heading>" --expect <hash>)')
        for heading, h in marks:
            print(f"{h} {heading}")
        levels = [n for _, n in fc_plan.headings(lines)]
        if any(2 <= a < b for a, b in zip(levels, levels[1:])):  # an H1 is the document's title: nobody writes it as a section
            print("(a section's hash covers its subsections, and --section replaces them too: it refuses a text that drops one unless "
                  "--subsections keep (own text only) or drop is given)")
        if fc_plan.compose_block_start(lines) is not None:
            print("(the task's compose block belongs to no section: --section keeps it wherever it sits, --description changes it)")

def writes_body(a, verb):
    """Whether the verb writes the body; refuses a flag mix that would write something other than what the caller meant."""
    if sum(bool(x) for x in (a.section, a.append, a.description)) > 1:
        sys.exit(f"{verb} takes one of --section, --append, --description")
    if a.section and not fc_plan.heading_level(a.section):
        sys.exit(f'{verb} --section takes a heading line such as "## Notes", not {a.section!r}')
    if a.expect and not a.section:
        sys.exit(f"{verb} --expect needs --section: it checks that section's hash")
    if getattr(a, "subsections", None) and not a.section:
        sys.exit(f"{verb} --subsections needs --section: it says what happens to that section's subsections")
    writes = bool(a.section or a.append or a.description)
    if a.file and not writes:  # the body file would be read by nothing
        sys.exit(f"{verb} --file needs --section, --append, or --description")
    return writes

def section_body(a, old, text):
    """The body with `--section` written from `text`. A section write replaces the section and its subsections, and never the task's
    compose block, so it refuses what would do either by accident: text that drops a subsection (`--subsections keep` writes the
    section's own text only, `drop` removes them) and text that holds a block the issue already has."""
    heading, mode, lines = a.section, getattr(a, "subsections", None), text.strip().split("\n")
    if fc_plan.compose_block_start(lines) is not None and fc_plan.compose_block_start(old.split("\n")) is not None:
        sys.exit(f"{heading}: the text holds a compose block and the issue already has one, and a section write leaves the task's block where it is: "
                 "nothing written; take the block out of the text, or write the whole body with --description, keeping exactly one block")
    if mode == "keep":
        if not text.strip():
            sys.exit(f"--subsections keep writes the section's own text: {heading} needs text (an empty body removes it with its subsections)")
        if any(i or fc_plan.heading_level(lines[0]) != fc_plan.heading_level(heading) for i, _ in fc_plan.headings(lines)):
            sys.exit(f"--subsections keep leaves the subsections of {heading} as they are, so its text holds no heading of its own: nothing written")
    elif text.strip() and mode != "drop" and (gone := fc_plan.dropped_subsections(old, heading, text)):
        sys.exit(f"{heading} holds {', '.join(gone)}, and the text drops {'it' if len(gone) == 1 else 'them'}, as a section write replaces its subsections too: "
                 "nothing written; write them back in the text, or pass --subsections keep to replace only the section's own text, or --subsections drop to remove them")
    return fc_plan.replace_section(old, heading, text, keep_subsections=mode == "keep")

def new_body(a, old):
    old = old or ""
    if a.expect and (now := fc_plan.section_hash(old, a.section)) != a.expect:
        sys.exit(f"{a.section} {'is gone' if now is None else 'changed'} since you read it (hash {a.expect}): nothing written; "
                 + ("read the body again and write from it" if now is None else "read it again, keep what changed, and write it back with the hash it shows now"))
    body = read_body(a)
    if a.section:
        return section_body(a, old, body)
    new = old.rstrip("\n") + "\n\n" + body.strip() + "\n" if a.append else body
    return one_block(old, new, "--append adds the text after the block the issue has: put the corrected block in the whole body" if a.append else "")

def one_block(old, new, how=""):
    """`new`, the body about to be written over `old`, unless it holds more compose blocks than a task has: a body write that adds one to an
    issue that has one ends with two, and `compose tasks` reads the first (#454, #485). A write that leaves the issue with no more blocks
    than it had goes through, so a body that already holds two can be written down to one."""
    n = len(fc_tasks.task_blocks(new))
    if n > max(1, len(fc_tasks.task_blocks(old))):
        sys.exit(f"the body would hold {n} compose blocks and the issue has {len(fc_tasks.task_blocks(old))}: a task has one, and `compose tasks` reads only the first: nothing written; "
                 + (how + "; " if how else "") + "write the whole body with --description, keeping exactly one block (the new one in place of the old)")
    return new

def block_kept(a, old):
    """The line that says a section write left the task's compose block where it sat inside the section, else None."""
    if a.section and fc_plan.block_inside(old or "", a.section):
        return f"the task's compose block sits in {a.section} and was kept: --section never changes it; --description writes the whole body, block included"

SETTLE_SECONDS, SETTLE_POLL = 30, 0.5

def await_write(read, old, new):
    """Return once `read()` no longer gives `old`, the text a body write replaced.
    Linear can serve a body from before the last write for several seconds, and the next section write starts from that read, so it would put the whole body back without this one.
    The reads back off, SETTLE_POLL doubling: seven over the whole wait."""
    old = (old or "").strip()
    if fc_plan.same_markdown(new, old):
        return
    deadline, pause = time.monotonic() + SETTLE_SECONDS, SETTLE_POLL
    while (read() or "").strip() == old:
        if time.monotonic() >= deadline:
            print(f"warning: Linear still serves the old text {SETTLE_SECONDS}s after the write: read it again, and write the next change only once it shows", file=sys.stderr)
            return
        time.sleep(min(pause, max(0, deadline - time.monotonic())))
        pause *= 2  # a read is a request out of the key's 2,500 an hour: a write slow to show costs a handful, not one every half second

def verb_project(a):
    writes = writes_body(a, "project")
    p = linear_project(a.repo)
    content = fc_linear.linear_gql("query($id: String!) { project(id: $id) { content } }", {"id": p["id"]})["project"]["content"] if writes or a.body else None
    if writes:
        new = new_body(a, content)
        fc_linear.linear_gql("mutation($id: String!, $in: ProjectUpdateInput!) { projectUpdate(id: $id, input: $in) { success } }", {"id": p["id"], "in": {"content": new}})
        await_write(lambda: fc_linear.linear_gql("query($id: String!) { project(id: $id) { content } }", {"id": p["id"]})["project"]["content"], content, new)
        print(f"updated {p['name']}")
        return
    print(f"{p['name']} on team {p['team']['name']}: {p['url']}")
    if a.body:
        print_body(content)

GET_CACHE_SECONDS = 30

def get_cache_files(a, issue_id=None):
    """The on-disk reads of this Linear project's issues (`issue_id` narrows to one), under ~/.compose/linear-get: one file per
    project and issue, so every teammate on the project shares them and another project's reads never answer this one."""
    try:
        project = json.loads(fc_core.repo_link(a.repo).read_text())["project"]
    except (OSError, ValueError, KeyError):
        return []  # no Linear link, no project to keep reads apart by: nothing is cached
    prefix = hashlib.sha1(project.encode()).hexdigest()[:12]
    folder = Path.home() / ".compose" / "linear-get"
    if issue_id:
        return [folder / f"{prefix}-{hashlib.sha1(issue_id.encode()).hexdigest()[:12]}.json"]
    return list(folder.glob(f"{prefix}-*.json"))

def read_issue(a, fetch):
    """`fetch()` is the one Linear read of `get`. The same issue of the same project read again within GET_CACHE_SECONDS is served
    from disk, so a teammate that loops on `get` costs one Linear call per window; --fresh bypasses it. What `get` prints from the
    issue (--body, --comments, --json, --any-project) is cut after this, so the flags never change which read is cached."""
    files = get_cache_files(a, a.target[0])
    if not files:
        return fetch()
    f = files[0]
    if not a.fresh and f.exists():
        try:
            hit = json.loads(f.read_text())
            if 0 <= time.time() - hit["at"] < GET_CACHE_SECONDS:
                print(f"(read {int(time.time() - hit['at'])}s ago from the cache; --fresh reads Linear again)", file=sys.stderr)
                return hit["issue"]
        except (OSError, ValueError, KeyError):
            pass  # an unreadable entry is a miss
    issue = fetch()
    f.parent.mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(f, json.dumps({"at": time.time(), "issue": issue}))
    return issue

def drop_get_cache(a):
    """A write to the project can change any issue a `get` cached, so a write forgets them all: the next read is fresh."""
    for f in get_cache_files(a):
        f.unlink(missing_ok=True)

def verb_get(a):
    if not a.target:
        sys.exit("get needs an issue id")
    issue = read_issue(a, lambda: fc_linear.linear_gql("query($id: String!) { issue(id: $id) { identifier title description state { name } labels { nodes { name } } project { id name } parent { identifier } inverseRelations(first: 50) { nodes { type issue { identifier state { name } } } } comments { nodes { body createdAt user { name } } } } }",
                                                      {"id": a.target[0]})["issue"])
    in_scope(issue, a)
    if a.json:
        print(json.dumps(issue, indent=1))
        return
    print(issue_line(issue))
    blockers = [f"{r['issue']['identifier']} ({r['issue']['state']['name']})" for r in issue.get("inverseRelations", {}).get("nodes", []) if r["type"] == "blocks"]
    if blockers:  # --blocked-by sets the whole list, so a caller needs these before changing it
        print("blocked by: " + ", ".join(blockers))
    if a.body:
        print_body(issue["description"])
    if a.comments:
        for c in sorted(issue["comments"]["nodes"], key=lambda c: c["createdAt"]):
            print(f"--- {c['user']['name']} {c['createdAt'][:10]}")
            print(c["body"])

def verb_list(a):
    if not a.parent and not a.search:
        sys.exit('list needs --parent <id> or --search "<terms>"')
    f = {"project": {"id": {"eq": linear_project(a.repo)["id"]}}}
    if a.parent:
        f["parent"] = {"id": {"eq": a.parent}}
    if a.search:
        f["searchableContent"] = {"contains": a.search}
    if a.state:
        f["state"] = {"name": {"eqIgnoreCase": a.state}}
    if a.label:
        f["labels"] = {"some": fc_labels.list_filter(a.label, fc_labels.types_map(a.repo))}
    query = "query($after: String, $f: IssueFilter) { issues(first: 100, after: $after, filter: $f) { pageInfo { hasNextPage endCursor } nodes { identifier title state { name } labels { nodes { name } } } } }"
    nodes, after = [], None
    while True:
        page = fc_linear.linear_gql(query, {"after": after, "f": f})["issues"]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    if a.json:
        print(json.dumps(nodes, indent=1))
        return
    for n in nodes:
        print(issue_line(n))

TASK_IDS = "query($after: String, $f: IssueFilter) { issues(first: 100, after: $after, filter: $f) { pageInfo { hasNextPage endCursor } nodes { description } } }"

def used_task_ids(a):
    """Every task id already taken in the project: the `id:` of each compose block on any issue of the Linear project, whatever linear.json's
    stories scope (one paged query, filtered by Linear to the issues that carry a block), plus the ids this project's results/ holds."""
    f = {"project": {"id": {"eq": linear_project(a.repo)["id"]}}, "description": {"contains": "```compose"}}
    used, after = set(), None
    while True:
        page = fc_linear.linear_gql(TASK_IDS, {"after": after, "f": f})["issues"]
        for n in page["nodes"]:
            for block in fc_tasks.TASK_BLOCK.finditer(n["description"] or ""):
                m = re.search(r"(?m)^\s*id:\s*(\S+)", block.group(1))
                if m:
                    used.add(m.group(1))
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    results = fc_core.project_dir(a) / "results"
    used |= {p.stem for p in results.glob("*.json") if p.stem.isdigit()} if results.is_dir() else set()
    return used

def verb_next_task_id(a):
    """Print the next free task id: one above the highest taken, so ids stay monotonic and none is reused."""
    used = used_task_ids(a)
    nums = [int(i) for i in used if i.isdigit()]
    print(f"{max(nums, default=0) + 1:03d}")

def issue_labels(issue):
    """The names of the labels on an issue as Linear returned it: what `fc_layout.laid_out` reads the ticket's level from."""
    return [l["name"] for l in ((issue.get("labels") or {}).get("nodes") or [])]

def verb_create(a):
    if not a.title:
        sys.exit("create needs --title")
    if why := fc_core.long_title(a.title):
        sys.exit(f"create: {why}")
    p = linear_project(a.repo)
    team = fc_linear.linear_gql("query($id: String!) { team(id: $id) { name states { nodes { id name } } %s } }" % fc_labels.TEAM_LABELS,
                                {"id": p["team"]["id"]})["team"]
    inp = {"teamId": p["team"]["id"], "projectId": p["id"], "title": a.title, "description": fc_layout.laid_out(one_block("", read_body(a)), ids(a.label))}
    inp.update(state_and_labels(team, a, fc_labels.types_map(a.repo)))
    inp.update(priority(a))
    if a.parent:
        inp["parentId"] = fc_linear.linear_issue(a.parent)["id"]
    try:
        issue = fc_linear.linear_gql("mutation($in: IssueCreateInput!) { issueCreate(input: $in) { issue { id identifier } } }", {"in": inp})["issueCreate"]["issue"]
    except OSError as e:  # the connection or the server failed: Linear may have committed the issue, a refusal (RuntimeError) it did not
        raise RuntimeError(f"{e}; the issue may have been created, so find it with `compose linear list --search \"{a.title}\"` before running create again") from e
    print(f"created {issue['identifier']}")  # the id is out before the blockers are linked: a failure while linking must not hide it
    if blockers := ids(a.blocked_by):  # repeated or comma-separated: every one is a blocker
        linked = []
        try:
            fc_linear.linear_blocked_by({"id": issue["id"]}, blockers, linked=linked)
        except (RuntimeError, OSError) as e:
            missing = [b for b in blockers if b not in linked]
            raise RuntimeError(f"{issue['identifier']} is created but not all its blockers are linked: {e}\n"
                               f"linked: {', '.join(linked) or 'none'}; missing: {', '.join(missing)}; finish with "
                               f"`compose linear update {issue['identifier']} --add-blocked-by {','.join(missing)}`, and do not run create again") from e

def ids(values):
    """The issue ids in repeated or comma-separated flag values, empty ones dropped."""
    return [x.strip() for v in values or () for x in v.split(",") if x.strip()]

def verb_update(a):
    if not a.target:
        sys.exit("update needs an issue id")
    writes = writes_body(a, "update")
    add, drop = ids(a.add_blocked_by), ids(a.drop_blocked_by)
    if not (writes or a.title or a.parent or a.state or a.label or a.add_label or a.drop_label or a.priority is not None or a.blocked_by is not None or add or drop):
        sys.exit("update needs --title, --state, --parent, --label, --add-label, --drop-label, --priority, --blocked-by, --add-blocked-by, --drop-blocked-by, "
                 "or a body: --section, --append, or --description with --file or stdin")
    if a.title and (why := fc_core.long_title(a.title)):
        sys.exit(f"update: {why}")
    if a.blocked_by is not None and (add or drop):
        sys.exit("update takes --blocked-by (the whole list) or --add-blocked-by/--drop-blocked-by, not both")
    prio = priority(a)  # a bad value refuses before the issue is read
    issue = fc_linear.linear_gql("query($id: String!) { issue(id: $id) { id identifier description %s project { id name } team { name states { nodes { id name } } %s } } }"
                                 % (fc_labels.ISSUE_LABELS, fc_labels.TEAM_LABELS), {"id": a.target[0]})["issue"]
    in_scope(issue, a)
    inp = {}
    if a.title:
        inp["title"] = a.title
    if a.parent:
        inp["parentId"] = fc_linear.linear_issue(a.parent)["id"]
    types = fc_labels.types_map(a.repo)
    inp.update(state_and_labels(issue["team"], a, types))
    inp.update(label_changes(issue["team"], a, (issue.get("labels") or {}).get("nodes") or [], types))
    inp.update(prio)
    same = False
    if writes:
        body = fc_layout.laid_out(new_body(a, issue["description"]), issue_labels(issue))
        same = body.strip() == (issue["description"] or "").strip()  # a write of what is already there changes nothing: Linear keeps the old text
        if not same:
            inp["description"] = body
    if inp:  # empty only when the blockers are the one change
        fc_linear.linear_gql("mutation($id: String!, $in: IssueUpdateInput!) { issueUpdate(id: $id, input: $in) { success } }", {"id": issue["id"], "in": inp})
        if "description" in inp:
            await_write(lambda: fc_linear.linear_gql("query($id: String!) { issue(id: $id) { description } }", {"id": issue["id"]})["issue"]["description"], issue["description"], inp["description"])
    dropped = []
    if a.blocked_by is not None:
        dropped = fc_linear.linear_blocked_by(issue, ids(a.blocked_by))
    elif add or drop:
        fc_linear.linear_blocked_by(issue, None, add=add, drop=drop)
    if same and not (inp or a.blocked_by is not None or add or drop):
        print(f"unchanged {issue['identifier']}: the body already reads as written, so nothing was sent")
    else:
        print(f"updated {issue['identifier']}")
    if note := block_kept(a, issue["description"]):
        print(note)
    if dropped:  # --blocked-by sets the whole list: say what it took away, in case the caller meant to add
        print(f"no longer blocked by: {', '.join(dropped)} (--add-blocked-by adds and keeps the rest)")

def verb_comment(a):
    if not a.target:
        sys.exit("comment needs an issue id")
    if len(a.target) > 1:  # words after the id were once dropped silently while the command waited on stdin
        sys.exit("comment takes the body from --file <path> or stdin, not from the command line: "
                 f"compose linear comment {a.target[0]} --file <path>")
    issue = fc_linear.linear_gql("query($id: String!) { issue(id: $id) { id identifier project { id name } } }",
                                 {"id": a.target[0]})["issue"]
    in_scope(issue, a)
    body = read_body(a).strip()
    if not body:
        sys.exit("comment needs a body: --file <path>, or stdin")
    fc_linear.linear_comment(issue, body)
    print(f"commented on {issue['identifier']}")

def linked_ids(proj):
    """The Linear issues a compose project is built on: its linear.json feature, stories, and design epic."""
    try:
        link = json.loads((proj / "linear.json").read_text())
    except (OSError, ValueError):
        return set()
    return {i for i in [link.get("feature"), link.get("design_epic"), *(link.get("stories") or [])] if isinstance(i, str)}

def under_posture(repo, issue=None, project=None):
    """The name and mode of the compose project under away or yolo that the write touches, or None. With `issue` (its identifier and
    its ancestors' identifiers), the projects built on that issue or one of its ancestors count; when none is, the project the command
    names (--project), else the one whose worktree the command runs in, counts. Without an issue, as for a reconcile of the whole
    Linear project, every project counts."""
    projects = sorted(fc_core.all_projects(Path(repo).resolve()).items())
    project = project or next((n for n, (wt, _) in projects if Path(wt).resolve() == Path(repo).resolve()), None)
    if issue:
        owners = [(n, p) for n, (_, p) in projects if linked_ids(p) & set(issue)]
        scope = owners or [(n, p) for n, (_, p) in projects if n == project] or ([] if project else [(n, p) for n, (_, p) in projects])
    else:
        scope = [(n, p) for n, (_, p) in projects]
    for name, proj in scope:
        if y := fc_tasks.yolo_state(proj):
            return name, y["mode"]

def lineage(issue):
    """The issue's identifier and its ancestors' identifiers, nearest first."""
    out = []
    while issue:
        out.append(issue["identifier"])
        issue = issue.get("parent")
    return out

CANCEL_GO = '(canceled on the go of the product owner, {date}; said: "{quote}")'  # fc_decide.CONFIRMED's shape, for a cancel

def verb_cancel(a):
    if not a.target:
        sys.exit("cancel needs an issue id")
    if len(a.target) > 1:
        sys.exit("cancel takes the reason from --file <path> or stdin, not from the command line: "
                 f"compose linear cancel {a.target[0]} --file <path>")
    scope = "query($id: String!) { issue(id: $id) { id identifier title project { id name } } }"
    issue = fc_linear.linear_gql(scope.replace("project {", "parent { identifier parent { identifier parent { identifier } } } project {"),
                                 {"id": a.target[0]})["issue"]
    quote = " ".join((getattr(a, "quote", None) or "").split())
    if (posture := under_posture(a.repo, lineage(issue), getattr(a, "project", None))) and not quote:
        sys.exit(f"{posture[0]} is under {posture[1]}: a cancel changes Linear only on the product owner's own go; pass their words "
                 f'(`--quote "<their message, verbatim>"`), or cancel it once {posture[1]} is off')
    in_scope(issue, a)
    reason = read_body(a).strip()
    if not reason:
        sys.exit("cancel needs a reason: --file <path>, or stdin")
    if quote:  # the product owner's go, logged on the issue as `decide` logs a confirmed line: their words, verbatim
        reason += "\n\n" + CANCEL_GO.format(date=time.strftime("%Y-%m-%d"), quote=quote.replace('"', "'"))
    if a.into:
        other = fc_linear.linear_gql(scope, {"id": a.into})["issue"]
        in_scope(other, a)
        reason = fc_linear.MERGED.format(id=other["identifier"], title=other["title"]) + "\n" + reason
    if fc_linear.linear_cancel(issue["identifier"], reason, a.into):
        print(f"canceled {issue['identifier']}")
    else:
        print(f"{issue['identifier']} is already Canceled: nothing written")

def verb_labels(a):
    """List the team's labels, a group's children as `Group > Name`, or create one: `--create "Type > Story"` under that group.
    A label that would duplicate one the team has (the `Type` group's child of that name included) is refused."""
    p = linear_project(a.repo)
    nodes = fc_linear.linear_gql("query($id: String!) { team(id: $id) { %s } }" % fc_labels.TEAM_LABELS, {"id": p["team"]["id"]})["team"]["labels"]["nodes"]
    if not a.create:
        for name in sorted(fc_labels.qualified(l) for l in nodes if not l.get("isGroup")):
            print(name)
        return
    group, child = fc_labels.split(a.create)
    if not child:
        sys.exit("labels --create needs a name: Story, or \"Type > Story\" for a group's child")
    if same := fc_labels.duplicate(nodes, a.create):
        sys.exit(f"team {p['team']['name']} already has label '{fc_labels.qualified(same)}': nothing created")
    inp, parent = {"name": child, "teamId": p["team"]["id"]}, None
    if group:
        parent = next((l for l in nodes if l.get("isGroup") and l["name"].lower() == group.lower()), None)
        if not parent:
            sys.exit(f"team {p['team']['name']} has no label group '{group}': make the group in Linear first")
        inp["parentId"] = parent["id"]
    fc_linear.linear_gql("mutation($in: IssueLabelCreateInput!) { issueLabelCreate(input: $in) { success } }", {"in": inp})
    print(f"created label {fc_labels.qualified({'name': child, 'parent': parent})}")

def verb_doc(a):
    if a.target == ["create"]:
        if not a.title:
            sys.exit("doc create needs --title")
        p = linear_project(a.repo)
        doc = fc_linear.linear_gql("mutation($in: DocumentCreateInput!) { documentCreate(input: $in) { document { url } } }",
                                   {"in": {"title": a.title, "content": read_body(a), "projectId": p["id"]}})["documentCreate"]["document"]
        print(doc["url"])
    elif a.target == ["list"]:
        p, after, docs = linear_project(a.repo), None, []
        while True:
            page = fc_linear.linear_gql("query($id: String!, $after: String) { project(id: $id) { documents(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { id title updatedAt url } } } }",
                                        {"id": p["id"], "after": after})["project"]["documents"]
            docs += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        if a.json:
            print(json.dumps([{"id": d["id"], "updatedAt": d["updatedAt"], "title": d["title"], "url": d.get("url") or ""} for d in docs]))
        else:
            for d in docs:
                print(f"{d['id']} {d['updatedAt'][:10]} {d['title']}")
    elif len(a.target) == 2 and a.target[0] == "update":
        writes = writes_body(a, "doc update")
        if not writes and not a.title:
            sys.exit("doc update needs --title, --section, --append, or --description")
        doc = fc_linear.linear_gql("query($id: String!) { document(id: $id) { id title content url project { id name } } }",
                                   {"id": a.target[1]})["document"]
        in_scope(doc, a, f"document {doc['title']}")
        inp = {"title": a.title} if a.title else {}
        if writes:
            inp["content"] = new_body(a, doc["content"])
        fc_linear.linear_gql("mutation($id: String!, $in: DocumentUpdateInput!) { documentUpdate(id: $id, input: $in) { success } }", {"id": doc["id"], "in": inp})
        if writes:
            await_write(lambda: fc_linear.linear_gql("query($id: String!) { document(id: $id) { content } }", {"id": doc["id"]})["document"]["content"], doc["content"], inp["content"])
        print(f"updated {doc['url']}")
    elif len(a.target) == 2 and a.target[0] == "get":
        doc = fc_linear.linear_gql("query($id: String!) { document(id: $id) { title content url } }",
                                   {"id": a.target[1]})["document"]
        print(doc["title"])
        print_body(doc["content"])
    else:
        sys.exit("doc takes create, list, get <id>, or update <id>")

def cmd_linear(a):
    if not fc_config.LINEAR_KEY:
        sys.exit(fc_config.needs_linear_key("compose linear"))
    if a.action != "update" and (getattr(a, "add_label", None) or getattr(a, "drop_label", None)):
        sys.exit("--add-label and --drop-label change an existing issue: compose linear update <id> takes them, and create takes --label")
    if a.action != "update" and (getattr(a, "add_blocked_by", None) or getattr(a, "drop_blocked_by", None)):
        sys.exit("--add-blocked-by and --drop-blocked-by change an existing issue: compose linear update <id> takes them, and create takes --blocked-by")
    if a.action != "cancel" and getattr(a, "quote", None):
        sys.exit("--quote is the product owner's go for a cancel: compose linear cancel <id> --quote \"<their words>\" --file <reason> takes it")
    try:
        globals()[f"verb_{a.action.replace('-', '_')}"](a)
    except (RuntimeError, OSError) as e:
        sys.exit(f"linear {a.action}: {e}")
    finally:
        if a.action in ("create", "update", "comment", "cancel"):  # the verbs that can change an issue
            drop_get_cache(a)
