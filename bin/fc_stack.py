"""The stack: the review layers registered as one gh stack, and each layer's PR title and body, written by the team lead or
generated from its commits and stories."""
import json, re, subprocess, sys
from pathlib import Path
import fc_core, fc_config, fc_tasks, fc_worktree, fc_layers


TITLE_CHARS = 72  # a PR title's length before its ` (i/n)` suffix, the Jira-key prefix included

def clip(text, room):
    """text in at most `room` characters: whole when it fits, else cut at a word boundary and ending in `...`."""
    if len(text) <= room:
        return text
    head = text[:room - 2]
    return (head.rsplit(" ", 1)[0] if " " in head else head[:-1]).rstrip(" ,;:-") + "..."

def title_prefix(proj):
    """`JS-2888: ` when the project branch starts with a Jira key, else ""."""
    key = re.match(r"([A-Za-z]+-\d+)-", fc_core.project_config(proj).get("branch") or "")
    return key.group(1).upper() + ": " if key else ""

def layer_title(repo, proj, l, n, metas, written=None):
    """A layer's PR title names its change: the team lead's `written` title when there is one, else the feature's title for a
    one-PR stack, the story's for a layer that holds every task of one story and no other story's, `Groundwork for <feature
    title>` for a layer of groundwork only, else its one commit's subject, its stories' titles (`<story>; <story>`) when its
    tasks span several and they fit, or its first task's title or first commit's subject plus ` and N more tasks` (`commits`).
    With the Jira-key prefix, at most TITLE_CHARS characters (cut at a word boundary, ending in `...`), and the ` (i/n)` suffix
    only when the stack has more than one PR."""
    cache = fc_tasks.linear_cache(proj)
    feature, issues = cache.get("feature") or {}, cache.get("issues") or {}
    held = {m["id"] for m in metas if m["id"] in l["tasks"]}
    stories = list(dict.fromkeys(story_of(issues, m) for m in metas if m["id"] in held))
    whole = len(stories) == 1 and None not in stories and all(m["id"] in held for m in metas if story_of(issues, m) in stories)
    named = [title_text(issues[s].get("title") or "") for s in stories if s]
    tasks = [t for t in l["tasks"] if t != "groundwork"]
    subjects = [fc_core.sh(["git", "log", "-1", "--format=%s", c], repo).stdout.strip() for c in l["commits"]]
    prefix = title_prefix(proj)
    room = TITLE_CHARS - len(prefix)
    if written:
        text = written
    elif n == 1 and feature.get("title"):
        text = clip(title_text(feature["title"]), room)
    elif whole and named[0]:
        text = clip(named[0], room)
    elif not tasks and feature.get("title"):
        text = clip(f"Groundwork for {title_text(feature['title'])}", room)
    elif len(subjects) == 1:
        text = clip(subjects[0], room)
    elif len(stories) > 1 and None not in stories and all(named) and len("; ".join(named)) <= room:
        text = "; ".join(named)
    else:
        names = {m["id"]: fc_layers.commit_message(m) for m in metas}
        first, more, unit = (names.get(tasks[0], tasks[0]), len(tasks) - 1, "task") if tasks else (subjects[0], len(subjects) - 1, "commit")
        tail = f" and {more} more {unit}{'s' if more > 1 else ''}" if more else ""
        text = clip(first, room - len(tail)) + tail
    idx = int(l["branch"].rsplit("layer-", 1)[1])
    return f"{prefix}{text}{f' ({idx}/{n})' if n > 1 else ''}"

WHY_CHARS = 400
GITHUB_ISSUE_RE = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)|(?<![\w/.-])([\w.-]+/[\w.-]+)#(\d+)")

def plain(text):
    """Linear markdown as plain text: its escapes (`r\\["x"\\]`) undone and links reduced to their text."""
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|<>~\"'])", r"\1", text or "")
    return re.sub(r"\[([^\]]*)\]\(<?[^)>]*>?\)", r"\1", text).strip()

def title_text(text):
    """A Linear title as plain text, without the decision cards it cites, as `(C3)` or `(C3, C4)`."""
    return re.sub(r"\s*\((?:C\d+[,/ ]*(?:and )?)+\)", "", plain(text)).strip()

def sentence(text):
    """A title as a sentence (title_text, ending in a full stop)."""
    text = title_text(text)
    return text + ("" if not text or text[-1] in ".!?" else ".")

