import argparse, json, os, re, signal, sys, time, warnings
from pathlib import Path
import fc_core, fc_herdr, fc_manager, fc_crew, fc_compact, fc_compactor, fc_transcript, fc_compactlog, fc_hooks, fc_watch, fc_factory

# The Claude Code Stop hook (`compose rewake`): judge whether a settled turn is a good moment to compact (a qualifying verdict starts the
# detached compactor), then park the watches whose first event wakes the idle session. Nothing here types into a pane.


def session_role(ev):
    """Which compaction-timed session the Stop hook runs in: ("lead", handoff or None, repo) for a team lead (COMPOSE_ROLE orchestrator
    with a project; handoff None when its project cannot be resolved), ("manager", handoff, repo) for an engineering manager (no
    COMPOSE_ROLE, in a herdr pane, and the repository's records say that pane manages something), else None. A failing manager check is None."""
    role, project, repo = os.environ.get("COMPOSE_ROLE"), os.environ.get("COMPOSE_PROJECT"), os.environ.get("COMPOSE_REPO") or "."
    if role == "orchestrator" and project:
        try:
            proj = fc_core.project_dir(argparse.Namespace(repo=repo, project=project))
            return "lead", fc_crew.handoff_path(proj, fc_herdr.agent_name(proj.name, "orchestrator")), repo
        except (Exception, SystemExit):
            return "lead", None, repo  # no handoff to point a compactor at: the watch still parks
    pane = fc_core.session_id()
    if role or not pane:
        return None
    try:
        main = fc_core.main_checkout(Path(ev.get("cwd") or os.getcwd()))
        if fc_manager.is_manager(main, pane):
            return "manager", fc_manager.manager_handoff(main, pane), str(main)
    except Exception:
        pass  # a malformed crew record or an unreadable repo: not a session compose can name a manager
    return None

def judge_turn(role, handoff, repo, ev):
    """Log this settled turn and, unless a compaction of the pane is already under way, ask whether it is a good moment to compact; a verdict
    that qualifies (or the ceiling) starts the detached compactor. Does nothing without a pane, a handoff, or a transcript on disk."""
    pane = fc_core.session_id()
    path = ev.get("transcript_path")
    transcript = Path(path) if path else None
    if not pane or handoff is None or transcript is None or not transcript.is_file():
        return
    tokens = fc_transcript.context_tokens(fc_transcript.records(transcript))
    fc_compactlog.append("turn", pane=pane, role=role, tokens=tokens)
    marker = fc_compactlog.inflight(pane)
    if marker:
        fc_compactlog.append("verdict", pane=pane, role=role, tokens=tokens, decision="in-flight",
                             reason=f"a {marker.get('mode') or 'compaction'} compaction is at {marker.get('step') or 'work'}")
        return
    verdict = fc_compact.consider(pane, role, transcript)
    mode = {"qualifies": "judged", "ceiling": "ceiling"}.get(verdict.get("decision"))
    if mode:
        fc_compactor.start(pane, role, transcript, mode, handoff, repo)

def manager_targets(repo, pane, session):
    """One watch's arguments per project the engineering manager in herdr pane `pane` holds: the project's own worktree (the repository
    itself when the project is not listed there), the name, and the Stop hook's settings."""
    known = fc_core.all_projects(Path(repo))
    return [argparse.Namespace(repo=str(known[proj.name][0] if proj.name in known else repo), project=proj.name, now=False, from_=None, timeout=0,
                               stall=None, interval=fc_watch.WATCH_INTERVAL, settle=fc_watch.WATCH_SETTLE, session=session)
            for proj in fc_manager.manager_projects(repo, pane)]

def _run(job):
    """A forked child: run the job and leave without touching what the parent set up. 2 is an event that wakes the session; every other end
    (the lock held by another watch, a failure) is 0, so it never ends its siblings. It dies bare on SIGTERM and SIGHUP, never through the
    parent's cleanup handler."""
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_DFL)
    code = 0
    try:
        job()
    except SystemExit as e:
        code = 2 if e.code == 2 else 0
    except BaseException:
        code = 0
    for out in (sys.stderr, sys.stdout):
        try:
            out.flush()
        except Exception:
            pass
    os._exit(code)

