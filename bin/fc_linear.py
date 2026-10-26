import json, os, re, sys, threading, time, urllib.error, urllib.request
from pathlib import Path
from typing import Callable, Optional
import fc_core, fc_config, fc_plan, fc_tasks


# A connection costs its `first` times what it holds, and the API key's hour is 3,000,000 points shared by the whole fleet: 50 relations and 20
# labels keep a page of 50 children near 56,000 points where 250 relations and the default 50 labels cost about 650,000.
LINEAR_TREE_FIELDS = """identifier title description state { name } parent { identifier } labels(first: 20) { nodes { name } }
  inverseRelations(first: 50) { nodes { type issue { identifier state { name } labels(first: 20) { nodes { name } } } } }"""
LINEAR_FEATURE = "query($id: String!) { issue(id: $id) { %s } }" % LINEAR_TREE_FIELDS
LINEAR_CHILDREN = """query($id: String!, $after: String) { issue(id: $id) { children(first: 50, after: $after) {
  pageInfo { hasNextPage endCursor } nodes { %s } } } }""" % LINEAR_TREE_FIELDS

def linear_children(ident):
    """Every child of an issue with the sync fields, all pages."""
    nodes, after = [], None
    while True:
        page = linear_gql(LINEAR_CHILDREN, {"id": ident, "after": after})["issue"]["children"]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]

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
# The key is the active workspace's `linear_api_key`; it goes in a header,
# never argv or a results file. Every call is best effort: a Linear outage prints one line and the run goes on.
LINEAR_URL = "https://api.linear.app/graphql"
LINEAR_ISSUE = """query($id: String!) { issue(id: $id) { id identifier description state { name type }
  team { states { nodes { id name type } } } parent { identifier } children { nodes { identifier state { name type } } } } }"""
_linear_warned = []

LINEAR_RETRIES = 5     # waits before a rate-limited call gives up
LINEAR_MAX_WAIT = 300  # the doubling pause stops growing here
LINEAR_CALL_SECONDS = 120  # one call, its rate-limit waits and slow replies included, never takes longer: it fails naming the time, so no caller waits silently

class LinearRateLimited(RuntimeError):
    def __init__(self, message, wait):
        super().__init__(message)
        self.wait = wait

def _rate_limit_wait(headers, attempt):
    """Seconds to wait: Retry-After, else the sooner reset header (UTC epoch seconds or milliseconds), else a doubling pause."""
    h = headers or {}
    try:
        if h.get("Retry-After"):
            return max(1, float(h["Retry-After"]))
        resets = [float(h[k]) for k in ("X-RateLimit-Complexity-Reset", "X-RateLimit-Requests-Reset") if h.get(k)]
    except ValueError:
        resets = []
    resets = [r / 1000 if r > 1e11 else r for r in resets]
    return max(1, min(resets) - time.time()) if resets else min(LINEAR_MAX_WAIT, 10 * 2 ** attempt)

def _linear_post(query, variables, attempt):
    req = urllib.request.Request(os.environ.get("LINEAR_URL") or LINEAR_URL, data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": fc_config.LINEAR_KEY})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out, headers = json.load(r), getattr(r, "headers", None)
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read().decode(errors="replace"))
        except ValueError:
            out = None
        headers = e.headers
        e.close()
        if not (isinstance(out, dict) and out.get("errors")):
            if e.code == 429:
                raise LinearRateLimited("Linear rate limit (HTTP 429)", _rate_limit_wait(headers, attempt))
            raise
    if out.get("errors"):
        message = "; ".join(x.get("message", "?") for x in out["errors"])[:300]
        if any((x.get("extensions") or {}).get("code") == "RATELIMITED" for x in out["errors"]):
            raise LinearRateLimited(message, _rate_limit_wait(headers, attempt))
        raise RuntimeError(message)
    return out["data"]

