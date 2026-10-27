import hashlib, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path
import fc_core, fc_config, fc_herdr, fc_tasks, fc_worktree


CONFIG_FILE_RE = re.compile(r"(^|/)(package(-lock)?\.json|[^/]*\.lock|pnpm-lock\.yaml|go\.(mod|sum)|requirements[^/]*\.txt|[^/]*\.(ya?ml|toml|ini)|\.[^/]*rc(\.\w+)?|[^/]*\.config\.\w+)$")

def layer_prefix(repo, proj):
    """Review layer branches are `<prefix>layer-NN`, named from the task prefix: the prefix recorded in verify.json (written when
    layers are first built) wins, else `fc_worktree.task_prefix(repo, proj)`, so a project whose `fast/<project>/` task branches
    exist, locally or as remote-tracking refs, keeps `fast/<project>/layer-NN`, and a new project names its layers from its
    stored branch."""
    cfg = fc_core.project_config(proj)
    if cfg.get("layer_prefix"):
        return cfg["layer_prefix"]
    return fc_worktree.task_prefix(repo, proj)

def layer_branches(repo, proj):
    """The project's layer branches, bottom first. Only `<prefix>layer-<digits>` is a layer: a backup such as `layer-01-prerebase` is not."""
    prefix = layer_prefix(repo, proj)
    return sorted((r for r in fc_worktree.heads(repo) if re.fullmatch(rf"{re.escape(prefix)}layer-\d+", r)), key=lambda r: int(r.rsplit("layer-", 1)[1]))

COMPOSE_COMMANDS = "verify|run|stack|resume|status|clean|sync|init|done|watch|crew|change|linear|report|reword|land|doctor|setup|update|cli|skill|project|tasks?|waves?"
PROCESS_RE = re.compile(
    rf"(?<![\w./-])compose(?=\s+(?:{COMPOSE_COMMANDS})\b)|\bby compose\b|(?<![\w./-])compose['’]s\b|`compose`"
    r"|\bwaves?\b(?=\s*[-#]?\d)|\b(?:this|next|last|first|second|third|each|every|previous) wave\b"
    r"|\btasks?[ -]?#?\d{2,3}\b|\blinear (?:issues?|tickets?|stor(?:y|ies)|tasks?|comments?|project)\b"
    r"|\b(?:fix|verify|review) rounds?(?=\s*[-#]?\d)|^co-authored-by:", re.I | re.M)
ROLE_RE = re.compile(r"\bteammates?\b|\bimplementers?\b|\bteam lead\b", re.I)  # narration in a body ("done by the implementer"), plain nouns in a task title

def typed_subject(subject, conv):
    """Whether a subject starts with one of the convention's types (`feat:`, `fix(api):`, `feat!:`); True when there is no convention."""
    return not conv or bool(re.match(r"(?:%s)(?:\([^)]*\))?!?: \S" % "|".join(map(re.escape, conv["types"])), subject))

def message_problem(message, conv=None, title=False):
    """Why a commit message breaks the commit rules, or "" when it keeps them: it names the process (compose and its commands,
    waves, task ids, teammates, implementers, the team lead, Linear issues, a numbered verify or fix round, a co-author trailer), or the
    repo declares a commit convention (verify.json `commit_convention`) and the subject starts with none of its types. A task
    title (`title=True`) may use the role words (implementer, teammate, team lead): in a title they name a part of the code."""
    subject = (message or "").strip().split("\n")[0]
    if not subject:
        return "the message is empty"
    if (hit := PROCESS_RE.search(message) or (not title and ROLE_RE.search(message))):
        return f"it mentions the process (`{hit.group(0).strip()}`); a commit describes the change itself, in plain words"
    if re.match(r"\s*\d{3}[:.\s-]", subject):
        return "it starts with a task id; a commit describes the change itself, in plain words"
    if not typed_subject(subject, conv):
        return f"it has no type prefix; the repo's commit convention starts with one of {', '.join(conv['types'])}"
    return ""

