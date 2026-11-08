import json, os, subprocess, sys, time
from pathlib import Path
import fc_host

# ---------- the Herdr app: herdr-core (`hc`) runs the terminals, `herdr-app` draws them ----------
# A compose agent is a named hc terminal (`hc run --name <agent>`), so its name reaches it in every `hc` command, and its status
# is what the program reports over OSC 7501 (Claude Code, pi and Codex do). The window is a view: `herdr-app ls` says which
# space and tab each terminal sits in, and a terminal started with the labels `herdr.space` and `herdr.tab` lands there. When the
# window is not open, agents still run and are reachable through hc; they have no space or tab until it opens.
# Every call is best-effort, like fc_herdr's: a refusal returns None and the run goes on as the headless path would.

STATUS = {"idle": "idle", "working": "working", "done": "done", "blocked": "blocked", "error": "blocked"}  # hc's words to compose's; error needs a person too

def _json(p):
    """The JSON a command printed: hc prints one line, herdr-app a pretty document; an error is JSON on stderr."""
    text = (p.stdout or p.stderr or "{}").strip()
    for candidate in (text, text.splitlines()[-1] if text else "{}"):
        try:
            got = json.loads(candidate)
            return got if isinstance(got, dict) else {}
        except json.JSONDecodeError:
            continue
    return {}

