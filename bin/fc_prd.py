"""`compose prd`: the product's PRD, one Linear document titled PRD in the repo's project, planned with the product owner before the
delivery plan (sections/0r.md): status, a seed that scans the project, and a write that needs their words. It never plans stories."""
import hashlib, json, re, sys, time
from collections import Counter
from pathlib import Path

import fc_core, fc_config, fc_linear, fc_linear_cmd, fc_plan, fc_tasks

PRD_TITLE = "PRD"
PRD_ID = "PRD"  # what `compose factory plan` and night prep call it
HEADINGS = ("## Problem", "## Who it is for", "## Goals", "## Non-goals", "## Anti-goals", "## The product at a glance", "## Open questions")
GLANCE = "## The product at a glance"
DRAFT = "> Draft: what the product is and who it is for, kept with the product owner; it never plans stories, the delivery plan and each feature's plan do."
SEED_LIMIT = 12000
MISSING_SECONDS = 5 * 60
FOUND_SECONDS = 6 * 3600
LOOKUP_ERRORS = (RuntimeError, OSError, ValueError, KeyError, TypeError, SystemExit)
DOCS_QUERY = """query($p: String!, $after: String) { project(id: $p) { documents(first: 50, after: $after) {
  pageInfo { hasNextPage endCursor } nodes { id title url } } } }"""
DOC_QUERY = "query($id: String!) { document(id: $id) { id title url content } }"
PROJECT_QUERY = "query($p: String!) { project(id: $p) { name description content } }"
ISSUES_QUERY = """query($p: ID!, $after: String) { issues(first: 100, after: $after, filter: {project: {id: {eq: $p}}}) {
  pageInfo { hasNextPage endCursor } nodes { identifier title description state { name type } labels { nodes { name } } parent { identifier } } } }"""
DOC_UPDATE = "mutation($id: String!, $content: String!) { documentUpdate(id: $id, input: {content: $content}) { success } }"
DOC_CREATE = "mutation($title: String!, $content: String!, $projectId: String!) { documentCreate(input: {title: $title, content: $content, projectId: $projectId}) { document { id title url content } } }"
GROUP_LABELS = ("feature", "epic")
PLAIN_LABELS = GROUP_LABELS + ("task", "ready")
STOP_WORDS = set("a an and the of to for in on with by from into as at is be it its or not no new add make use can when after before via all their this that".split())
HINT_FILES = ("README.md", "README", "README.rst", "DESIGN.md")

def is_prd_title(title):
    """A document counts as the PRD when its title is PRD (any case), starts with PRD and a space, - or :, or says product requirements."""
    t = (title or "").strip().lower()
    return t == "prd" or bool(re.match(r"prd[ \-:]", t)) or "product requirements" in t

def _paged(query, variables, pick):
    nodes, after = [], None
    while True:
        page = pick(fc_linear.linear_gql(query, {**variables, "after": after}))
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]

def _docs(proj):
    return _paged(DOCS_QUERY, {"p": proj["id"]}, lambda r: r["project"]["documents"])

def _pick(docs):
    """The PRD among the documents: one titled PRD wins, else the first with a PRD-like title; None when there is none."""
    return next((d for d in docs if (d["title"] or "").strip() == PRD_TITLE), None) or next((d for d in docs if is_prd_title(d["title"])), None)

def find_prd(repo):
    """The PRD's {id, title, url}, or None when the project has none. Raises when it cannot look (no Linear key or link, Linear down)."""
    if not fc_config.LINEAR_KEY:
        raise RuntimeError("no LINEAR_API_KEY: the PRD cannot be looked up")
    return _pick(_docs(fc_linear_cmd.linear_project(repo)))

def read_prd(repo):
    """The PRD with its content, or None when there is none."""
    found = find_prd(repo)
    return fc_linear.linear_gql(DOC_QUERY, {"id": found["id"]})["document"] if found else None

def glance(content):
    """The body of the PRD's product at a glance section, '' when it has none."""
    lines = (content or "").split("\n")
    span = fc_plan.section_span(lines, GLANCE)
    return "\n".join(lines[span[0] + 1:span[1]]).strip() if span else ""

def stamp_path(repo):
    return fc_core.repo_link(repo).with_name("prd.json")

def stamp(repo):
    """Note that the PRD was just written, so the factory's next lookup looks again. The factory's own state is the loop's to write."""
    fc_core.atomic_write(stamp_path(repo), json.dumps({"written": time.time()}) + "\n")