def commit_kind(files, subject=""):
    """What a commit changes, as fc_core.typed knows it: test (tests only), docs, deps (dependency or config files only), fix (a
    subject that starts with Fix), else feat."""
    tests = bool(files) and all(re.search(r"(^|/)(tests?|__tests__)/|[._-](test|spec)\.|(^|/)test_", f) for f in files)
    return ("test" if tests else "docs" if files and all(fc_config.DOC_RE.search(f) for f in files)
            else "deps" if files and all(CONFIG_FILE_RE.search(f) for f in files) else "fix" if re.match(r"fix\b", subject, re.I) else "feat")

def scrub_message(message, files, conv=None):
    """A message that keeps the commit rules, for a commit whose own does not: the task id cut off the subject, the body lines
    that name the process dropped, a type prefix added when the convention wants one; when no subject is left, `Update <files>`
    (`Add tests in <files>` for tests only). The message itself when it already keeps the rules."""
    if not message_problem(message, conv):
        return message
    first, *rest = message.strip().split("\n")
    subject = re.sub(r"^\s*(?:tasks?[ -]?#?)?\d{3}[:.\s-]*", "", first).strip()
    kind = commit_kind(files, subject)
    if not subject or PROCESS_RE.search(subject) or ROLE_RE.search(subject):
        names = ", ".join(Path(f).name for f in files[:3]) or "files"
        subject = f"Add tests in {names}" if kind == "test" else f"Update {names}"
    if not typed_subject(subject, conv):
        subject = fc_core.typed(subject, kind, conv)
    return "\n".join([subject, *(l for l in rest if not PROCESS_RE.search(l) and not ROLE_RE.search(l))]).rstrip()

def commit_message(meta, conv=None):
    """A task's commit says what changed, in the task title's words: no tool name, task id, or process (a title that names the
    process gives `Update <files>`). With a commit convention it starts with the type of the change: docs or dependency/config
    files only, else fix for a "Fix ..." title, else feat."""
    title = re.sub(r"^\s*\d{3}[:.\s-]*", "", meta.get("title") or "").strip()
    files = meta.get("files") or []
    msg = title if title and not message_problem(title, title=True) else "Update " + ", ".join(files or ["files"])
    return fc_core.typed(msg, commit_kind(files, title), conv)


def chain_order(repo, proj):
    """Branches bottom to top, in stack_order (deps, then id), except that ancestry wins: a branch whose tip already contains
    another's sits above it (a rebase can never move a commit below history it contains). Only real ancestry counts: a task
    based on a low layer has as many commits as the layer above its base, and is not on that layer's line."""
    order = fc_worktree.stack_order(repo, proj)
    tip = {b: fc_core.sh(["git", "rev-parse", b], repo).stdout.strip() for b in order}
    below = {b: set() for b in order}  # b -> the other branches its tip contains; one at the same tip is no constraint
    prefix = fc_worktree.task_prefix(repo, proj)
    for a in order:
        held = fc_core.sh(["git", "for-each-ref", "--contains", tip[a], "--format=%(refname)", f"refs/heads/{prefix}*"], repo).stdout.split()
        for b in (r.removeprefix("refs/heads/") for r in held):
            if b in below and tip[b] != tip[a]:
                below[b].add(a)
    out = []
    while order:  # ancestry has no cycles, so some branch always has everything it contains placed
        b = next(b for b in order if below[b] <= set(out))
        out.append(b)
        order.remove(b)
    return out

STACK_STOP = ""  # why the last linearize stopped short, with the commands that resolve it; empty when the chain is whole

def chain_conflict(b, prev, files, upstream, source):
    """The STACK_STOP of a chain rebase of `b` onto `prev` that conflicted in `files`, with the command that resolves it."""
    return (f"{b} conflicts when rebased onto {prev} in {files}; the layers below it are kept. Resolve by hand: "
            f"git checkout {b} && git rebase --onto {prev} {upstream} ({source}), then re-run")

def linearize(repo, proj, trunk, wt, through=()):
    with fc_worktree.STACK_LOCK:
        return _linearize(repo, proj, trunk, wt, through)

