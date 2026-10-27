"""Implementer commands in process groups the run can stop: each child starts in its own session, so a stop (Ctrl-C) kills
the whole group, tool children included, and nothing new starts after it. A pause kills one task's groups the same way, and `run` raises
`Paused` in that task's thread. Stdlib only, no fc_ module."""
import contextlib, fcntl, os, shutil, re, signal, subprocess, sys, threading, time

STOP = threading.Event()  # set once the run is stopped


class Stopped(Exception):
    """Raised instead of starting a command after the stop."""


HOOKS = {}  # name -> zero-argument callable stop() calls: herdr agents register their esc and pane close here
RUNNING = set()  # pids of the groups `run` is waiting on
TASK = threading.local()  # a caller sets TASK.id around the commands it wants pausable; a thread without .id is no task
TASK_GROUPS = {}  # task id -> pids of the groups `run` is waiting on for that task
PAUSED = set()  # task ids paused now
KILLED = set()  # pids of groups a pause killed: `run` raises Paused for them even when the task was resumed before it noticed
_LOCK = threading.Lock()  # guards PAUSED and TASK_GROUPS


class Paused(Exception):
    """Raised by `run` in the thread of a paused task, instead of starting a command or after its group was killed."""


def run(cmd, cwd=None, env=None, timeout=None, input=None):
    """Like subprocess.run(cmd, capture_output=True, text=True, stdin=DEVNULL), but in a session of its own and tracked in
    RUNNING while it runs. `input` goes in on stdin instead. Returns a subprocess.CompletedProcess; raises Stopped after the stop, Paused when the calling thread's task (TASK.id) is paused, TimeoutExpired past `timeout`."""
    tid = getattr(TASK, "id", None)
    if STOP.is_set():
        raise Stopped
    if tid is not None and tid in PAUSED:
        raise Paused
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL if input is None else subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    RUNNING.add(p.pid)
    with _LOCK:
        KILLED.discard(p.pid)  # a reused pid must not inherit a stale entry
    late = False
    if tid is not None:
        with _LOCK:
            TASK_GROUPS.setdefault(tid, set()).add(p.pid)
            late = tid in PAUSED  # paused between the check above and now: pause_task did not see this group
            if late:
                KILLED.add(p.pid)
    try:
        if tid is not None and late:
            kill_group(p.pid)
        try:
            out, err = p.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            kill_group(p.pid)
            e.stdout, e.stderr = p.communicate()  # what the command printed before it was killed
            if (p.pid in KILLED or (tid is not None and tid in PAUSED)) and not STOP.is_set():
                raise Paused
            raise
        except KeyboardInterrupt:  # Ctrl-C or a SIGINT to this process: the command is part of what is being stopped
            kill_group(p.pid)
            raise
        if p.pid in KILLED or (tid is not None and tid in PAUSED):
            raise Paused
        if STOP.is_set():
            raise Stopped
        return subprocess.CompletedProcess(cmd, p.returncode, out, err)
    finally:
        RUNNING.discard(p.pid)
        if tid is not None:
            with _LOCK:
                KILLED.discard(p.pid)
                TASK_GROUPS.get(tid, set()).discard(p.pid)


def run_limited(cmd, cwd=None, env=None, timeout=None):
    """`run` for a check or a case, where passing the limit is a failure to report, not a crash: the CompletedProcess has exit 124 (as
    `timeout(1)`), the output printed before the kill, and a last stderr line naming the limit. Raises Stopped after the stop."""
    try:
        return run(cmd, cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        err = (e.stderr or "") + ("\n" if e.stderr and not e.stderr.endswith("\n") else "") + f"TIMEOUT: did not finish within {timeout}s\n"
        return subprocess.CompletedProcess(cmd, 124, e.stdout or "", err)


def children_outside(pid):
    """Pids of the processes that descend from `pid` and sit in a group other than its own: a server a test runner starts in
    a group of its own (Playwright's webServer), which a kill of the group does not reach. [] when `ps` is not there."""
    try:
        rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True, timeout=10).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    kids = {}
    for row in rows:
        if len(parts := row.split()) == 3 and all(x.isdigit() for x in parts):
            kids.setdefault(int(parts[1]), []).append((int(parts[0]), int(parts[2])))
    found, todo = [], [pid]
    while todo:
        for child, group in kids.get(todo.pop(), []):
            todo.append(child)
            if group != pid:
                found.append(child)
    return found


