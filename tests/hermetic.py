"""Imported first by every test module: no test, and no CLI it runs, sees the real home.

On import the process gets a temp HOME and COMPOSE_CONFIG and loses the key variables and the caller's test slot, so paths
the bin modules compute at import land there; each test then runs with its own fresh temp HOME (see temp_home) and COMPOSE_CONFIG, and its environment is restored after.
A test that still changes the real ~/.compose files that hold keys or config (created, written, moved or deleted, from the test
process or any subprocess) fails, naming the files, never their content.
Nor does a test reach the real machine: `lsof` and `herdr` on PATH are fakes (see guard_dir). The fake lsof answers only for the
listeners a test registers with listen() and, for a port the call names, the machine's socket table (netstat), so it never walks the machine's processes; the fake herdr answers its version and integration status and fails
the test that ran any other call, naming it. Every process a test registers is killed when the test ends."""
import atexit, hashlib, json, os, pwd, shutil, signal, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

REAL = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".compose"  # the account's home, whatever HOME says
GUARDED = ("config.json", "auth.json", ".env", "openrouter.key")
KEYS = ("OPENROUTER_API_KEY", "TEXT_MODEL_API_KEY", "OPENCODE_CONFIG_CONTENT", "TYPESAFE_API_KEY")
CALLERS = ("COMPOSE_TEST_SLOT",  # the slot of a `compose timed` running the suite: a lock under the real home, not a test's
           "HERDR_PANE_ID",  # the herdr pane the suite runs in is the session identity of a watch, crew start, or yolo: a test sets its own
           "COMPOSE_ROLE",  # the suite run in a teammate's tab (a toolsmith's) is no engineering manager: a test sets its own
           "HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_SESSION", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_BIN_PATH")  # the suite is no pane of the user's herdr: a test that needs one sets its own


INSTALLED = {  # what compose's required tools keep in a home, faked so `compose init` goes ahead without the user's machine
    ".local/share/gh/extensions/gh-stack/gh-stack": "#!/bin/sh\necho 'gh stack (fake)'\n",
    ".claude/skills/herdr/SKILL.md": "# herdr (fake)\n",
    ".claude/plugins/marketplaces/i-have-adhd/skills/i-have-adhd/SKILL.md": "# i-have-adhd (fake)\n",
}


_made = []  # every directory tempfile.mkdtemp made in this process, removed when the test that made it ends (else at exit)
_mkdtemp = tempfile.mkdtemp


def mkdtemp(*a, **kw):
    d = _mkdtemp(*a, **kw)
    _made.append(d)
    return d


def remove_made(since=0):
    """Remove every directory made since `since`, and whatever sits beside one under its name: a pi turn keeps its sessions
    as <clone>.pi-sessions and a crew test adds a <repo>-slot worktree, made by mkdir rather than mkdtemp, so only the
    directory they are named after is tracked. A mkdtemp name is random, so a sibling that starts with it was derived from it."""
    while len(_made) > since:
        parent, name = os.path.split(_made.pop())
        try:
            kin = [e for e in os.listdir(parent) if e.startswith(name)]
        except OSError:
            kin = [name]
        for e in kin:
            shutil.rmtree(os.path.join(parent, e), ignore_errors=True)


_stubs = None  # one read-only file per distinct executable body a test made; every stub path is a symlink to its body's file
_chmod = os.chmod


def stub_store():
    global _stubs
    if _stubs is None:
        _stubs = Path(_mkdtemp(prefix="compose-stubs-")).resolve()
        atexit.register(shutil.rmtree, _stubs, ignore_errors=True)
    return _stubs


def chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
    """os.chmod, with every executable file a test makes shared by body. macOS malware-scans each new executable file on its
    first run (about 150 ms of XprotectService, synchronous), and the suite makes thousands of stub tools with a few dozen
    distinct bodies, so a file given an executable mode is moved into the store under its body's hash (or dropped for the
    twin already there) and its path becomes a symlink, which runs without a new scan. A linked stub given a non-executable
    mode becomes its own file again, and so does one written again through Path.write_text or write_bytes (still executable,
    so it is shared again under its new body). Store files are read-only, so any other way of writing through a link fails
    with PermissionError rather than changing every twin."""
    p = Path(path) if dir_fd is None and follow_symlinks and isinstance(path, (str, os.PathLike)) else None
    if p is not None and p.is_symlink() and str(p.resolve()).startswith(str(stub_store()) + "/"):
        if mode & 0o111:
            return
        body = p.read_bytes()
        p.unlink()
        p.write_bytes(body)
    elif p is not None and mode & 0o111 and p.is_file():
        shared = stub_store() / hashlib.sha256(p.read_bytes()).hexdigest()
        if shared.exists():
            p.unlink()
        else:
            try:
                os.replace(p, shared)
            except OSError:  # another file system: a copy, scanned once for everyone
                shutil.copy2(p, shared)
                p.unlink()
            _chmod(shared, 0o555)
        p.symlink_to(shared)
        return
    return _chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)


