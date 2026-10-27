import json, re, sys
from pathlib import Path
from typing import Callable, Optional
import fc_core, fc_config, fc_linearapi, fc_plan, fc_tasks


# A connection costs its `first` times what it holds, and the API key's hour is 3,000,000 points shared by the whole fleet: 50 relations and 20
# labels keep a page of 50 children near 56,000 points where 250 relations and the default 50 labels cost about 650,000.
LINEAR_TREE_FIELDS = """identifier title description state { name } parent { identifier } labels(first: 20) { nodes { name } }
  inverseRelations(first: 50) { nodes { type issue { identifier state { name } labels(first: 20) { nodes { name } } } } }"""
LINEAR_FEATURE = "query($id: String!) { issue(id: $id) { %s } }" % LINEAR_TREE_FIELDS
LINEAR_CHILDREN = """query($id: String!, $after: String) { issue(id: $id) { children(first: 50, after: $after) {
  pageInfo { hasNextPage endCursor } nodes { %s } } } }""" % LINEAR_TREE_FIELDS

def linear_pages(query, variables, *path):
    """Every node of the connection at `path` in the query's answer, all pages: the query pages on `$after`."""
    nodes, after = [], None
    while True:
        page = linear_gql(query, {**variables, "after": after})
        for key in path:
            page = page[key]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]

def linear_children(ident):
    """Every child of an issue with the sync fields, all pages."""
    return linear_pages(LINEAR_CHILDREN, {"id": ident}, "issue", "children")

def linear_sync_payload(proj):
    """The payload `sync` takes on stdin, read from Linear (read-only) for the feature in linear.json: the feature, its
    children, and theirs. A project scoped to stories fetches only those stories' children. A `design_epic` in linear.json adds
    that epic's features, their stories, and the stories' tasks."""
    link = json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}
    if not link.get("feature"):
        sys.exit(f'{proj / "linear.json"}: needs {{"feature": "<issue id, e.g. ENG-123>"}}, the Linear feature this project builds')
    if not fc_config.LINEAR_KEY:
        sys.exit(f"{fc_config.needs_linear_key('sync')}; "
                 f"or pipe the JSON to compose sync --stdin --project {proj.name} ({fc_tasks.SYNC_SHAPE})")
    stories = set(link.get("stories") or [])
    try:
        feature = linear_gql(LINEAR_FEATURE, {"id": link["feature"]})["issue"]
        if not feature:
            sys.exit(f"sync: Linear has no issue {link['feature']}")
        issues = []
        for child in linear_children(link["feature"]):
            if stories and child["identifier"] not in stories:
                continue
            issues += [child, *linear_children(child["identifier"])]
        for feature_issue in linear_children(link["design_epic"]) if link.get("design_epic") else []:
            issues.append(feature_issue)
            for story in linear_children(feature_issue["identifier"]):
                issues += [story, *linear_children(story["identifier"])]
    except (RuntimeError, OSError) as e:  # urllib errors name the status, never the key
        sys.exit(f"sync: Linear failed ({str(e)[:200]}); compose sync --stdin still works ({fc_tasks.SYNC_SHAPE})")
    return {"feature": {"id": feature["identifier"], "title": feature["title"], "description": feature["description"] or ""},
            "issues": [{"id": i["identifier"], "title": i["title"], "description": i["description"] or "", "status": i["state"]["name"],
                        "parentId": i["parent"]["identifier"] if i["parent"] else None,
                        "labels": [l["name"] for l in i["labels"]["nodes"]],
                        "blockedBy": [{"id": r["issue"]["identifier"], "status": r["issue"]["state"]["name"],
                                       "labels": [l["name"] for l in r["issue"]["labels"]["nodes"]]}
                                      for r in i["inverseRelations"]["nodes"] if r["type"] == "blocks"]}
                       for i in issues]}


# ---------- Linear: the CLI keeps the state table true, so the team lead spends no model turn on it ----------
# Every call goes through fc_linearapi: paced with every other compose process on the machine, and a rate limit holds the
# fleet as long as Linear says. Every call here is best effort: a Linear outage prints one line and the run goes on.
LINEAR_ISSUE = """query($id: String!) { issue(id: $id) { id identifier description state { name type }
  team { states { nodes { id name type } } } parent { identifier } children { nodes { identifier state { name type } } } } }"""
_linear_warned = []

