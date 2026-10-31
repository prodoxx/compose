"""`compose update`: pull the CLI's own checkout --ff-only (rebasing commits only it has onto upstream), relink what compose owns, run doctor.
A run or verify live on this machine freezes the CLI, and so does a teammate mid-turn: updating then changes the code and the skill under
them. Each project pins the version it started on (~/.compose/pins), and an update names, per project, the skill files that changed
since the pin, so its team lead re-reads those and nothing else."""
import atexit, json, os, shlex, sys, threading, time
from pathlib import Path
import fc_core, fc_config, fc_tasks, fc_init, fc_links, fc_hooks, fc_herdr, fc_pin


def live_dir():
    return fc_config.CONFIG_PATH.parent / "live"

_stages, _stages_lock = {}, threading.Lock()  # the verify stages working in this process, name -> when they started

def hold(kind, project, repo):
    """Record that this process is running `kind` (run, verify, resume) on `project` until it exits, where every project of this
    machine can see it: `compose update` refuses while one is live, and `compose status` lists it with how long it has run. A process
    that dies without exiting leaves a file whose pid is dead."""
    f = live_dir() / f"{os.getpid()}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(f, json.dumps({"pid": os.getpid(), "kind": kind, "project": project, "repo": str(Path(repo).resolve()),
                                        "since": time.strftime("%H:%M"), "started": time.time()}))
    atexit.register(f.unlink, missing_ok=True)
    _stages.clear()
    if hold_stage not in fc_herdr.STAGE_OBSERVERS:
        fc_herdr.STAGE_OBSERVERS.append(hold_stage)

def hold_stage(name, state):
    """Keep in this process's hold file the verify stages that are working and since when (`stages`): a stage going `working` is added,
    one going idle or blocked is dropped. A process that holds nothing records nothing."""
    f = live_dir() / f"{os.getpid()}.json"
    with _stages_lock:
        if state == "working":
            _stages.setdefault(name, time.time())
        else:
            _stages.pop(name, None)
        try:
            held = json.loads(f.read_text())
        except (OSError, ValueError):
            return
        fc_core.atomic_write(f, json.dumps({**held, "stages": dict(_stages)}))

def live_work():
    """The runs and verifies alive on this machine, each {pid, kind, project, repo, since}; the files of dead processes are removed."""
    found = []
    for f in sorted(live_dir().glob("*.json")) if live_dir().exists() else []:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and fc_tasks.pid_alive(d.get("pid")):
            found.append(d)
        else:
            f.unlink(missing_ok=True)
    return found

def live_on(proj, repo, kinds):
    """The runs and verifies of `kinds` alive on this project, other than this process. The hold file is matched by project name and
    by the repository, whichever of its checkouts the work was started from."""
    common = lambda path: (c := fc_core.checkout(path)) and c.common
    mine = common(repo)
    return [w for w in live_work()
            if w.get("kind") in kinds and w.get("project") == Path(proj).name and w["pid"] != os.getpid() and mine and common(w["repo"]) == mine]

def refuse_while_verifying(proj, repo):
    """Exit when another process is running `verify` on this project: it stands in the CLI worktree at the stack top, and anything
    that checks other branches out there (stack, top, reword, land) would make its stages judge other code."""
    for w in live_on(proj, repo, ("verify",)):
        sys.exit(f"STOP  verify is running on {w['project']} (pid {w['pid']}, since {w['since']}) and holds the stack top in the CLI worktree: "
                 "this would check other branches out under it. Wait for it to end, then re-run")

def refuse_while_building(proj, repo):
    """Exit when another process is running `run` or `resume` on this project: it links and registers the stack in the CLI worktree
    until it exits, even after its last log line, so a verify standing at the stack top there would be moved off it."""
    for w in live_on(proj, repo, ("run", "resume")):
        sys.exit(f"STOP  {w['kind']} is running on {w['project']} (pid {w['pid']}, since {w['since']}) and checks the stack's branches out in the CLI worktree "
                 "verify stands on: its checks would judge other code. Wait for it to end (it exits after it registers the stack), then re-run")

SHARED_STATE = {"run": ("run", "resume"), "resume": ("run", "resume"), "verify": ("verify",)}  # who writes the same results/ files as `cmd`

def refuse_while_running(cmd, proj, repo):
    """Exit before `cmd` (run, resume, verify) touches anything when another process of the same state is alive on this project: a
    second one would empty the log the first still writes, and a file it leaves behind (the exit code, the run marker) would end the second."""
    for w in live_on(proj, repo, SHARED_STATE[cmd]):
        sys.exit(f"STOP  {w['kind']} is already running on {w['project']} (pid {w['pid']}, since {w['since']}) and writes the same results files, "
                 f"even after it printed its last line while it finishes up: follow it with `compose watch`, or start {cmd} again once it has ended")

# ---------- the version pin: what a project runs on, and what changed under it at an update ----------

def mid_turn(pins, agents=None):
    """[(project, teammate)] for every teammate of a pinned project herdr shows working: a turn an update would change the skill under."""
    agents = fc_herdr.live_agents() if agents is None else agents
    return [(p["project"], ag["name"]) for p in pins for ag in agents or []
            if ag.get("agent_status") == "working" and fc_herdr.agent_id(p["project"], ag.get("name") or "")]