def _linearize(repo, proj, trunk, wt, through=()):
    """Rebase the project's unmerged task branches into one chain on the current trunk tip, layer on layer, inside the CLI's
    worktree. Returns the branches that now form a clean chain; a conflict stops the chain there and is reported, and STACK_STOP
    says where: a caller that acts on the stack top (verify, top, the stack) refuses the partial chain then. With `through`, the
    chain stops once the last of those branches is placed: the layers above stay as they are, carrying what they were built on."""
    global STACK_STOP
    STACK_STOP = ""
    chain = chain_order(repo, proj)
    linear, prev = [], trunk
    replaced = fc_worktree.replaced_tips(repo, proj)
    prefix = fc_worktree.task_prefix(repo, proj)
    tips = list(replaced)  # (branch, a tip it no longer has): rebuilt layers, plus every layer this pass rebases
    before = {}  # branch -> its tip before this pass rebased it
    contains = lambda anc, b: fc_core.sh(["git", "merge-base", "--is-ancestor", anc, b], repo, check=False).returncode == 0
    waiting = set(through)
    for b in chain:
        # A replaced layer's old commits are dropped even while it is not back in the chain (still running, failed, or moved
        # above b by a re-plan): b may already sit on prev and carry them.
        stale = [t for rb, t in replaced if rb != b and contains(t, b) and not contains(t, prev)]
        if stale or not contains(prev, b):
            # b still carries the old commits of what it was built on: a layer below that was rebuilt or rebased, a layer still
            # chained as it was but now below a new one, or the trunk itself when it was rewritten (rebased onto a moved main).
            # Replay only b's own commits: start above the deepest tip it was built on whose work the chain already holds, or
            # that is being rebuilt: one recorded in this pass (a replaced layer, or a layer rebased just now), a chained layer
            # as it is now, or one in the reflog of a layer below (fork-point), which holds a rewrite made before this pass,
            # by hand or by a redo whose replaced ref is gone. Else one in the trunk's reflog; without any, a plain rebase.
            # A reflog tip that the layer's own tip sits below is no rewrite: the layer was moved back (its commits moved up
            # by hand to b), so those commits are b's own now.
            old = [(t, "the old base recorded in this pass") for rb, t in tips if (rb in linear or t in stale) and contains(t, b)]
            old += [(x, f"{x}, the chained layer it sits on") for x in linear if contains(x, b)]
            for x in dict.fromkeys([prev, *linear]):  # b may sit on the old tip of any layer below, not only the one it is going onto
                fork = fc_core.sh(["git", "merge-base", "--fork-point", x, b], repo, check=False).stdout.strip()
                start = before.get(x) or fc_core.sh(["git", "rev-parse", x], repo).stdout.strip()
                old += [(fork, f"the old base from the reflog of {x}")] if fork and (fork == start or not contains(start, fork)) else []
            upstream, source = max(old, key=lambda o: int(fc_core.sh(["git", "rev-list", "--count", f"{trunk}..{o[0]}"], repo).stdout)) if old else ("", "")
            if not upstream:
                upstream = fc_core.sh(["git", "merge-base", "--fork-point", trunk, b], repo, check=False).stdout.strip()
                source = f"the old base from the reflog of {trunk}"
            if not upstream:
                upstream, source = prev, "no old base found: a plain rebase"
            before[b] = fc_core.sh(["git", "rev-parse", b], repo).stdout.strip()
            tips.append((b, before[b]))
            fc_core.sh(["git", "checkout", "-q", b], wt)
            r = fc_core.sh(["git", "rebase", "-q", "--onto", prev, upstream], wt, check=False)
            if r.returncode:
                files = ", ".join(fc_core.sh(["git", "diff", "--name-only", "--diff-filter=U"], wt, check=False).stdout.split()) or "no file git names"
                fc_core.sh(["git", "rebase", "--abort"], wt, check=False)
                fc_core.sh(["git", "checkout", "-q", "--detach", trunk], wt, check=False)
                STACK_STOP = chain_conflict(b, prev, files, upstream, source)
                fc_core.say(f"stack  STOP  {STACK_STOP}")
                break
        linear.append(b)
        prev = b
        waiting.discard(b)
        if through and not waiting:
            break
    fc_core.sh(["git", "checkout", "-q", "--detach", trunk], wt, check=False)
    for rb, t in replaced:  # an old tip no layer carries any more is done with
        if not any(contains(t, b) for b in chain):
            fc_core.sh(["git", "update-ref", "-d", f"refs/compose/replaced/{proj.name}/{rb.removeprefix(prefix)}/{t}"], repo, check=False)
    return linear