WRITES = [0]  # the mutations this process sent: a caller tells by it whether a step wrote to Linear

def linear_gql(query, variables):
    """fc_linearapi.linear_gql, the one name every read and write here goes through (and a test replaces)."""
    if query.lstrip().startswith("mutation"):
        WRITES[0] += 1
    return fc_linearapi.linear_gql(query, variables)

def linear_issue(ident):
    issue = linear_gql(LINEAR_ISSUE, {"id": ident})["issue"]
    if not issue:
        raise RuntimeError(f"no issue {ident}")
    return issue

def linear_move(issue, state):
    """Move an issue to the team state named `state` (case-insensitive). No-op when it is already there."""
    if (issue["state"]["name"] or "").lower() == state.lower():
        return False
    match = [n for n in issue["team"]["states"]["nodes"] if n["name"].lower() == state.lower()]
    if not match:
        raise RuntimeError(f"{issue['identifier']}: the team has no {state!r} state")
    linear_gql("mutation($id: String!, $s: String!) { issueUpdate(id: $id, input: {stateId: $s}) { success } }",
               {"id": issue["id"], "s": match[0]["id"]})
    fc_core.say(f"linear {issue['identifier']} -> {state}")
    return True

def linear_comment(issue, body):
    linear_gql("mutation($id: String!, $b: String!) { commentCreate(input: {issueId: $id, body: $b}) { success } }",
               {"id": issue["id"], "b": body})

MERGED = "Merged into {id} ({title})."

def linear_cancel(ident: str, comment: str, into: Optional[str] = None, done: tuple = (), on_step: Optional[Callable] = None) -> bool:
    """Cancel an issue with a reason. The comment goes first, then the duplicate relation when `into` is given, and the move to the
    team's canceled state goes last. False, nothing written, when it is already canceled; a finished or reasonless cancel raises.
    A caller resuming after a failure names the steps already written in `done` ("comment", "relation"); `on_step` hears each step as it lands."""
    issue = linear_issue(ident)
    state = issue["state"]
    if state["type"] == "canceled":
        return False
    if state["type"] == "completed":
        raise RuntimeError(f"{issue['identifier']} is {state['name']}: a finished ticket is never canceled")
    if "comment" not in done and not (comment or "").strip():
        raise RuntimeError("a cancel needs a reason comment")
    target = [n for n in issue["team"]["states"]["nodes"] if n["type"] == "canceled" and (n["name"] or "").lower() in fc_tasks.CANCELED]
    if not target:
        raise RuntimeError(f"{issue['identifier']}: the team has no canceled state")
    other = linear_issue(into) if into else None
    if other and other["id"] == issue["id"]:
        raise RuntimeError(f"{issue['identifier']} cannot be merged into itself")
    if other and other["state"]["type"] == "canceled":
        raise RuntimeError(f"{other['identifier']} is {other['state']['name']}: a ticket is merged only into an open or finished one")
    if "comment" not in done:
        linear_comment(issue, comment.strip())
        on_step and on_step("comment")
    if other and "relation" not in done:
        linear_gql("mutation($id: String!, $o: String!) { issueRelationCreate(input: {issueId: $id, relatedIssueId: $o, type: duplicate}) { success } }",
                   {"id": issue["id"], "o": other["id"]})
        on_step and on_step("relation")
    linear_gql("mutation($id: String!, $s: String!) { issueUpdate(id: $id, input: {stateId: $s}) { success } }",
               {"id": issue["id"], "s": target[0]["id"]})
    return True

def linear_ready():
    """True when the workspace has a Linear key; otherwise say once how to set it, and the caller skips Linear."""
    if fc_config.LINEAR_KEY:
        return True
    if not _linear_warned:
        _linear_warned.append(1)
        fc_core.say(f"linear skipped: workspace {fc_config.WORKSPACE!r} has no Linear key; set it: pbpaste | compose workspace key {fc_config.WORKSPACE} --linear")
    return False

def linear_note(meta, body):
    """A routine comment on a task's issue (held, released). A failure is said and never stops the command."""
    if not meta or not meta.get("linear") or not linear_ready():
        return
    try:
        linear_comment(linear_issue(meta["linear"]), body)
    except Exception as e:  # noqa: BLE001
        fc_core.say(f"linear {meta['linear']}: comment failed ({str(e)[:200]})")

