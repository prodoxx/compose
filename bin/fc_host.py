import os, shutil
from pathlib import Path
import fc_harness

# ---------- the terminal host ----------
# The fleet is visible and steerable inside a terminal host: herdr (the TUI, `herdr`), the Herdr app (herdr-core's `hc` runs
# the terminals, `herdr-app` draws the window), or Rex (`rex`). Each marks its terminals with its own variables; compose reads
# them once per call, so a test sets its own. Outside any host every command runs headless, as it does with COMPOSE_NO_HERDR.

HERDR, HC, REX = "herdr", "hc", "rex"
KINDS = (HERDR, HC, REX)
NAMES = {HERDR: "herdr", HC: "the Herdr app", REX: "Rex"}
SESSION_VARS = {HERDR: "HERDR_PANE_ID", HC: "HC_TERMINAL_ID", REX: "REX_BLOCK"}
def which(name):
    return shutil.which(name)

def rex_bin():
    """The rex CLI: in REX_BIN_DIR (Rex sets it in every terminal it opens), else on PATH. None when neither has it: the app's own copy
    lives in its bundle, so outside a Rex terminal `rex` is found only once the person put it on PATH."""
    d = os.environ.get("REX_BIN_DIR")
    if d and os.access(Path(d) / "rex", os.X_OK):
        return str(Path(d) / "rex")
    return which("rex")

def kind():
    """Which host this session's terminal belongs to, or None (headless). `COMPOSE_NO_HERDR` makes every host headless;
    `COMPOSE_HOST=<kind>` names one for a test or a session whose variables were lost. herdr is checked first: a herdr pane
    inside the Herdr app or Rex is still steered through herdr."""
    if os.environ.get("COMPOSE_NO_HERDR"):
        return None
    if forced := os.environ.get("COMPOSE_HOST"):
        return forced if forced in KINDS else None
    if os.environ.get("HERDR_ENV") == "1" and os.environ.get("HERDR_SOCKET_PATH"):
        return HERDR
    if os.environ.get("HC_TERMINAL_ID") and os.environ.get("HC_TERMINAL_SOCKET") and which("hc"):
        return HC
    if os.environ.get("REX_SESSION") and os.environ.get("REX_BLOCK") and rex_bin():
        return REX
    return None

def inside():
    return kind() is not None

def name(k=None):
    """The host's name for a message: 'herdr', 'the Herdr app', 'Rex'; 'a terminal host' when none."""
    return NAMES.get(k or kind(), "a terminal host")

def session_id():
    """This session's identity in its host: the herdr pane, the hc terminal, or the Rex block it runs in. None headless.
    Each host keeps that id for the terminal's life, so a Claude session resumed in it is the same session."""
    k = kind()
    return (os.environ.get(SESSION_VARS[k]) or None) if k else None

def installed():
    """The hosts this machine has, in the order bare `compose` offers them: the Herdr app, herdr, Rex."""
    out = []
    if which("hc") and which("herdr-app"):
        out.append(HC)
    if which("herdr"):
        out.append(HERDR)
    if rex_bin():
        out.append(REX)
    return out

# ---------- a project's space under the space it was started from ----------
# herdr groups a project's worktree space under the repo's space. The Herdr app and Rex have flat lists, so compose names the
# project's space `<parent> › <slug>` and puts it right after its parent and the parent's other projects, which reads as the
# same tree.

SPACE_SEP = " › "

def space_root(name):
    """The top of a space's tree: a project's space names its parent first (`dtt › recommender` -> `dtt`)."""
    return (name or "").split(SPACE_SEP)[0]

def space_name(parent, slug):
    """The name a project's space gets under `parent` (a space's name, or None: the plain slug)."""
    return f"{space_root(parent)}{SPACE_SEP}{slug}" if parent and space_root(parent) != slug else slug

def is_space_of(name, slug):
    """Whether a space named `name` is the project `slug`'s: the plain slug, or the slug under a parent."""
    return bool(name) and (name == slug or name.endswith(SPACE_SEP + slug))

def after_group(names, parent):
    """The index (0-based, in `names`) of the last space of `parent`'s tree: the parent or its last project; None when absent."""
    root = space_root(parent)
    hits = [i for i, n in enumerate(names) if n == root or (n or "").startswith(root + SPACE_SEP)]
    return hits[-1] if hits else None

SHELL_GUARD = (("DISABLE_AUTO_UPDATE", "true"), ("DISABLE_UPDATE_PROMPT", "true"))
# A host starts a teammate by typing its command into a new shell (herdr) or runs it under the login shell (hc, rex), so a
# prompt that shell asks first swallows the command. oh-my-zsh's update prompt is the known one; both variables are its
# old-style switches (an explicit `zstyle ':omz:update' mode` in the user's files outranks them: fc_herdr.shell_startup_check).

def pane_env():
    """The variables a new pane gets from the caller, as {name: value}: SHELL_GUARD, then the caller's COMPOSE_CONFIG and HOME (a scratch
    config stays scratch in the CLI, agent, or teammate that pane runs), each harness row's home variable when set (the pane's harness
    must use the home compose wrote hooks, trust and skills into, and its sign-in), and PATH (compose decides a harness is installed by
    its own PATH, so the pane must run with that PATH)."""
    out = dict(SHELL_GUARD)
    homes = [h for k in fc_harness.rows() if (h := fc_harness.HARNESSES[k].get("home_env")) and os.environ.get(h)]
    out.update({k: os.environ[k] for k in ("COMPOSE_CONFIG", "HOME", *homes, "PATH") if os.environ.get(k)})
    return out

def with_env(env, argv):
    """`argv` run with `env` set, for a host that passes no environment of its own to a new terminal (hc, rex): `env K=V ... argv`."""
    return ["env", *(f"{k}={v}" for k, v in env.items()), *argv] if env else list(argv)