def _within(seconds, fn):
    """fn() run in a thread of its own and given up on after `seconds`: a reply that trickles in keeps urlopen's per-read timeout from ever firing."""
    box = {}
    def work():
        try:
            box["out"] = fn()
        except BaseException as e:  # noqa: BLE001  re-raised in the caller
            box["err"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(max(0, seconds))
    if t.is_alive():
        raise RuntimeError(f"Linear gave no answer within {LINEAR_CALL_SECONDS}s; a write may still have gone through, so read the issue before repeating it")
    if "err" in box:
        raise box["err"]
    return box["out"]

def linear_gql(query, variables):
    """One GraphQL call, bounded by LINEAR_CALL_SECONDS. A rate-limited call did not run, so it waits as long as Linear says
    (while that fits in the time left) and runs again, saying so on stderr."""
    deadline = time.monotonic() + LINEAR_CALL_SECONDS
    for attempt in range(LINEAR_RETRIES + 1):
        try:
            return _within(deadline - time.monotonic(), lambda: _linear_post(query, variables, attempt))
        except LinearRateLimited as e:
            if e.wait > deadline - time.monotonic() or attempt == LINEAR_RETRIES:
                raise RuntimeError(f"{e} (Linear lets calls through again in about {int(e.wait)}s; run it again then)") from None
            print(f"Linear rate limit: waiting {int(e.wait)}s, then retry {attempt + 1} of {LINEAR_RETRIES}", file=sys.stderr)
            time.sleep(e.wait)

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

def linear_blocked_by(issue, want):
    """Make exactly the issues in `want` (identifiers) block `issue`: creates first, then deletes, so a failed create never leaves it under-blocked."""
    rels = [r for r in linear_gql(LINEAR_BLOCKERS, {"id": issue["id"]})["issue"]["inverseRelations"]["nodes"] if r["type"] == "blocks"]
    have = {r["issue"]["identifier"]: r["id"] for r in rels}
    for ident in want:
        if ident not in have:
            linear_gql("mutation($a: String!, $b: String!) { issueRelationCreate(input: {issueId: $a, relatedIssueId: $b, type: blocks}) { success } }",
                       {"a": linear_issue(ident)["id"], "b": issue["id"]})
    for ident, rid in have.items():
        if ident not in want:
            linear_gql("mutation($id: String!) { issueRelationDelete(id: $id) { success } }", {"id": rid})

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

def task_holds(issue):
    """The unfinished blockers outside the issue that hold back any of its children (a story's tasks), each named by its
    parent story (by itself when it has none), in first-seen order. A child read without its relations holds nothing."""
    kids = [c for c in ((issue.get("children") or {}).get("nodes") or ()) if ((c.get("state") or {}).get("name") or "").lower() not in fc_tasks.CANCELED]
    own, out = {c.get("identifier") for c in kids} | {issue.get("identifier")}, []
    for r in (r for c in kids for r in ((c.get("inverseRelations") or {}).get("nodes") or ())):
        b = r["issue"]
        who = (b.get("parent") or {}).get("identifier") or b["identifier"]
        if (r["type"] == "blocks" and b["identifier"] not in own and who not in own and who not in out
                and not fc_tasks.blocker_finished(b["state"]["name"], [l["name"] for l in b["labels"]["nodes"]])):
            out.append(who)
    return out

def ready_ids(issues):
    """The identifiers, in input order, of the issues that can start now: state Todo or Backlog and every `blocks`
    blocker finished (Done or Canceled, or In Review carrying the Design label), none of their tasks waiting on another
    story's (task_holds), and none carries the needs-shaping label."""
    return [i["identifier"] for i in issues
            if (i["state"]["name"] or "").lower() in ("todo", "backlog")
            and NEEDS_SHAPING not in [l["name"].lower() for l in i["labels"]["nodes"]]
            and all(fc_tasks.blocker_finished(r["issue"]["state"]["name"], [l["name"] for l in r["issue"]["labels"]["nodes"]])
                    for r in i["inverseRelations"]["nodes"] if r["type"] == "blocks")
            and not task_holds(i)]

def open_questions(description):
    """The list items (`-` or `*` bullets) of the `## Open questions` section: an H2 heading with an optional suffix, running to the
    next H1 or H2 heading, so bullets under an H3 child count. An H3 `### Open questions` elsewhere does not. [] when the section
    is absent, empty or says None."""
    m = re.search(r"^##[ \t]+Open questions\b[^\n]*$(.*?)(?=^#{1,2}[ \t]|\Z)", description or "", re.M | re.S)
    items = [b.group(1) for b in map(re.compile(r"^\s*[-*]\s+(.*\S)").match, (m.group(1) if m else "").splitlines()) if b]
    return [i for i in items if i.strip().rstrip(".").lower() != "none"]

LINEAR_READY_TREE = """query($id: String!) { issue(id: $id) { team { labels { nodes { id name } } }
  children { nodes { ...ReadyNode } } parent { children { nodes { ...ReadyNode } } }
  inverseRelations(first: 50) { nodes { type issue { identifier state { name } labels { nodes { name } } } } } } }
fragment ReadyNode on Issue { id identifier title description state { name } labels { nodes { id name } }
  children { nodes { identifier state { name } inverseRelations(first: 50) { nodes { type issue { identifier state { name } labels { nodes { name } } parent { identifier } } } } } }
  inverseRelations(first: 250) { nodes { type issue { identifier state { name } labels { nodes { name } } } } } }"""

def linear_mark_ready(feature):
    """Set the team's Ready label on the feature's children that can start now and take it off the rest, the same for the
    epic's children when the feature has one; one read, then one update per issue whose labels change. A story with no task
    that is not Canceled has nothing to start, so it is not ready; a feature needs no story. Returns the ready issues, the
    feature's children first. No story is ready while a feature that blocks this one is not built yet. An issue with open questions waits."""
    tree = linear_gql(LINEAR_READY_TREE, {"id": feature})["issue"]
    ready_label = next((l["id"] for l in tree["team"]["labels"]["nodes"] if l["name"].lower() == "ready"), None)
    if not ready_label:
        raise RuntimeError("team has no label 'Ready'; compose linear labels --create Ready makes it")
    update = "mutation($id: String!, $labels: [String!]!) { issueUpdate(id: $id, input: {labelIds: $labels}) { success } }"
    out = []
    waiting = any(r["type"] == "blocks" and not fc_plan.base_built(r["issue"]["state"]["name"] or "", [l["name"] for l in r["issue"]["labels"]["nodes"]])
                  for r in tree["inverseRelations"]["nodes"])
    groups = [(tree["children"]["nodes"], True), *([(tree["parent"]["children"]["nodes"], False)] if tree["parent"] else [])]
    for group, stories in groups:
        cut = {n["identifier"] for n in group if any((c["state"]["name"] or "").lower() != "canceled" for c in n["children"]["nodes"])}
        asking = {n["identifier"] for n in group if open_questions(n.get("description"))}
        ready = {i for i in ready_ids(group) if (i in cut or not stories) and i not in asking and not (stories and waiting)}
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
    return out

LINEAR_PLAN_TREE = """query($id: String!) { issue(id: $id) { ...PlanNode parent { ...PlanNode } } }
fragment PlanNode on Issue { id identifier title url description labels { nodes { name } } project { id documents(first: 250) { nodes { id title content url } } }
  children { nodes { ...PlanChild children { nodes { ...PlanChild } } } } }
fragment PlanChild on Issue { id identifier title url state { name } labels { nodes { name } }
  inverseRelations(first: 250) { nodes { type issue { identifier state { name } labels { nodes { name } } } } } }"""

def linear_plan(feature):
    """Keep the map's delivery plan document in step: create and link it once, then rewrite only its data sections."""
    tree = linear_gql(LINEAR_PLAN_TREE, {"id": feature})["issue"]
    parent = tree["parent"]
    names = lambda n: [l["name"] for l in n["labels"]["nodes"]]
    epic = bool(parent) and "epic" in [x.lower() for x in names(parent)]
    story = lambda n: {"id": n["identifier"], "title": n["title"], "url": n["url"], "state": n["state"]["name"], "labels": names(n),
                       "blockers": [{"id": r["issue"]["identifier"], "state": r["issue"]["state"]["name"], "labels": names(r["issue"])}
                                    for r in n["inverseRelations"]["nodes"] if r["type"] == "blocks"]}
    lane = lambda n: {"id": n["identifier"], "title": n["title"], "url": n["url"], "stories": [story(c) for c in n["children"]["nodes"]]}
    map_ = parent if epic else tree
    lanes = [lane(n) for n in map_["children"]["nodes"] if "feature" in [x.lower() for x in names(n)]] if epic else [lane(tree)]
    title = f"{map_['identifier']}: delivery plan"
    doc = next((d for d in map_["project"]["documents"]["nodes"] if d["title"] == title), None)
    if not doc:
        doc = linear_gql("mutation($title: String!, $content: String!, $project: String!) { documentCreate(input: {title: $title, content: $content, projectId: $project}) { success document { id title url content } } }",
                         {"title": title, "content": fc_plan.plan_skeleton(), "project": map_["project"]["id"]})["documentCreate"]["document"]
        linear_gql("mutation($id: String!, $description: String!) { issueUpdate(id: $id, input: {description: $description}) { success } }",
                   {"id": map_["id"], "description": fc_plan.with_plan_link(map_["description"], title, doc["url"])})
    content = doc["content"]
    for heading, text in fc_plan.plan_sections(lanes).items():
        content = fc_plan.replace_section(content, heading, text)
    if content.rstrip("\n") != doc["content"].rstrip("\n"):
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