LINEAR_BLOCKERS = "query($id: String!) { issue(id: $id) { inverseRelations { nodes { id type issue { id identifier } } } } }"

def linear_blocked_by(issue, want, add=(), drop=()):
    """Make exactly the issues in `want` (identifiers) block `issue`, or with `want` None its blockers plus `add` less `drop`:
    creates first, then deletes, so a failed create never leaves it under-blocked. Returns the identifiers it stopped blocking it."""
    rels = [r for r in linear_gql(LINEAR_BLOCKERS, {"id": issue["id"]})["issue"]["inverseRelations"]["nodes"] if r["type"] == "blocks"]
    have = {r["issue"]["identifier"]: r["id"] for r in rels}
    if want is None:
        want = [i for i in have if i not in drop] + [i for i in add if i not in have]
    for ident in want:
        if ident not in have:
            linear_gql("mutation($a: String!, $b: String!) { issueRelationCreate(input: {issueId: $a, relatedIssueId: $b, type: blocks}) { success } }",
                       {"a": linear_issue(ident)["id"], "b": issue["id"]})
    for ident, rid in have.items():
        if ident not in want:
            linear_gql("mutation($id: String!) { issueRelationDelete(id: $id) { success } }", {"id": rid})
    return [i for i in have if i not in want]

def linear_restore(ident, fields):
    """Put an issue's title, description, state, and blockers back as `fields` names them."""
    issue = linear_issue(ident)
    text = {k: fields[k] for k in ("title", "description") if k in fields}
    if text:
        linear_gql("mutation($id: String!, $in: IssueUpdateInput!) { issueUpdate(id: $id, input: $in) { success } }", {"id": issue["id"], "in": text})
    if "status" in fields:
        linear_move(issue, fields["status"])
    if "blockedBy" in fields:
        linear_blocked_by(issue, fields["blockedBy"])

def rollup_state(children):
    """The state table for a story or epic: Done when every child is Done, In Review when every child is In Review or
    Done, In Progress once any child started, else None (leave it). Canceled children do not count."""
    children = [c for c in children if c["state"].get("type") != "canceled"]
    names = [(c["state"]["name"] or "").lower() for c in children]
    if names and all(n == "done" for n in names):
        return "Done"
    if names and all(n in ("in review", "done") for n in names):
        return "In Review"
    if any(c["state"]["type"] in ("started", "completed") for c in children):
        return "In Progress"
    return None

def linear_roll_up(proj, ident, verified=False, moved=None):
    """Roll `ident` and its parents up the table. `moved` ({identifier: state name}) holds the states this step just set;
    they stand over the read, which can still return a child's state from before its move. The feature reaches In Review
    only when verify passed, and the CLI never marks the feature Done (that needs the project branch merged)."""
    feature = json.loads((proj / "linear.json").read_text()).get("feature")
    moved = dict(moved or {})
    while ident:
        issue = linear_issue(ident)
        states = {n["name"].lower(): n for n in issue["team"]["states"]["nodes"]}
        target = rollup_state([{**c, "state": states[moved[c["identifier"]].lower()]} if c["identifier"] in moved else c
                               for c in issue["children"]["nodes"]])
        if issue["identifier"] == feature:
            if target in ("In Review", "Done"):
                target = "In Review" if verified else "In Progress"
            if target:
                linear_move(issue, target)
            refresh_ready(proj)
            return
        if target:
            linear_move(issue, target)
            moved[issue["identifier"]] = target
        ident = (issue.get("parent") or {}).get("identifier")
    refresh_ready(proj)