def written_at(repo):
    try:
        return float(json.loads(stamp_path(repo).read_text()).get("written") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0

def check(repo, state, now):
    """Whether the PRD exists, from the cache in state["prd"]: looked up again at most every 5 minutes while missing, every 6 hours
    while found, and always after a `compose prd write`. True, False, or None while it has never been looked up. A failed lookup
    changes nothing and the next turn tries again; it never blocks building."""
    cache = state.get("prd") if isinstance(state.get("prd"), dict) else {}
    found, checked = cache.get("found"), cache.get("checked") or 0
    if isinstance(found, bool) and now - checked < (FOUND_SECONDS if found else MISSING_SECONDS) and written_at(repo) < checked:
        return found
    try:
        doc = find_prd(repo)
    except LOOKUP_ERRORS:
        return found if isinstance(found, bool) else None
    state["prd"] = {"found": bool(doc), "checked": now, **({"title": doc["title"], "url": doc["url"]} if doc else {})}
    return bool(doc)

def _issues(proj):
    return _paged(ISSUES_QUERY, {"p": proj["id"]}, lambda r: r["issues"])

def _labels(n):
    return [l["name"] for l in (n.get("labels") or {}).get("nodes") or []]

def _state(n):
    return (n.get("state") or {}).get("name") or ""

def _done(n):
    return (n.get("state") or {}).get("type") == "completed" or _state(n).lower() == "done"

def _canceled(n):
    return (n.get("state") or {}).get("type") == "canceled" or _state(n).lower() in fc_tasks.CANCELED

def ticket_print(repo):
    """A hash of the project's tickets as the seed lists them (ids, titles, states): night prep prepares the PRD again when it changes."""
    rows = sorted((n["identifier"], n.get("title") or "", _state(n)) for n in _issues(fc_linear_cmd.linear_project(repo)))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]

def _first_lines(text, n=2, width=160):
    return " ".join([l.strip() for l in (text or "").split("\n") if l.strip()][:n])[:width]

def _line(n):
    labels = _labels(n)
    desc = _first_lines(n.get("description"))
    return f"{n['identifier']} ({_state(n)}; {', '.join(labels) or 'no labels'}) {n.get('title') or ''}" + (f": {desc}" if desc else "")

def _grouped(issues):
    """The open tickets as (group, [children]): a group is a top-level ticket or one labeled Feature or Epic, and each other ticket
    sits under its nearest open group; Task tickets are left out."""
    open_ = {n["identifier"]: n for n in issues if not _done(n) and not _canceled(n) and "task" not in [l.lower() for l in _labels(n)]}
    def is_group(n):
        return any(l.lower() in GROUP_LABELS for l in _labels(n)) or (n.get("parent") or {}).get("identifier") not in open_
    def home(n):
        seen, p = set(), (n.get("parent") or {}).get("identifier")
        while p in open_ and p not in seen:
            seen.add(p)
            if is_group(open_[p]):
                return p
            p = (open_[p].get("parent") or {}).get("identifier")
        return None
    groups = {i: [] for i, n in open_.items() if is_group(n) or home(n) is None}
    for i, n in open_.items():
        if i not in groups:
            groups[home(n)].append(n)
    key = lambda i: fc_plan.issue_num(i)
    return [(open_[g], sorted(groups[g], key=lambda n: key(n["identifier"]))) for g in sorted(groups, key=key)]

def _hints(repo):
    """The first lines of the repo's README, DESIGN.md and PRD-like files in its root or docs/."""
    root, out = Path(repo), []
    paths = [root / f for f in HINT_FILES] + sorted(p for d in (root, root / "docs") if d.is_dir() for p in d.iterdir()
                                                   if p.is_file() and p.suffix.lower() in (".md", ".txt", "") and is_prd_title(p.stem.replace("_", " ")))
    for p in dict.fromkeys(paths):
        try:
            text = p.read_text(errors="replace") if p.is_file() else ""
        except OSError:
            continue
        lines = [l.rstrip() for l in text.split("\n") if l.strip()][:8]
        if lines:
            out.append(f"{p.relative_to(root)}:\n" + "\n".join("  " + l[:200] for l in lines))
    return out

def _guess(groups, issues):
    words = Counter(w for n in issues if not _canceled(n) for w in dict.fromkeys(re.findall(r"[a-z][a-z0-9]{3,}", (n.get("title") or "").lower()))
                    if w not in STOP_WORDS)
    labels = Counter(l for n in issues if not _canceled(n) for l in _labels(n) if l.lower() not in PLAIN_LABELS)
    themes = [f"{k} ({v})" for k, v in labels.most_common(6) if v >= 2] + [f"{k} ({v})" for k, v in words.most_common(8) if v >= 2 and k not in labels]
    return "\n".join(["## A guess at the product (a guess from the tickets, not the product owner's words: play it back as a guess)", "",
                      "Candidate features (the open top-level tickets):", *[f"- {g['identifier']} {g.get('title') or ''}" for g, _ in groups[:20]],
                      "", "Recurring themes: " + (", ".join(themes) or "none")])

