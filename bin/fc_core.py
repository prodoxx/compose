import atexit, fcntl, hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path


# ---------- output and cost ledger ----------

def now():
    return time.strftime("%H:%M:%S")

def fmt(seconds):
    return f"{seconds:.1f}s" if seconds < 90 else f"{int(seconds // 60)}m{seconds % 60:04.1f}s"

def say(msg):
    print(f"[{now()}] {msg}", flush=True)

class Ledger:
    """Tokens and dollars per provider. cost=None means tokens are known but no price is configured."""
    def __init__(self):
        self.lock, self.rows = threading.Lock(), {}
    def add(self, provider, tokens_in=0, tokens_out=0, cost=None, calls=1):
        with self.lock:
            r = self.rows.setdefault(provider, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "priced": True})
            r["calls"] += calls; r["tokens_in"] += tokens_in; r["tokens_out"] += tokens_out
            if cost is None:
                r["priced"] = False
            else:
                r["cost"] += cost
    def total(self):
        return sum(r["cost"] for r in self.rows.values())
    def line(self):
        parts = [f"{p} ${r['cost']:.4f}" + ("" if r["priced"] else "+?") + f" ({r['calls']} calls, {r['tokens_in']}in/{r['tokens_out']}out)"
                 for p, r in sorted(self.rows.items())]
        return "cost  " + "  ".join(parts) + f"  total ${self.total():.4f}" if parts else "cost  none"
    def snapshot(self):
        return {**{p: dict(r) for p, r in self.rows.items()}, "total": round(self.total(), 6)}

LEDGER = Ledger()

def sh(cmd, cwd, timeout=None, check=True, env=None):
    p = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    if check and p.returncode:
        raise RuntimeError(f"{cmd}\n{p.stdout}\n{p.stderr}")
    return p

class Checkout(tuple):
    """Where a checkout lives: its worktree root, its own git dir, and the common dir every worktree of the repository shares."""
    top = property(lambda s: s[0]); git_dir = property(lambda s: s[1]); common = property(lambda s: s[2])
    @property
    def main(self):
        """The main checkout a linked worktree belongs to: the common dir's parent. A submodule's common dir sits under its
        superproject's .git/modules, and a bare repository has no main checkout, so either is its own."""
        return self.common.parent if self.common.name == ".git" else self.top

_checkouts = {}

def checkout(path):
    """The Checkout `path` is in, None outside a git worktree. Asked once per process per path: a directory stays in the checkout
    it is in for as long as its git dir exists, so only a found one is kept, and a removed one is asked again."""
    here = Path(path).resolve()
    c = _checkouts.get(here)
    if c is None or not c.git_dir.is_dir():
        p = sh(["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"], here, check=False) \
            if here.is_dir() else None
        lines = p.stdout.splitlines() if p and p.returncode == 0 else []
        c = Checkout(Path(l).resolve() for l in lines) if len(lines) == 3 else None
        if c is None:
            _checkouts.pop(here, None); return None
        _checkouts[here] = c
    return c

def main_checkout(path):
    """The main checkout of the repository `path` is in; outside a git worktree, `path` itself."""
    c = checkout(path)
    return c.main if c else Path(path).resolve()

def worktree_branches(repo):
    """Every checkout of this repository (main plus linked worktrees), as [(path, branch)] in the order git lists them, main checkout first, branch empty when detached."""
    out = []
    for line in sh(["git", "worktree", "list", "--porcelain"], repo, check=False).stdout.splitlines():
        if line.startswith("worktree "):
            out.append((Path(line[len("worktree "):]), ""))
        elif line.startswith("branch "):
            out[-1] = (out[-1][0], line[len("branch refs/heads/"):])
    return out or [(Path(repo).resolve(), "")]

def worktrees(repo):
    """Every checkout of this repository (main plus linked worktrees), as paths."""
    return [wt for wt, _ in worktree_branches(repo)]

def repo_link(path):
    """The repo's Linear link, `.compose/linear.json` (SKILL.md step 0): one file in the main checkout that every worktree of
    the repo reads, else the checkout's own (one written in a linked worktree); when neither exists, the main checkout's path,
    where it belongs. Outside a git repo, the folder's own."""
    here, c = Path(path).resolve(), checkout(path)
    if c is None:
        return here / ".compose" / "linear.json"
    found = [d / ".compose" / "linear.json" for d in dict.fromkeys((c.main, c.top))]
    return next((f for f in found if f.exists()), found[0])