def linear_event(proj, meta, event, result=None):
    """One task event through the state table: `dispatched` moves the task In Progress, `passed` and `hand_done` In Review
    with a comment, `failed` keeps it In Progress with the failure as a comment; then its story and the feature roll up."""
    if not meta.get("linear"):
        return
    if not linear_ready():
        return
    try:
        issue = linear_issue(meta["linear"])
        state = "In Review" if event in ("passed", "hand_done") else "In Progress"
        linear_move(issue, state)
        if meta.get("id"):  # the stored tasks say so from here on: what waits on this task reads it there, until the next sync
            fc_tasks.set_cached_state(proj, meta["id"], state)
        if event == "passed":
            linear_comment(issue, f"Passed: branch `{result['branch']}`, {result['attempts']} attempt(s), ${result['cost']:.4f}, model `{result['model']}`.")
        elif event == "hand_done":
            linear_comment(issue, f"Finished by hand: {result['note']}")
        elif event == "unproven":
            last = (result.get("log") or [{}])[-1]
            why = f" Why: {last['error']}." if "proof" in result and last.get("error") else ""
            linear_comment(issue, f"Unproven: branch `{result['branch']}` passes its tests after {result['attempts']} attempt(s), ${result['cost']:.4f}, model `{result['model']}`; its regression test is not proven yet.{why}")
        elif event != "dispatched":
            last = (result.get("log") or [{}])[-1]
            tail = (last.get("test_output") or "").strip().splitlines()
            why = last.get("contradiction") or last.get("error") or (tail[-1] if tail else "no detail")
            linear_comment(issue, f"{result['status']} after {result['attempts']} attempt(s) (${result['cost']:.4f}, model `{result['model']}`): {why[:400]}")
        parent = (issue.get("parent") or {}).get("identifier")
        if parent:
            linear_roll_up(proj, parent, moved={issue["identifier"]: state})
    except Exception as e:  # noqa: BLE001  Linear never stops a run
        fc_core.say(f"linear {meta.get('linear')}: update failed ({str(e)[:200]}); the run goes on")

def linear_merged(feature, metas, project_merged, scope=None):
    """clean's step through the state table: every task proven merged moves to Done, its story rolls up, and the feature is
    Done once every story is and the project branch is merged; otherwise the feature is left as verify put it. A merged
    project rolls up every story of the feature in `scope` (all of them when None), so a task moved outside clean counts. A Canceled
    task stays Canceled. With a feature and nothing proven merged one line says the states are unchanged; without a key
    one line says so; a Linear failure is said and never stops the clean."""
    if not feature and not metas:
        return
    if not linear_ready():
        return
    if not (metas or (feature and project_merged)):
        fc_core.say("linear  nothing proven merged; states unchanged")
        return
    try:
        stories = []
        for m in metas:
            issue = linear_issue(m["linear"])
            if issue["state"].get("type") == "canceled":
                continue
            linear_move(issue, "Done")
            parent = (issue.get("parent") or {}).get("identifier")
            if parent and parent != feature and parent not in stories:
                stories.append(parent)
        if feature and project_merged:
            stories += [c["identifier"] for c in linear_issue(feature)["children"]["nodes"]
                        if c["identifier"] not in stories and c["state"].get("type") != "canceled" and (not scope or c["identifier"] in scope)]
        for s in stories:
            issue = linear_issue(s)
            target = rollup_state(issue["children"]["nodes"])
            if target:
                linear_move(issue, target)
        if feature and project_merged:
            issue = linear_issue(feature)
            if rollup_state(issue["children"]["nodes"]) == "Done":
                linear_move(issue, "Done")
    except Exception as e:  # noqa: BLE001
        fc_core.say(f"linear: moving merged work to Done failed ({str(e)[:200]}); the states are yours to set")

def linear_reconcile(proj, verified=False):
    """The end of a run and verify PASS: every story under the feature rolls up from its tasks again, then the feature from
    its stories, so a roll-up a task event missed (parallel tasks, a read behind the write) is made true. A project scoped to
    stories re-rolls only those. The feature moves to In Review only when `verified`."""
    link = proj / "linear.json"
    if not link.exists() or not fc_config.LINEAR_KEY:
        return
    meta = json.loads(link.read_text())
    feature, scope = meta.get("feature"), set(meta.get("stories") or [])
    try:
        moved = {}
        for child in linear_issue(feature)["children"]["nodes"]:
            if scope and child["identifier"] not in scope:
                continue
            story = linear_issue(child["identifier"])
            target = rollup_state(story["children"]["nodes"])
            if target:
                linear_move(story, target)
                moved[story["identifier"]] = target
        linear_roll_up(proj, feature, verified=verified, moved=moved)
    except Exception as e:  # noqa: BLE001
        fc_core.say(f"linear {feature}: update failed ({str(e)[:200]})")

NEEDS_SHAPING = "needs-shaping"

def freed(b, its_feature, feature):
    """A blocker no longer holds back what it blocks: finished (fc_tasks.blocker_finished), or verified (In Review) under the same
    feature, whose stack the blocked work builds on (fc_plan.base_built)."""
    s, ls = b["state"]["name"] or "", [l["name"] for l in b["labels"]["nodes"]]
    return fc_tasks.blocker_finished(s, ls) or (feature is not None and its_feature == feature and fc_plan.base_built(s, ls))

