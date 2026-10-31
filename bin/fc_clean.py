import json, shutil, subprocess, sys
from pathlib import Path
import fc_core, fc_tasks, fc_linear, fc_herdr, fc_worktree, fc_layers, fc_crew

# ---------- clean: remove a finished or abandoned project's worktrees, branches, tabs, and results ----------

def patch_merged(out):
    """Whether `git cherry` output proves every commit's patch is in the target: it lists some, and each starts with `-`."""
    return bool(out.strip()) and all(l.startswith("-") for l in out.splitlines())

def changes_nothing(out, tree):
    """Whether `git merge-tree --write-tree` output names the target's own tree: merging the branch in would change nothing."""
    return out.split("\n", 1)[0] == tree

def pr_evidence(out):
    """The merged-PR line from `gh pr list --json` output, or None for no merged PR or output that does not parse."""
    try:
        prs = json.loads(out or "[]")
    except json.JSONDecodeError:
        prs = []
    return f"PR #{prs[0]['number']} merged {str(prs[0].get('mergedAt', ''))[:10]}" if prs else None

def merge_evidence(repo, branch, targets, has_remote):
    """Why a branch counts as merged, or None. Four independent signals, any suffices:
    git ancestry (fast-forward and merge-commit merges), every commit's patch in the target (`git cherry`: a target
    fast-forwarded to a layer built locally, which holds cherry-picked copies), a merge of the branch into the target that
    would change nothing (`git merge-tree --write-tree` gives the target's own tree without a conflict: the branch tip's
    changes are all there, even where a cherry-pick on a moved base gave its commits other patch ids), or a merged pull request
    for the branch on GitHub (covers squash and rebase merges, where the commits are rewritten and ancestry says nothing).
    Any doubt means not merged."""
    for t in targets:
        if fc_core.sh(["git", "rev-parse", "--verify", "-q", t], repo, check=False).returncode:
            continue
        if fc_core.sh(["git", "merge-base", "--is-ancestor", branch, t], repo, check=False).returncode == 0:
            return f"contained in {t}"
        p = fc_core.sh(["git", "cherry", t, branch], repo, check=False)
        if p.returncode == 0 and patch_merged(p.stdout):
            return f"contained in {t} by patch"
        p = fc_core.sh(["git", "merge-tree", "--write-tree", t, branch], repo, check=False)
        if p.returncode == 0 and changes_nothing(p.stdout, fc_core.sh(["git", "rev-parse", "-q", f"{t}^{{tree}}"], repo, check=False).stdout.strip()):
            return f"merging it into {t} changes nothing"
    if has_remote:
        p = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number,mergedAt", "--limit", "1"],
                           cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if p.returncode == 0 and pr_evidence(p.stdout):
            return pr_evidence(p.stdout)
    return None

def merged_into(repo, branch, targets, has_remote=False):
    return merge_evidence(repo, branch, targets, has_remote) is not None

def first_plus(out):
    """The first commit `git cherry -v` lists as unmatched, as (sha, subject, how many more), or None when it lists none."""
    plus = [l for l in out.splitlines() if l.startswith("+")]
    if not plus:
        return None
    _, sha, subject = plus[0].split(" ", 2)
    return sha, subject, len(plus) - 1

def first_unmatched(repo, branch, targets):
    """The first commit on the branch with no patch in the first target that resolves, as a line for clean's kept output, or None."""
    for t in targets:
        if fc_core.sh(["git", "rev-parse", "--verify", "-q", t], repo, check=False).returncode == 0:
            break
    else:
        return None
    first = first_plus(fc_core.sh(["git", "cherry", "-v", t, branch], repo, check=False).stdout)
    if not first:
        return None
    sha, subject, more = first
    short = fc_core.sh(["git", "rev-parse", "--short", sha], repo).stdout.strip()
    return f"first unmatched commit {short} {subject}" + (f" and {more} more" if more else "")

