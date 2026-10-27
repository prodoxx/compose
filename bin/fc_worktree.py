import json, os, re, shutil, sys, threading, time
from pathlib import Path
import fc_core, fc_config, fc_tasks


# ---------- stacks (gh stack) ----------

_prefixes = {}
_commons = {}
_heads = {}

def _common(repo):
    """The git common dir of `repo`, looked up once per process; a repo that has none yet is looked up again next time."""
    key = Path(repo).resolve()
    if key not in _commons and (common := common_dir(repo)):
        _commons[key] = common
    return _commons.get(key)

def refs_stamp(common):
    def st(p):
        try:
            s = os.stat(p)
            return (s.st_mtime_ns, s.st_size)
        except OSError:
            return None
    dirs = [d for sub in ("heads", "remotes") for d, _, _ in os.walk(Path(common) / "refs" / sub)]
    return (st(Path(common) / "packed-refs"), *(st(d) for d in dirs))

def task_prefix(repo, proj):
    """What every task, bench and test-writer branch of the project starts with: verify.json `task_prefix` when recorded, else
    `fast/<project>/` while such branches exist (recorded once), else the project branch plus `-`."""
    key = (Path(repo).resolve(), Path(proj).resolve())
    f = proj / "verify.json"
    def stamp():
        common = _common(repo)
        try:
            s = os.stat(f)
            vs = (s.st_mtime_ns, s.st_size)
        except OSError:
            vs = None
        return (vs, refs_stamp(common) if common else None)
    hit = _prefixes.get(key)
    if hit and hit[0] == stamp():
        return hit[1]
    rec = json.loads(f.read_text()) if f.exists() else {}
    if rec.get("task_prefix"):
        answer = rec["task_prefix"]
    elif fc_core.sh(["git", "for-each-ref", "--format=%(refname)", f"refs/heads/fast/{proj.name}/", f"refs/remotes/*/fast/{proj.name}/*"],
                    repo, check=False).stdout.strip():
        if f.exists():
            fc_core.atomic_write(f, json.dumps({**rec, "task_prefix": f"fast/{proj.name}/"}, indent=2))
        answer = f"fast/{proj.name}/"
    else:
        answer = fc_core.project_branch(proj) + "-"
    _prefixes[key] = (stamp(), answer)
    return answer

def heads(repo):
    """The local branch names, listed once and again only when the refs change."""
    common = _common(repo)
    stamp = refs_stamp(common) if common else None
    hit = _heads.get(common) if common else None
    if hit and hit[0] == stamp:
        return hit[1]
    out = fc_core.sh(["git", "for-each-ref", "--format=%(refname)", "refs/heads"], repo, check=False).stdout
    names = {l.removeprefix("refs/heads/") for l in out.split()}
    if common:  # without a common dir there is nothing to tell this repo's entry from another's: list afresh every time
        _heads[common] = (stamp, names)
    return names