def hc_err(*args, timeout=60, text=False):
    """One `hc` call: (result, None), or (None, error kind) when hc refused (`{error: {kind, message, hint}}`) or did not answer.
    `text` for the commands that print text (`read`). `--json` goes right after the subcommand: after `--` it would reach the program."""
    args = list(map(str, args))
    try:
        p = subprocess.run(["hc", args[0], *([] if text else ["--json"]), *args[1:]], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"hc {' '.join(map(str, args[:1]))}: {e}"
    if text:
        return (p.stdout, None) if p.returncode == 0 else (None, _json(p).get("error", {}).get("kind") or p.stderr.strip()[-200:] or "error")
    out = _json(p)
    if p.returncode or "error" in out:
        return None, (out.get("error") or {}).get("kind") or p.stderr.strip()[-200:] or "error"
    return out, None

def hc(*args, timeout=60):
    return hc_err(*args, timeout=timeout)[0]

def app(command, **args):
    """`herdr-app do <command> --key value ...`: its result, or None when the window is not open or refused."""
    argv = ["herdr-app", "do", command]
    for k, v in args.items():
        if v is not None:
            argv += [f"--{k}", v if isinstance(v, str) else json.dumps(v)]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = _json(p)
    return None if p.returncode or "error" in out else out

def app_ls():
    """The window's spaces, each with its tabs and their terminals; None when the window is not open."""
    try:
        p = subprocess.run(["herdr-app", "ls"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = _json(p)
    return out.get("spaces") if p.returncode == 0 and isinstance(out.get("spaces"), list) else None

def _terminals(spaces):
    """(space id, tab id, terminal) for every terminal the window shows."""
    for s in spaces or []:
        for t in s.get("tabs") or []:
            for term in t.get("terminals") or []:
                yield s.get("id"), t.get("id"), term

def agents():
    """Every named live terminal as a compose agent record: name, pane_id (the terminal), workspace_id and tab_id (from the window,
    when open), agent_status. None when hc did not answer."""
    listed = hc("ls")
    if listed is None:
        return None
    placed = {term.get("terminal"): (s, t) for s, t, term in _terminals(app_ls())}
    out = []
    for term in listed.get("terminals") or []:
        if not term.get("name"):
            continue
        s, t = placed.get(term.get("address"), (None, None))
        out.append({"name": term["name"], "pane_id": term.get("address"), "workspace_id": s, "tab_id": t, "agent_status": STATUS.get(term.get("status"))})
    return out

def agent(ref):
    """One terminal by name or address, as agents() shapes it, with its foreground pids and cwd; None when there is none."""
    got, err = hc_err("get", ref)
    if not got or got.get("state") != "live":
        return None
    labels, fg = got.get("labels") or {}, got.get("foreground") or {}
    return {"name": got.get("name"), "pane_id": got.get("address"), "workspace_id": labels.get("herdr.space"), "tab_id": labels.get("herdr.tab"),
            "agent_status": STATUS.get((got.get("status") or {}).get("state")), "pids": [fg["pid"]] if fg.get("pid") else [], "cwd": (fg.get("cwd") or got.get("cwd")),
            "labels": labels}

def prompt(name, text, wait=False, timeout=None):
    """`hc send`: (result, None) once the text is pasted and submitted (and, with `wait`, the turn it started ended), else (None, err):
    agent_blocked (a dialog is up: hc refuses to type over it), agent_not_found, finished, timeout, agent_prompt_stalled (hc saw no turn start)."""
    args = ["send", name, text] + (["--wait"] + (["--timeout", str(int(timeout))] if timeout else []) if wait else [])
    got, err = hc_err(*args, timeout=(timeout or 180) + 30 if wait else 60)
    if err:
        return None, {"blocked": "agent_blocked", "not_found": "agent_not_found", "finished": "finished"}.get(err, err)
    reason = ((got or {}).get("wait") or {}).get("reason") or (got or {}).get("reason")  # `send --wait` nests the wait's outcome
    if wait and reason == "timeout":
        return got, "timeout"
    if wait and reason == "not_started":
        return got, "agent_prompt_stalled"
    return got, None

def send_keys(name, *keys):
    return hc("keys", name, *keys) is not None

def read(ref, lines=12, source="recent-unwrapped", fmt="text"):
    """The terminal's screen (`visible`) or its last `lines` of history above it, as plain text; "" when it cannot be read."""
    out, _ = hc_err("read", ref, "--lines", "0" if source == "visible" else str(lines), text=True)
    return out or ""

def wait(name, timeout=None, until=None):
    """Wait for `name`'s turn to end (`hc wait`), or with `until` for that status. None when it did, else timeout."""
    if until:
        end = time.time() + (timeout or 180)
        while time.time() < end:
            if (agent(name) or {}).get("agent_status") == until:
                return None
            time.sleep(2)
        return "timeout"
    got, err = hc_err("wait", name, *(["--timeout", str(int(timeout))] if timeout else []), timeout=(timeout or 180) + 30)
    if err:
        return err
    return "timeout" if (got or {}).get("reason") == "timeout" else None

# ---------- the window: spaces and tabs ----------

def find_space(slug, wt):
    """The open space named `slug`, else the one whose folder is the project worktree; None when the window shows neither or is closed."""
    spaces = app_ls()
    for s in spaces or []:
        if fc_host.is_space_of(s.get("name"), slug):
            return s["id"]
    for s in spaces or []:
        if s.get("folder") and Path(s["folder"]).resolve() == Path(wt).resolve():
            return s["id"]
    return None

def own_space(spaces):
    """The space this session's terminal sits in, as the window lists it; None when it shows it nowhere."""
    me = os.environ.get("HC_TERMINAL_ID")
    return next((s for s in spaces or [] for t in s.get("tabs") or [] for term in t.get("terminals") or [] if me and term.get("terminal") == me), None)

def space(slug, wt):
    """The project's space, made when the window shows none: its terminals in the project worktree, its first tab `shell`. The window
    lists spaces flat, so it is named `<parent> › <slug>` after the space this session runs in and moved right after that space's
    tree (fc_host.space_name), as herdr groups a worktree space under its repo's. None when the window is not open: agents then run
    in hc without a space until it is."""
    if found := find_space(slug, wt):
        return found
    parent = own_space(app_ls())
    made = app("space.new", name=fc_host.space_name((parent or {}).get("name"), slug), folder=str(wt))
    if not made or not made.get("space"):
        return None
    if made.get("tab"):
        app("tab.rename", id=made["tab"], name="shell")
    if parent:
        others = [s for s in app_ls() or [] if s.get("id") != made["space"]]
        if (at := fc_host.after_group([s.get("name") for s in others], parent.get("name"))) is not None:
            app("space.move", id=made["space"], index=at + 2)  # 1-based, right after the tree's last space
    return made["space"]

def tabs(space):
    """{label: {"tab_id", "label"}} for the space's tabs; {} when the window is closed."""
    for s in app_ls() or []:
        if s.get("id") == space:
            return {t.get("name"): {"tab_id": t.get("id"), "label": t.get("name")} for t in s.get("tabs") or []}
    return {}

def tab_of(terminal):
    """(space id, tab id) of the tab the window shows `terminal` in; (None, None) when it shows it nowhere."""
    for s, t, term in _terminals(app_ls()):
        if term.get("terminal") == terminal:
            return s, t
    return None, None

def start(space, label, cwd, env, name, argv, join=False, title=None):
    """Start `argv` as the named terminal `name` in `cwd` with `env`, shown in the space's tab labelled `label`: joined to that tab when
    `join` and it exists (the implementers' tab), else in a tab of its own, named `label`. {"tab": id or None, "pane": the terminal}.
    No parent: compose's own process is no terminal of hc's, and a teammate must outlive the session that started it.
    Raises RuntimeError when hc refused."""
    labels = []
    if space:
        labels += ["--label", f"herdr.space={space}"]
        if join and (tab := tabs(space).get(label)):
            labels += ["--label", f"herdr.tab={tab['tab_id']}"]
    labels += ["--label", f"compose.tab={label}"] + (["--label", f"compose.title={title}"] if title else [])
    got, err = hc_err("run", "--bg", "--no-parent", "--no-notify", "--name", name, "--cwd", str(cwd), *labels, "--",
                      *fc_host.with_env({**fc_host.pane_env(), **env}, argv), timeout=90)
    if err or not (got or {}).get("address"):
        raise RuntimeError(f"hc run: {err or 'no terminal'}")
    pane = got["address"]
    tab = None
    if space:
        for _ in range(10):  # the window places it in a moment
            _, tab = tab_of(pane)
            if tab:
                break
            time.sleep(0.5)
        if tab and (not join or label not in tabs(space)):
            app("tab.rename", id=tab, name=label)
        if tab:
            hc("label", pane, f"herdr.tab={tab}")  # so `hc get` names its tab from now on, window open or not
    return {"tab": tab, "pane": pane}

def pane_label(pane, title=None, state=None, tokens=None, clear=None):
    """Display labels on the terminal (`hc get` shows them): compose.title, compose.state, and one compose.<token> per token."""
    kv = []
    if title is not None:
        kv.append(f"compose.title={title}")
    if state is not None:
        kv.append(f"compose.state={state}")
    for k, v in (tokens or {}).items():
        kv.append(f"compose.{k}={v}")
    for k in clear or ():
        kv.append(f"compose.{k}=")
    if kv:
        hc("label", pane, *kv)

def close_pane(pane):
    hc("kill", pane)
    hc("rm", pane)

def close_tab(tab):
    app("tab.close", id=tab, confirmed=True)

def tab_gone(tab):
    """True when the window is open and shows no such tab. A closed window cannot say, so nothing is gone then."""
    spaces = app_ls()
    return spaces is not None and not any(t == tab for _, t, _ in _terminals(spaces)) and not any(tt.get("id") == tab for s in spaces for tt in s.get("tabs") or [])

def close_space(space):
    app("space.close", id=space, confirmed=True)

def space_is(space, slug):
    return any(s.get("id") == space and fc_host.is_space_of(s.get("name"), slug) for s in app_ls() or [])

def focus(space=None, tab=None, pane=None):
    if pane:
        app("agent.focus", terminal=pane)
    elif tab:
        app("tab.select", id=tab)
    elif space:
        app("space.select", id=space)

def live(pane):
    """Whether the terminal still runs (the engineering manager's seat)."""
    return (hc("get", pane) or {}).get("state") == "live"

def pane_pids(pane):
    return (agent(pane) or {}).get("pids") or []

def manager_label(pane):
    """The compose.manager label on the terminal, "" when none."""
    return ((hc("get", pane) or {}).get("labels") or {}).get("compose.manager") or ""

def set_manager_label(pane, text):
    hc("label", pane, f"compose.manager={text or ''}")

def manager_panes():
    """{terminal: its compose.manager label} over every live terminal that has one."""
    out = {}
    for term in (hc("ls") or {}).get("terminals") or []:
        got = hc("get", term.get("address"))
        if label := ((got or {}).get("labels") or {}).get("compose.manager"):
            out[term["address"]] = label
    return out

def attach():
    """Bare `compose` on a terminal outside any host, with the Herdr app installed: open it and say what to do there."""
    if sys.platform == "darwin":
        subprocess.run(["open", "-a", "Herdr"], check=False, capture_output=True)
    print("open Herdr, run claude in a tab and type /compose", flush=True)