def own_commits(repo, branch):
    """False when the branch never got a commit of its own: it still sits where it was created (an empty or design-only project),
    so ancestry says nothing about it being merged. Unknown (no reflog) counts as having commits."""
    log = fc_core.sh(["git", "reflog", "show", "--format=%H", f"refs/heads/{branch}"], repo, check=False).stdout.split()
    return not log or log[-1] != fc_core.sh(["git", "rev-parse", f"refs/heads/{branch}"], repo, check=False).stdout.strip()

def live_crew(proj):
    """Why the project still has people working: teammates whose last report is not done and whose tab is not closed, or agents
    alive in its herdr space. The team lead (orchestrator) is the one running the post-merge clean, so it does not count."""
    lead, why = fc_herdr.agent_name(proj.name, "orchestrator"), []
    listed = fc_herdr.herdr("agent", "list")
    agents = None if listed is None else listed.get("agents", [])
    for f in sorted(fc_crew.crew_dir(proj).glob("*.json")):
        c = json.loads(f.read_text())
        last = (fc_crew.crew_reports(proj, c["name"]) or [{}])[-1]
        if c["name"] != lead and not c.get("closed") and last.get("state") != "done" and not fc_crew.tab_closed(c, agents):
            why.append(f"{c['name']} last reported {last.get('state', 'nothing')}")
    space = fc_herdr.herdr_project_space(proj) if fc_herdr.herdr_env() else None
    tabs = {t["tab_id"] for t in fc_herdr.herdr_project_tabs(proj, space)} if space else set()  # a space of several projects holds other projects' agents too: only agents named for this project count
    why += [f"{ag.get('name')} is alive in its herdr space" for ag in agents or [] if ag.get("tab_id") in tabs and ag.get("name") != lead
            and fc_herdr.agent_id(proj.name, ag.get("name") or "")]
    return why

def runs_inside(here, root):
    """Whether the working directory `here` (None when it is gone) is the worktree `root` or inside it."""
    return bool(here and (here == root or root in here.parents))

def lead_working(record, last, force):
    """Whether the team lead's record says it is still working: it has one, `force` is off, it is not closed, and its last report is not done."""
    return bool(record and not force and not record.get("closed") and last.get("state") != "done")

def live_run_or_crew(run_pid, run_live, crew, force):
    """The reasons abandon stops before the worktree check: a live run, and live crew unless `force`."""
    return ([f"a run is live (pid {run_pid}): stop it first"] if run_live else []) + ([f"live crew ({'; '.join(crew)}); --force overrides"] if crew and not force else [])

def standing_in(wt, proj, force=False):
    """Who would lose the project worktree from under them if clean removed it now: this command, when it runs inside it, and a
    team lead whose last report is not done while its tab is open (`--project <slug> --force` overrides the team lead, never this
    command). Returning a slot ends every process left in it, the pipe reading this command's output included, and the
    worktree holds the project's record, so the team lead's last report would find no project."""
    why, root = [], Path(wt).resolve()
    try:
        here = Path.cwd().resolve()
    except FileNotFoundError:
        here = None
    if runs_inside(here, root):
        why.append("this command runs inside it")
    lead = fc_herdr.agent_name(proj.name, "orchestrator")
    c = fc_crew.crew_record(proj, lead)
    last = (fc_crew.crew_reports(proj, lead) or [{}])[-1]
    if lead_working(c, last, force) \
            and not fc_crew.tab_closed(c, (fc_herdr.herdr("agent", "list") or {}).get("agents") if fc_herdr.herdr_env() else None):
        why.append(f"{lead} last reported {last.get('state', 'nothing')}")
    return why