def seed_text(repo):
    proj = fc_linear_cmd.linear_project(repo)
    info = fc_linear.linear_gql(PROJECT_QUERY, {"p": proj["id"]})["project"] or {}
    docs = _docs(proj)
    prd = _pick(docs)
    prd = fc_linear.linear_gql(DOC_QUERY, {"id": prd["id"]})["document"] if prd else None
    issues = _issues(proj)
    groups = _grouped(issues)
    built = sorted((n for n in issues if _done(n) and "task" not in [l.lower() for l in _labels(n)]), key=lambda n: fc_plan.issue_num(n["identifier"]))
    tickets = []
    for g, kids in groups:
        tickets += ["- " + _line(g)] + ["  - " + _line(k) for k in kids]
    body = "\n".join([f"PRD seed for {proj['name']} ({proj['url']}): read it, then play back a guess at the PRD (sections/0r.md).", "",
                      "## Project", "", f"Name: {info.get('name') or proj['name']}", f"Description: {info.get('description') or 'none'}",
                      "Content:", (info.get("content") or "none").strip(), "",
                      "## Existing PRD", "", f"{prd['title']} ({prd['url']}):\n{(prd['content'] or '').strip()}" if prd else "No PRD yet.", "",
                      "## Documents", "", *([f"- {d['title']}" for d in docs] or ["none"]), "",
                      "## Open tickets (epics and features with their stories)", "", *(tickets or ["none"]), "",
                      "## Built so far", "", *([f"- {n.get('title') or ''}" for n in built] or ["nothing yet"]), "",
                      "## Repo hints", "", *(_hints(repo) or ["none"]), ""])
    guess = _guess(groups, issues)
    room = SEED_LIMIT - len(guess) - 80
    if len(body) > room:
        cut = body[:room].rsplit("\n", 1)[0]
        body = cut + f"\n\n[seed trimmed: {len(body) - len(cut)} characters cut to keep it near {SEED_LIMIT}]\n"
    return body.rstrip("\n") + "\n\n" + guess + "\n"

def verb_status(a):
    found = find_prd(a.repo)
    print(f"prd: {found['title']} {found['url']}" if found else "prd: none")

def verb_seed(a):
    sys.stdout.write(seed_text(a.repo))

def _missing(content):
    lines = set(content.split("\n"))
    return [h for h in HEADINGS if h not in lines]

def _history(content):
    lines = (content or "").split("\n")
    span = fc_plan.section_span(lines, "## History")
    return [l for l in lines[span[0] + 1:span[1]] if l.strip()] if span else []

def build(text, old, words):
    """The PRD's new content: the file marked a draft at the top, the old History kept, and a History line with their words."""
    body = text.strip()
    if not body.startswith(DRAFT):
        body = DRAFT + "\n\n" + body
    kept = _history(old)
    if kept:
        body = fc_plan.replace_section(body, "## History", "\n".join(["## History", ""] + kept))
    return fc_plan.with_history(body, f'- {time.strftime("%Y-%m-%d")}: written; the product owner said: "{words}"')

def verb_write(a):
    if not (a.quote or "").strip():
        sys.exit('prd write needs --quote "<the product owner\'s words>": the PRD is theirs, so it is written only on what they said (under away and yolo too)')
    text = fc_linear_cmd.read_body(a)
    missing = _missing(text)
    if missing:
        sys.exit(f"prd write: the file has no {', '.join(missing)}; a PRD has all of {', '.join(HEADINGS)}; nothing written")
    words = " ".join(a.quote.split())
    proj = fc_linear_cmd.linear_project(a.repo)
    found = _pick(_docs(proj))
    if found:
        doc = fc_linear.linear_gql(DOC_QUERY, {"id": found["id"]})["document"]
        content = build(text, doc["content"] or "", words)
        fc_linear.linear_gql(DOC_UPDATE, {"id": doc["id"], "content": content})
        fc_linear_cmd.await_write(lambda: fc_linear.linear_gql(DOC_QUERY, {"id": doc["id"]})["document"]["content"], doc["content"], content)
    else:
        doc = fc_linear.linear_gql(DOC_CREATE, {"title": PRD_TITLE, "content": build(text, "", words), "projectId": proj["id"]})["documentCreate"]["document"]
    stamp(a.repo)
    print(f"prd written: {doc['url']}")

def cmd_prd(a):
    if not fc_config.LINEAR_KEY:
        sys.exit(fc_config.needs_linear_key("compose prd"))
    try:
        globals()[f"verb_{a.action}"](a)
    except (RuntimeError, OSError, ValueError) as e:
        sys.exit(f"prd {a.action}: {e}")