def rename_legacy_branches(repo, proj):
    """Move a project whose `fast/<project>/` branches were never pushed onto the names a new project gets, once: every local
    fast/<project>/ branch becomes `<project branch>-` plus the same rest (`git branch -m` keeps its reflog and config, and moves it
    in any worktree that has it checked out), and every name recorded for them follows: the project's JSON files (verify.json's
    prefixes, layers.json, pr_text.json, results and change snapshots) and the local gh stack state. A project with any fast/ ref
    on a remote keeps its names, since its PRs carry them; so does one whose run is live in another process, which holds the old
    names, or one where a new name is taken. Returns {old: new}, empty when nothing was renamed."""
    legacy, new = f"fast/{proj.name}/", fc_core.project_branch(proj) + "-"
    if task_prefix(repo, proj) != legacy or fc_core.sh(["git", "for-each-ref", "--format=%(refname)", f"refs/remotes/*/fast/{proj.name}/*"],
                                                        repo, check=False).stdout.strip():
        return {}
    olds = fc_core.sh(["git", "for-each-ref", "--format=%(refname)", f"refs/heads/{legacy}"], repo, check=False).stdout.split()
    names = {b: new + b.removeprefix(legacy) for b in (r.removeprefix("refs/heads/") for r in olds)}
    run = fc_tasks.run_marker(proj) or {}
    taken = [n for n in names.values() if fc_core.sh(["git", "rev-parse", "--verify", "-q", f"refs/heads/{n}"], repo, check=False).returncode == 0]
    if taken or (run.get("pid") not in (None, os.getpid()) and fc_tasks.pid_alive(run["pid"])):
        fc_core.say(f"names  {legacy} kept for now: " + (f"{taken[0]} already exists" if taken else f"run pid {run['pid']} is live"))
        return {}
    for old, n in names.items():
        fc_core.sh(["git", "branch", "-m", old, n], repo)
    _prefixes.clear()
    values = {**names, legacy: new}
    def rename(v):
        if isinstance(v, dict):
            return {names.get(k, k): rename(x) for k, x in v.items()}
        if isinstance(v, list):
            return [rename(x) for x in v]
        return values.get(v, v) if isinstance(v, str) else v
    stack_state = fc_core.checkout(repo).common / "gh-stack"
    for f in [*proj.glob("*.json"), *(proj / "results").rglob("*.json"), stack_state]:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if rename(data) != data:
            fc_core.atomic_write(f, json.dumps(rename(data), indent=2))
    if names:
        fc_core.say(f"names  renamed {len(names)} never-pushed {legacy}* branch(es) to {new}*")
    return names

def task_branch(repo, proj, tid):
    """Task `tid`'s branch: the task prefix plus the id."""
    return task_prefix(repo, proj) + tid

def own_branches(repo, proj):
    """The project's local task (`NNN`), bench (`NNN-bench-...`) and test-writer (`tests-NNN`) branches, merged or not."""
    prefix = task_prefix(repo, proj)
    pat = rf"{re.escape(prefix)}(\d{{3}}(-bench-.+)?|tests-\d{{3}})"
    return [b for b in fc_core.sh(["git", "branch", "--list", f"{prefix}*"], repo, check=False).stdout.split()
            if re.fullmatch(pat, b)]

def replace_branch(repo, proj, branch):
    """Delete a task branch that is about to be rebuilt. Its old tip is kept under a ref: layers stacked above still carry the
    old commits, and the chain rebase must drop them instead of replaying them onto the new layer."""
    tip = fc_core.sh(["git", "rev-parse", "--verify", "-q", branch], repo, check=False).stdout.strip()
    if tip:
        fc_core.sh(["git", "update-ref", f"refs/compose/replaced/{proj.name}/{branch.removeprefix(task_prefix(repo, proj))}/{tip}", tip], repo)
    fc_core.sh(["git", "branch", "-D", branch], repo, check=False)

def replaced_tips(repo, proj):
    """[(branch, old tip)] for the project's replaced task branches (see replace_branch)."""
    out = fc_core.sh(["git", "for-each-ref", "--format=%(refname) %(objectname)", f"refs/compose/replaced/{proj.name}/"], repo, check=False).stdout
    return [(task_branch(repo, proj, ref.split("/")[-2]), tip) for ref, tip in (l.split() for l in out.splitlines())]

def task_branches(repo, proj):
    """Unmerged task branches with their base branch: {branch: base}. Base is the task branch (if any) the branch was built on."""
    prefix = task_prefix(repo, proj)
    refs = fc_core.sh(["git", "for-each-ref", "--format=%(refname)", "--no-merged=HEAD", f"refs/heads/{prefix}*"], repo, check=False).stdout.split()
    branches = [b for b in (r.removeprefix("refs/heads/") for r in refs) if re.fullmatch(rf"{re.escape(prefix)}\d+", b)]  # task branches only, not bench tags
    out = {}
    for b in branches:
        tid = b.removeprefix(prefix)
        res = proj / "results" / f"{tid}.json"
        base = json.loads(res.read_text()).get("base", "HEAD") if res.exists() else "HEAD"
        out[b] = base if base in branches else None
    return out