REVIEW_LAYER_LINES = 300  # verify.json `review_layer_lines` changes it
def commit_facts(repo, c):
    """(files, changed lines, {generated file: lines}) of one commit. Lockfiles are not counted: nobody reads them. A file the
    commit deletes whole counts one line, as `branch_budget` sizes a task branch: the change is the deletion, not its content."""
    deleted = set(fc_core.sh(["git", "show", "--diff-filter=D", "--name-only", "--no-renames", "--format=", c], repo).stdout.splitlines())
    files, counted, generated = [], 0, {}
    for l in fc_core.sh(["git", "show", "--numstat", "--no-renames", "--format=", c], repo).stdout.splitlines():
        a, d, f = l.split("\t", 2)
        n = (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)  # a binary file shows "-"
        files.append(f)
        if fc_worktree.GENERATED_RE.search(f):
            generated[f] = generated.get(f, 0) + n
        else:
            counted += 1 if f in deleted else n
    return files, counted, generated

def group_units(units, budget):
    """Consecutive units into layers of at most `budget` counted lines, in order. A unit (a task with its tests, or one groundwork
    commit) is never split, so one bigger than the budget is a layer of its own; groundwork and a task never share a layer."""
    layers = []
    for u in units:
        if layers and (layers[-1][-1]["name"] == "groundwork") == (u["name"] == "groundwork") \
                and sum(x["lines"] for x in layers[-1]) + u["lines"] <= budget:
            layers[-1].append(u)
        else:
            layers.append([u])
    return layers

def test_writers(proj, tasks):
    """(task id, first report, last report) of each run of a task's test-writing teammate `tests-NNN`, in epoch seconds, the latest
    started first: it made its commits between its first and last report, and a run ends with its `done`. A writer redone later has
    one span per run, never one over the gap between them, which holds other writers' commits. Writers of one test file never run
    at once, and a later one starts only once the earlier one's commits are collected, so the latest whose span holds a commit's
    author date made it."""
    spans = []
    for t in tasks:
        f = Path(proj) / "results" / "crew" / f"{fc_herdr.agent_name(proj.name, f'tests-{t}')}.status"
        run = []
        for l in f.read_text().splitlines() if f.exists() else []:
            if not l.strip():
                continue
            r = json.loads(l)
            run.append(time.mktime(time.strptime(r["at"], "%Y-%m-%d %H:%M:%S")))
            if r.get("state") == "done":
                spans.append((t, min(run), max(run)))
                run = []
        if run:
            spans.append((t, min(run), max(run)))
    return sorted(spans, key=lambda s: -s[1])

def applies_without(repo, c, drop, files):
    """Whether commit c applies to its parent with the earlier commits `drop` taken out of `files` first: what a cherry-pick of c
    meets in a layer below theirs. Checked on a scratch index, so the repo's own index and tree are untouched."""
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(d, "index")}
        git = lambda *a, patch=None: subprocess.run(["git", *a], cwd=repo, input=patch, capture_output=True, env=env).returncode == 0
        diff = lambda x, *paths: subprocess.run(["git", "diff", "--binary", f"{x}^", x, "--", *paths], cwd=repo, capture_output=True, check=True).stdout
        return git("read-tree", f"{c}^") and all(git("apply", "--cached", "-R", patch=diff(p, *sorted(files))) for p in reversed(drop)) \
            and git("apply", "--cached", patch=diff(c))