def remove_project(main_repo, name, wt, proj, pbranch, force=False):
    """Take a merged project away: stop its servers, close its herdr space or tabs, hand its worktree back (to the pool when it is
    a slot), and delete its branch and its change-of-course refs. `force` closes the tabs of unfinished teammates too."""
    fc_worktree.stop_listeners_under(wt)
    close_space(proj, name, force)  # while its record is still in the worktree
    if fc_worktree.is_slot(main_repo, wt):  # back to the pool with its installs; the branch is let go first so it can be deleted
        fc_core.sh(["git", "checkout", "-q", "--detach"], wt, check=False)
        fc_worktree.release_slot(main_repo, wt, own=name)
    else:
        fc_core.sh(["git", "worktree", "remove", "--force", str(wt)], main_repo, check=False)
    if wt.exists() and not fc_worktree.is_slot(main_repo, wt):
        shutil.rmtree(wt, ignore_errors=True)
    fc_core.sh(["git", "worktree", "prune"], main_repo, check=False)
    fc_core.sh(["git", "branch", "-D", pbranch], main_repo, check=False)
    refs = fc_core.sh(["git", "for-each-ref", "--format=delete %(refname)", f"refs/compose/{name}/", f"refs/compose/replaced/{name}/"],
              main_repo, check=False).stdout  # its change-of-course and replaced-tip records: nothing left to undo or replay
    if refs.strip():
        subprocess.run(["git", "update-ref", "--stdin"], cwd=main_repo, input=refs, text=True, capture_output=True)

def close_space(proj, name, force=False):
    """Close what the project holds in herdr: its tabs in a space it shares, or the space of its own.
    A tab whose crew record's last report is not done stays open, one line saying so (unless `force`); the team lead gets no exception.
    The engineering manager's pane stops naming the project."""
    fc_herdr.herdr_unname_manager(name)
    space = fc_herdr.herdr_project_space(proj)
    if not space:
        return
    crew = {c["tab"]: c for c in (json.loads(f.read_text()) for f in sorted(fc_crew.crew_dir(proj).glob("*.json"))) if c.get("tab")}
    kept, closed = [], []
    for t in fc_herdr.herdr_project_tabs(proj, space):
        c = crew.get(t["tab_id"])
        if c:
            last = (fc_crew.crew_reports(proj, c["name"]) or [{}])[-1]
            if last.get("state") != "done" and not force:
                print(f"clean left {c['name']} open (last report: {last.get('state', 'nothing')})")
                kept.append(t["tab_id"])
                continue
        closed.append(t["tab_id"])
    if fc_herdr.space_record(proj).exists():
        for tid in closed:
            fc_herdr.herdr("tab", "close", tid)
    elif fc_herdr.herdr_space_is(space, name):
        if kept:
            for tid in closed:
                fc_herdr.herdr("tab", "close", tid)
        else:
            fc_herdr.herdr("workspace", "close", space)

def crew_holds_clean(crew, force, explicit):
    """Whether live crew keeps the project from a clean: only `--project <slug> --force` together override it."""
    return bool(crew and not (force and explicit))

def branch_fate(why, holders, layer_why, delete_unmerged):
    """(why, remove) for a task or layer branch: merged on its own proof, or through every layer that held it, goes; unmerged
    goes only on `--delete-unmerged`, else stays."""
    if not why and holders and all(lb in layer_why for lb in holders):
        why = f"held by {', '.join(holders)}, merged: {layer_why[holders[0]]}"
    return why, bool(why or delete_unmerged)

def project_proof(why, own, explicit, delete_unmerged, held, layer_why):
    """(why, project_merged, empty) for the project branch from its merge evidence `why`: with no commits of its own (`own` false)
    nothing was merged unless named or `--delete-unmerged`; else every layer merged, or `--delete-unmerged`, stands for a merge."""
    if why and not own and not (explicit or delete_unmerged):
        return None, False, True
    if not why and held and all(lb in layer_why for lb in held):  # layers squash the project branch's commits: its merge is theirs
        return f"every layer built from it is merged ({', '.join(held)})", True, False
    if not why and delete_unmerged:
        return "UNMERGED, removed on --delete-unmerged", False, False
    return why, bool(why), False