def project_config(proj):
    f = Path(proj) / "verify.json"
    return json.loads(f.read_text()) if f.exists() else {}

def project_branch(proj):
    """The project's branch: `branch` in its verify.json (`init --branch`), else `compose/<name>`."""
    return project_config(proj).get("branch") or f"compose/{Path(proj).name}"

def commit_convention(proj):
    """A repo's commit convention, declared once as `commit_convention` in verify.json: {"types": [...], "example": "feat: ..."}.
    verify.json is where the CLI already reads per-repo settings, and it needs no parsing of prose in AGENTS.md. None when undeclared."""
    conv = project_config(proj).get("commit_convention")
    return conv if isinstance(conv, dict) and conv.get("types") else None

COMMIT_TYPES = {"feat": ["feat"], "fix": ["fix", "feat"], "test": ["test"], "deps": ["chore", "build"], "docs": ["docs", "chore"]}  # kind -> allowed types, best first


def typed(message, kind, conv):
    """`message` with the convention's type prefix for what the commit is (feat, fix, test, deps, docs); plain when no convention
    is declared or it allows none of the kind's types."""
    t = next((t for t in COMMIT_TYPES[kind] if conv and t in conv["types"]), None)
    return f"{t}: {message}" if t else message

def commit_rule(conv):
    """The sentence a brief carries so whoever commits follows the repo's convention; empty when none is declared."""
    if not conv:
        return ""
    return (f"Start every commit message with a type prefix, one of {', '.join(conv['types'])} (for example `{conv.get('example') or conv['types'][0] + ': ...'}`): "
            "tests are test, dependencies and config are chore or build, docs are docs, the rest feat or fix. ")


def all_projects(repo):
    """{project name: (worktree path, project dir)} over every worktree. A project lives in its own worktree
    (`compose-<name>` on the project's branch, `compose/<name>` unless `init --branch` chose one), created by `init`; older projects may sit in the main checkout."""
    found = {}
    for wt, on_branch in worktree_branches(repo):
        root = wt / ".compose"
        if not root.exists():
            continue
        for p in sorted(root.iterdir()):
            if not (p.is_dir() and (p / "verify.json").exists()):
                continue
            # Once a project branch is merged, its folder shows up in other checkouts too; the project's own worktree
            # (checked out on its branch) wins, so clean and status keep pointing at the right place.
            if p.name not in found or on_branch == project_branch(p):
                found[p.name] = (wt, p)
    return found

def project_dir(a):
    """Resolve --project to its directory, and point a.repo at the worktree that holds it, so every git command runs there."""
    repo = Path(a.repo).resolve()
    projects = all_projects(repo)
    if a.project:
        if a.project not in projects:
            sys.exit(f"no project {a.project!r}; `compose init --project {a.project}` creates one. Known: {sorted(projects)}")
        wt, proj = projects[a.project]
    else:
        if len(projects) != 1:
            sys.exit(f"pass --project; found {sorted(projects)}")
        wt, proj = next(iter(projects.values()))
    a.repo = str(wt)
    return proj


def atomic_write(path, text, keep_secrets=False):
    """Write a state file so a reader sees the old file or the new one, never a half-written or empty one. Secrets are
    scrubbed unless the file is the one that holds them (keep_secrets)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text if keep_secrets else scrub(text))
    note_home_write(path, tmp)
    os.replace(tmp, path)


# ---------- secrets: never in the env of project code, never in a text the CLI sends to a model or keeps ----------
# apply_config exports keys for the CLI's own callers (pi, OpenCode, the text model, jev). A test that prints its env must not
# find them, and nothing it prints may carry them to a model or into results/.
SECRET_VARS = {"OPENROUTER_API_KEY", "TEXT_MODEL_API_KEY", "TYPESAFE_API_KEY", "OPENCODE_CONFIG_CONTENT"}  # apply_config adds the config env's *_API_KEY names
SECRET_VALUES = set()  # apply_config adds the workspace's keys and those in ~/.compose/.env
REDACTED = "<redacted>"

def clean_env(env=None):
    """`env` (the process env by default) without the secret vars, and with a scratch COMPOSE_CONFIG: for every process that
    runs project code, a model's tools included."""
    return {**{k: v for k, v in (os.environ if env is None else env).items() if k not in SECRET_VARS}, "COMPOSE_CONFIG": scratch_config()}