def path_chmod(self, mode, *, follow_symlinks=True):
    """Path.chmod through chmod: on 3.9 it calls the os.chmod pathlib bound at import, which never sees the patch."""
    chmod(self, mode, follow_symlinks=follow_symlinks)


def shared(p):
    return _stubs is not None and p.is_symlink() and str(p.resolve()).startswith(str(_stubs) + "/")


def writing_anew(write):
    """Path.write_text or write_bytes: a linked stub written again is its own file again, with the executable mode it had."""
    def wrapped(self, *a, **kw):
        was_shared = shared(self)
        if was_shared:
            self.unlink()
        out = write(self, *a, **kw)
        if was_shared:
            chmod(self, 0o755)
        return out
    return wrapped


if tempfile.mkdtemp is not mkdtemp:
    tempfile.mkdtemp = mkdtemp
    os.chmod = chmod
    Path.chmod = path_chmod
    Path.write_text = writing_anew(Path.write_text)
    Path.write_bytes = writing_anew(Path.write_bytes)
    atexit.register(remove_made)


def temp_home():
    """{HOME, COMPOSE_CONFIG, DO_NOT_TRACK} for a new temp home that holds only INSTALLED and a fresh trail update cache: no keys,
    no config, no links. trail (graft) spawns a detached child to ask npm for a newer version when ~/.graft/update-check.json
    is a day old or missing, and another to flush telemetry; each writes under HOME after the test removed it, so the home
    came back as an empty .graft. A cache checked now and DO_NOT_TRACK spawn neither. COMPOSE_NO_PREFLIGHT=1 keeps the preflight's
    key calls, gh login check and machine warnings (fc_init.start_checks) out of a test: they ask the network and read this machine.
    COMPOSE_NO_AWAKE=1 keeps a test that reaches run, verify, watch or crew start from leaving a caffeinate on the suite's pid.
    COMPOSE_NO_LOAD=1 switches the load gate off: a test that reaches crew start, timed, run or verify would otherwise read this
    machine's real load and hold or wait when it is high; a test of the gate pops it or patches fc_load.off.
    GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, GIT_COMMITTER_NAME and GIT_COMMITTER_EMAIL give a test a git identity, because a temp
    HOME has no ~/.gitconfig, so git would refuse its commits with "Author identity unknown"."""
    home = Path(tempfile.mkdtemp(prefix="compose-home-")).resolve()
    (home / "guard").mkdir()
    for rel, text in INSTALLED.items():
        f = home / rel
        f.parent.mkdir(parents=True)
        f.write_text(text)
        f.chmod(0o755)
    (home / ".graft").mkdir()
    (home / ".graft" / "update-check.json").write_text(json.dumps({"latest": None, "checkedAt": int(time.time() * 1000)}))
    return {"HOME": str(home), "COMPOSE_CONFIG": str(home / ".compose" / "config.json"), "DO_NOT_TRACK": "1", "COMPOSE_NO_PREFLIGHT": "1", "COMPOSE_NO_AWAKE": "1", "COMPOSE_NO_LOAD": "1",
            "PATH": guarded_path(), "COMPOSE_TEST_GUARD": str(home / "guard"), **git_env(),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


FAKE_LSOF = """#!{python}
import os, sys
data = os.environ.get("COMPOSE_TEST_GUARD")
args = sys.argv[1:]
if data:
    with open(os.path.join(data, "calls"), "a") as f:
        f.write("lsof " + " ".join(args) + "\\n")
rows = []
try:
    rows = [l.split(None, 2) for l in open(os.path.join(data, "listeners")).read().splitlines() if l.strip()]
except (OSError, TypeError):
    pass
if "-p" in args:  # cwd of the named pids: `-Fpn`
    wanted = args[args.index("-p") + 1].split(",")
    for pid, port, cwd in rows:
        if pid in wanted:
            print("p" + pid + "\\nn" + cwd)
else:  # listeners, all or on the named ports: `-t`
    ports = [a.rsplit(":", 1)[1] for a in args if a.startswith("-iTCP:") or a.startswith("tcp:")]
    found = [pid for pid, port, cwd in rows if not ports or port in ports]
    if ports and not found:  # a port the test owns but did not register (a server its code under test started): the socket table, never the processes
        import subprocess
        for l in subprocess.run(["netstat", "-anv", "-p", "tcp"], capture_output=True, text=True).stdout.splitlines():
            f = l.split()
            if len(f) > 5 and f[5] == "LISTEN" and f[3].rsplit(".", 1)[-1] in ports:
                found += [x.rsplit(":", 1)[1] for x in f if ":" in x and x.rsplit(":", 1)[1].isdigit()][:1]
    for pid in dict.fromkeys(found):
        print(pid)
"""
FAKE_HERDR = """#!/bin/sh
case "$*" in
  "--version") echo "herdr 99.0.0"; exit 0;;
  "integration status") for a in pi omp claude codex copilot; do echo "$a: current (v1) (/fake/$a)"; done; exit 0;;
esac
echo "herdr $*" >> "${COMPOSE_TEST_GUARD:-/dev/null}/calls"
echo '{"error": {"code": "test_guard", "message": "a test must not reach the real herdr"}}'
exit 1
"""
_guard = None


def guard_dir():
    """The directory holding the fake lsof and herdr, first on every test's PATH."""
    global _guard
    if _guard is None:
        _guard = Path(_mkdtemp(prefix="compose-guard-")).resolve()
        atexit.register(shutil.rmtree, _guard, ignore_errors=True)
        for name, body in (("lsof", FAKE_LSOF.format(python=sys.executable)), ("herdr", FAKE_HERDR)):
            (_guard / name).write_text(body)
            _chmod(_guard / name, 0o755)
    return _guard


_git_env = None


def git_env():
    """On macOS /usr/bin/git is an xcrun shim that costs ~7 ms a call on top of git's own ~4, and the suite makes tens of
    thousands of git calls. guard_dir gets a link to the real git; git finds its helpers through the link only when told where."""
    global _git_env
    if _git_env is None:
        _git_env = {}
        if sys.platform == "darwin" and shutil.which("git") == "/usr/bin/git":
            try:
                real = Path(subprocess.run(["xcrun", "-f", "git"], capture_output=True, text=True, timeout=10).stdout.strip())
            except (OSError, subprocess.SubprocessError):
                real = Path()
            core, templates = real.parents[1] / "libexec" / "git-core", real.parents[1] / "share" / "git-core" / "templates"
            if real.name == "git" and real.is_file() and core.is_dir():
                (guard_dir() / "git").symlink_to(real)
                _git_env = {"GIT_EXEC_PATH": str(core), **({"GIT_TEMPLATE_DIR": str(templates)} if templates.is_dir() else {})}
    return _git_env


def guarded_path():
    return os.pathsep.join([str(guard_dir())] + [p for p in os.environ.get("PATH", "").split(os.pathsep) if p != str(guard_dir())])


def listen(pid, port, cwd):
    """Tell the fake lsof that process `pid`, started by the test, listens on `port` with working directory `cwd`. The process is killed when the test ends."""
    with open(Path(os.environ["COMPOSE_TEST_GUARD"]) / "listeners", "a") as f:
        f.write(f"{pid} {port} {cwd}\n")


def guard_calls(tool=None, clear=False):
    """The lines the fake lsof and herdr logged for this test (`lsof ...`, `herdr ...`), only `tool`'s when given. `clear` forgets them."""
    f = Path(os.environ["COMPOSE_TEST_GUARD"]) / "calls"
    lines = f.read_text().splitlines() if f.exists() else []
    if clear:
        f.unlink(missing_ok=True)
    return [l for l in lines if tool is None or l.split()[0] == tool]


def guarded(data):
    """Kill every process the test registered with listen(), then fail the test if it ran the fake herdr."""
    f = Path(data) / "listeners"
    for l in f.read_text().splitlines() if f.exists() else []:
        try:
            os.kill(int(l.split()[0]), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    calls = Path(data) / "calls"
    herdr = [l for l in calls.read_text().splitlines() if l.startswith("herdr ")] if calls.exists() else []
    if herdr:
        raise AssertionError(f"this test ran the real herdr, which starts agents and tabs on the user's machine: {'; '.join(herdr)}")


def snapshot():
    """Each guarded file's (inode, size, mtime), or None when absent: stat only, nothing is opened."""
    out = {}
    for name in GUARDED:
        try:
            s = (REAL / name).stat()
            out[name] = (s.st_ino, s.st_size, s.st_mtime_ns)
        except FileNotFoundError:
            out[name] = None
    return out


def untouched(before):
    changed = [n for n, s in snapshot().items() if s != before[n]]
    if changed:
        raise AssertionError(f"this test changed the real {REAL}: {', '.join(changed)}")


_run = unittest.TestCase.run
_own = unittest.TestCase.__dict__.get("__init_subclass__")  # TestCase's own, when this Python has one
_init_subclass = _own.__func__ if _own else lambda cls, **kw: super(unittest.TestCase, cls).__init_subclass__(**kw)
_world = {}  # test module name -> {name: module} of the fc_* modules in sys.modules when its last test class was defined


def fc_modules():
    return {n: m for n, m in sys.modules.items() if n.startswith("fc_")}


def remember_world(cls, **kw):
    """A test file reloads every fc_* module at import, so with several files in one process the bin modules' own lazy
    `import fc_x` would find the last file's copies while an earlier file patches its own. The classes are defined after
    the file's reload, so the modules then in sys.modules are the file's world; its tests run in it (see run)."""
    _init_subclass(cls, **kw)
    _world[cls.__module__] = fc_modules()


class in_world:
    """sys.modules holds the fc_* modules of the file a test belongs to for the test, and what it held before after."""
    def __init__(self, test):
        self.world = _world.get(type(test).__module__)

    def __enter__(self):
        if self.world is not None:
            self.before = fc_modules()
            for n in self.before.keys() - self.world.keys():
                del sys.modules[n]
            sys.modules.update(self.world)

    def __exit__(self, *exc):
        if self.world is not None:
            self.world.update({n: m for n, m in fc_modules().items() if n not in self.world})  # a module first imported by a test stays the file's
            for n in fc_modules().keys() - self.before.keys():
                del sys.modules[n]
            sys.modules.update(self.before)


def run(self, result=None):
    made = len(_made)
    env = mock.patch.dict(os.environ, temp_home())
    env.start()
    try:
        self.addCleanup(untouched, snapshot())  # the first cleanup added runs last, after the test's own
        self.addCleanup(guarded, os.environ["COMPOSE_TEST_GUARD"])
        with in_world(self):
            return _run(self, result)
    finally:
        env.stop()
        remove_made(made)  # the test's temp home and every directory it or its setUp made


run.hermetic = True
if not getattr(unittest.TestCase.run, "hermetic", False):
    for k in KEYS + CALLERS:
        os.environ.pop(k, None)
    os.environ.update(temp_home())
    unittest.TestCase.run = run
    unittest.TestCase.__init_subclass__ = classmethod(remember_world)