def first_sentence(desc, key):
    """The first sentence of a description's opening prose that says something about the product, or "": nothing past the
    first heading, no list or next-action line, and no sentence that cites a decision card (C3), a ticket (`<key>-12`), or an
    issue it fixes, so no process text and no brief reaches a reader."""
    cites = re.compile(r"\bC\d+\b|^(Fixes|Closes|Resolves|Card|Decisions?)\b" + (rf"|\b{re.escape(key)}-\d+\b" if key else ""))
    for para in re.split(r"\n\s*\n", plain(desc)):
        para = " ".join(para.split())
        if para.startswith("#"):
            break
        if not para or re.match(r"([-*+]|\d+\.)\s|Next:", para):
            continue
        for s in re.split(r"(?<=[.!?])\s+", para):
            if not cites.search(s) and not GITHUB_ISSUE_RE.search(s) and "github.com/" not in s:
                return sentence(s)
    return ""

def issue_refs(text):
    """The GitHub issues a text names, as `owner/repo#N`, in order: issue URLs and `owner/repo#N` references."""
    return list(dict.fromkeys(f"{a or c}#{b or d}" for a, b, c, d in GITHUB_ISSUE_RE.findall(text or "")))

def linear_url(proj, ident):
    """A Linear issue's link, in the workspace of the repo's `.compose/linear.json` project; the bare id when that is unknown."""
    f = fc_core.repo_link(Path(proj).parent.parent)
    m = re.match(r"https://linear\.app/([^/]+)/", json.loads(f.read_text()).get("project") or "" if f.exists() else "")
    return f"[{ident}](https://linear.app/{m.group(1)}/issue/{ident})" if m else ident

def story_of(issues, m):
    """The Linear id of the story a task sits under, or None when its parent is not a cached story (the feature)."""
    parent = (issues.get(m.get("linear")) or {}).get("parentId")
    return parent if parent in issues else None

def issue_lines(proj, l, metas, layers):
    """The GitHub issues a layer's stories name, one line each: `- Fixes owner/repo#N` on the layer that holds the last of the
    issue's tasks, `- Part of owner/repo#N` below it. An issue belongs to the story that names it, else to the feature and all
    its tasks; a groundwork layer has none."""
    cache = fc_tasks.linear_cache(proj)
    feature, issues = cache.get("feature") or {}, cache.get("issues") or {}
    story = lambda m: story_of(issues, m)
    text = lambda s: issues[s] if s else feature
    owner = {}
    for s in [*dict.fromkeys(story(m) for m in metas if story(m)), None]:
        for ref in issue_refs(text(s).get("title", "") + "\n" + text(s).get("description", "")):
            owner.setdefault(ref, s)
    here = [x for x in layers if x["branch"] == l["branch"]]
    landed = {t for x in layers[:layers.index(here[0]) + 1] for t in x["tasks"]} if here else set(l["tasks"])
    later = {m["id"] for m in metas if m["id"] not in landed and not fc_tasks.linear_done(m)}
    refs = []
    for ref, s in owner.items():
        held = {m["id"] for m in metas if s is None or story(m) == s}
        if held & set(l["tasks"]):
            refs.append(f"- {'Part of' if held & later else 'Fixes'} {ref}")
    return refs

def layer_why(proj, l, metas, layers):
    """A layer's `## Why`: what its stories are for, in at most three sentences (their titles, and for a one-story layer the
    first sentence of its description; the feature's when its tasks sit under no story), then its issue_lines. A groundwork
    layer says what it prepares. At most WHY_CHARS characters, the issue lines kept whole."""
    cache = fc_tasks.linear_cache(proj)
    feature, issues = cache.get("feature") or {}, cache.get("issues") or {}
    if not any(t != "groundwork" for t in l["tasks"]):
        return [sentence(f"Groundwork for {feature.get('title') or 'the tasks above'}")[:WHY_CHARS]]
    stories = list(dict.fromkeys(story_of(issues, m) for m in metas if m["id"] in l["tasks"]))
    text = lambda s: issues[s] if s else feature
    said = [sentence(text(s).get("title", "")) for s in stories]
    if len(stories) == 1:
        said.append(first_sentence(text(stories[0]).get("description", ""), (feature.get("id") or "").split("-")[0]))
    refs = issue_lines(proj, l, metas, layers)
    room, why = WHY_CHARS - sum(len(r) + 1 for r in refs), []
    for s in filter(None, said[:3]):
        if len(" ".join([*why, s])) <= room:
            why.append(s)
    if not why and said and room > 3:
        why = [said[0][:room - 3].rsplit(" ", 1)[0] + "..."]
    return [" ".join(why), *refs] if why else refs

