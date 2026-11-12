import json, os, re, subprocess, sys, time
from pathlib import Path
import fc_core, fc_host

# ---------- Rex (superlogical.com/rex): a session per project, a window per tab, a block per pane ----------
# A compose agent is a terminal block whose label starts with its name (`<name> · <title> · <state>`): Rex names nothing itself,
# and every block id is unique across sessions, so a block id reaches its block in any command. Status is what the program
# reports over OSC 7501 (`block call program_status`), which Claude Code, pi and Codex do. Text is pasted with `rex send` and
# submitted with Enter, as a person would. Every call is best-effort, like fc_herdr's.

SEP = " · "
NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
STATUS = {"idle": "idle", "working": "working", "done": "done", "blocked": "blocked", "error": "blocked"}
TEAMMATE_RULE = (" You run in a tab of the project's Rex session, which compose names after the project and finds by that name: "
                 "never rename the Rex session, even when a hook or a guide asks you to name it after your task.")
PROMPT_START_WAIT = 5  # seconds a prompt gets to show as a turn (working or blocked) before it counts as stalled, as herdr's own gate

def _json(p):
    try:
        return json.loads(p.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}

PLAIN = {"send", "send-key", "kill", "focus", "do"}  # the commands that answer no JSON; `block`/`window` subcommands that act are plain too

HERE_VARS = ("REX_SESSION", "REX_BLOCK")

def cli_env():
    """The environment every `rex` call runs with: this terminal's own, without REX_SESSION and REX_BLOCK. Rex scopes `-b BLOCK` (and
    `capture`, `send`, `block rename|inspect|close`) to REX_SESSION when it is set, so from the manager's terminal a block in the
    project's session would be "not found"; without them a block id reaches its block in any session. REX_SERVER stays: it names
    the server this terminal belongs to. A command that needs a session names it with `-s`."""
    return {k: v for k, v in os.environ.items() if k not in HERE_VARS}

