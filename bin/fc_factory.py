"""`compose factory`: start and stop the build loop process, watch what it does, and set its pace, planning and offers."""
import copy, fcntl, json, os, signal, subprocess, sys, time, traceback
from pathlib import Path

import fc_core, fc_config, fc_herdr, fc_change, fc_watch, fc_factorystate, fc_factoryturn, fc_night, fc_prd

POLL = 2.0  # seconds between looks at the event log in one watch

def lock_path(repo):
    path = fc_factorystate.state_path(repo).with_name("factory.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

def alive(pid):
    """True while a process with this pid exists (one we may not signal still counts)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

def clock(t=None):
    return time.strftime("%H:%M:%S", time.localtime(t))

def spawn_loop(repo):
    """The loop as a process of its own, out of this terminal's session; its output goes to .compose/factory.log. Returns its pid."""
    log = fc_factorystate.state_path(repo).with_name("factory.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "compose.py"), "factory", "loop", "--repo", str(repo)],
                             start_new_session=True, stdin=subprocess.DEVNULL, stdout=out, stderr=out)
    return p.pid

def _sleep(seconds, stopping):
    end = time.monotonic() + seconds
    while not stopping() and (left := end - time.monotonic()) > 0:
        time.sleep(min(1.0, left))

def running(repo):
    """True while a loop holds the repo's lock: the lock, not a recorded pid, says whether a loop runs."""
    with open(lock_path(repo), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False

HUMAN_KEYS = ("stopped", "day_tickets", "planning", "declined_until", "manager_pane", "mode")  # set by verbs while a turn runs: the disk's value wins

def _night(turn, before, disk):
    """The night record both the loop and the verbs write. A night the verbs started, ended or lifted meanwhile (another `since`) is the disk's;
    else the turn's, with the disk's stop count and any draining or end the disk gained since the turn began."""
    since = lambda n: n["since"] if isinstance(n, dict) else None
    if not isinstance(disk, dict) or since(disk) != since(turn):
        return disk
    merged = dict(turn, stops=disk["stops"])
    for k in ("draining", "ended"):
        if disk[k] and disk[k] != (before or {}).get(k):
            merged[k] = disk[k]
    return merged

def _merge(repo, state, before):
    """Save the turn's state, keeping what a verb changed on disk while the turn ran. Returns the merged state."""
    with fc_factorystate.edit(repo) as disk:
        for k in HUMAN_KEYS:
            state[k] = disk[k]
        state["night"] = _night(state["night"], before["night"], disk["night"])
        gone = set(before["out_of_order"]) - set(state["out_of_order"])
        state["out_of_order"] = [i for i in disk["out_of_order"] if i not in gone] + [i for i in state["out_of_order"] if i not in disk["out_of_order"] and i not in before["out_of_order"]]
        if "offer" in before["said"] and "offer" not in disk["said"] and state["said"].get("offer") == before["said"]["offer"]:
            state["said"].pop("offer")  # declined meanwhile
        disk.clear(); disk.update(state)
    return state

def _saved(repo, story, record):
    """Put one dispatch on disk as it happens: a crash later in the turn must not lose it (and start the story again)."""
    with fc_factorystate.edit(repo) as disk:
        disk["dispatched"][story] = record

def _pane(repo):
    """Record the engineering manager's current herdr pane: the loop starts team leads from it, not from the pane that started the loop."""
    pane = os.environ.get("HERDR_PANE_ID")
    if pane and fc_factorystate.load_state(repo)["manager_pane"] != pane:
        with fc_factorystate.edit(repo) as state:
            state["manager_pane"] = pane

def run_loop(repo, turns=None):
    """One turn after another until SIGTERM or a stop (after the turn it is in) or `turns` are done. Only the loop writes pid and started."""
    lock = open(lock_path(repo), "w")
    for tries in range(20):  # a status or watch probe holds the lock for an instant: try again before saying another loop has it
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if tries == 19:
                lock.close()
                sys.exit("factory: already running")
            time.sleep(0.05)
    stop = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: stop.append(signum))
    try:
        with fc_factorystate.edit(repo) as state:
            state["pid"], state["started"] = os.getpid(), time.time()
        done = 0
        while not stop and (turns is None or done < turns):
            try:
                state = fc_factorystate.load_state(repo)
                before = copy.deepcopy(state)
                try:
                    lines = fc_night.tick(repo, state, time.time(), stopping=lambda: bool(stop), saved=lambda story, record: _saved(repo, story, record))
                except Exception as e:  # turn keeps its own lines on a failure; this is the last guard
                    traceback.print_exc()
                    lines = [fc_factorystate.turn_failed(f"{type(e).__name__}: {e}")]
                for line in lines:
                    fc_factorystate.add_event(repo, line)
                if _merge(repo, state, before)["stopped"]:
                    stop.append("stopped")
            except Exception as e:  # the state or the log could not be read or written
                traceback.print_exc()
                try:
                    fc_factorystate.add_event(repo, fc_factorystate.turn_failed(f"{type(e).__name__}: {e}"))
                except Exception:
                    traceback.print_exc()
            done += 1
            if not stop and (turns is None or done < turns):
                _sleep(fc_config.FACTORY["turn_seconds"], lambda: bool(stop))
    finally:
        signal.signal(signal.SIGTERM, previous)
        lock.close()

def status_lines(repo, state):
    p = fc_factorystate.pace(state)
    if running(repo):
        head = f'running (pid {state["pid"]}, since {clock(state["started"])})'
    elif state["stopped"]:
        head = f'stopped (since {clock(state["stopped"])})'
    else:
        head = "not running"
    lines = [f'factory: {head}, {p["mode"]} pace {p["cap"]} tickets at once']
    for ident, r in sorted(state["dispatched"].items()):
        lines.append(f'  dispatched: {ident} ({r["project"]}, {r["kind"]})')
    if state["offer"]:
        lines.append(f'  last offer: {state["offer"]["id"]} ({state["offer"]["layer"]})')
    if state["planning"]:
        lines.append(f'  planning: {state["planning"]["id"]}')
    if state["declined_until"] and state["declined_until"] > time.time():
        lines.append(f'  no planning offers until {clock(state["declined_until"])[:5]}')
    return lines

START_WAIT = 30  # seconds start waits for the new loop to take its lock or die, before it calls it started

def _exited(pid):
    """True once our child pid is gone. No child to wait for means it exited and was reaped already: spawn_loop drops its Popen, and
    subprocess reaps a dropped Popen's exited child on the next Popen, which start's own git calls make."""
    try:
        return os.waitpid(pid, os.WNOHANG)[0] == pid
    except ChildProcessError:
        return True

def verb_start(a, repo):
    if not fc_herdr.herdr_env():
        sys.exit("factory: start it inside herdr: the team leads it starts live there (sections/0h.md)")
    state = fc_factorystate.load_state(repo)
    if running(repo):
        sys.exit(f'factory: already running (pid {state["pid"]}); compose factory status shows it')
    with fc_factorystate.edit(repo) as state:
        state["stopped"], state["pid"] = None, None  # the old pid would read as a lost loop until the new one takes the lock
        state["manager_pane"] = os.environ.get("HERDR_PANE_ID") or state["manager_pane"]
    pid = spawn_loop(repo)
    for _ in range(START_WAIT * 10):  # the loop takes its lock a moment after it starts, longer on a loaded machine
        if running(repo):
            break
        if _exited(pid):
            log = fc_factorystate.state_path(repo).with_name("factory.log")
            try:
                last = (log.read_text().strip().splitlines() or [""])[-1]
            except OSError:
                last = ""
            sys.exit(f"factory: the loop did not start; see .compose/factory.log: {last}".rstrip(": "))
        time.sleep(0.1)
    p = fc_factorystate.pace(state)
    print(f'factory: started (pid {pid}), {p["mode"]} pace {p["cap"]} tickets at once; compose factory watch wakes you on its events')

def verb_stop(a, repo):
    with fc_factorystate.edit(repo) as state:  # at night the first stop drains, and a second or --now holds the work and ends the night
        live = fc_night.live(state)
        line = fc_night.stop_stage(repo, state, a.now, a.hold)
        at_once = live and not fc_night.live(state)
    if line:
        print(line)
        return
    held = 0
    with fc_factorystate.edit(repo) as state:
        state["stopped"] = time.time()
        for ident, record in sorted(state["dispatched"].items()) if a.hold and not at_once else ():  # at once held them
            got = fc_factoryturn.project_for(repo, record["feature"])
            if not got:
                continue
            rests = fc_change.rests_on(got[2], [ident])
            if rests:
                fc_change.hold_tasks(got[2], list(rests), f"factory stopped: {a.hold}")
                held += len(rests)
        pid = state["pid"]
    if pid and pid != os.getpid() and alive(pid) and running(repo):
        os.kill(pid, signal.SIGTERM)
    if not at_once:
        print(f"factory: stopped; held {held} tasks" if a.hold else "factory: stopped; running work finishes")

def verb_status(a, repo):
    state = fc_factorystate.load_state(repo)
    lines = status_lines(repo, state)
    if a.json:
        print(json.dumps({"running": running(repo), "pid": state["pid"], "pace": fc_factorystate.pace(state), "dispatched": state["dispatched"],
                          "offer": state["offer"], "planning": state["planning"], "lines": lines}, indent=2))
    else:
        print("\n".join(lines))

def _seen_path(repo):
    return fc_factorystate.state_path(repo).with_name(f"factory-watch-{fc_watch.watch_key()}.json")

def verb_watch(a, repo):
    path = _seen_path(repo)
    _pane(repo)
    try:
        mem = json.loads(path.read_text())
    except (OSError, ValueError):
        mem = None
    if not isinstance(mem, dict):  # a new watcher starts at the log's end: replaying old events would have it act on each again
        seen = max([e["n"] for e in fc_factorystate.events_since(repo, 0)], default=0)
        path.parent.mkdir(parents=True, exist_ok=True)
        fc_core.atomic_write(path, json.dumps({"seen": seen, "lost": None}))
        print(f"[{fc_core.now()}] event factory=watching from={seen}  (earlier events: compose factory status)")
        return
    seen, lost = mem.get("seen", 0), mem.get("lost")
    deadline = None if a.timeout is None else time.monotonic() + float(a.timeout)
    while True:
        new = fc_factorystate.events_since(repo, seen)
        state = fc_factorystate.load_state(repo)
        out = [f'[{clock(e["at"])}] {e["line"]}' for e in new]
        if new:
            seen = new[-1]["n"]
        if state["pid"] and not state["stopped"] and not running(repo) and lost != state["pid"]:
            lost = state["pid"]
            out.append(f"[{fc_core.now()}] event factory=loop-lost  (the loop is not running: compose factory start runs it again; its state is on disk)")
        if out:
            path.parent.mkdir(parents=True, exist_ok=True)
            fc_core.atomic_write(path, json.dumps({"seen": seen, "lost": lost}))
            print("\n".join(out))
            return
        if deadline is not None and time.monotonic() >= deadline:
            print(f"[{fc_core.now()}] event timeout")
            sys.exit(2)
        time.sleep(POLL if deadline is None else max(0.0, min(POLL, deadline - time.monotonic())))

def verb_pace(a, repo):
    with fc_factorystate.edit(repo) as state:
        p = fc_factorystate.pace(state)
        if a.target is not None and a.target.isdigit() and int(a.target) >= 1:
            state["day_tickets"] = int(a.target)
    if a.target is None:
        print(f'factory: {p["mode"]} pace {p["cap"]} tickets at once')
        return
    if not a.target.isdigit() or int(a.target) < 1:
        sys.exit(f"factory pace takes a whole number of 1 or more, not {a.target}")
    print(f'factory: {p["mode"]} pace {a.target} tickets at once (was {p["cap"]}); nothing running is stopped')

def verb_plan(a, repo):
    if a.done:
        with fc_factorystate.edit(repo) as state:
            state["planning"] = None
        print("factory: planning done; offers resume")
        return
    if not a.target:
        sys.exit("factory plan takes the ticket being planned (compose factory plan NEB-30), PRD for the PRD, or --done")
    target = fc_prd.PRD_ID if a.target.upper() == fc_prd.PRD_ID else a.target
    with fc_factorystate.edit(repo) as state:
        state["planning"] = {"id": target, "at": time.time()}
        out_of_order = target != fc_prd.PRD_ID and target != (state["offer"] or {}).get("id")  # the PRD sits ahead of every ticket
        if out_of_order and target not in state["out_of_order"]:
            state["out_of_order"].append(target)
    print(f"factory: planning {target}" + (" (out of order: once it is specced you are asked whether to move it up)" if out_of_order else ""))

def verb_decline(a, repo):
    with fc_factorystate.edit(repo) as state:
        state["declined_until"] = time.time() + 60 * fc_config.FACTORY["offer_snooze_minutes"]
        state["said"].pop("offer", None)
    print(f'factory: no planning offers until {clock(state["declined_until"])[:5]}; ready work keeps building')

def verb_loop(a, repo):
    run_loop(repo)

def cmd_factory(a):
    if a.action in fc_night.VERBS:
        return getattr(fc_night, f"verb_{a.action}")(a)
    if a.action in ("start", "loop") and not fc_config.LINEAR_KEY:
        sys.exit(fc_config.needs_linear_key("compose factory"))
    globals()[f"verb_{a.action}"](a, Path(a.repo).resolve())