def layer_body(repo, proj, l, metas, layers=(), written=None):
    """A layer's PR body: `## Why` (layer_why), `## What changed` (one line per task, the subject of its first commit that is
    not one of its test commits, else its title; one per groundwork commit), `## How it was tested` (each test command and
    passing verify check once, then whether compose verify passed) and `## Tickets` (its tasks' Linear links), each left out
    when empty. `written` (pr_text's Why and What changed lines) replaces the first two, its Why followed by the issue_lines.
    `layers` is the whole stack, bottom first, so an issue's `Fixes` lands on the layer that completes it; without
    it, every task not in this layer is still to come. The layer's size stays on its `layer` line, out of the body."""
    res = Path(proj) / "results"
    read = lambda name: json.loads((res / f"{name}.json").read_text()) if (res / f"{name}.json").exists() else {}
    by_id = {m["id"]: m for m in metas}
    tasks = [by_id[t] for t in dict.fromkeys(l["tasks"]) if t in by_id]
    subject = lambda c: fc_core.sh(["git", "log", "-1", "--format=%s", c], repo).stdout.strip()
    changed = []
    for t, commits in zip(l["tasks"], l["units"]):
        m = by_id.get(t)
        if not m:  # groundwork: one line per commit
            changed += [subject(c) for c in commits]
            continue
        own = [c for c in commits if not set(fc_layers.commit_facts(repo, c)[0]) <= set(m["protected"])]
        changed.append(subject(own[0]) if own else fc_layers.commit_message(m))
    verify = read("verify")
    checks = [c["cmd"] for c in ((verify.get("rounds") or [{}])[-1].get("checks") or []) if c.get("exit") == 0]
    how = list(dict.fromkeys(f"`{t}`" for t in [*(m["test"] for m in tasks), *checks]))
    if verify:
        how.append("compose verify passed" if verify.get("passed") else "compose verify failed")
    why, changed = ([*written[0], *issue_lines(proj, l, metas, list(layers))], written[1]) if written else \
        (layer_why(proj, l, metas, list(layers)), [f"- {x}" for x in changed])
    sections = [("Why", why), ("What changed", changed),
                ("How it was tested", [f"- {x}" for x in how]), ("Tickets", [f"- {linear_url(proj, m['linear'])}" for m in tasks])]
    return "\n\n".join(f"## {h}\n" + "\n".join(lines) for h, lines in sections if lines)

def pr_names(layers):
    """Each layer's PR text file name, from what it holds (`001-002`, `groundwork`, `groundwork-after-003`), so a renumbered
    layer keeps its file and a regrouped one no longer matches it. Only groundwork split over the budget repeats a name: the
    higher layers get `-2`, `-3`."""
    held, names = [], []
    for l in layers:
        held.append("-".join(dict.fromkeys(k.replace(" ", "-") for k in l.get("keys", l["tasks"]))))
        n = held.count(held[-1])
        names.append(f"{held[-1]}-{n}" if n > 1 else held[-1])
    return names

ISSUE_LINE_RE = re.compile(r"^(-\s*)?(Fixes|Closes|Resolves|Part of)\b", re.I)

def pr_text(path, room):
    """The team lead's text for a layer's PR from its file, or None when there is none: line 1 `# <title>` (at most `room`
    characters, no ticket key or `(i/n)`), then `## Why` (prose, no issue lines) and `## What changed` (`- ` bullets), nothing
    else. Returns (title, Why lines, What changed lines); anything else raises ValueError with the reason, never a guess."""
    if not path.exists():
        return None
    lines = path.read_text().splitlines()
    title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else ""
    if not title:
        raise ValueError("line 1 must be `# <title>`")
    if re.match(r"[A-Za-z]+-\d+:", title) or re.search(r"\(\d+/\d+\)$", title):
        raise ValueError("the title carries a ticket key or (i/n); compose adds those")
    if len(title) > room:
        raise ValueError(f"the title is {len(title)} characters, at most {room}")
    heads = [i for i, x in enumerate(lines) if x.startswith("#")][1:]
    if [lines[i] for i in heads] != ["## Why", "## What changed"] or any(x.strip() for x in lines[1:heads[0]]):
        raise ValueError("after the title it must be `## Why` then `## What changed`, nothing else")
    strip = lambda part: "\n".join(part).strip().splitlines()
    why, changed = strip(lines[heads[0] + 1:heads[1]]), strip(lines[heads[1] + 1:])
    if not why or not changed:
        raise ValueError("`## Why` and `## What changed` must not be empty")
    if any(ISSUE_LINE_RE.match(x) for x in why):
        raise ValueError("`## Why` names an issue to fix; compose adds the Fixes and Part of lines")
    if any(x.strip() and not x.startswith("- ") for x in changed):
        raise ValueError("every line of `## What changed` must be a `- ` bullet")
    return title, why, changed