def park(jobs):
    """Run the watches `jobs` under this process: the first to exit 2 (an event) ends the others and wakes the session through SystemExit(2);
    when every one ends without an event this returns. One job runs in-process, its non-wake ends swallowed as a forked one's are; several run
    in a forked child each, before any of them keeps the Mac awake (each does that itself). The children end with this process: a SIGTERM or
    SIGHUP to it is forwarded to them (they are reaped before it goes on to die as it would have), and when its own parent is gone it ends
    them and returns, since each child's own check only sees this process, which stays."""
    if len(jobs) == 1:
        try:
            jobs[0]()
        except SystemExit as e:
            if e.code == 2:
                raise
        except Exception:
            pass
        return
    for out in (sys.stderr, sys.stdout):
        out.flush()  # a child would flush its copy of what the parent buffered, and print it twice
    parent, live = os.getppid(), set()
    def end_children():
        for pid in live:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for pid in live:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        live.clear()
    previous = {sig: signal.getsignal(sig) or signal.SIG_DFL for sig in (signal.SIGTERM, signal.SIGHUP)}
    def forwarded(sig, _frame):
        end_children()
        signal.signal(sig, previous[sig])
        os.kill(os.getpid(), sig)  # the disposition this process had: compose's cleanup handler, or dying
    for sig in previous:
        signal.signal(sig, forwarded)
    try:
        for job in jobs:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)  # "fork() in a multi-threaded process" would land in the wake text on stderr
                pid = os.fork()
            if pid == 0:
                _run(job)
            live.add(pid)
        while live:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                if os.getppid() != parent:
                    return end_children()  # the session that ran this hook is gone
                time.sleep(fc_watch.WATCH_INTERVAL)
            elif pid in live:  # not the compactor the judge started, a child of this process too
                live.discard(pid)
                if os.waitstatus_to_exitcode(status) == 2:
                    end_children()
                    raise SystemExit(2)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

def cmd_rewake(a):
    """Claude Code Stop hook (the plugin's hooks.json, asyncRewake): judge whether the settled turn of a team lead or an engineering
    manager is a good moment to compact, then park the watches whose event wakes the idle session: a team lead's one watch, or an
    engineering manager's one per project it holds and one for the factory when it holds that seat, under this process so the first event
    wakes the session once. A team lead's herdr tab carries the role, project, and worktree. Every other session returns at once, a headless
    one (`claude -p`, which Claude Code marks CLAUDE_CODE_ENTRYPOINT=sdk-*) too. A wake names skill files changed since the last one."""
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
        return
    try:
        ev = json.load(sys.stdin)
    except Exception:
        ev = {}
    if not isinstance(ev, dict):
        ev = {}
    fc_hooks.restore_pane_env(ev.get("cwd"))  # a team lead restored after a computer restart has lost the env its tab was created with
    found = session_role(ev)
    if found is None:
        return
    role, handoff, repo = found
    try:
        judge_turn(role, handoff, repo, ev)
    except Exception:
        pass  # the hook never fails the session because of the judge
    session = re.sub(r"[^\w-]", "", ev.get("session_id") or "")
    if role == "lead":
        fc_watch.cmd_watch(argparse.Namespace(repo=repo, project=os.environ["COMPOSE_PROJECT"], now=False, from_=None, timeout=0, stall=None,
                                              interval=fc_watch.WATCH_INTERVAL, settle=fc_watch.WATCH_SETTLE, session=session), rewake=True)
        return
    pane = fc_core.session_id()
    try:
        jobs = [lambda ns=ns: fc_watch.cmd_watch(ns, rewake=True) for ns in manager_targets(repo, pane, session)]
        if fc_manager.holds_factory(repo, pane):
            jobs.append(lambda: fc_factory.verb_watch(argparse.Namespace(timeout=None), Path(repo), rewake=True))
    except Exception:
        return  # an unreadable repository: nothing to park, and the hook never fails the session
    park(jobs)