# ---------- the user's ~/.compose: project code gets its own, and a write to the user's is flagged ----------
# When the project is compose itself, the code under test is a compose that reads and writes its config, keys, and caches.
# HOME stays as it is: git's identity and the toolchains' caches (rustup, uv, npm, gh) live there and tests need them.
# Other compose sessions write the user's config and keys too (`compose workspace`, setup, a key moved), so each such write
# is journaled with who made it, and only a change no compose outside the watched command wrote is flagged.
SCRATCH_CONFIGS = []  # removed when the CLI exits
HOME_WRITES = "home-writes.jsonl"  # the journal: one line per write the CLI makes to the user's ~/.compose
HOME_WRITES_MAX = 64 * 1024  # bytes; past it the journal keeps its newer half
HOME_WRITES_SLACK = 10  # seconds a journal line may predate home_state: a write is journaled just before its file is replaced
HOME_HASH_MAX = 1 << 20  # bytes; a larger file is compared by size and mtime, not content
CLI_OWN = {"model-probe.json", "skill-seen", "tool-bugs", "tool-bugs.jsonl", "test-slots", "live", "linear-get", "history.jsonl", "started", "usage", "load.json", "load.lock", "shed.json", HOME_WRITES}  # what other compose processes write there at any time
CLI_CHECKOUT = Path(__file__).resolve().parents[1]  # left out of home_state where it is under ~/.compose: code, not config

SCRATCH_PREFIX = "compose-config-"