def merged_by_project(metas, merged, statuses, finished):
    """The tasks a merged project holds the code of beyond `merged`: every one with a finished (or already green) status."""
    return [t for t in metas if t not in merged and statuses.get(t) in finished]

def settle_clean(main_repo, name, wt, proj, pbranch, link, metas, merged, project_merged, remove, line):
    """The end of a clean: Linear moves with all the worktree holds first, then the worktree goes, then the summary line."""
    fc_linear.linear_merged(link.get("feature"), [metas[t] for t in merged], project_merged, scope=link.get("stories"))  # before the worktree goes, with all it holds
    fc_linear.refresh_ready(proj, link.get("feature"))
    if remove:
        remove_project(main_repo, name, wt, proj, pbranch)
    print(line)

def clean_project(main_repo, name, wt, proj, delete_unmerged=False, explicit=False, force=False, delete_remote=False):
    """The post-merge step. Fetch, then remove what the reviewer's merges made redundant: task clones and the CLI's stack
    worktree always; task branches merged into the project branch (locally or on the remote); and, once every layer and the
    project branch itself are merged, the project worktree and branch. Unmerged work is kept and named. Merged layer branches on a remote are only named, and
    deleted (a push) when `delete_remote` is set.
    With live crew (and no `--project <slug> --force`) only the branches proven merged go, which is reversible housekeeping: no
    slot, task clone, stack worktree, or project worktree is touched, since the crew stands on them, and a proven branch some
    clone still has checked out stays. Everything unproven stays and is named; `--delete-unmerged` is refused."""
    import shutil
    repo = wt
    crew = live_crew(proj)
    crewed = crew_holds_clean(crew, force, explicit)
    if crewed and delete_unmerged:
        print(f"clean  {name}: kept, it has live crew ({'; '.join(crew)}); --project {name} --force overrides")
        return
    if crewed:
        print(f"clean  {name}: live crew ({'; '.join(crew)}): removing only branches proven merged; --project {name} --force removes the rest")
    has_remote = bool(fc_core.sh(["git", "remote"], repo, check=False).stdout.strip())
    if has_remote:
        fc_core.sh(["git", "fetch", "-q", "--prune"], repo, check=False)
    trunk = fc_core.current_branch(repo)
    if not crewed:
        for held in fc_worktree.slots(proj).values():  # test-writing teammates' slots and stack slots go back to the pool
            fc_worktree.release_slot(repo, held)
        fc_worktree.slots(proj, {r: None for r in fc_worktree.slots(proj)})
        for w in list((proj / "worktrees").iterdir()) if (proj / "worktrees").exists() else []:  # iterdir: .stackroot is a dotfile
            if fc_worktree.UNSAVED_CLONE in w.name:
                print(f"clean  {name}: removing {w}, the clone a failed pause snapshot kept with unsaved edits")
            fc_core.sh(["git", "worktree", "remove", "--force", str(w)], repo, check=False)
            if w.exists():
                shutil.rmtree(w, ignore_errors=True)
        fc_core.sh(["git", "worktree", "prune"], repo, check=False)
    targets = [trunk, f"origin/{trunk}", "main", "origin/main", "master", "origin/master"]
    review = fc_worktree.review_base(repo, fc_core.project_config(proj))
    targets = list(dict.fromkeys([*([review, f"origin/{review}"] if review else []), *targets]))
    kept, merged = [], []  # merged: the tasks whose branches this clean proved merged
    link = json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}
    metas = {m["id"]: m for m in fc_tasks.load_tasks(proj) if m.get("linear")}  # read now: a merged project's files go with its worktree
    prefix = fc_layers.layer_prefix(repo, proj)
    rec_file = proj / "layers.json"
    held = json.loads(rec_file.read_text()) if rec_file.exists() else {}  # layer branch -> the tasks it held
    remote_layers = {}  # layer branch -> remotes still carrying it
    for ref in fc_core.sh(["git", "for-each-ref", "--format=%(refname:short)", f"refs/remotes/*/{prefix}layer-*"], repo, check=False).stdout.split():
        remote, _, lb = ref.partition("/")
        remote_layers.setdefault(lb, []).append(remote)
    layer_why = {}  # layers proven merged: squash and rebase merges rewrite commits, so this is the PR (or ancestry), never the tasks' own
    for lb in dict.fromkeys([*held, *remote_layers, *fc_core.sh(["git", "branch", "--list", f"{prefix}layer-*"], repo, check=False).stdout.split()]):
        why = merge_evidence(repo, lb, targets, has_remote)
        if why:
            layer_why[lb] = why
    for lb, remotes in remote_layers.items():  # a push: clean never makes one on its own, so it names the merged layers and waits for --delete-remote
        for remote in remotes if lb in layer_why else []:
            if not delete_remote:
                print(f"clean  {remote}/{lb} is merged ({layer_why[lb]}) and stays on the remote: `compose clean --project {name} --delete-remote` deletes it, or delete the branch on GitHub")
            elif fc_core.sh(["git", "push", "-q", remote, "--delete", lb], repo, check=False).returncode == 0:
                print(f"clean  deleted {remote}/{lb} on the remote: {layer_why[lb]}")
            else:
                print(f"clean  could not delete {remote}/{lb} on the remote; left in place")
    for b in [*fc_worktree.own_branches(repo, proj), *fc_core.sh(["git", "branch", "--list", f"{prefix}layer-*"], repo, check=False).stdout.split()]:
        if "-bench-" in b:  # bench runs are measurements, never work to keep
            fc_core.sh(["git", "branch", "-D", b], repo, check=False)
            continue
        why = merge_evidence(repo, b, targets, has_remote)
        holders = [lb for lb, tasks in held.items() if b.removeprefix(fc_worktree.task_prefix(repo, proj)) in tasks]
        why, gone = branch_fate(why, holders, layer_why, delete_unmerged)
        if gone and crewed and (clone := fc_worktree.checked_out(repo, b)):  # git would refuse, and the crew is working in it
            kept.append(b)
            print(f"clean  {b}: {why}, but checked out in {clone}: kept while its crew works")
            continue
        if gone:
            print(f"clean  {b}: {why if why else 'UNMERGED, deleted on --delete-unmerged; any PR on GitHub is unaffected'}")
            fc_core.sh(["git", "branch", "-D", b], repo, check=False)
            if why and b.removeprefix(fc_worktree.task_prefix(repo, proj)) in metas:
                merged.append(b.removeprefix(fc_worktree.task_prefix(repo, proj)))
        else:
            kept.append(b)
            fu = first_unmatched(repo, b, targets)
            print(f"clean  {b}: kept unmerged" + (f", {fu}" if fu else ""))
    merged += [t for t in metas if t not in merged and any(t in ts for ts in held.values())
               and all(lb in layer_why for lb, ts in held.items() if t in ts)]
    if (proj / "verify.json").exists() and "layer_prefix" in fc_core.project_config(proj) and not crewed \
            and not fc_core.sh(["git", "branch", "--list", f"{fc_core.project_config(proj)['layer_prefix']}layer-*"], repo, check=False).stdout.strip():
        fc_core.atomic_write(proj / "verify.json", json.dumps({k: v for k, v in fc_core.project_config(proj).items() if k != "layer_prefix"}, indent=2))  # merged and cleaned: a later stack names afresh
    if (proj / "verify.json").exists() and "task_prefix" in fc_core.project_config(proj) and not crewed and not fc_worktree.own_branches(repo, proj):
        fc_core.atomic_write(proj / "verify.json", json.dumps({k: v for k, v in fc_core.project_config(proj).items() if k != "task_prefix"}, indent=2))  # merged and cleaned: a later stack names afresh
    line = f"clean  {name}: task branches " + (f"kept unmerged: {', '.join(kept)}" if kept else "all merged and removed")
    pbranch, project_merged, remove = fc_core.project_branch(proj), False, False
    if crewed:
        line += "; slots, task clones, and the project worktree stay while the crew works"
    if wt != main_repo and not kept and trunk == pbranch and not crewed:
        on = [fc_core.project_config(proj)["review_base"]] if fc_core.project_after(proj) else []  # merged into the project it was built on counts
        ptargets = [*on, "main", "origin/main", "master", "origin/master", fc_core.current_branch(main_repo)]
        why = merge_evidence(main_repo, pbranch, ptargets, has_remote)
        why, project_merged, empty = project_proof(why, not why or own_commits(main_repo, pbranch), explicit, delete_unmerged, held, layer_why)
        if empty:
            line += f"; project branch {pbranch} has no commits of its own (empty or design-only), so nothing was merged: kept; name it with --project {name} to remove it"
        if why:
            stay = standing_in(wt, proj, force=force and explicit)
            remove = not stay
            if stay:  # the team lead's done finishes it (fc_crew.finish_clean_after_turn)
                fc_core.atomic_write(proj / "results" / "clean.json", json.dumps({"at": fc_core.now(), "why": why}, indent=2))
                lead = fc_herdr.agent_name(name, "orchestrator")
                line += (f"; project branch {pbranch}: {why}; worktree {wt} and branch {pbranch} kept while {'; '.join(stay)}: "
                         + (f"they go when {lead} reports done" if any(s.startswith(lead) for s in stay)
                            else f"run `compose clean --repo {main_repo} --project {name}` from outside it to remove them"))
            else:
                line += f"; project branch {pbranch}: {why}; removed worktree {wt} and branch {pbranch}"
        elif "no commits of its own" not in line:
            line += f"; project branch {pbranch} not merged yet" + (
                f" (layers not merged: {', '.join(lb for lb in held if lb not in layer_why)})" if held else " (no recorded layers to prove it)"
            ) + f": worktree kept; --delete-unmerged removes it anyway"
            fu = first_unmatched(main_repo, pbranch, ptargets)
            print(f"clean  {pbranch}: kept unmerged" + (f", {fu}" if fu else ""))
    if project_merged:  # the project branch holds every finished task's code, a task with no branch of its own (skipped as green, done by hand) too
        status = lambda t: json.loads(f.read_text()).get("status") if (f := proj / "results" / f"{t}.json").exists() else None
        merged += merged_by_project(metas, merged, {t: status(t) for t in metas}, (*fc_tasks.FINISHED, "already_green"))
    settle_clean(main_repo, name, wt, proj, pbranch, link, metas, merged, project_merged, remove, line)