def layer_pr(repo, proj, l, n, metas, layers=()):
    """A layer's PR title and body, and where they came from: the team lead's file `pr/<name>.md` (pr_names, pr_text) gives the
    title, Why and What changed; without a valid one they are generated from the layer's commits and stories (layer_title,
    layer_body). The Jira key, ` (i/n)`, the issue lines, How it was tested and Tickets are always compose's.
    Returns (title, body, `written: <file>` or `generated: <what to do about the file>`)."""
    stack = list(layers) or [l]
    name = pr_names(stack)[next((i for i, x in enumerate(stack) if x["branch"] == l["branch"]), 0)]
    path = Path(proj) / "pr" / f"{name}.md"
    shown = f".compose/{Path(proj).name}/pr/{name}.md"
    try:
        written = pr_text(path, TITLE_CHARS - len(title_prefix(proj)))
        source = f"written: {shown}" if written else f"generated: write {shown}"
    except ValueError as e:
        written, source = None, f"generated: {shown} refused: {e}"
    title = layer_title(repo, proj, l, n, metas, written and written[0])
    return title, layer_body(repo, proj, l, metas, layers, written and written[1:]), source

def stale_pr_files(proj, layers):
    """A line for each PR text file that matches no layer: its tasks moved, so it is never applied to different ones."""
    names = pr_names(layers)
    return [f".compose/{Path(proj).name}/pr/{f.name} matches no layer, so no PR uses it; the layers' files are {', '.join(f'{x}.md' for x in names)}"
            for f in sorted((Path(proj) / "pr").glob("*.md")) if f.stem not in names]