def review_units(repo, proj, base, trunk, chain, held=()):
    """The project's work as ordered units: every commit the project branch carries over `base` that no task owns (groundwork,
    one unit per commit), then each chain task with its acceptance-test commits first and its own commits after. A test commit
    is a task's when every file it touches is in that task's `protected:`; when its author date falls in the span of a test
    writer whose task protects its files, it is that writer's task's alone. The tests of a task not in the chain yet ride with
    the first one whose own commits edit their files (it was built on them), else the last one. The layers must apply: an owned commit goes to its writer's task, or without one to the first task that
    protects its files, as long as it applies there without the earlier commits on its files placed higher (a test written in
    its own part of a shared file skips them; one written on top of them does not). Else it goes no lower than the highest of
    them, to the first task at or above it that protects its files; one no task owns is a groundwork unit of its own right
    above the highest one's unit, never inside it. A unit's `owned` says which tasks each of its test commits is for (its writer's
    task, else every task that protects its files), so build_layers can tell where a task's tests exist."""
    protected = {m["id"]: set(m["protected"]) for m in fc_tasks.code_tasks(fc_tasks.load_tasks(proj))}
    ids = [b.removeprefix(fc_worktree.task_prefix(repo, proj)) for b in chain]
    def unit(name, commits, key=None):
        facts = [commit_facts(repo, c) for c in commits]
        gen = {}
        for _, _, g in facts:
            for f, n in g.items():
                gen[f] = gen.get(f, 0) + n
        return {"name": name, "key": key or name, "commits": commits, "lines": sum(n for _, n, _ in facts), "generated": gen,
                "owned": {c: of[c] for c in commits if c in of}}
    # slot 2i is task i's unit, 2i+1 the groundwork right above it; placed: (commit, slot, files) of each commit placed in one
    edits = [set(fc_core.sh(["git", "log", "--no-merges", "--name-only", "--no-renames", "--format=", f"{a}..{b}"], repo).stdout.split())
             for a, b in zip([trunk, *chain], chain)]  # the files each chain task's own commits touch
    ground, slots, placed, writers, of = [], {}, [], test_writers(proj, protected), {}
    for c, at in (l.split() for l in fc_core.sh(["git", "log", "--reverse", "--no-merges", "--format=%H %at", f"{base}..{trunk}"], repo).stdout.splitlines()):
        files = set(commit_facts(repo, c)[0])
        writer = next((t for t, a, b in writers if a <= int(at) <= b and files and files <= protected[t]), None)
        owners = [i for i, t in enumerate(ids) if files and files <= protected.get(t, set()) and writer in (None, t)]
        if not owners and files and any(files <= protected[t] for t in held):
            continue
        if not owners and files and any(files <= protected[t] for t in set(protected) - set(ids)):
            owners = [next((i for i, e in enumerate(edits) if e & files), len(ids) - 1)]
        on = [(p, s) for p, s, f in placed if f & files]
        floor = max((s for _, s in on), default=None)
        fits = lambda slot: floor is None or slot >= floor or applies_without(repo, c, [p for p, s in on if s > slot], files)
        if owners:
            slot = next((2 * i for i in owners if fits(2 * i)), floor)
            of[c] = [ids[i] for i in owners]
        else:
            slot = None if floor is None else floor | 1
        if slot is None:
            ground.append(unit("groundwork", [c]))
        else:
            slots.setdefault(slot, []).append(c)
            placed.append((c, slot, files))
    units, prev = ground, trunk
    for i, (b, t) in enumerate(zip(chain, ids)):
        units.append(unit(t, slots.get(2 * i, []) + fc_core.sh(["git", "rev-list", "--reverse", "--no-merges", f"{prev}..{b}"], repo).stdout.split()))
        units += [unit("groundwork", [c], f"groundwork after {t}") for c in slots.get(2 * i + 1, [])]
        prev = b
    return toolchain_first(repo, units, {m["id"] for m in fc_tasks.code_tasks(fc_tasks.load_tasks(proj)) if m.get("kind") == "toolchain"})