def _signal(pid, sig, group=False):
    """Send `sig` to a process, or to the group `pid` leads; False when it is gone (macOS says EPERM for a group of zombies)."""
    try:
        (os.killpg if group else os.kill)(pid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def kill_group(pid, proc=None):
    """SIGTERM to the process group `pid` and to what it started in groups of their own, SIGKILL after 5 s to whatever is still
    alive; a group already gone is not an error. `proc` is the Popen of the leader when the caller is its parent: it is reaped
    while this waits, as a zombie leader keeps its group alive on Linux."""
    outside = children_outside(pid)  # before the signal: once the leader is gone they are reparented and cannot be found
    term = _signal(pid, signal.SIGTERM, group=True)
    term = any([_signal(child, signal.SIGTERM) for child in outside]) or term
    if not term:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc:
            proc.poll()
        if not any([_signal(pid, 0, group=True)] + [_signal(child, 0) for child in outside]):  # gone, or only zombies
            return
        time.sleep(0.1)
    for child in outside:
        _signal(child, signal.SIGKILL)
    _signal(pid, signal.SIGKILL, group=True)


def stop():
    """Set STOP, kill every group in RUNNING, call every hook in HOOKS. The hooks are taken first: a group killed lets the
    thread that waited on it go on, and that thread may take its hook out on its way out, before its own cleanup ends."""
    STOP.set()
    hooks = list(HOOKS.values())
    for pid in list(RUNNING):
        kill_group(pid)
    for hook in hooks:
        hook()


def on_termination(cleanup):
    """Install the SIGTERM and SIGHUP handler (a closed pane, a `timed` limit above this process, a kill): left to die bare the
    CLI skips atexit, so every scratch config it made stays in the temp folder and a command `timed` started runs on without
    it. The handler runs `cleanup`, then stop(), then exits 128 + the signal: no SystemExit, which a run's worker threads would
    hold open until their tasks end."""
    def terminated(sig, _frame):
        cleanup()
        stop()
        os._exit(128 + sig)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, terminated)


def pause_task(tid, hook=None):
    """Pause task `tid`: its later commands raise Paused, every group in TASK_GROUPS[tid] is killed, and HOOKS[hook] is called
    when present (taken first, as stop() does). False when `tid` was already paused."""
    with _LOCK:
        if tid in PAUSED:
            return False
        PAUSED.add(tid)
        pids = list(TASK_GROUPS.get(tid, ()))
        KILLED.update(pids)
    fn = HOOKS.get(hook) if hook else None
    for pid in pids:
        kill_group(pid)
    if fn:
        fn()
    return True


def task_paused(tid):
    """Whether task `tid` is paused."""
    return tid in PAUSED


def resume_task(tid):
    """Let task `tid` start commands again."""
    with _LOCK:
        PAUSED.discard(tid)


def restore_sigint():
    """Make SIGINT stop this process again when it started with SIGINT ignored: a background job of a non-interactive shell, or
    nohup, hands that on, Python then installs no KeyboardInterrupt handler, and every command this process starts inherits the
    ignore (exec keeps it, and resets only a handler), so a `kill -INT` stopped neither the verify nor its suite."""
    if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
        signal.signal(signal.SIGINT, signal.default_int_handler)


AWAKE = {}  # pid -> the caffeinate that keeps the Mac awake while that process lives