def rollout(pins):
    """One line per pinned project: the skill files that changed since its pin (what its team lead re-reads, and nothing else), then
    the pin moves to the version now installed."""
    try:
        cur = fc_hooks.skill_hashes()
    except (FileNotFoundError, OSError):
        cur = {}
    lines = []
    for p in pins:
        changed = [fc_pin.skill_name(f) for f in fc_hooks.skill_changed(p.get("skill") or {}, cur)] if p.get("skill") else []
        was = p.get("commit", "?")
        if changed:
            lines.append(f"update  {p['project']}: since its pin ({was}) these skill files changed: {', '.join(changed)}. Tell its team lead: "
                         f"\"compose updated: re-read {', '.join(changed)}; nothing else changed\"")
        elif p.get("skill"):
            lines.append(f"update  {p['project']}: no skill file changed since its pin ({was}); its team lead has nothing to re-read")
        else:
            lines.append(f"update  {p['project']}: pinned without skill hashes; tell its team lead which sections the merged fixes touched")
        fc_pin.pin(p["proj"], p.get("repo"), renew=True)
    return lines

def local_commits(checkout):
    """(branch, upstream, [(mark, sha, subject)]) for the commits the checkout's branch has that its upstream lacks, None when it has none.
    `git cherry` marks each one `-` when its patch is already upstream under another hash, `+` when it is only here."""
    git = lambda *args: fc_core.sh(["git", *args], checkout, check=False)
    branch, upstream = (git("rev-parse", "--abbrev-ref", ref).stdout.strip() for ref in ("HEAD", "@{u}"))
    cherry = git("cherry", "-v", upstream, "HEAD")
    if not upstream or cherry.returncode or not cherry.stdout.strip():
        return None
    return branch, upstream, [tuple(line.split(" ", 2)) for line in cherry.stdout.splitlines()]

def listing(commits, upstream):
    return [f"  {mark} {sha[:12]} {subject} ({'already on ' + upstream if mark == '-' else 'only here'})" for mark, sha, subject in commits]

def diverged(checkout):
    """The state and the way out when the checkout's branch has commits its upstream lacks, else None."""
    found = local_commits(checkout)
    if not found:
        return None
    branch, upstream, commits = found
    n = len(commits)
    if all(mark == "-" for mark, _, _ in commits):
        fix = f"git -C {shlex.quote(str(checkout))} reset --hard {upstream}"
        why = "Every change is already upstream, so drop the local copies:"
    else:
        fix = f"git -C {shlex.quote(str(checkout))} rebase {upstream}"
        why = "Replay them on top of it by hand (changes already upstream are skipped), then continue the rebase:"
    return "\n".join([f"{checkout}: {branch} has {n} commit{'s' if n > 1 else ''} that {upstream} does not, so it cannot fast-forward:",
                      *listing(commits, upstream), why, f"  {fix}", "Then run compose update again."])

def rebase_local(checkout):
    """Replay the commits only this checkout has onto its upstream (git drops the ones already there). The lines to print and
    whether the checkout now sits on top of upstream; a rebase that conflicts is aborted, leaving the checkout as it was."""
    found = local_commits(checkout)
    if not found:
        return [], False
    branch, upstream, commits = found
    before = fc_core.sh(["git", "rev-parse", "--short", "HEAD"], checkout).stdout.strip()
    r = fc_core.sh(["git", "rebase", upstream], checkout, check=False)
    if r.returncode:
        fc_core.sh(["git", "rebase", "--abort"], checkout, check=False)
        said = [l.strip() for l in (r.stdout + r.stderr).splitlines() if l.startswith("CONFLICT") or l.startswith("error:")]
        err = "; ".join(said or (r.stdout + r.stderr).split("\n")[:2])
        return [f"{checkout}: rebasing {branch} onto {upstream} stopped ({err[:300]}); the rebase was aborted and {branch} is back at {before}", diverged(checkout)], False
    kept = [c for c in commits if c[0] == "+"]
    return [f"{checkout}: {branch} had {len(commits)} commit{'s' if len(commits) > 1 else ''} {upstream} lacked; rebased onto it, "
            f"{len(commits) - len(kept)} already upstream dropped:", *listing(commits, upstream),
            *([f"{len(kept)} commit{'s' if len(kept) > 1 else ''} stay only in this checkout: put them on a branch and open a PR, "
               "or they will block the next machine that pulls from here"] if kept else [])], True


def cmd_update(a):
    """Exits 1 while a run or verify is live (unless --force), on local changes, or a failed pull or rebase, else with doctor's code."""
    checkout = Path(__file__).resolve().parents[1]
    live, pins = live_work(), fc_pin.pinned()
    busy = mid_turn(pins) if not a.force else []
    if (live or busy) and not a.force:
        sys.exit("\n".join(["compose update stopped: updating the CLI changes the code and the skill under work that is running now:",
                            *(f"  {w['project']}: {w['kind']} live since {w['since']} (pid {w['pid']}) in {w['repo']}" for w in live),
                            *(f"  {slug}: {name} is mid-turn" for slug, name in busy),
                            "Fixes roll out between phases: wait for the run, verify, or turn to end, or run compose update --force to update anyway."]))
    if fc_core.sh(["git", "status", "--porcelain"], checkout, check=False).stdout.strip():
        sys.exit(f"{checkout} has local changes: commit or stash them, then run compose update again")
    p = fc_core.sh(["git", "pull", "--ff-only"], checkout, check=False)
    if p.returncode:
        fetched = fc_core.sh(["git", "fetch", "-q"], checkout, check=False).returncode == 0  # a failed fetch says nothing about upstream
        lines, ok = rebase_local(checkout) if fetched else ([], False)
        if not lines:
            sys.exit(" ".join((p.stdout + p.stderr).split()))
        if not ok:
            sys.exit("\n".join(lines))
        print("\n".join(lines))
    for line in fc_links.relink_owned(checkout):
        print(line)
    for line in rollout(pins):
        print(line)
    sys.exit(fc_init.cmd_doctor(a))