def toolchain_first(repo, units, toolchain):
    """`units` with each `toolchain` task's unit moved to the bottom, below groundwork, so every layer has the tools verify's checks
    run (a Storybook build, a new lint). It moves only past units that touch none of its files, so the layers still replay the
    chain; one that cannot stays where it is and says which files hold it."""
    seen = {}
    def files(u):
        for c in u["commits"]:
            if c not in seen:
                seen[c] = set(commit_facts(repo, c)[0])
        return set().union(*(seen[c] for c in u["commits"]))
    low, rest = [], []
    for u in units:
        if u["name"] in toolchain:
            hit = [x for x in rest if files(x) & files(u)]
            if not hit:
                low.append(u)
                continue
            shared = sorted(set().union(*(files(x) for x in hit)) & files(u))
            fc_core.say(f"stack  note  toolchain task {u['name']} stays above {', '.join(dict.fromkeys(x['name'] for x in hit))}: "
                        f"it changes {', '.join(shared)} too, so a check its tools bring fails below its layer")
        rest.append(u)
    return low + rest

def commit_key(repo, c):
    """What names a commit across rebuilds and rebases, which change its hash: its author, author date, and the files it touches."""
    who = fc_core.sh(["git", "show", "-s", "--format=%an <%ae> %aI", c], repo).stdout.strip()
    files = fc_core.sh(["git", "show", "--name-only", "--no-renames", "--format=", c], repo).stdout.split()
    return hashlib.sha1("\0".join([who, *sorted(files)]).encode()).hexdigest()[:16]

def rewords(proj):
    """{commit_key: {"message", "was"}}: the messages `compose reword` chose, applied to the layers every time they are rebuilt."""
    f = Path(proj) / "rewords.json"
    return json.loads(f.read_text()) if f.exists() else {}

def layer_message(repo, proj, c, conv=None):
    """(message, why) for the message commit c has in a layer when it differs from its own: the one `compose reword` chose for it,
    else its own with the process scrubbed out when it breaks the commit rules (scrub_message), `why` naming that rule.
    None when its own stands."""
    own = fc_core.sh(["git", "show", "-s", "--format=%B", c], repo).stdout.strip()
    chosen = rewords(proj).get(commit_key(repo, c))
    if chosen:
        return (chosen["message"], "chosen with compose reword") if chosen["message"] != own else None
    if not (why := message_problem(own, conv)):
        return None
    files = fc_core.sh(["git", "show", "--name-only", "--no-renames", "--format=", c], repo).stdout.split()
    return scrub_message(own, files, conv), why