def keep_awake(pid=None):
    """Keep a Mac awake while `pid` (default: this process) lives: `caffeinate -dims -w <pid>` holds the display, idle, disk and
    (on AC) system-sleep assertions, and ends with that process however it dies, so none leaks. One per pid; returns it, or None
    off macOS, where caffeinate is missing, and under COMPOSE_NO_AWAKE=1. It cannot stop a lid closed on battery (see
    fc_checks.battery_report)."""
    pid = pid or os.getpid()
    held = AWAKE.get(pid)
    if held and held.poll() is None:
        return held
    if sys.platform != "darwin" or not shutil.which("caffeinate") or os.environ.get("COMPOSE_NO_AWAKE"):
        return None
    try:
        AWAKE[pid] = subprocess.Popen(["caffeinate", "-dims", "-w", str(pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None
    return AWAKE[pid]


SLEPT_MIN = 60  # seconds of sleep that count: a short gap between the two clocks is no more than their read skew


def clock():
    """The wall clock and the awake clock now, for `slept`. time.monotonic() stops while a Mac sleeps (and so does every timeout
    built on it, subprocess's included); time.time() does not."""
    return time.time(), time.monotonic()


def slept(since):
    """Seconds the machine slept since `since` (a `clock()`): the wall time that passed less the awake time. 0 under SLEPT_MIN."""
    wall, awake = since
    gap = (time.time() - wall) - (time.monotonic() - awake)
    return gap if gap >= SLEPT_MIN else 0


def slept_text(seconds):
    return f"the machine slept for {round(seconds / 60)} minutes"


QUICK_SLOTS = 2  # every other command (see `suite`), in lanes of their own so a short run never waits on a suite; COMPOSE_QUICK_SLOTS overrides
SLOT_SAY_AFTER = 1.0  # seconds a wait for a slot stays silent

SCOPED_RE = re.compile(  # a name filter flag | one test file (python, go, ruby, rust, js/ts, jvm, swift) | one dotted test module
    r"(?:^|\s)(?:-k|-run|-t|-only|--grep|-g|--filter|--only|--testNamePattern|--tests|--test-name-pattern|-testPathPattern|--testPathPattern)(?:[\s=]|$)"
    r"|(?:^|[\s/='\"])(?:test_\w+\.py|\w+_test\.(?:py|go|rb|rs)|\w+\.(?:test|spec)\.[cm]?[jt]sx?|\w+(?:Test|Tests|Spec)\.(?:java|kt|swift))(?![\w*])"
    r"|(?:^|\s)(?:\w+\.)*test_\w+(?:\.\w+)*(?:\s|$)")


SUITE_RE = re.compile(  # a test runner by name | a build tool or package manager asked for its tests or checks
    r"(?:^|[\s/])(?:pytest|py\.test|tox|nox|jest|vitest|mocha|rspec|playwright|cypress|phpunit|ctest)(?:\s|$)"
    r"|\bunittest\b|\btests\.run\b|\bnode\s+(?:\S+\s+)*--test\b"
    r"|\b(?:npm|yarn|pnpm|bun|deno|go|cargo|swift|dotnet|rake|make|mix|mvn|gradlew?|sbt|bazel|xcodebuild|flutter)\b.*?\s(?:test|tests|check|verify|nextest)(?:[\s:]|$)")


def _parts(cmd):
    return [x for x in re.split(r"&&|\|\||;|\n", cmd or "") if x.strip() and not re.match(r"\s*(?:cd|export|source|\.|set|echo)\s", x)]


def scoped(cmd):
    """Whether the shell line `cmd` runs a slice of the tests, not a suite: a name filter (`-k`, `-run`, `--grep`, `-t`, ...), one
    test file, or one test module. A line of several commands is scoped only when each one past a `cd`, `export` or `source` is."""
    parts = _parts(cmd)
    return bool(parts) and all(SCOPED_RE.search(x) for x in parts)


def suite(cmd):
    """Whether the shell line `cmd` runs a whole test suite: a test runner or a `test`/`check` target of a build tool, with no
    filter or file on it. Only a suite takes a suite slot (suite_slots); anything else (`--help`, `uv sync`, a scoped run) is short or
    bounded by the caller and takes a quick lane (QUICK_SLOTS), so it never queues behind other teams' suites."""
    return any(SUITE_RE.search(x) and not SCOPED_RE.search(x) for x in _parts(cmd))


def suite_slots():
    """How many full-suite `timed` commands run at once on this machine: one full suite at a time, unless COMPOSE_TEST_SLOTS says more."""
    return max(1, int(os.environ.get("COMPOSE_TEST_SLOTS") or 1))


@contextlib.contextmanager
def slot(say=None, quick=False):
    """One of suite_slots() machine-wide slots (lock files under ~/.compose/test-slots), held while a timed command runs: five teammates
    each running a suite at once leave the machine doing little else. A `quick` command (anything but a `suite`) takes one of QUICK_SLOTS
    in a lane of its own (files `q0`, `q1`, ...), so it waits only on other quick runs; the two lanes together bound the load.
    Yields the slot's number, which goes in the command's env as COMPOSE_TEST_SLOT so a compose it runs takes none: a suite under
    a slot runs compose itself, and with one slot its timed commands would wait on their parent for ever; a process already
    under one yields that. `say` gets one line once the wait passes SLOT_SAY_AFTER. The lock goes with the process, so a killed
    command frees its slot."""
    held = os.environ.get("COMPOSE_TEST_SLOT")
    if held:
        yield held
        return
    n = max(1, int(os.environ.get("COMPOSE_QUICK_SLOTS") or QUICK_SLOTS)) if quick else suite_slots()
    prefix = "q" if quick else ""
    d = os.path.join(os.path.expanduser("~"), ".compose", "test-slots")
    os.makedirs(d, exist_ok=True)
    fds = [os.open(os.path.join(d, f"{prefix}{i}"), os.O_RDWR | os.O_CREAT, 0o644) for i in range(n)]
    t0, said = time.monotonic(), False
    try:
        while True:
            for i, fd in enumerate(fds):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue
                yield f"{prefix}{i}"
                return
            if say and not said and time.monotonic() - t0 >= SLOT_SAY_AFTER:
                say(f"waiting for a {'quick-run' if quick else 'test'} slot: {n} {'quick ' if quick else ''}timed command{'s' if n > 1 else ''} already run on this machine")
                said = True
            time.sleep(0.2)
    finally:
        for fd in fds:
            os.close(fd)  # closing releases the lock

def nice_floor(pid=None, floor=10):
    """Raise the niceness of this process (pid None or 0) or another to at least floor; a higher niceness is never lowered.
    Returns the niceness the process has afterwards, or None on OSError (the process is gone or not ours)."""
    pid = pid or 0
    try:
        if os.getpriority(os.PRIO_PROCESS, pid) < floor:
            os.setpriority(os.PRIO_PROCESS, pid, floor)
        return os.getpriority(os.PRIO_PROCESS, pid)
    except OSError:
        return None