def rex_err(*args, timeout=60, text=False):
    """One `rex` call: (result, None), or (None, error) when Rex refused (exit 3: the target is not there) or did not answer.
    The commands that list or inspect answer JSON; `text` for `capture`; the ones that act answer nothing (result {})."""
    exe = fc_host.rex_bin()
    if not exe:
        return None, "rex is not installed"
    args = list(map(str, args))
    plain = text or args[0] in PLAIN or (args[0] in ("block", "window") and len(args) > 1 and args[1] in ("rename", "close", "focus")) \
        or args[:2] in (["session", "move"], ["session", "rename"])
    raw = args[:2] == ["block", "call"]  # answers JSON by itself and takes no --json
    cut = args.index("--") if "--" in args else len(args)  # --json goes before the program's own arguments
    argv = args if plain or raw else [*args[:cut], "--json", *args[cut:]]
    try:
        p = subprocess.run([exe, *argv], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=cli_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"rex {' '.join(args[:1])}: {e}"
    if p.returncode:
        return None, "not_found" if p.returncode == 3 else p.stderr.strip()[-200:] or "error"
    return (p.stdout if text else {} if plain else _json(p)), None

def rex(*args, timeout=60):
    return rex_err(*args, timeout=timeout)[0]

def sessions():
    return (rex("ls") or {}).get("sessions") or []

def blocks(session):
    """The session's terminal blocks: block_id, label, window_id."""
    return [b for b in (rex("block", "ls", "-s", session) or {}).get("blocks") or [] if (b.get("flavor") or "").startswith("com.superlogical.terminal")]

def name_of(label):
    """The agent name a block label carries (`<name> · ...`, as compose labels every block it starts), else None: a plain `shell`
    or a label the person gave carries no separator."""
    head = (label or "").split(SEP)[0]
    return head if SEP in (label or "") and NAME.fullmatch(head) else None

def status(block):
    """The program's reported status in `block`, in compose's words; None when nothing reported."""
    got = rex("block", "call", "-b", block, "program_status")
    recs = (got or {}).get("records") or []
    root = next((r for r in recs if not r.get("id")), recs[0] if recs else {})
    return STATUS.get(root.get("state"))

def agents():
    """Every labelled block as a compose agent record, over every session: name, pane_id (the block), workspace_id (the session),
    tab_id (the window), agent_status. None when Rex did not answer."""
    listed = rex("ls")
    if listed is None:
        return None
    out = []
    for s in listed.get("sessions") or []:
        for b in blocks(s["session_id"]):
            if name := name_of(b.get("label")):
                out.append({"name": name, "pane_id": b["block_id"], "workspace_id": s["session_id"], "tab_id": b.get("window_id"), "agent_status": status(b["block_id"])})
    return out

def find(ref):
    """The block of agent `ref` (a name, else a block id): (session, block record) or (None, None)."""
    for s in sessions():
        for b in blocks(s["session_id"]):
            if b["block_id"] == ref or name_of(b.get("label")) == ref:
                return s["session_id"], b
    return None, None

def agent(ref):
    s, b = find(ref)
    if not b:
        return None
    proc = rex("block", "call", "-b", b["block_id"], "process") or {}
    fg = proc.get("foreground") or proc.get("child") or {}
    return {"name": name_of(b.get("label")), "pane_id": b["block_id"], "workspace_id": s, "tab_id": b.get("window_id"), "agent_status": status(b["block_id"]),
            "pids": [fg["pid"]] if fg.get("pid") else [], "cwd": fg.get("cwd"), "label": b.get("label")}

def _block(ref):
    _, b = find(ref)
    return b["block_id"] if b else None

def prompt(name, text, wait=False, timeout=None):
    """Paste `text` into the agent's block and press Enter. (result, None) once submitted (and, with `wait`, once the turn it started
    ended), else (None, err): agent_blocked (a dialog is up), agent_not_found, agent_prompt_stalled (no turn started within
    PROMPT_START_WAIT), timeout."""
    block = _block(name)
    if not block:
        return None, "agent_not_found"
    if status(block) == "blocked":
        return None, "agent_blocked"
    if rex_err("send", "-b", block, text)[1] or rex_err("send-key", "-b", block, "enter")[1]:
        return None, "send_failed"
    if not wait:
        return {"block": block}, None
    end, seen = time.time() + (timeout or 180), False
    for _ in range(PROMPT_START_WAIT * 2):
        if status(block) in ("working", "blocked"):
            seen = True
            break
        time.sleep(0.5)
    if not seen:
        return {"block": block}, "agent_prompt_stalled"
    return {"block": block}, wait_block(block, end)

def wait_block(block, end):
    """None once the block's program is idle, done or blocked, else "timeout" at `end`."""
    while time.time() < end:
        if status(block) in ("idle", "done", "blocked"):
            return None
        time.sleep(2)
    return "timeout"

def send_keys(name, *keys):
    block = _block(name)
    return bool(block) and rex_err("send-key", "-b", block, *keys)[1] is None

def read(ref, lines=12, source="recent-unwrapped", fmt="text"):
    """The block's last `lines` lines (its screen and scrollback), plain text; "" when it cannot be read."""
    block = _block(ref)
    if not block:
        return ""
    out, _ = rex_err("capture", "-b", block, "--trim", *(["--unwrap"] if source != "visible" else []), text=True)
    rows = (out or "").rstrip("\n").split("\n")
    return "\n".join(rows[-(40 if source == "visible" else lines):])

def wait(name, timeout=None, until=None):
    block = _block(name)
    if not block:
        return "agent_not_found"
    end = time.time() + (timeout or 180)
    if until:
        while time.time() < end:
            if status(block) == until:
                return None
            time.sleep(2)
        return "timeout"
    return wait_block(block, end)

# ---------- sessions and windows ----------

def space_file(slug, wt):
    return Path(wt) / ".compose" / slug / "results" / "rex.json"

def remember(slug, wt, session, label):
    f = space_file(slug, wt)
    if not f.parent.parent.is_dir():  # no project there (yet): nothing to keep it with
        return
    try:
        f.parent.mkdir(exist_ok=True)
        fc_core.atomic_write(f, json.dumps({"session": session, "label": label}))
    except OSError:
        pass

def find_space(slug, wt):
    """The project's session: the one compose made for it (its id kept in results/rex.json), else one labelled for the project.
    A Claude Code session started in a Rex terminal is told by Rex's SessionStart hook to name its session after its task, so a
    teammate renames the project's session (`shop-architect`); compose finds it by its id and names it back."""
    listed = sessions()
    try:
        kept = json.loads(space_file(slug, wt).read_text())
    except (OSError, ValueError):
        kept = {}
    if s := next((s for s in listed if kept.get("session") and s["session_id"] == kept["session"]), None):
        if not fc_host.is_space_of(s.get("label"), slug) and kept.get("label"):
            rex("session", "rename", s["session_id"], kept["label"])
        return s["session_id"]
    if s := next((s for s in listed if fc_host.is_space_of(s.get("label"), slug)), None):
        remember(slug, wt, s["session_id"], s["label"])
        return s["session_id"]
    return None

def space(slug, wt):
    """The project's session, made when none is the project's: its terminals in the project worktree, its first window `shell`. Rex lists
    sessions flat, so it is labelled `<parent> › <slug>` after the session this one runs in (REX_SESSION) and moved right after that
    session's tree (fc_host.space_name), as herdr groups a worktree space under its repo's."""
    if found := find_space(slug, wt):
        return found
    listed, me = sessions(), os.environ.get("REX_SESSION")
    parent = next((s for s in listed if me and me in (s.get("session_id"), s.get("label"))), None)
    label = fc_host.space_name((parent or {}).get("label"), slug)
    made = rex("new", label, "-c", str(wt), "--window", "shell", "--", *shell_tab())
    if new := (made or {}).get("session_id"):
        remember(slug, wt, new, label)
    if new and parent and (at := fc_host.after_group([s.get("label") for s in listed], parent.get("label"))) is not None:
        rex("session", "move", new, "--after", listed[at]["session_id"])
    return new

def shell_tab():
    """The program of the project's `shell` tab: the person's login shell with fc_host.SHELL_GUARD, so it opens at a prompt and not at
    oh-my-zsh's update question (a plain `rex new` starts the shell with the server's environment, which has no guard)."""
    return fc_host.with_env(dict(fc_host.SHELL_GUARD), [os.environ.get("SHELL") or "/bin/sh", "-l"])

def tabs(space):
    return {w.get("label"): {"tab_id": w.get("window_id"), "label": w.get("label")} for w in (rex("window", "ls", "-s", space) or {}).get("windows") or []}

def start(space, label, cwd, env, name, argv, join=False, title=None):
    """Start `argv` in `cwd` with `env` as a block labelled `<name> · <title>`: split into the window labelled `label` when `join` and it
    exists (the implementers'), else in a new window named `label`. {"tab": window id, "pane": block id}. Raises RuntimeError when Rex refused."""
    if not space:
        raise RuntimeError("rex: no session for the project")
    shown = SEP.join((name, title or label))
    cmd = ["--focus=false", "-c", str(cwd), "--", *fc_host.with_env({**fc_host.pane_env(), **env}, argv)]
    tab = tabs(space).get(label, {}).get("tab_id") if join else None
    if tab:
        anchors = [b["block_id"] for b in blocks(space) if b.get("window_id") == tab]
        direction = "right" if len(anchors) % 2 else "below"
        got, err = rex_err("split", "-s", space, "-b", anchors[-1], "--split", direction, "--label", shown, *cmd, timeout=90)
    else:
        got, err = rex_err("window", "new", "-s", space, label, *cmd, timeout=90)
    ids = (got or {}).get("block_ids") or []
    if err or not ids:
        raise RuntimeError(f"rex {'split' if tab else 'window new'}: {err or 'no block'}")
    if not tab:
        tab = got.get("window_id")
        rex("block", "rename", ids[0], shown)
    return {"tab": tab, "pane": ids[0]}

def pane_label(pane, title=None, state=None, tokens=None, clear=None):
    """The block's label: `<name> · <title> · <state> · <tokens>`, each part kept from the last label when not given now."""
    _, b = find(pane)
    if not b:
        return
    parts = (b.get("label") or "").split(SEP)
    name = parts[0] if parts and NAME.fullmatch(parts[0]) else None
    old = {"title": parts[1] if len(parts) > 1 else "", "state": parts[2] if len(parts) > 2 else "", "tokens": SEP.join(parts[3:])}
    if title and name and title.startswith(name + ": "):  # crew start titles a pane `<name>: <title>` for herdr; the label carries the name already
        title = title[len(name) + 2:]
    new = {"title": old["title"] if title is None else title, "state": old["state"] if state is None else state,
           "tokens": old["tokens"] if tokens is None else " ".join(f"{k}={v}" for k, v in tokens.items())}
    rex("block", "rename", pane, SEP.join(x for x in (name, new["title"], new["state"], new["tokens"]) if x))

def close_pane(pane):
    rex("block", "close", pane)

def close_tab(tab):
    rex("window", "close", tab)

def tab_gone(tab):
    return rex_err("window", "inspect", tab)[1] == "not_found"

def close_space(space):
    rex("kill", space)

def space_is(space, slug):
    return fc_host.is_space_of((rex("session", "inspect", space) or {}).get("label"), slug)

def focus(space=None, tab=None, pane=None):
    """Focus the tab and pane in the session, and ask Rex.app to show the session. None when done; else a line saying the app did
    not switch (it takes client actions only with Remote Control on, a setting that is the person's to change)."""
    if tab:
        rex("focus", *(["-s", space] if space else []), tab)
    if pane:
        rex("block", "focus", pane)
    if space:
        _, err = rex_err("do", "session.select", f"session_id={space}")  # the app shows it, when one client is connected and lets the CLI drive it
        if err:
            shown = (rex("session", "inspect", space) or {}).get("label") or space
            return f"Rex.app did not switch to it ({err.removeprefix('Error: ')}); pick the session '{shown}' in its sidebar"
    return None

def live(pane):
    return rex_err("block", "inspect", pane)[1] is None

def pane_pids(pane):
    return (agent(pane) or {}).get("pids") or []

MANAGER = "manager"

def manager_label(pane):
    """The block's label when it is compose's manager label, "" when the block has none or a label of its own."""
    _, b = find(pane)
    label = (b or {}).get("label") or ""
    return label if label == MANAGER or label.startswith(MANAGER + ": ") else ""

def set_manager_label(pane, text):
    rex("block", "rename", pane, text or "shell")

def manager_panes():
    out = {}
    for s in sessions():
        for b in blocks(s["session_id"]):
            if (label := b.get("label") or "") == MANAGER or label.startswith(MANAGER + ": "):
                out[b["block_id"]] = label
    return out

def attach():
    """Bare `compose` on a terminal outside any host, with Rex installed: open it and say what to do there."""
    if sys.platform == "darwin":
        subprocess.run(["open", "-a", "Rex"], check=False, capture_output=True)
    print("open Rex, run claude in a tab and type /compose", flush=True)
