import shlex
import fc_core, fc_tasks, fc_worktree, fc_layers


# A blocker outside the feature's tasks holds its task until Linear says it is finished (fc_tasks.blocker_finished), but Linear lags
# the code: a foundation issue stays In Review, or Todo, until a human merges its PR, long after the project that built it is in
# the trunk of the project that needs it. Git is the record of that, so a blocker the state still holds is let go when the project
# whose last sync has it shows its code in the waiting project's trunk.

def in_base(repo, ref, base):
    return fc_core.sh(["git", "merge-base", "--is-ancestor", ref, base], repo, check=False).returncode == 0

def missing(repo, ref, base):
    """Why `ref` is not in `base`, naming the commit the base lacks (the tip of `ref`) so a lead sees why a blocker still holds."""
    tip = fc_core.sh(["git", "log", "-1", "--format=%h %s", ref], repo, check=False).stdout.strip()
    return f"{ref} is not in {base}" + (f" (missing {tip})" if tip else "")

def landed_as(repo, ref, base):
    """The commit of `base` that carries the same patch as the tip of `ref` (same `git patch-id --stable`), or "": the tip rebased
    into the base under another hash, as a project stacked with `init --after` holds its parent's tasks. Only what base added since
    it diverged from `ref` is searched."""
    fork = fc_core.sh(["git", "merge-base", ref, base], repo, check=False).stdout.strip()
    tip = fc_core.sh(f"git show {shlex.quote(ref)} | git patch-id --stable", repo, check=False).stdout.split()
    if not fork or not tip:
        return ""
    seen = fc_core.sh(f"git log -p --no-merges --format=%H {fork}..{shlex.quote(base)} | git patch-id --stable", repo, check=False).stdout
    return next((line.split()[1] for line in seen.splitlines() if line.split()[0] == tip[0]), "")

def judge(repo, base, blocker, owners, elsewhere=()):
    """(proof, None) when git shows the code of `blocker` is in `base`, else (None, why). `owners` is {project name: project dir}.
    `elsewhere` is the waiting project's tasks of other repos (fc_tasks.elsewhere): this repo's git cannot show such a blocker landed,
    so it holds until Linear says it is finished, and the why names the repo it is built in.
    A task blocker counts when its passed task branch is an ancestor of base, or its tip is in base under another hash (same patch-id). Else, or for a blocker that is no task (a story), when
    every task of its project passed and the top layer of that project's stack is an ancestor of base: the layers are rebuilt
    commits, so a task whose branch was rebased into them is found there."""
    if other := next((t for t in elsewhere if t["linear"] == blocker), None):
        return None, f"{blocker} is a task of {other['repo']}: it holds until it is finished there"
    why = []
    for slug, owner in owners.items():
        cache = fc_tasks.linear_cache(owner)
        tasks = fc_tasks.code_tasks(cache["tasks"])
        task = next((t for t in tasks if t["linear"] == blocker), None)
        if not task and blocker != cache["feature"].get("id") and blocker not in cache.get("issues", {}):
            continue
        if task and fc_tasks.task_finished(owner, task):
            branch = fc_worktree.task_branch(repo, owner, task["id"])
            if in_base(repo, branch, base):
                return f"{slug} task {task['id']}: {branch} is in {base}", None
            if twin := landed_as(repo, branch, base):
                return f"{slug} task {task['id']}: {branch} is in {base} as {twin[:7]}", None
            why += [f"{slug} task {task['id']}: {missing(repo, branch, base)}"]
        open_ = [m["id"] for m in tasks if not fc_tasks.task_finished(owner, m)]
        layers = fc_layers.layer_branches(repo, owner)
        if open_:
            why += [f"{slug}: task{'s' * (len(open_) > 1)} {', '.join(open_)} not passed yet"]
        elif not layers:
            why += [f"{slug}: no stack built yet"]
        elif in_base(repo, layers[-1], base):
            return f"{slug}: its stack top {layers[-1]} is in {base}", None
        else:
            why += [f"{slug}: its stack top {missing(repo, layers[-1], base)}"]
    return None, "; ".join(why) or f"no project of this repository has {blocker} in its last sync"

def released(repo, proj, tasks, base):
    """({blocker id: proof}, {blocker id: why it still holds}) for each blocker the state still holds back from `tasks`, as of
    their last sync. Nothing is asked of git when no task waits."""
    ids = sorted({w.split()[0] for t in tasks for w in t.get("waits_on") or [] if not w.startswith("the route (")})  # a route hold is no blocker
    if not ids:
        return {}, {}
    owners = {slug: p for slug, (_, p) in fc_core.all_projects(repo).items() if p != proj}
    aside = fc_tasks.elsewhere(proj)
    verdicts = {b: judge(repo, base, b, owners, aside) for b in ids}
    return {b: v[0] for b, v in verdicts.items() if v[0]}, {b: v[1] for b, v in verdicts.items() if not v[0]}