def task_holds(issue, feature=None):
    """The blockers outside the issue that still hold back any of its children (a story's tasks; freed, with `feature` the story's
    feature), each named by its parent story (by itself when it has none), in first-seen order. A child read without its relations holds nothing."""
    kids = [c for c in ((issue.get("children") or {}).get("nodes") or ()) if ((c.get("state") or {}).get("name") or "").lower() not in fc_tasks.CANCELED]
    own, out = {c.get("identifier") for c in kids} | {issue.get("identifier")}, []
    for r in (r for c in kids for r in ((c.get("inverseRelations") or {}).get("nodes") or ())):
        b = r["issue"]
        who = (b.get("parent") or {}).get("identifier") or b["identifier"]
        if (r["type"] == "blocks" and b["identifier"] not in own and who not in own and who not in out
                and not freed(b, ((b.get("parent") or {}).get("parent") or {}).get("identifier"), feature)):
            out.append(who)
    return out

def ready_ids(issues, parent=None):
    """The identifiers, in input order, of the issues that can start now: state Todo or Backlog and every `blocks`
    blocker freed (Done or Canceled, In Review carrying the Design label, or In Review under the same `parent`: a story
    verified in its feature's stack frees its siblings), none of their tasks waiting on another story's (task_holds), and
    none carries the needs-shaping label."""
    return [i["identifier"] for i in issues
            if (i["state"]["name"] or "").lower() in ("todo", "backlog")
            and NEEDS_SHAPING not in [l["name"].lower() for l in i["labels"]["nodes"]]
            and all(freed(r["issue"], (r["issue"].get("parent") or {}).get("identifier"), parent)
                    for r in i["inverseRelations"]["nodes"] if r["type"] == "blocks")
            and not task_holds(i, parent)]

def open_questions(description):
    """The list items (`-` or `*` bullets) of the `## Open questions` section: an H2 heading with an optional suffix, running to the
    next H1 or H2 heading, so bullets under an H3 child count. An H3 `### Open questions` elsewhere does not. [] when the section
    is absent, empty or says None."""
    m = re.search(r"^##[ \t]+Open questions\b[^\n]*$(.*?)(?=^#{1,2}[ \t]|\Z)", description or "", re.M | re.S)
    items = [b.group(1) for b in map(re.compile(r"^\s*[-*]\s+(.*\S)").match, (m.group(1) if m else "").splitlines()) if b]
    return [i for i in items if i.strip().rstrip(".").lower() != "none"]

# The Ready marks and the delivery plan read a feature or epic as flat pages, never a nested tree: a connection costs its `first`
# times what it holds, so a page of 50 children with 25 relations of 10 labels stays near 20,000 points where the same tree
# nested two levels deep cost millions, most of the key's 3,000,000 points an hour, in one call.
READY_NODE = """fragment ReadyNode on Issue { id identifier title url description state { name } labels(first: 10) { nodes { id name } }
  inverseRelations(first: 25) { nodes { type issue { identifier state { name } labels(first: 10) { nodes { name } } parent { identifier parent { identifier } } } } } }"""
READY_ROOT = """id identifier title url description project { id } labels(first: 10) { nodes { name } }
  team { labels(first: 5, filter: { name: { eqIgnoreCase: "Ready" } }) { nodes { id name } } }
  parent { id identifier title url description project { id } labels(first: 10) { nodes { name } } }
  inverseRelations(first: 25) { nodes { type issue { identifier state { name } labels(first: 10) { nodes { name } } } } }"""
LINEAR_READY_ROOT = "query($id: String!) { issue(id: $id) { %s } }" % READY_ROOT
# ready_reads: what linear_mark_ready reads, for many features at once (Linear's id filter takes identifiers too).
LINEAR_READY_ROOTS = """query($ids: [ID!], $after: String) { issues(first: 50, after: $after, filter: { id: { in: $ids } }) {
  pageInfo { hasNextPage endCursor } nodes { %s } } }""" % READY_ROOT
