"""The decisions of a verify fix round: whether the checkout moved under it, which files a round reverts or may commit, and which
confirmed findings it cannot land (a lower layer owns their file). Imports only lower modules."""
from pathlib import Path
import fc_tasks, fc_worktree

def checkout_moved(stood_on, now):
    """Whether the branch the stages ran on is not the one verify started on: a fix commit stays on its branch, only a checkout moves it ("" is a detached HEAD)."""
    return now != stood_on

def void_message(repo, stood_on, now):
    """The line verify ends with when the checkout moved during a stage: nothing verified, nothing recorded."""
    return (f"verify VOID  nothing verified or recorded: {repo} stood on {stood_on or 'a detached HEAD'} when the stages started and stands on "
            f"{now or 'a detached HEAD'} now, so something else checked a branch out there during the run (compose stack, top, a hand checkout) "
            "and the stages judged that code, not the stack top; run verify again")

def unwired(files):
    """The sorted files without blanks and without the agent wiring (fc_worktree.WIRING): trail's and graft's hooks rewrite it, so a fix never commits or reverts it."""
    return sorted(set(files) - {"", *fc_worktree.WIRING})

def revert_plan(fixed, added):
    """(files to check out from HEAD, files to delete) when a fix round is rejected: what the fix changed and HEAD has, and what it added."""
    return sorted(set(fixed) - set(added)), sorted(set(added) & set(fixed))

def fix_strays(touched, allowed, named=()):
    """Files a verify fix round may not touch. A fixer gets the same scope as the tasks: only files the stack already
    changes. With no stack diff (verify on a plain checkout) it is free except for lockfiles, which a fixer only rewrites
    when it is working around a broken environment, not fixing a finding. A lockfile a task names in `files:` is part of the work."""
    return [f for f in touched if f not in allowed and f not in named] if allowed else [f for f in touched if Path(f).name in fc_worktree.LOCKFILES and f not in named]

def wrong_layer(tasks, files, top):
    """The files of a fix that belong to another layer: some task's `files:` lists them and the top task's does not.
    A fix commits on the top layer, so it may change only what that layer owns (or what no task names)."""
    code = fc_tasks.code_tasks(tasks)
    owners = {f: sorted(t["id"] for t in code if f in t["files"]) for f in files}
    return {f: o for f, o in owners.items() if o and top not in o} if top else {}

def top_task(main, proj):
    """The id of the stack's top task, None without a stack."""
    order = fc_worktree.stack_order(main, proj)
    return order[-1].removeprefix(fc_worktree.task_prefix(main, proj)) if order else None

def held_findings(main, proj, confirmed):
    """[(finding, owner task ids, top task id)] for the confirmed review findings no fix round can land: each names a file a lower layer's
    task lists and the top task does not (wrong_layer), and a fix commits on the top layer, so the round would only be reverted. They go to the team
    lead: dismiss the finding when it is false, else cut a top task."""
    top = top_task(main, proj) if confirmed else None
    if not top:
        return []
    tasks, out = fc_tasks.load_tasks(proj), []
    for i in confirmed:
        foreign = wrong_layer(tasks, [str(i.get("file") or "").removeprefix("./")], top)
        if foreign:
            out.append((i, sorted({x for o in foreign.values() for x in o}), top))
    return out

def cut_top_task(foreign, top):
    """What to do about a fix that belongs to a lower layer: only a task above the stack can change those files."""
    return f"cut a new top task (id above {top}) whose files: lists {', '.join(sorted(foreign))}, run it, then verify again"