def layer_order(tasks):
    """The ids of `tasks` bottom to top as one stack: a task after its `after:` tasks among them, a `kind: toolchain` task (it sets
    up the tools verify's checks run) before the rest, then by id. A task whose `after:` task has a higher id is placed right above
    that task, never after the whole pass over the ids, so a story's tasks stay together when a later story's tasks have ids in
    between. The one rule the chain (stack_order, then chain_order) and `compose tasks` share, so the order a planner reviews is
    the order the stack gets."""
    metas = {m["id"]: m for m in tasks}
    ids = sorted(metas, key=lambda t: (metas[t].get("kind") != "toolchain", t))
    order, placed = [], set()
    def place(tid):
        placed.add(tid)  # before its dependencies, so a cycle in `after:` ends here instead of looping
        deps = {d.strip() for d in str(metas[tid].get("after") or "").split(",")} & set(metas)
        for dep in sorted(deps, key=ids.index):
            if dep not in placed:
                place(dep)
        order.append(tid)
    for tid in ids:
        if tid not in placed:
            place(tid)
    return order

def stack_order(repo, proj, tasks=None):
    """All unmerged task branches of the project as one chain, bottom to top, in layer_order: a task after its `after:` tasks,
    then by id. One feature, one stack: every task is a layer, so a reviewer reads one layer's diff at a time. Once tasks are
    synced, a branch whose task is gone from the sync (canceled by a re-plan or an undo) is no layer; its branch is left alone.
    `tasks`: the synced tasks when the caller holds them already, else they are read here."""
    bases = task_branches(repo, proj)
    metas = {m["id"]: m for m in fc_tasks.code_tasks(fc_tasks.load_tasks(proj) if tasks is None else tasks)}
    synced = (proj / "results" / "tasks.json").exists()
    prefix = task_prefix(repo, proj)
    ids = [t for t in (b.removeprefix(prefix) for b in bases) if t in metas or not synced]
    return [task_branch(repo, proj, t) for t in layer_order([metas.get(t, {"id": t}) for t in ids])]

TREEHOUSE_INSTALL = "curl -fsSL https://kunchenguid.github.io/treehouse/install.sh | sh"

def use_treehouse():
    """Slots come from a treehouse pool when treehouse is installed (COMPOSE_NO_TREEHOUSE=1 turns it off), else from plain
    git worktrees. The pool lives inside the repository (`.treehouse/`, git-ignored): a teammate started in a slot then sits under
    a folder Claude Code already trusts, where a slot under ~/.treehouse would stop at the trust prompt."""
    return bool(shutil.which("treehouse")) and os.environ.get("COMPOSE_NO_TREEHOUSE") != "1"

def treehouse(repo, *args, check=True):
    return fc_core.sh(["treehouse", "--root", ".", *args], repo, check=check)

def slot_on(repo, branch, holder, detach=False):
    """Lease a treehouse slot and check `branch` out in it (detached for a read-and-rebase worktree). The branch must exist:
    the work lives on branches, a slot is only a place to stand, and returning it keeps the branch.
    The lease is cut from the default branch, never from `branch`: `return` resets a slot to its lease's base, and treehouse
    reuses a slot only when its commit is an ancestor of the next base, so a slot returned onto a task or test branch was
    "in use or dirty" for every lease after it and the pool ran dry while `status` showed it free.
    An earlier holder's borrowed installs (ready_checkout's links) go: `return` keeps ignored files, and an install through
    such a link lands in another checkout's. The slot's own installs stay."""
    exclude_paths(repo, ["/.treehouse/"])
    p = treehouse(repo, "get", "--lease", "--json", "--no-fetch", "--base", default_branch(repo) or branch, "--lease-holder", holder, check=False)
    if p.returncode and "in use or dirty" in p.stdout + p.stderr:
        sys.exit(f"{holder}: " + (pool_shortfall(repo, 1) or p.stderr.strip().splitlines()[-1]))
    if p.returncode:
        raise RuntimeError(f"treehouse get for {holder}\n{p.stdout}\n{p.stderr}")
    path = Path(json.loads(p.stdout)["path"])
    checkout_in_slot(path, branch, detach)
    return path

