import json, shutil, subprocess, time
from pathlib import Path
import fc_core, fc_proc, fc_readguard, fc_worktree

def paused_ref(proj, tid):
    """The ref a paused task's work is kept on in the main repo."""
    return f"refs/compose/{Path(proj).name}/paused/{tid}"

def parents(base, prev):
    """The `commit-tree` parent flags of a pause: the base, and an earlier pause's commit when there was one, so its files stay reachable from the ref."""
    return ["-p", base, *(["-p", prev] if prev else [])]

def snapshot(repo, proj, tid, wt, base):
    """Commit the whole state of a task's worktree (tracked, new and deleted files) on top of `base` and fetch it into `repo` as
    refs/compose/<project>/paused/<id>. An earlier pause's commit on that ref becomes a second parent, so the files a settle or
    a carry-over left out of this state stay on the ref. Returns (that ref, the files the state changes against `base`)."""
    ident = ["-c", "user.name=compose", "-c", "user.email=compose@localhost"]
    ref = paused_ref(proj, tid)
    fc_core.sh(["git", "add", "-A"], wt)
    tree = fc_core.sh(["git", "write-tree"], wt).stdout.strip()
    prev = fc_core.sh(["git", "rev-parse", "-q", "--verify", ref + "^{commit}"], repo, check=False).stdout.strip()
    if prev:  # the clone may not hold that commit: fetch it by its ref, never by sha
        fc_core.sh(["git", "fetch", "-q", str(repo), f"+{ref}:refs/compose/before/{tid}"], wt)
    commit = fc_core.sh(["git", *ident, "commit-tree", tree, *parents(base, prev), "-m", f"paused {tid}"], wt).stdout.strip()
    fc_core.sh(["git", "update-ref", f"refs/compose/paused/{tid}", commit], wt)
    fc_core.sh(["git", "fetch", "-q", str(wt), f"+refs/compose/paused/{tid}:{ref}"], repo)
    return ref, fc_core.sh(["git", "diff", "--name-only", base, commit], wt).stdout.split()

def ends_paused(status, tid):
    """Whether a task's attempts end `paused`: the loop broke on the pause, or a hold landed while the last test or checks ran
    and they failed. A pass that came first stays passed; a payment failure or a contradiction keeps its own status, so the
    run still halts on the one and reports the other."""
    return status == "paused" or ((status == "failed" or status.startswith("rejected_")) and fc_proc.task_paused(tid))

def keeps_clone(last, wt):
    """Whether the last result says a failed pause snapshot left its edits in this clone: the clone is then moved aside, never deleted."""
    return bool(last.get("snapshot_failed")) and last.get("worktree") == str(wt)

def clear_clone(proj, tid, wt):
    """Remove a task's previous clone before a fresh start; one a failed pause snapshot left with its edits is moved aside, and said."""
    if not wt.exists():
        return
    try:
        last = json.loads((Path(proj) / "results" / f"{tid}.json").read_text())
    except (OSError, ValueError):
        last = {}
    if keeps_clone(last, wt):
        aside = wt.with_name(f"{wt.name}{fc_worktree.UNSAVED_CLONE}{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.move(str(wt), str(aside))
        fc_core.say(f"{tid} note    the clone a failed pause snapshot kept is moved to {aside}; rescue its edits from there")
        return
    shutil.rmtree(wt)

def failed_record(meta, wt, base_name, log, attempts, cost, seconds):
    """The result of a pause whose snapshot failed: `failed`, no branch, and `snapshot_failed` naming the clone that kept the edits."""
    return {"id": meta["id"], "title": meta.get("title"), "status": "failed", "model": meta["model"], "attempts": attempts,
            "seconds": seconds, "cost": round(cost, 6), "branch": "-", "base": base_name, "worktree": str(wt), "snapshot_failed": True, "log": log}

def paused_record(meta, base_name, log, attempts, cost, seconds, started, block):
    """The result of a task a hold stopped: `paused`, no branch, the kept work (`block`: ref, base, files, at, reason) under `paused`."""
    return {"id": meta["id"], "title": meta.get("title"), "status": "paused", "model": meta["model"], "attempts": attempts,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)), "seconds": seconds, "cost": round(cost, 6),
            "branch": "-", "base": base_name, "log": log, "paused": block}