def abandon_project(main_repo, name, wt, proj, force=False, dry=False):
    """Remove an unmerged project the user gave up on. It lists what goes (the worktree with its results and local changes, the
    task, layer, and project branches, slots, herdr tabs) and what stays (Linear, branches pushed to a remote, PRs, the main checkout),
    then removes the worktree and the local branches. A live run or this command standing in the worktree stops it; so does live
    crew or an open team lead, unless `force`."""
    heads = lambda *pat: fc_core.sh(["git", "for-each-ref", "--format=%(refname:short)", *(f"refs/heads/{p}" for p in pat)], main_repo, check=False).stdout.split()
    pbranch = fc_core.project_branch(proj)
    branches = [b for b in dict.fromkeys([*fc_worktree.own_branches(main_repo, proj), *heads(f"{fc_layers.layer_prefix(main_repo, proj)}layer-*"), pbranch]) if heads(b)]
    pushed = [r for b in branches for r in fc_core.sh(["git", "for-each-ref", "--format=%(refname:short)", f"refs/remotes/*/{b}"], main_repo, check=False).stdout.split()]
    held = [str(w) for w in [*fc_worktree.slots(proj).values(), *((proj / "worktrees").iterdir() if (proj / "worktrees").exists() else [])]]
    dirty = len(fc_worktree.dirty_paths(wt)) if wt.exists() else 0
    base = fc_worktree.review_base(main_repo, fc_core.project_config(proj))
    ahead = fc_core.sh(["git", "rev-list", "--count", f"{base}..{pbranch}"], main_repo, check=False).stdout.strip() if base else ""
    link = json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}
    crew = live_crew(proj)
    stop = live_run_or_crew(fc_tasks.run_marker(proj).get("pid") if fc_tasks.run_alive(proj) else None, fc_tasks.run_alive(proj), crew, force) + standing_in(wt, proj, force=force)
    print(f"abandon  {name}: removes")
    for line in (f"worktree {wt} ({'returned to the treehouse pool' if fc_worktree.is_slot(main_repo, wt) else 'removed'}), with its .compose/{name} results and {dirty} uncommitted path(s)",
                 f"local branches {', '.join(branches)}" + (f" ({ahead} commit(s) on {pbranch} past {base})" if ahead else ""),
                 *([f"stack and test worktrees {', '.join(held)}"] if held else []), "its herdr tabs, and the space when it is the project's own",
                 f"change-of-course refs under refs/compose/{name}/"):
        print(f"  {line}")
    print("abandon  keeps")
    for line in ("Linear issues, untouched (cancel them there if the work is dropped)" + (f": feature {link['feature']}" if link.get("feature") else ""),
                 *([f"branches pushed to a remote stay there: {', '.join(pushed)}"] if pushed else []), "GitHub PRs, and the main checkout with its other worktrees"):
        print(f"  {line}")
    if stop:
        sys.exit(f"abandon  {name}: nothing removed, " + "; ".join(stop))
    if dry:
        return print(f"abandon  {name}: --dry-run, nothing removed")
    for slot in fc_worktree.slots(proj).values():
        fc_worktree.release_slot(main_repo, slot)
    fc_worktree.slots(proj, {r: None for r in fc_worktree.slots(proj)})
    for w in list((proj / "worktrees").iterdir()) if (proj / "worktrees").exists() else []:
        fc_core.sh(["git", "worktree", "remove", "--force", str(w)], main_repo, check=False)
        shutil.rmtree(w, ignore_errors=True)
    fc_core.sh(["git", "worktree", "prune"], main_repo, check=False)
    for b in branches[:-1]:  # the project branch goes with its worktree
        fc_core.sh(["git", "branch", "-D", b], main_repo, check=False)
    remove_project(main_repo, name, wt, proj, pbranch, force=True)
    left = [str(wt)] * (wt.exists() and not fc_worktree.is_slot(main_repo, wt)) + [str(proj)] * proj.exists() + heads(*branches)  # a returned slot stays on disk
    if left:
        sys.exit(f"abandon  {name}: could not remove {', '.join(left)}")
    print(f"abandon  {name}: removed worktree {wt} and {len(branches)} local branch(es)")

def cmd_clean(a):
    main_repo = Path(a.repo).resolve()
    main_repo = fc_core.main_checkout(main_repo)
    if getattr(a, "abandon", False):
        projects = fc_core.all_projects(main_repo)
        if a.project not in projects or a.delete_unmerged:
            sys.exit("clean --abandon names one project and nothing else: `compose clean --abandon --project <slug>`"
                     + (f" (known: {sorted(projects)})" if a.project not in projects else ""))
        wt, proj = projects[a.project]
        if wt == main_repo:
            sys.exit(f"abandon  {a.project}: it lives in the main checkout {wt}, not in a worktree of its own to remove")
        return abandon_project(main_repo, a.project, wt, proj, force=a.force, dry=getattr(a, "dry_run", False))
    projects = fc_core.all_projects(main_repo)
    if a.project:
        projects = {a.project: projects[a.project]} if a.project in projects else {}
    if not projects:
        sys.exit("no projects to clean")
    for name, (wt, proj) in projects.items():
        clean_project(main_repo, name, wt, proj, delete_unmerged=a.delete_unmerged, explicit=bool(a.project), force=a.force,
                      delete_remote=getattr(a, "delete_remote", False))