def checkout_in_slot(path, branch, detach=False):
    fc_core.sh(["git", "checkout", "-q", *(["--detach"] if detach else []), branch], path)
    for d in TOOL_DIRS:  # an earlier holder's link to a checkout's tool folder: `return` keeps it, and a dead one makes mkdir raise
        if (path / d.rstrip("/")).is_symlink():
            (path / d.rstrip("/")).unlink()
    for d in fc_core.sh(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"], path, check=False).stdout.splitlines():
        if Path(d).name in fc_config.DEP_DIRS and (path / d).is_symlink():
            (path / d).unlink()

def park_slot(path, at):
    """Empty a slot of its holder's work, keeping its lease and installs, so the next holder can take it without a gap in which
    another project's lease takes it: detached at `at`, the holder's branch free to delete, and compose's leftovers gone as in
    release_slot. The lease keeps the first holder's label: treehouse does not relabel a leased slot."""
    clear_leftovers(path)
    fc_core.sh(["git", "checkout", "-q", "--detach", at], path)
    fc_core.sh(["git", "reset", "-q", "--hard"], path)
    fc_core.sh(["git", "clean", "-fdq"], path)  # ignored files stay, as `return` keeps them

def pool_shortfall(repo, need):
    """None when the pool can lease `need` more slots, else one line: how many it needs, how many are free, who holds the rest.
    Free is an available slot, or room under max_trees (treehouse.toml in the main checkout, else treehouse's default 16)."""
    if need <= 0:
        return None
    entries = json.loads(treehouse(repo, "status", "--json", check=False).stdout or "[]")
    cfg = fc_core.checkout(repo).common.parent / "treehouse.toml"
    m = re.search(r"^\s*max_trees\s*=\s*(\d+)", cfg.read_text(), re.M) if cfg.exists() else None
    cap = int(m.group(1)) if m else 16
    free = sum(e.get("status") == "available" for e in entries) + max(0, cap - len(entries))
    if free >= need:
        return None
    holders = [e.get("lease_holder") or f"slot {e.get('name')} ({e.get('status')})" for e in entries if e.get("status") != "available"]
    return (f"needs {need} worktree slot(s), {free} free of max_trees = {cap}; held by {', '.join(holders)}. "
            "Free slots (compose testcrews --collect, compose clean, treehouse return) or raise max_trees in treehouse.toml")

def is_slot(repo, path):
    """Whether a path is a treehouse slot of this repository's pool, asked of treehouse rather than guessed from the path."""
    if not use_treehouse():
        return False
    p = treehouse(repo, "status", "--json", check=False)
    try:
        return any(Path(e["path"]).resolve() == Path(path).resolve() for e in json.loads(p.stdout or "[]"))
    except (json.JSONDecodeError, KeyError, TypeError):
        return False

def default_branch(repo):
    """The repository's default branch, as treehouse infers it: the remote's HEAD, else main, else master."""
    p = fc_core.sh(["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], repo, check=False)
    for b in ([p.stdout.strip().split("/", 1)[-1]] if p.returncode == 0 else []) + ["main", "master"]:
        if fc_core.sh(["git", "rev-parse", "--verify", "-q", f"refs/heads/{b}"], repo, check=False).returncode == 0:
            return b
    return None

def review_base(repo, cfg):
    """The branch a project's work is reviewed and stacked on: `review_base` in its verify.json while it names a commit, else the
    default branch. init records the branch the project started from; one made before it did holds "main" there, also in a
    repository whose trunk is master. None when neither resolves."""
    base = cfg.get("review_base")
    if base and fc_core.sh(["git", "rev-parse", "--verify", "-q", f"{base}^{{commit}}"], repo, check=False).returncode == 0:
        return base
    return default_branch(repo)

def release_slot(repo, path, own=None):
    """Hand a slot back to the pool with its installs intact, and say whether treehouse took it: `return` fails while a live
    process keeps respawning in the slot, and the slot stays leased. compose's own leftovers go first: `return` keeps ignored
    files, so a project's `.compose/` results or graph would otherwise reach the next agent that leases the slot.
    A slot that is still a project's own worktree is never cleared or returned, unless that project is `own`, the one being
    removed: its `.compose/<name>` holds the plan cache, verify.json, and run results (#253 wiped a live project's this way)."""
    held = [n for n, (wt, _) in fc_core.all_projects(repo).items() if n != own and Path(wt).resolve() == Path(path).resolve()]
    if held:
        fc_core.say(f"slot     {path} kept: it is project {', '.join(held)}'s worktree; `compose clean --project {held[0]}` gives it back")
        return False
    clear_leftovers(path)
    return treehouse(repo, "return", "--force", str(path), check=False).returncode == 0

def checked_out(repo, branch):
    """The other worktree of `repo` that has `branch` checked out, or None: git refuses to delete a branch held there."""
    here, path = Path(repo).resolve(), None
    for line in fc_core.sh(["git", "worktree", "list", "--porcelain"], repo, check=False).stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{branch}" and path and path.resolve() != here:
            return path
    return None

def clear_leftovers(path):
    for d in (".compose", "graft", ".trail"):
        t = Path(path) / d
        if t.is_symlink() or t.is_file():
            t.unlink()
        elif t.is_dir():
            shutil.rmtree(t, ignore_errors=True)

def common_dir(path):
    """The git common dir of the checkout at `path`, None when it is not in one: every worktree of a repository shares it."""
    c = fc_core.checkout(path)
    return c.common if c else None

def shared_checkout(path, cli_repo):
    """Why a toolsmith may not commit or edit in `path`, else None: it is the installed copy (~/.compose/src), a main checkout of the
    compose repo `cli_repo` belongs to, or a worktree of it on main, master, or no branch. A path outside that repo is none of this."""
    path = Path(path).resolve()
    c = fc_core.checkout(path)
    top = str(c.top) if c else ""
    if top and c.top == (Path.home() / ".compose" / "src").resolve():
        return f"{top} (the installed copy of compose)"
    theirs = common_dir(path)
    if not top or theirs is None or theirs != common_dir(cli_repo):
        return None
    if c.git_dir == theirs:
        return f"{top} (a main checkout of the compose repo)"
    branch = fc_core.sh(["git", "branch", "--show-current"], path).stdout.strip()
    return f"{top} (on {branch or 'no branch'})" if branch in ("", "main", "master") else None

def repo_worktree(repo, name):
    """(worktree, branch): a new worktree of `repo` on a new branch off its HEAD, under its main checkout's .claude/worktrees
    (a treehouse slot with treehouse), so a new worktree never nests inside a linked one (a project worktree)."""
    theirs = common_dir(repo)
    if not theirs:
        sys.exit(f"{repo} is not a git repo, so there is no worktree of it to work on")
    top, base = theirs.parent, fc_core.sh(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    exclude = theirs / "info" / "exclude"
    if "/.claude/worktrees/" not in (exclude.read_text() if exclude.exists() else ""):
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as f:
            f.write("/.claude/worktrees/\n")
    branch = f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    if use_treehouse():
        fc_core.sh(["git", "branch", branch, base], top)
        return slot_on(top, branch, f"compose:{name}"), branch
    path = top / ".claude" / "worktrees" / branch
    fc_core.sh(["git", "worktree", "add", "-q", "-b", branch, str(path), base], top)
    return path, branch

def slots(proj, update=None):
    """The project's leased slots by role (`stackroot`, `tests-NNN`), kept in results so later commands find them.
    A recorded slot that exists but is no checkout of the project's repository is dropped and named: a clone or copy that
    carries the file would otherwise rebuild the original's branches in its stack worktree and clean out its slots."""
    f = Path(proj) / "results" / "slots.json"
    have = json.loads(f.read_text()) if f.exists() else {}
    if have:
        mine = common_dir(proj)
        foreign = {k: v for k, v in have.items() if Path(v).exists() and (mine is None or common_dir(v) != mine)}
        for role, path in foreign.items():
            theirs = common_dir(path)
            fc_core.say(f"slots  dropped {role} {path}: a checkout of {theirs.parent if theirs else 'no repository'}, "
                        f"not of {mine.parent if mine else proj}; a new slot is used instead")
        if foreign:
            update = {**{k: None for k in foreign}, **(update or {})}
    if update is not None:
        have = {k: v for k, v in {**have, **update}.items() if v}
        f.parent.mkdir(parents=True, exist_ok=True)
        fc_core.atomic_write(f, json.dumps(have, indent=2))
    return have

def stack_worktree(repo, proj, trunk):
    """A detached worktree the CLI owns for every gh stack operation, so the user's checkout is never switched.
    Detached because a branch checked out in the user's tree cannot be checked out again elsewhere."""
    with STACK_LOCK:
        held = slots(proj).get("stackroot")
        if held and (Path(held) / ".git").exists():
            wt = Path(held)
            fc_core.sh(["git", "checkout", "-q", "--detach", trunk], wt, check=False)
        elif use_treehouse():
            wt = slot_on(repo, trunk, f"compose:{proj.name}:stackroot", detach=True)
            slots(proj, {"stackroot": str(wt)})
        else:
            wt = proj / "worktrees" / ".stackroot"
            if not (wt / ".git").exists():
                fc_core.sh(["git", "worktree", "prune"], repo, check=False)
                fc_core.sh(["git", "worktree", "add", "-q", "--detach", str(wt), trunk], repo)
            else:
                fc_core.sh(["git", "checkout", "-q", "--detach", trunk], wt, check=False)
        ready_checkout(repo, wt)
        tools, link = repo / ".compose", wt / ".compose"  # e2e scripts live there, and verify points them at this worktree
        if tools.is_dir() and not link.exists() and not link.is_symlink():
            link.symlink_to(tools)
            exclude_paths(wt, ["/.compose"])  # the `/.compose/` rule does not cover a link
    return wt

def exclude_paths(wt, rules):
    """Add git ignore rules to the checkout's info/exclude, which no commit carries. A linked worktree shares the main
    checkout's file, so one call covers every checkout of the repository."""
    exclude = Path(fc_core.sh(["git", "rev-parse", "--git-path", "info/exclude"], wt).stdout.strip())
    exclude = exclude if exclude.is_absolute() else wt / exclude
    have = exclude.read_text().splitlines() if exclude.exists() else []
    add = [l for l in rules if l not in have]
    if add:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("".join(l + "\n" for l in have + add))

TOOL_DIRS = ("graft/", ".trail/", ".compose/")  # trail moves a repo's graph from graft/ to .trail/cache/
WIRING = (".claude/settings.json", ".claude/settings.local.json")  # the agent wiring trail's and graft's session hooks rewrite
UNSAVED_CLONE = "-unsaved-"  # in the name of a task clone a failed pause snapshot kept, moved aside: `compose clean` names it before it goes

def dirty_paths(repo):
    """The paths `git status` shows changed or untracked in a checkout, without the agent wiring (WIRING) and the tools' own
    folders (TOOL_DIRS, the `.ignore` file): a graph tool's session hook rewrites a tracked .claude/settings.json whenever a
    session starts there, and info/exclude cannot hide a tracked file; a graph built before the checkout ignored graft/ or
    .trail/ shows up as untracked. None of that is a change of the user's, and no clone needs it."""
    fields, paths = fc_core.sh(["git", "status", "--porcelain", "-z", "-uall"], repo).stdout.split("\0"), []
    while fields:
        entry = fields.pop(0)
        if entry:
            paths.append(entry[3:])
            if entry[0] in "RC":  # a rename or copy: its source path is the next field
                fields.pop(0)
    return [p for p in paths if p not in WIRING and p != ".ignore" and not p.startswith(TOOL_DIRS)]

def graph_dir(root):
    """The folder holding the repo's trail graph: .trail/cache/, else an old `graft/` graph.
    None when neither has one."""
    return next((Path(root) / d for d in (".trail/cache", "graft") if (Path(root) / d / "INDEX.md").exists()), None)

def ignore_tool_files(wt):
    """Keep compose's and trail's own files out of the repository, before anything else is written: every checkout
    ignores them through info/exclude (and `/.ignore`), and an existing `.gitignore` gets the two lines, so a clone
    of the delivered branch ignores them too. Returns True when `.gitignore` changed (the caller commits it)."""
    exclude_paths(wt, ["/" + d for d in TOOL_DIRS] + ["/.ignore"])
    gi = wt / ".gitignore"
    if not gi.exists():
        return False
    lines = gi.read_text().splitlines()
    have = {l.strip().strip("/") for l in lines}
    add = [d for d in TOOL_DIRS if d.strip("/") not in have]
    if add:
        gi.write_text("\n".join(lines + add) + "\n")
    return bool(add)

def ignored_entries(wt):
    return fc_core.sh(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"], wt, check=False).stdout.splitlines()

def dep_dirs(wt):
    return [d.rstrip("/") for d in ignored_entries(wt) if d.endswith("/") and Path(d.rstrip("/")).name in fc_config.DEP_DIRS]

def env_files(wt):
    return [f for f in ignored_entries(wt) if not f.endswith("/") and (Path(f).name == ".env" or Path(f).name.startswith(".env."))]

def stamp_trail_wiring(wt):
    """trail's session-start hook rewrites the wired files whenever .trail/cache/.cache/wiring-stamp.json is missing or names
    another version, and .trail/ is git-ignored, so every fresh worktree or clone would get a tracked .claude/settings.json
    edited. A stamp at the installed version makes the hook leave tracked wiring alone."""
    if not shutil.which("trail") or fc_core.sh(["git", "ls-files", "--error-unmatch", ".trail/hook.cjs"], wt, check=False).returncode:
        return
    version = fc_core.sh(["trail", "--version"], wt, check=False).stdout.strip()
    stamp = wt / ".trail" / "cache" / ".cache" / "wiring-stamp.json"
    if version and not (stamp.exists() and json.loads(stamp.read_text() or "{}").get("version") == version):
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps({"version": version, "hosts": ["claude"], "opts": {"global": False, "mcp": True, "hooks": True, "statusline": True}}))

def ready_checkout(repo, wt, borrow=True, install=None):
    """Make a task clone or the CLI's worktree runnable like the main checkout: symlink each git-ignored dependency install
    (node_modules at any depth, vendor, .venv) and each git-ignored .env or .env.* file from it, and keep those links and
    trail's graph out of git status through the checkout's info/exclude. A link is a file to git, so a `node_modules/` rule would not cover it; `/client/node_modules` does.
    `borrow=False` (a task that rewrites a lockfile) links no installs: an install there would write through the link into everyone's.
    `install` (verify.json `install`) runs in the checkout instead of linking: bundlers write a dependency's real path into
    unminified output, so a borrowed install builds different bytes than the project worktree's own. Links from an earlier
    borrow are removed first, so the command never writes through one. Call it with `install` once the checkout sits on the
    commit whose checks will run; it installs again only when that commit's lockfiles differ from the last install's."""
    deps, envs = dep_dirs(repo), env_files(repo)
    exclude_paths(wt, ["/graft/", "/.trail/", *("/" + w for w in WIRING), "/.ignore", *("/" + d for d in deps + envs)])
    stamp_trail_wiring(wt)
    for e in envs:  # settings a build or dev server reads; an install never writes them, so a lockfile task links them too
        link = wt / e
        if link.parent.is_dir() and not link.exists() and not link.is_symlink():
            link.symlink_to(repo / e)
    if install and borrow:
        stamp = Path(fc_core.sh(["git", "rev-parse", "--path-format=absolute", "--git-path", "compose-install"], wt).stdout.strip())
        locks = "".join(l + "\n" for l in fc_core.sh(["git", "ls-tree", "-r", "HEAD"], wt, check=False).stdout.splitlines()
                        if l.rsplit("/", 1)[-1].split("\t")[-1] in LOCKFILES)
        if stamp.exists() and stamp.read_text() == locks:
            return
        for d in deps:
            if (wt / d).is_symlink():
                (wt / d).unlink()
        fc_core.sh(install, wt)
        stamp.write_text(locks)
        return
    for d in deps if borrow else ():
        link = wt / d
        if link.parent.is_dir() and not link.exists() and not link.is_symlink():
            link.symlink_to(repo / d)

def names_lockfile(meta):
    return any(GENERATED_RE.search(f) for f in meta["files"])

def adopt_installs(repo, wt):
    """A passed task that rewrote a lockfile installed fresh in its own clone. Move that install into the project worktree so
    later tasks and verify borrow the new dependencies; each directory is swapped in with one rename."""
    import shutil
    for d in dep_dirs(wt):
        new, old = wt / d, repo / d
        if new.is_symlink() or not new.is_dir() or not old.parent.is_dir():
            continue
        tmp = old.with_name(old.name + ".adopt")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(new, tmp, symlinks=True)
        aside = old.with_name(old.name + ".old")  # the path is never missing: old moves aside, the copy moves in, then the slow delete
        if aside.is_symlink():
            aside.unlink()
        else:
            shutil.rmtree(aside, ignore_errors=True)
        if os.path.lexists(old):
            old.rename(aside)
        tmp.rename(old)
        if aside.is_symlink():
            aside.unlink()
        else:
            shutil.rmtree(aside, ignore_errors=True)

STACK_LOCK = threading.RLock()  # parallel tasks share one stack worktree; git there must run one thread at a time

GENERATED_RE = re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|bun\.lockb?|go\.sum|[^/]+\.lock)$")

LOCKFILE_RE = re.compile(r"(^|/)(bun\.lockb?|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|poetry\.lock|uv\.lock|go\.sum)$")

LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb", "go.sum", "Cargo.lock", "poetry.lock", "uv.lock",
             "Pipfile.lock", "Gemfile.lock", "composer.lock")


# ---------- the servers a worktree's teammates or run left behind ----------
def listeners_under(root):
    """Pids listening on a TCP port whose working directory is inside `root`: the servers and dev watchers a teammate or run
    left behind. Listening narrows it to servers, so the herdr shells and agent sessions that also sit in the worktree stay.
    Two lsof calls however many servers the machine has (each one walks every process), the second for all the pids at once."""
    pids = sorted(set(fc_core.sh("lsof -nP -iTCP -sTCP:LISTEN -t", ".", check=False).stdout.split()))
    if not pids:
        return []
    root, out, pid = str(Path(root).resolve()), [], None
    for l in fc_core.sh(["lsof", "-nP", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(pids)], ".", check=False).stdout.splitlines():
        if l.startswith("p"):
            pid = l[1:]
        elif l.startswith("n") and pid in pids and (l[1:] == root or l[1:].startswith(root + "/")):
            out.append(pid)
    return out

def stop_listeners_under(root):
    pids = listeners_under(root)
    if pids:
        fc_core.sh(["kill", *pids], ".", check=False)
    return pids