def gh_pr_text(wt, b):
    """The title and body of the PR on branch b, or None when it has none."""
    v = subprocess.run(["gh", "pr", "view", b, "--json", "title,body"], cwd=wt, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return json.loads(v.stdout) if v.returncode == 0 else None

def write_pr_text(proj, wt, b, before, title, body):
    """Write a layer's generated title and body on its PR only when the CLI owns the text: the PR was just created (before is None),
    or its text still equals what the CLI wrote last time (recorded in pr_text.json). A person's or reviewer's edit, and any PR
    from before the record existed, keeps its text; the whole text is regenerated or none of it."""
    rec_file = Path(proj) / "pr_text.json"
    rec = json.loads(rec_file.read_text()) if rec_file.exists() else {}
    last = rec.get(b)
    if before is not None and not (last and (before["title"], before["body"].strip()) == (last["title"], last["body"].strip())):
        return False
    r = subprocess.run(["gh", "pr", "edit", b, "--title", title, "--body", body], cwd=wt, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode == 0:
        fc_core.atomic_write(rec_file, json.dumps({**rec, b: {"title": title, "body": body}}, indent=2))
    return r.returncode == 0

def layer_base(repo, proj, trunk):
    """The branch the layers sit on: the review base (fc_worktree.review_base), else the trunk; the trunk itself when it is not
    the project's branch or the review base is gone."""
    base = fc_worktree.review_base(repo, fc_core.project_config(proj)) or trunk
    if trunk != fc_core.project_branch(proj) or fc_core.sh(["git", "rev-parse", "--verify", "-q", base], repo, check=False).returncode:
        base = trunk
    return base

def head_problem(repo, proj):
    """Why the stack must not be built from where `repo` stands, or "" when it can. task_branches reads the unmerged branches
    against HEAD, so HEAD on one of the project's own task or layer branches (a checkout by hand, a teammate, a stuck rebase)
    hides every task branch it contains: the chain is cut short and the layers above it are deleted. A HEAD on the project
    branch, or anywhere that holds none of the project's unmerged tasks, is fine."""
    branch = fc_core.project_branch(proj)
    here = fc_core.sh(["git", "branch", "--show-current"], repo, check=False).stdout.strip()
    own = [*fc_worktree.own_branches(repo, proj), *fc_core.sh(["git", "branch", "--list", f"{fc_layers.layer_prefix(repo, proj)}layer-*", "--format=%(refname:short)"], repo, check=False).stdout.split()]
    fix = f"Switch back with `git switch {branch}` in {repo}, then re-run"
    if here in own:
        return f"HEAD is on {here}, one of the project's own branches, not on {branch}; the layers and the stack are left as they were. {fix}"
    if fc_core.sh(["git", "rev-parse", "--verify", "-q", branch], repo, check=False).returncode:
        return ""
    prefix = fc_worktree.task_prefix(repo, proj)
    held = fc_core.sh(["git", "for-each-ref", "--format=%(refname:short)", "--merged=HEAD", f"--no-merged={branch}", f"refs/heads/{prefix}*"], repo, check=False).stdout.split()
    held = [b for b in held if re.fullmatch(rf"{re.escape(prefix)}\d+", b)]
    if held:
        return (f"HEAD holds the unmerged task branches {', '.join(held)}, which would be left out of the stack; the layers and the stack are "
                f"left as they were. {fix}")
    return ""

def fetch_landed_base(wt, base):
    """Bring the base a layer landed on in from origin: (the ref the layers are cut on, the fetch). The local branch is moved
    along when no checkout holds it. One that does (the user's tree) is never moved under them, and git would refuse anyway, so
    the layers are cut on origin/<base>, fetched on its own."""
    held = f"branch refs/heads/{base}" in fc_core.sh(["git", "worktree", "list", "--porcelain"], wt, check=False).stdout.splitlines()
    if held:
        return f"origin/{base}", fc_core.sh(["git", "fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"], wt, check=False)
    return base, fc_core.sh(["git", "fetch", "-q", "origin", f"{base}:{base}"], wt, check=False)

def upstream_ahead(repo, base, upstream):
    """How many commits `upstream` (the base's remote branch) holds that the local `base` does not, after a fetch of it. The user's checkout
    holds `base` and compose never moves it, so it can fall behind; a fetch that fails (offline) leaves what was fetched before."""
    remote, _, branch = upstream.partition("/")
    fc_core.sh(["git", "fetch", "-q", remote, f"+refs/heads/{branch}:refs/remotes/{upstream}"], repo, check=False)
    return int(fc_core.sh(["git", "rev-list", "--count", f"{base}..{upstream}"], repo, check=False).stdout.strip() or 0)

MAX_STACK_PRS = 10  # the PRs a stack holds unless verify.json or the config says `max_stack_prs`

def stack_cap(cfg, settings):
    """The most PRs a stack holds: verify.json's `max_stack_prs` (`cfg`), else the active workspace's settings (its entry over the top level), else MAX_STACK_PRS."""
    return int(cfg.get("max_stack_prs") or settings.get("max_stack_prs") or MAX_STACK_PRS)

def base_ahead(base, upstream, ahead, submit):
    """(`stop` for a submit, `warn` without one, else ""; what it says) for a base `ahead` commits past its `upstream`: its unpushed commits would go into the PRs. A base that is level with or behind its upstream (ahead 0) says nothing."""
    if not ahead:
        return "", ""
    them = "them" if ahead > 1 else "it"
    said = f"{base} is {ahead} commit{'s' * (ahead > 1)} ahead of {upstream}, so the PRs would carry {them}"
    if submit:
        return "stop", f"{said}; nothing was built or pushed. Push {base}, or wait until {upstream} has {them}, then re-run"
    return "warn", f"{said}; --submit stops until {base} is pushed"

HELD_STATUSES = ("passed", "already_green", "hand_done")

def held_tasks(order, statuses, finished):
    """The ids of `order` (bottom to top) a submit leaves out: from the first one whose result in `statuses` is not passed, already green or done by hand and that is not `finished` on Linear, up to the top, so a failed task holds back the ones after it."""
    return order[next((i for i, t in enumerate(order) if statuses[t] not in HELD_STATUSES and t not in finished), len(order)):]

def register_stack(repo, proj, trunk, submit=False, preview=False, gate=None):
    """Make the project's passed task branches one gh stack, layer by layer, without touching the user's checkout. Branches built
    in parallel from trunk are rebased onto the layer below (tasks own disjoint files, so this is normally clean; a conflict
    stops the chain there, is reported, and nothing is built or registered). The stack itself is review layers on the real base (fc_worktree.review_base: `review_base`, else the default
    branch), not one PR per task and not the project branch as a PR: groundwork and each task's tests are cut into the layers
    too, so the project branch carries nothing the layers do not; when they cannot be built (build_layers' STOP), the layers and
    the registered stack stay as they were. Nothing is pushed unless submit=True; preview=True prints each layer's
    PR title and body as submit would write them, with no gh call. A project whose fast/<project>/ branches were never pushed
    first moves onto the names a new project gets (fc_worktree.rename_legacy_branches). A base ahead of its upstream would put
    its unpushed commits into the PRs: the stack says so, and a submit stops before anything changes. `gate(repo, proj, layers,
    trunk, wt)` runs on a submit once the layers stand and before anything is pushed; the reason it returns, when it returns one,
    stops the submit the same way."""
    if why := head_problem(repo, proj):
        fc_layers.STACK_STOP = why
        fc_core.say(f"stack  STOP  {why}")
        return []
    fc_worktree.rename_legacy_branches(repo, proj)
    if not fc_worktree.stack_order(repo, proj):
        return []
    cfg = fc_core.project_config(proj)
    base = layer_base(repo, proj, trunk)
    up = fc_core.sh(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{base}@{{upstream}}"], repo, check=False)
    upstream = up.stdout.strip() if up.returncode == 0 else ""
    ahead = int(fc_core.sh(["git", "rev-list", "--count", f"{upstream}..{base}"], repo).stdout) if upstream else 0
    behind = upstream_ahead(repo, base, upstream) if upstream and not ahead else 0
    level, said = base_ahead(base, upstream, ahead, submit)
    if level == "stop":
        fc_layers.STACK_STOP = said
        fc_core.say(f"stack  STOP  {said}")
        return []
    if level == "warn":
        fc_core.say(f"stack  WARN  {said}")
    held, through = [], ()
    if submit:
        metas = fc_tasks.code_tasks(fc_tasks.load_tasks(proj))
        by_id = {m["id"]: m for m in metas}
        order = fc_worktree.layer_order(metas)
        def status(t):
            f = proj / "results" / f"{t}.json"
            return (json.loads(f.read_text()).get("status") if f.exists() else None) or "pending"
        held = held_tasks(order, {t: status(t) for t in order}, {t for t in order if fc_tasks.linear_done(by_id[t])})
        cut = len(order) - len(held)
        if held:
            fc_core.say(f"stack  hold  {held[0]} is {status(held[0])}; {', '.join(held)} not submitted")
            through = [b for b in fc_worktree.stack_order(repo, proj) if b.removeprefix(fc_worktree.task_prefix(repo, proj)) in order[:cut]]
            if not through:
                fc_layers.STACK_STOP = f"nothing to submit: {held[0]} is {status(held[0])}"
                fc_core.say(f"stack  STOP  {fc_layers.STACK_STOP}")
                return []
    wt = fc_worktree.stack_worktree(repo, proj, trunk)
    gh = lambda *args: subprocess.run(["gh", "stack", *args], cwd=wt, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    rec_file, landed = proj / "layers.json", []  # recorded layers whose PR was merged: the rest is built on the base they moved
    for b in json.loads(rec_file.read_text()) if rec_file.exists() else []:
        v = subprocess.run(["gh", "pr", "view", b, "--json", "state"], cwd=wt, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if v.returncode == 0 and json.loads(v.stdout).get("state") == "MERGED":
            landed.append(b)
    built = upstream if behind else base  # the ref the layers are cut on: a base behind its remote is not the one the PRs land on
    if behind and not landed:
        fc_core.say(f"stack  base  {base} is {behind} commit{'s' * (behind > 1)} behind {upstream}; building on {upstream}")
    if landed:
        built, f = fetch_landed_base(wt, base)
        if f.returncode:
            fc_layers.STACK_STOP = f"{', '.join(landed)} landed but {base} cannot be fetched from origin: {(f.stderr or f.stdout).strip()[-300:]}; nothing was built or pushed"
            fc_core.say(f"stack  STOP  {fc_layers.STACK_STOP}")
            return []
        fc_core.say(f"stack  landed {', '.join(landed)}; building on origin/{base}")
    linear = fc_layers.linearize(repo, proj, trunk, wt, through=through)
    if not linear or fc_layers.STACK_STOP:  # a stack of the layers below the stop would leave the rest out of review
        return []
    budget = int(cfg.get("review_layer_lines") or fc_layers.REVIEW_LAYER_LINES)
    cap = stack_cap(cfg, fc_config.settings(fc_config.read_config())[1])
    layers = fc_layers.build_layers(repo, proj, built, trunk, linear, wt, budget, cap, held=held, landed=landed)
    if layers is None:
        return []
    stack, stack_base = ([l["branch"] for l in layers], base) if layers else (linear, trunk)
    # A stack from an earlier wave is rebuilt with the grown chain: every existing stack that holds any of these branches is
    # unstacked locally (GitHub side untouched), then one init registers the whole chain.
    for b in linear + stack:
        fc_core.sh(["git", "checkout", "-q", b], wt, check=False)
        if gh("view", "--json").returncode == 0:
            gh("unstack", "--local")
    # gh stack 0.2+ refuses to init from a detached HEAD ("finding current branch: not on any branch"), and the trunk is often
    # checked out in the user's tree, so init runs from one of the stack's own branches, which it adopts in place.
    for b in stack:
        if fc_core.sh(["git", "checkout", "-q", b], wt, check=False).returncode == 0:
            break
    p = gh("init", "--base", stack_base, *stack)
    if p.returncode:
        fc_core.say(f"stack  FAIL  {' <- '.join(stack)}: {(p.stderr or p.stdout).strip()[-300:]}")
        fc_core.sh(["git", "checkout", "-q", "--detach", trunk], wt, check=False)
        return []
    fc_core.say(f"stack  ok    ({stack_base}) <- {' <- '.join(stack)}")
    for l in layers or []:
        fc_core.say(f"layer  {l['branch'].removeprefix(fc_layers.layer_prefix(repo, proj))}  {fc_layers.layer_note(l, budget)}")
    for l in layers if preview and layers else []:
        title, body, source = layer_pr(repo, proj, l, len(layers), fc_tasks.code_tasks(fc_tasks.load_tasks(proj)), layers)
        fc_core.say(f"pr     {l['branch']}  {title}  [{source}]\n" + "\n".join(f"    {x}" if x else "" for x in body.splitlines()))
    for line in stale_pr_files(proj, layers) if (preview or submit) and layers else []:
        fc_core.say(f"pr     {line}")
    if submit and gate and layers and (why := gate(repo, proj, layers, trunk, wt)):
        fc_layers.STACK_STOP = why
        fc_core.say(f"stack  STOP  {why}")
        fc_core.sh(["git", "checkout", "-q", "--detach", trunk], wt, check=False)
        return []
    if submit:
        fc_core.sh(["git", "checkout", "-q", stack[-1]], wt, check=False)
        before = {b: gh_pr_text(wt, b) for b in stack}  # which PRs exist, and their text, before this submit touches anything
        q = gh("submit", "--auto")
        fc_core.say(f"submit {'ok  ' if q.returncode == 0 else 'FAIL'}  {(q.stdout + q.stderr).strip()[-400:]}")
        if q.returncode == 0 and layers:
            fc_layers.record_layers(proj, layers)
        prs = {}
        for b in stack if q.returncode == 0 else []:  # a real GitHub stack is one PR per layer, each on the layer below
            v = subprocess.run(["gh", "pr", "view", b, "--json", "number,baseRefName,url"], cwd=wt, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            pr = json.loads(v.stdout) if v.returncode == 0 else None
            fc_core.say(f"submit {'PR #%d on %s  %s' % (pr['number'], pr['baseRefName'], pr['url']) if pr else 'NO PR for'}  {b}")
            lay = next((l for l in layers or [] if l["branch"] == b), None)
            if pr:
                prs[b] = pr["number"]
            if pr and lay:
                title, body, source = layer_pr(repo, proj, lay, len(layers), fc_tasks.code_tasks(fc_tasks.load_tasks(proj)), layers)
                fc_core.say(f"pr     {b}  {title}  [{source}]")
                write_pr_text(proj, wt, b, before[b], title, body)
        if prs:
            f = proj / "results" / "prs.json"
            fc_core.atomic_write(f, json.dumps({**(json.loads(f.read_text()) if f.exists() else {}), **prs}, indent=2))
    fc_core.sh(["git", "checkout", "-q", "--detach", trunk], wt, check=False)  # free the branches for the user's checkout
    return [stack]