def cherry_pick_same(wt, c, message=None):
    """Cherry-pick keeping the original committer and date, so a layer rebuilt from unchanged commits gets the same hashes and
    a resubmit pushes nothing new; `message` replaces the commit's own. None when it applied, else the files it conflicts in
    (aborted, so the tree is as before)."""
    who = fc_core.sh(["git", "show", "-s", "--format=%cn%n%ce%n%cI", c], wt).stdout.splitlines()
    env = {**os.environ, "GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1], "GIT_COMMITTER_DATE": who[2]}
    p = fc_core.sh(["git", "cherry-pick", "--allow-empty", c], wt, check=False, env=env)
    if p.returncode == 0:
        if message:
            fc_core.sh(["git", "commit", "--amend", "--allow-empty", "-q", "-m", message], wt, env=env)
        return None
    conflicts = fc_core.sh(["git", "diff", "--name-only", "--diff-filter=U"], wt, check=False).stdout.split()
    fc_core.sh(["git", "cherry-pick", "--abort"], wt, check=False)
    return conflicts

def record_layers(proj, layers):
    """Remember which tasks each layer holds once the stack is submitted: from then on a layer has a PR, so its tasks stay in it.
    A land in progress (land.json) belongs to the layers it started from, so it ends here."""
    (Path(proj) / "results" / "land.json").unlink(missing_ok=True)
    fc_core.atomic_write(Path(proj) / "layers.json", json.dumps({l["branch"]: list(dict.fromkeys(l.get("keys", l["tasks"]))) for l in layers}, indent=2))

def group_by_record(proj, units, budget, max_prs=None, landed=()):
    """Groups of units: each recorded layer keeps its own tasks (groundwork commits go to the layer that held groundwork, or
    groundwork above that task), and
    units no layer holds yet are grouped as usual into new layers above. `max_prs` caps the groups (None: no cap): while the
    groups outnumber it, the adjacent unrecorded pair of the same kind (both all-groundwork, or both holding tasks) with the
    fewest combined lines folds, the lower pair on a tie, else the adjacent pair of any kind with the fewest; recorded layers
    never fold. A `landed` layer gets no group: the units its keys hold are on the base already.
    [(recorded branch or None, units, folded)]."""
    rec_file = Path(proj) / "layers.json"
    rec = json.loads(rec_file.read_text()) if rec_file.exists() else {}
    groups, seen = [], set()
    for b, keys in rec.items():
        held = [i for i, u in enumerate(units) if i not in seen and u.get("key", u["name"]) in keys]
        seen.update(held)
        if held and b not in landed:
            groups.append((b, [units[i] for i in held], False))
    groups += [(None, g, False) for g in group_units([u for i, u in enumerate(units) if i not in seen], budget)]
    kind = lambda g: "groundwork" if all(u["name"] == "groundwork" for u in g[1]) else "task"
    while max_prs is not None and len(groups) > max_prs:
        pairs = [(0 if kind(a) == kind(b) else 1, sum(u["lines"] for u in a[1]) + sum(u["lines"] for u in b[1]), i)
                 for i, (a, b) in enumerate(zip(groups, groups[1:])) if a[0] is None and b[0] is None]
        if not pairs:
            break
        i = min(pairs)[2]
        groups[i:i + 2] = [(None, groups[i][1] + groups[i + 1][1], True)]
    return groups

def differing_files(changed, tasks, held):
    """The files of `changed` (layers' top against the chain's) that make the layers differ from the chain: all but the protected files of the `held` tasks, which the layers leave out on purpose."""
    skip = {f for m in tasks if m["id"] in held for f in m["protected"]}
    return [f for f in changed if f not in skip]

def build_layers(repo, proj, base, trunk, chain, wt, budget, max_prs=None, held=(), landed=()):
    """Cut the project into review layers on `base`: branches `<layer_prefix>layer-NN`, each at most `budget` counted lines
    (groundwork first, then tasks with their tests), rebuilt from the commits every time. Returns [{branch, tasks, lines,
    generated, commits, units, tested}] (units: the commits of each entry of tasks, in order; tested: the tasks whose `test` the gate
    runs on the layer: the lowest one that holds a task's code and every test commit written for it). The layers are built on a detached HEAD and
    the layer branches move only once all of them are built and add up to the same tree as the task chain; else nothing is
    changed, STACK_STOP says why, and it returns None."""
    units = review_units(repo, proj, base, trunk, chain, held)  # groundwork is layers of its own, never mixed into a task's
    groups = group_by_record(proj, units, budget, max_prs, landed)
    layers, tips, prev, prefix = [], [], base, layer_prefix(repo, proj)
    code_at, test_at = {}, {}  # task -> index of the layer with its code, and of the last layer with a test commit for it
    cfg_file = Path(proj) / "verify.json"
    if cfg_file.exists() and fc_core.project_config(proj).get("layer_prefix") != prefix:  # recorded once, so later stacks never have to guess
        fc_core.atomic_write(cfg_file, json.dumps({**fc_core.project_config(proj), "layer_prefix": prefix}, indent=2))
    kept = "the layers and the stack are left as they were"
    conv = fc_core.commit_convention(proj)
    first_line = lambda c: fc_core.sh(["git", "show", "-s", "--format=%s", c], repo).stdout.strip()
    def stop(why):
        global STACK_STOP
        fc_core.sh(["git", "checkout", "-q", "--detach", trunk], wt, check=False)
        STACK_STOP = why
        fc_core.say(f"stack  STOP  {why}")
    recorded = {b for b, _, _ in groups}
    for i, (rec_branch, g, folded) in enumerate(groups, 1):  # a new layer is numbered above the landed ones, and never takes a recorded name
        branch = rec_branch or next(f"{prefix}layer-{n:02d}" for n in range(i + len(landed), 1000) if f"{prefix}layer-{n:02d}" not in recorded)
        fc_core.sh(["git", "checkout", "-q", "--detach", prev], wt)
        units = []
        for u in g:
            units.append([])
            for c in u["commits"]:
                message, why = layer_message(repo, proj, c, conv) or (None, "")
                if message:
                    fc_core.say(f"stack  note  {c[:7]} message: {first_line(c)!r} becomes {message.splitlines()[0]!r}: {why}")
                conflicts = cherry_pick_same(wt, c, message)
                if conflicts is not None:
                    files = ", ".join(conflicts) or "no file git names"
                    if fc_core.sh(["git", "merge-base", "--is-ancestor", base, trunk], repo, check=False).returncode:  # trunk is behind base
                        at = fc_core.sh(["git", "rev-parse", "--short", base], repo).stdout.strip()
                        stop(f"{c[:7]} does not apply on layer {i}: {trunk} conflicts with {base} {at} in {files}; {kept}. "
                             f"Rebase {trunk} onto {base}, then re-run")
                    else:
                        stop(f"{c[:7]} does not apply on layer {i} (conflicts in {files}); {kept}")
                    return None
                units[-1].append(fc_core.sh(["git", "rev-parse", "HEAD"], wt).stdout.strip())
        if landed and len(set(fc_core.sh(["git", "rev-parse", "HEAD", prev], wt).stdout.split())) == 1:  # nothing over the layer below: the base holds it
            continue
        gen = {}
        for u in g:
            for f, n in u["generated"].items():
                gen[f] = gen.get(f, 0) + n
        layers.append({"branch": branch, "tasks": [u["name"] for u in g], "keys": [u["key"] for u in g], "lines": sum(u["lines"] for u in g), "generated": gen,
                       "commits": [c for cs in units for c in cs], "units": units,
                       **({"fold_cap": max_prs} if folded else {})})
        for u in g:
            if u["name"] != "groundwork":
                code_at[u["name"]] = len(layers) - 1
            for ts in u.get("owned", {}).values():
                test_at.update({t: len(layers) - 1 for t in ts})
        prev = fc_core.sh(["git", "rev-parse", "HEAD"], wt).stdout.strip()
        tips.append(prev)
    fc_core.sh(["git", "checkout", "-q", "--detach", trunk], wt, check=False)
    if layers and fc_core.sh(["git", "merge-base", "--is-ancestor", base, chain[-1]], repo, check=False).returncode == 0:
        differ = differing_files(fc_core.sh(["git", "diff", "--name-only", prev, chain[-1]], repo).stdout.split(), fc_tasks.code_tasks(fc_tasks.load_tasks(proj)), held)
        if differ:
            stop(f"the layers differ from {chain[-1]} in {', '.join(differ)}; {kept}")
            return None
    for l in layers:
        l["tested"] = []
    for t, at in code_at.items():  # a task is tested on the lowest layer with its code and its tests, which may be above its code
        layers[max(at, test_at.get(t, 0))]["tested"].append(t)
    for l, tip in zip(layers, tips):
        fc_core.sh(["git", "branch", "-f", l["branch"], tip], repo)
    for old in layer_branches(repo, proj):
        if old not in {l["branch"] for l in layers}:
            fc_core.sh(["git", "branch", "-D", old], repo, check=False)
    return layers

def layer_note(l, budget):
    """One layer as a line for the log: what it holds, its size, and the generated files kept out of the count."""
    names = [t for t in l["tasks"] if t != "groundwork"]
    held = ([f"groundwork x{l['tasks'].count('groundwork')}"] if "groundwork" in l["tasks"] else []) + ([f"task {', '.join(names)}"] if names else [])
    gen = "".join(f"; generated, not counted: {f} ({n} lines)" for f, n in l["generated"].items())
    over = ""
    if l["lines"] > budget:
        over = (f"; over the {budget}-line budget to stay within {l['fold_cap']} PRs" if l.get("fold_cap")
                else f"; over the {budget}-line budget, a {'commit' if l['tasks'] == ['groundwork'] else 'task'} is never split" if len(l["tasks"]) == 1 else "")
    return f"{' + '.join(held)}: {l['lines']} lines{over}{gen}"