def scratch_config():
    """A COMPOSE_CONFIG in a fresh, empty directory of its own."""
    if not SCRATCH_CONFIGS:
        atexit.register(remove_scratch)
    SCRATCH_CONFIGS.append(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
    return str(Path(SCRATCH_CONFIGS[-1]) / "config.json")

def is_scratch(config):
    """Whether `config` (a COMPOSE_CONFIG path) is one scratch_config made: no workspace, no keys."""
    return bool(config) and Path(config).parent.name.startswith(SCRATCH_PREFIX)

def remove_scratch():
    """Remove every scratch config this process made: at exit, and on a SIGTERM or SIGHUP, which would skip exit otherwise."""
    while SCRATCH_CONFIGS:
        shutil.rmtree(SCRATCH_CONFIGS.pop(), ignore_errors=True)

def checkout_entries(home):
    """The entries of `home` (~/.compose) that hold a CLI checkout: `src`, where install.sh puts it, and this CLI's own."""
    home = home.resolve()
    own = CLI_CHECKOUT.relative_to(home).parts[:1] if home in CLI_CHECKOUT.parents else ()
    return {"src", *own}

def entry_state(p):
    """What home_state compares for one entry: a file's content hash, so rewriting the same bytes is no change, else size and mtime."""
    st = p.lstat()
    if p.is_file() and not p.is_symlink() and st.st_size <= HOME_HASH_MAX:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    return (st.st_size, st.st_mtime_ns)

def home_state():
    """When it was taken, and name -> entry_state of each entry of the user's ~/.compose: the CLI's own logs and caches and
    its checkout left out."""
    home, at, state = Path.home() / ".compose", time.time(), {}
    skip = CLI_OWN | checkout_entries(home) if home.is_dir() else set()
    for p in home.iterdir() if home.is_dir() else []:
        if p.name not in skip and not p.name.endswith(".tmp"):
            try:
                state[p.name] = entry_state(p)
            except FileNotFoundError:  # removed while it was read
                pass
    return {"at": at, "entries": state}

def note_home_write(path, tmp):
    """Journal the write about to replace `path` with the file `tmp`, when `path` is in the user's ~/.compose: its content's hash,
    the writer's pid and session, and whether the writer runs on a scratch config. Before the replace, so whoever sees the new
    content finds its line. A journal that cannot be written never fails the write: the change is then flagged, never hidden."""
    home = Path.home() / ".compose"
    path = Path(path)
    if path.name in CLI_OWN or not home.is_dir() or path.parent.resolve() != home.resolve():
        return
    line = json.dumps({"at": time.time(), "name": path.name, "sha": hashlib.sha256(Path(tmp).read_bytes()).hexdigest(), "pid": os.getpid(),
                       "sid": os.getsid(0), "scratch": is_scratch(os.environ.get("COMPOSE_CONFIG"))}) + "\n"
    try:
        with os.fdopen(os.open(home / HOME_WRITES, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600), "a+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # two sessions appending and trimming at once lose no line
            f.write(line.encode())
            f.flush()
            if f.tell() > HOME_WRITES_MAX:
                f.seek(0)
                data = f.read()
                f.truncate(0)
                f.write(data[data.index(b"\n", len(data) // 2) + 1:])
    except OSError:
        pass

def home_writes_outside(since, sid):
    """{(name, sha)} the journal holds from `since` on, written by a compose outside the command running in session `sid`:
    this process itself, or one on the user's own config in another session. A writer on a scratch config is code under
    test whatever its session (it wrote the user's by its own path), and one in `sid` is the command's."""
    try:
        lines = (Path.home() / ".compose" / HOME_WRITES).read_text().splitlines()
    except OSError:
        return set()
    out = set()
    for line in lines:
        try:
            w = json.loads(line)
            if w["at"] >= since - HOME_WRITES_SLACK and (w["pid"] == os.getpid() or not w["scratch"] and w["sid"] != sid):
                out.add((w["name"], w["sha"]))
        except (ValueError, KeyError, TypeError):  # a line cut by a trim that a reader raced
            continue
    return out

def flag_home(before, what, sid=None):
    """Say which entries of the user's ~/.compose changed since `before` (a home_state()) while `what` ran, if any, leaving out
    a file whose new content a compose outside `what` wrote (home_writes_outside). `sid` is the session `what` runs in, when it
    has one of its own."""
    after = home_state()["entries"]
    changed = [n for n in sorted(before["entries"].keys() | after.keys()) if before["entries"].get(n) != after.get(n)]
    outside = home_writes_outside(before["at"], sid) if changed else set()
    changed = [n for n in changed if (n, after.get(n)) not in outside]
    if changed:
        say(f"WARNING: ~/.compose changed while {what} ran: {', '.join(changed)}, and no compose outside it wrote that. Project code "
            "gets its own COMPOSE_CONFIG, so this one wrote the user's by its own path, unless another program did; check those files")

def scrub(text):
    """text with the value of every secret var in the process env, and every key apply_config collected, as <redacted>."""
    values = {os.environ.get(k) or "" for k in SECRET_VARS} | SECRET_VALUES
    for v in sorted((v for v in values if len(v) >= 8), key=len, reverse=True):  # a short value would redact ordinary text
        text = text.replace(v, REDACTED)
    return text

class Scrubbed:
    """A text stream that scrubs what it writes: the CLI's stdout and stderr, which a detached run writes to results/<cmd>.log."""
    def __init__(self, stream):
        self.stream = stream
    def write(self, text):
        return self.stream.write(scrub(text))
    def __getattr__(self, name):
        return getattr(self.stream, name)


def digests(root, files):
    return {f: hashlib.sha256((root / f).read_bytes()).hexdigest() if (root / f).exists() else None for f in files}


def figma_key(text):
    """The Figma file key in a figma.com URL or a bare key, else None."""
    m = re.search(r"figma\.com/(?:design|board|slides)/([A-Za-z0-9]+)(/branch/([A-Za-z0-9]+))?", text)
    if m:
        return m.group(3) or m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9]{10,40}", text) else None


def current_branch(repo):
    """The branch the user is on: the stack's trunk. A detached HEAD falls back to main."""
    return sh(["git", "branch", "--show-current"], repo).stdout.strip() or "main"


# ---------- verify ----------

def load_env(path):
    """KEY=value lines of an env file; a missing file is an empty env (keys may all come from the process env)."""
    env = {}
    path = Path(path).expanduser()
    for line in (path.read_text() if path.exists() else "").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def port_listening(port):
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def report_time(at):
    return time.mktime(time.strptime(at, "%Y-%m-%d %H:%M:%S"))

def session_id():
    """This session's identity: the herdr pane it runs in (herdr never reuses a pane id), else None outside herdr. The engineering
    manager stays in the pane it was started in, so a Claude session resumed there is the same one."""
    return os.environ.get("HERDR_PANE_ID") or None


# ---------- init / clean ----------

def project_after(proj):
    """The project this one is built on (`init --after`), or None. Its branch is the review base in verify.json."""
    cfg = project_config(proj)
    base = cfg.get("review_base", "")
    return cfg.get("after") or (base.removeprefix("compose/") if base.startswith("compose/") else None)


# ---------- showing paths ----------

def tilde(text):
    """text (a path or a line) with the home directory followed by / shown as ~/."""
    return str(text).replace(str(Path.home()) + "/", "~/")