def paused_result(repo, proj, meta, wt, base, base_name, log, attempts, cost, started, reason):
    """A task a hold stopped mid-flight: its work kept on a ref, its worktree removed, the result written as `paused`
    (the run treats it as pending again). `attempts` is the count spent and `cost` what the turns cost."""
    try:
        ref, files = snapshot(repo, proj, meta["id"], wt, base)
    except RuntimeError as e:  # the edits could not be kept: the clone stays as they are, and the result says why
        log.append({"error": f"pause snapshot failed: {str(e)[-500:]}"})
        failed = failed_record(meta, wt, base_name, log, attempts, cost, round(time.time() - started, 1))
        fc_core.say(f"{meta['id']} ERROR   pause snapshot failed; the clone is kept at {wt}: rescue it before the task runs again,"
                    f" which moves it aside: {str(e)[-200:]}")
        fc_core.atomic_write(Path(proj) / "results" / f"{meta['id']}.json", json.dumps(failed, indent=2))
        return failed
    total = round(time.time() - started, 1)
    result = paused_record(meta, base_name, log, attempts, cost, total, started, {"ref": ref, "base": base, "files": files, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason})
    (Path(proj) / "results").mkdir(exist_ok=True)
    fc_core.atomic_write(Path(proj) / "results" / f"{meta['id']}.json", json.dumps(result, indent=2))  # first: a crash after it loses no record
    shutil.rmtree(wt, ignore_errors=True)
    fc_readguard.guard_file(wt).unlink(missing_ok=True)  # its read counts go with the clone
    fc_core.say(f"{meta['id']} paused  {reason}; work kept on {ref} ({len(files)} files)")
    return result

CARRY_NOTE = "WORK KEPT FROM BEFORE A PLAN CHANGE: {files} already hold edits made under the old brief. Keep what still fits the brief below and change what does not.\n\n"

def carry_note(files):
    return CARRY_NOTE.format(files=", ".join(files)) if files else ""

def _apply_one(wt, base, name):
    """One kept file's change onto the fresh clone; False (and the file back as HEAD has it) when it does not apply."""
    made = subprocess.run(["git", "diff", "--binary", base, "FETCH_HEAD", "--", name], cwd=wt, capture_output=True)
    if made.returncode or not made.stdout or subprocess.run(["git", "apply", "--3way", "--index"], cwd=wt, input=made.stdout, capture_output=True).returncode:
        subprocess.run(["git", "checkout", "HEAD", "--", name], cwd=wt, capture_output=True)
        return False
    return True

def carried(last):
    """The paused block a released task keeps in its `running` result, so a stopped run still carries its edits next start."""
    data = json.loads(last) if last else {}
    block = data.get("paused") if data.get("status") == "paused" else data.get("kept_from")
    return {"kept_from": block} if block else {}

def carry_plan(last):
    """(ref, base, files) a released task may take from its last result's pause: the files its `keep` choice allows (all, the chosen ones for `part`);
    None when it holds no pause, the choice is `discard`, or nothing is left to take."""
    paused = last.get("paused") if last.get("status") == "paused" else last.get("kept_from")
    choice = (paused or {}).get("choice") or "keep"
    if not paused or choice == "discard":
        return None
    wanted = [f for f in paused["files"] if choice != "part" or f in paused.get("keep", [])]
    return (paused["ref"], paused["base"], wanted) if wanted else None

def split_kept(wanted, owned, applies):
    """(kept, dropped) of the wanted files: kept are those the new brief still owns and `applies(file)` puts on the fresh clone; the rest stay on the ref."""
    kept, dropped = [], []
    for f in wanted:
        (kept if f in owned and applies(f) else dropped).append(f)
    return kept, dropped

def _fetch_ref(repo, wt, ref):
    """Whether the pause's ref could be fetched from the main repo into the fresh clone."""
    return subprocess.run(["git", "fetch", "-q", str(repo), ref], cwd=wt, capture_output=True).returncode == 0

def carry_over(repo, wt, meta, last):
    """Start a released task from the edits its pause kept: those the new brief still owns, that the chosen `keep` allows
    and that apply to the fresh clone. Returns the files kept; the rest stay on the ref."""
    plan = carry_plan(last)
    if not plan:
        return []
    ref, base, wanted = plan
    if not _fetch_ref(repo, wt, ref):
        fc_core.say(f"{meta['id']} kept    nothing; the ref {ref} could not be fetched")
        return []
    kept, dropped = split_kept(wanted, meta["files"], lambda f: _apply_one(wt, base, f))
    fc_core.say(f"{meta['id']} kept    {', '.join(kept) or 'nothing'}" + (f"; dropped {', '.join(dropped)}: they stay on {ref}" if dropped else ""))
    return kept