LINEAR_READY_KIDS = """query($ids: [ID!], $after: String) { issues(first: 50, after: $after, filter: { parent: { id: { in: $ids } } }) {
  pageInfo { hasNextPage endCursor } nodes { ...ReadyNode parent { identifier } } } }
""" + READY_NODE
LINEAR_READY_GRANDKIDS = """query($ids: [ID!], $after: String) { issues(first: 50, after: $after, filter: { parent: { parent: { id: { in: $ids } } } }) {
  pageInfo { hasNextPage endCursor } nodes { ...ReadyNode parent { identifier } } } }
""" + READY_NODE
LINEAR_READY_CHILDREN = """query($id: String!, $after: String) { issue(id: $id) { children(first: 50, after: $after) {
  pageInfo { hasNextPage endCursor } nodes { ...ReadyNode } } } }
""" + READY_NODE
LINEAR_READY_GRANDCHILDREN = """query($id: ID!, $after: String) { issues(first: 50, after: $after, filter: { parent: { parent: { id: { eq: $id } } } }) {
  pageInfo { hasNextPage endCursor } nodes { ...ReadyNode parent { identifier } } } }
""" + READY_NODE

def linear_group(ident, uuid):
    """An issue's children (ReadyNode each), each with its own children under `children.nodes`: the children paged by the
    identifier, the grandchildren paged by the issue's uuid in one filtered query, and stitched together here."""
    nodes = linear_pages(LINEAR_READY_CHILDREN, {"id": ident}, "issue", "children")
    kids = {}
    for g in linear_pages(LINEAR_READY_GRANDCHILDREN, {"id": uuid}, "issues") if nodes else []:
        kids.setdefault(g["parent"]["identifier"], []).append(g)
    for n in nodes:
        n["children"] = {"nodes": kids.get(n["identifier"], [])}
    return nodes

def ready_reads(features):
    """What linear_mark_ready reads for each of `features` and their epics, in a few paged reads for all of them at once:
    {"roots": {feature: its root}, "groups": {feature or epic: its linear_group}}. A reconcile over every feature reads this
    once, not three reads per feature."""
    roots = {n["identifier"]: n for n in linear_pages(LINEAR_READY_ROOTS, {"ids": list(features)}, "issues")} if features else {}
    heads = {n["identifier"]: n["id"] for r in roots.values() for n in (r, r["parent"]) if n}
    kids = linear_pages(LINEAR_READY_KIDS, {"ids": list(heads.values())}, "issues") if heads else []
    grand = {}
    for g in linear_pages(LINEAR_READY_GRANDKIDS, {"ids": list(heads.values())}, "issues") if kids else []:
        grand.setdefault(g["parent"]["identifier"], []).append(g)
    groups = {h: [] for h in heads}
    for n in kids:
        n["children"] = {"nodes": grand.get(n["identifier"], [])}
        groups[n["parent"]["identifier"]].append(n)
    return {"roots": roots, "groups": groups}

def linear_mark_ready(feature, marked=None, reads=None):
    """Set the team's Ready label on the feature's children that can start now and take it off the rest, the same for the
    epic's children when the feature has one; one read per group, then one update per issue whose labels change. A story with
    no task that is not Canceled has nothing to start, so it is not ready; a feature needs no story. Returns the ready issues,
    the feature's children first. No story is ready while a feature that blocks this one is not built yet; a sibling verified
    (In Review) blocks nothing. An issue with open questions waits. `marked`, a set, names the features and epics whose
    children this run marked already: their group is skipped, and each group marked here is added, so a reconcile over
    every feature reads each epic once. `reads`, ready_reads' answer, stands in for the reads."""
    root = reads["roots"].get(feature) if reads else linear_gql(LINEAR_READY_ROOT, {"id": feature})["issue"]
    if not root:
        raise RuntimeError(f"no issue {feature}")
    ready_label = next((l["id"] for l in root["team"]["labels"]["nodes"] if l["name"].lower() == "ready"), None)
    if not ready_label:
        raise RuntimeError("team has no label 'Ready'; compose linear labels --create Ready makes it")
    update = "mutation($id: String!, $labels: [String!]!) { issueUpdate(id: $id, input: {labelIds: $labels}) { success } }"
    out = []
    waiting = any(r["type"] == "blocks" and not fc_plan.base_built(r["issue"]["state"]["name"] or "", [l["name"] for l in r["issue"]["labels"]["nodes"]])
                  for r in root["inverseRelations"]["nodes"])
    for node, stories in ((root, True), (root["parent"], False)):
        if not node or (marked is not None and node["identifier"] in marked):
            continue
        parent = node["identifier"]
        group = reads["groups"][parent] if reads else linear_group(parent, node["id"])
        cut = {n["identifier"] for n in group if any((c["state"]["name"] or "").lower() != "canceled" for c in n["children"]["nodes"])}
        asking = {n["identifier"] for n in group if open_questions(n.get("description"))}
        ready = {i for i in ready_ids(group, parent) if (i in cut or not stories) and i not in asking and not (stories and waiting)}
        for n in group:
            have = [l["id"] for l in n["labels"]["nodes"]]
            if n["identifier"] in ready:
                if ready_label not in have:
                    linear_gql(update, {"id": n["id"], "labels": have + [ready_label]})
                    fc_core.say(f"linear {n['identifier']} -> Ready")
                out.append({"id": n["identifier"], "title": n["title"]})
            elif ready_label in have:
                linear_gql(update, {"id": n["id"], "labels": [l for l in have if l != ready_label]})
                fc_core.say(f"linear {n['identifier']} -> not Ready")
        if marked is not None:
            marked.add(parent)
    return out

LINEAR_PROJECT_DOCS = """query($id: String!, $after: String) { project(id: $id) { documents(first: 50, after: $after) {
  pageInfo { hasNextPage endCursor } nodes { id title url } } } }"""
LINEAR_DOCUMENT = "query($id: String!) { document(id: $id) { id title url content } }"

def linear_plan(feature):
    """Keep the map's delivery plan document in step: create and link it once, then rewrite only its data sections. The map is
    the feature's epic when it has one (its features are the lanes, their stories each lane's), else the feature (one lane)."""
    root = linear_gql(LINEAR_READY_ROOT, {"id": feature})["issue"]
    parent = root["parent"]
    names = lambda n: [l["name"] for l in n["labels"]["nodes"]]
    epic = bool(parent) and "epic" in [x.lower() for x in names(parent)]
    story = lambda n: {"id": n["identifier"], "title": n["title"], "url": n["url"], "state": n["state"]["name"], "labels": names(n),
                       "blockers": [{"id": r["issue"]["identifier"], "state": r["issue"]["state"]["name"], "labels": names(r["issue"])}
                                    for r in n["inverseRelations"]["nodes"] if r["type"] == "blocks"]}
    lane = lambda n, kids: {"id": n["identifier"], "title": n["title"], "url": n["url"], "stories": [story(c) for c in kids]}
    map_ = parent if epic else root
    group = linear_group(map_["identifier"], map_["id"])
    lanes = [lane(n, n["children"]["nodes"]) for n in group if "feature" in [x.lower() for x in names(n)]] if epic else [lane(root, group)]
    title = f"{map_['identifier']}: delivery plan"
    found = next((d for d in linear_pages(LINEAR_PROJECT_DOCS, {"id": map_["project"]["id"]}, "project", "documents") if d["title"] == title), None)
    doc = linear_gql(LINEAR_DOCUMENT, {"id": found["id"]})["document"] if found else None
    if not doc:
        doc = linear_gql("mutation($title: String!, $content: String!, $project: String!) { documentCreate(input: {title: $title, content: $content, projectId: $project}) { success document { id title url content } } }",
                         {"title": title, "content": fc_plan.plan_skeleton(), "project": map_["project"]["id"]})["documentCreate"]["document"]
        linear_gql("mutation($id: String!, $description: String!) { issueUpdate(id: $id, input: {description: $description}) { success } }",
                   {"id": map_["id"], "description": fc_plan.with_plan_link(map_["description"], title, doc["url"])})
    content = doc["content"]
    for heading, text in fc_plan.plan_sections(lanes).items():
        content = fc_plan.replace_section(content, heading, text)
    if not fc_plan.same_markdown(content, doc["content"]):
        linear_gql("mutation($id: String!, $content: String!) { documentUpdate(id: $id, input: {content: $content}) { success } }", {"id": doc["id"], "content": content})

def ready_line(items):
    return "can start now: " + (", ".join(f"{i['id']} {i['title']}" for i in items) or "nothing")

def cmd_ready(a):
    """Mark the stories and features that can start now with the Ready label in Linear, and print them."""
    proj = fc_core.project_dir(a)
    if not fc_config.LINEAR_KEY:
        sys.exit(fc_config.needs_linear_key("ready"))
    feature = json.loads((proj / "linear.json").read_text()).get("feature")
    try:
        items = linear_mark_ready(feature)
        fc_core.atomic_write(proj / "results" / "ready.json", json.dumps(items, indent=2))
        print(ready_line(items))
        linear_plan(feature)
    except RuntimeError as e:
        sys.exit(f"ready: {e}")

def refresh_ready(proj, feature=None):
    """The best-effort refresh of the Ready marks after roll-ups, sync, and clean: mark what can start now and store the
    list in results/ready.json. `feature` defaults to the project's linear.json feature; a failure is one line, never raised."""
    if not feature:
        link = proj / "linear.json"
        feature = json.loads(link.read_text()).get("feature") if link.exists() else None
        if not feature:
            return
    if not linear_ready():
        return
    try:
        items = linear_mark_ready(feature)
    except Exception as e:  # noqa: BLE001
        fc_core.say(f"linear ready: {str(e)[:200]}")
        return
    if (proj / "results").exists():
        fc_core.atomic_write(proj / "results" / "ready.json", json.dumps(items, indent=2))
    try:
        linear_plan(feature)
    except Exception as e:  # noqa: BLE001
        fc_core.say(f"linear plan: {str(e)[:200]}")

def linear_feature_after(repo, proj):
    """The project this one was built after (`after` in verify.json, or its compose/ branch as review_base) gets its feature to
    block this project's feature in Linear: the link is added once and other blockers stand."""
    after = fc_core.project_after(proj)
    if not after:
        return
    link = proj / "linear.json"
    feature = json.loads(link.read_text()).get("feature") if link.exists() else None
    if not feature:
        return
    other = fc_core.all_projects(repo).get(after)
    other_link = other[1] / "linear.json" if other else None
    other_feature = json.loads(other_link.read_text()).get("feature") if other_link and other_link.exists() else None
    if not other_feature:
        fc_core.say(f"linear: no feature for project {after}, so {feature} is not linked")
        return
    have = {r["issue"]["identifier"] for r in linear_gql(LINEAR_BLOCKERS, {"id": feature})["issue"]["inverseRelations"]["nodes"] if r["type"] == "blocks"}
    if other_feature in have:
        return
    linear_gql("mutation($a: String!, $b: String!) { issueRelationCreate(input: {issueId: $a, relatedIssueId: $b, type: blocks}) { success } }",
               {"a": linear_issue(other_feature)["id"], "b": linear_issue(feature)["id"]})
    fc_core.say(f"linear {feature} blocked by {other_feature}")


def refresh_tasks(proj):
    """Before a run: read the feature and its tasks from Linear again into results/tasks.json, so a task body edited since the last
    sync reaches its implementer, and say each one that changed. Without a Linear key the stored tasks stand, and it says so."""
    stored = proj / "results" / "tasks.json"
    link = json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}
    if not link.get("feature"):  # nothing to read: the stored tasks stand
        return
    if not fc_config.LINEAR_KEY:
        if stored.exists():
            fc_core.say(f"no Linear key: run uses the tasks synced at {fc_tasks.linear_cache(proj).get('synced_at')}")
        return
    old = {t["id"]: t["body"] for t in fc_tasks.load_tasks(proj)} if stored.exists() else {}
    for t in fc_tasks.sync_tasks(proj, linear_sync_payload(proj)):
        if t["id"] in old and old[t["id"]] != t["body"]:
            fc_core.say(f"{t['id']} body changed on Linear since the last sync: refreshed")


def cmd_sync(a):
    """Store the feature and its tasks, read from Linear with the workspace key or piped in as JSON (--stdin), and list them."""
    proj = fc_core.project_dir(a)
    if a.stdin:
        try:
            data = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            sys.exit(f"sync: stdin is not JSON ({e}); expected {fc_tasks.SYNC_SHAPE}")
    else:
        data = linear_sync_payload(proj)
    tasks = fc_tasks.sync_tasks(proj, data)
    f = fc_tasks.linear_cache(proj)["feature"]
    print(f"{f['id']} {f['title']}: {len(tasks)} task(s) synced")
    for m in tasks:
        print(f"  {m['id']:4} {m['linear']:9} {m['linear_state'][:12]:12} {m['title'][:50]:50} after {m['after'] or '-'}"
              + (f"  waits on {', '.join(m['waits_on'])}" if m.get("waits_on") else ""))
    if not a.stdin:
        refresh_ready(proj)
        linear_feature_after(Path(a.repo).resolve(), proj)
