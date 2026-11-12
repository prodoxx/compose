import json, os, re, shlex, shutil, signal, subprocess, sys, threading, time
from pathlib import Path
import fc_core, fc_harness, fc_proc, fc_compactlog, fc_models, fc_host, fc_hc, fc_rex, fc_herdr

# ---------- the fleet, whichever terminal host shows it ----------
# Every command that starts, steers, reads or closes an agent goes through here. Inside herdr (fc_herdr) the calls are the herdr CLI's;
# inside the Herdr app (fc_hc) they are hc's and herdr-app's; inside Rex (fc_rex) they are rex's. The records compose keeps are the
# same everywhere: an agent is {name, pane_id, workspace_id, tab_id, agent_status}, a teammate's crew record names its space, tab
# and pane in the host's ids, and a project's space is found again from the host, never guessed. Outside any host every call
# answers None or "" and the run goes on headless.

BACKENDS = {fc_host.HC: fc_hc, fc_host.REX: fc_rex}
SPACES = fc_herdr.HERDR_SPACES  # project name -> space id, filled by space()
IMPLEMENTERS_TAB = fc_herdr.IMPLEMENTERS_TAB
agent_name, agent_id, agent_ref, project_worktree, tab_label, space_record = \
    fc_herdr.agent_name, fc_herdr.agent_id, fc_herdr.agent_ref, fc_herdr.project_worktree, fc_herdr.tab_label, fc_herdr.space_record

class StartFailed(RuntimeError):
    """A teammate did not start; `tab` and `pane` name what the host made for it (None when nothing), for the caller to close."""
    def __init__(self, why, tab=None, pane=None):
        super().__init__(why)
        self.tab, self.pane = tab, pane

def kind():
    """The host this session runs in: herdr when fc_herdr says so (a test stands in for it there), else fc_host's answer."""
    if fc_herdr.herdr_env():
        return fc_host.HERDR
    k = fc_host.kind()
    return None if k == fc_host.HERDR else k

def inside():
    """Whether the fleet is visible: this session runs in a terminal host."""
    return kind() is not None

def host_name():
    return fc_host.name(kind())

def pane_word():
    """How a status line names a pane: `herdr pane`, `the Herdr app pane`, `Rex pane`; `pane` outside any host."""
    return f"{host_name()} pane" if kind() else "pane"

def session_var():
    """The variable that carries a session's id in this host (HERDR_PANE_ID outside any), for a command run as another session."""
    return fc_host.SESSION_VARS.get(kind(), "HERDR_PANE_ID")

def backend():
    return BACKENDS.get(kind())

def teammate_rule():
    """A line the host adds to every teammate's system prompt ("" when it adds none)."""
    return getattr(backend(), "TEAMMATE_RULE", "")

# ---------- agents ----------

def agents():
    """Every live agent the host lists, or None when it did not answer (a host that cannot say must not count anyone as gone)."""
    if (b := backend()):
        return b.agents()
    listed = fc_herdr.herdr("agent", "list")
    return None if listed is None else listed.get("agents", [])

def agent_list():
    """agents() in the shape herdr answers `agent list` with, {"agents": [...]}, or None when the host did not answer."""
    listed = agents()
    return None if listed is None else {"agents": listed}

def live_agents():
    """agents(), [] when the host did not answer."""
    if (b := backend()):
        return b.agents() or []
    return fc_herdr.live_agents()

def agent(ref):
    """One agent by name (or pane): its record, with `agent_session` (herdr: the pi session file) when the host knows one. None when there is none."""
    if (b := backend()):
        return b.agent(ref)
    got = fc_herdr.herdr("agent", "get", ref)
    return got["agent"] if isinstance(got, dict) and isinstance(got.get("agent"), dict) else None

def agent_status(ref):
    return (agent(ref) or {}).get("agent_status")

def prompt(name, text, wait=False, timeout=None, check=False):
    """Type `text` into the agent as its next prompt. (result, None) once submitted, and with `wait` once the turn it started ended;
    else (None, err): agent_blocked (a dialog is up: the host refuses to type over it), agent_not_found, agent_prompt_stalled (no turn
    started), timeout (`timeout` seconds passed with `wait`). `check` raises RuntimeError naming the error instead."""
    if (b := backend()):
        result, err = b.prompt(name, text, wait=wait, timeout=timeout)
        if not err and (pane := (b.agent(name) or {}).get("pane_id")):
            try:
                fc_compactlog.typed(pane, text, agent=name)  # the compactor must not take a typed prompt for the product owner's
            except OSError:
                pass
    elif wait:
        result, err = fc_herdr.herdr_error("agent", "prompt", name, text, "--wait", "--timeout", str(int((timeout or 180) * 1000)), timeout=(timeout or 180) + 30)
    elif check:
        try:
            result, err = fc_herdr.herdr("agent", "prompt", name, text, check=True), None
        except RuntimeError as e:  # `herdr agent prompt: <code>`
            result, err = None, str(e).rsplit(": ", 1)[-1] or "error"
    else:
        result = fc_herdr.herdr("agent", "prompt", name, text)
        err = None if result is not None else "refused"
    if err and check:
        raise RuntimeError(f"agent prompt {name}: {err}")
    return result, err

def send_keys(name, *keys):
    if (b := backend()):
        return b.send_keys(name, *keys)
    return fc_herdr.herdr("agent", "send-keys", name, *keys) is not None

def read(ref, lines=12, source="recent-unwrapped", fmt="text"):
    """What the agent's pane shows: its screen (`visible`) or its last `lines` lines of output. "" when it cannot be read.
    `fmt` ansi keeps styling where the host can (herdr); the others answer plain text."""
    if (b := backend()):
        return b.read(ref, lines=lines, source=source, fmt=fmt)
    return fc_herdr.herdr_text("agent", "read", ref, "--source", source, *(["--format", "ansi"] if fmt == "ansi" else []), "--lines", str(lines))

def wait(name, timeout=None, until=None):
    """Wait for the agent's turn to end (or for status `until`): None when it did, else "timeout" or the host's error."""
    if (b := backend()):
        return b.wait(name, timeout=timeout, until=until)
    return fc_herdr.herdr_error("agent", "wait", name, "--timeout", str(int((timeout or 180) * 1000)), *(["--until", until] if until else []), timeout=(timeout or 180) + 30)[1]

# ---------- spaces, tabs, panes ----------

def space(proj):
    """The project's space, made when the host shows none (herdr: a worktree space grouped under the repo's; the Herdr app: a space named for
    the project; Rex: a session), remembered for the process. None outside a host."""
    if (b := backend()):
        slug = Path(proj).name
        if slug not in SPACES:
            if found := b.space(slug, project_worktree(proj)):
                SPACES[slug] = found
        return SPACES.get(slug)
    return fc_herdr.herdr_space(proj)

def project_space(proj):
    """The project's space when the host shows one; never makes one."""
    if (b := backend()):
        return b.find_space(Path(proj).name, project_worktree(proj))
    return fc_herdr.herdr_project_space(proj)

def tabs(space):
    """{label: {"tab_id", "label"}} of the space's tabs."""
    if (b := backend()):
        return b.tabs(space) if space else {}
    return {k: {"tab_id": t.get("tab_id"), "label": k} for k, t in fc_herdr.herdr_tabs(space).items()} if space else {}

def project_tabs(proj, space):
    """The project's tabs in its space: in a herdr space it shares, only those labelled with its name."""
    if backend():
        return list(tabs(space).values())
    return fc_herdr.herdr_project_tabs(proj, space) if space else []

def start_teammate(proj, space, label, cwd, env, agent, kind_, program, argv):
    """A teammate's session: a tab labelled `label` in the project's space, running `program argv` (herdr: `agent start --kind kind_`
    in the tab's shell) as the agent named `agent`, with `env` in its environment. {"tab", "pane"}; raises StartFailed naming what was made."""
    if (b := backend()):
        try:
            made = b.start(space, label, cwd, env, agent, [program, *argv], title=label)
        except RuntimeError as e:
            raise StartFailed(str(e)) from e
        b.wait(agent, timeout=60)  # until its first idle: the brief goes in only once the session can take it
        return made
    tab = fc_herdr.herdr_tab(space, tab_label(proj, label), cwd, *[x for k, v in env.items() for x in ("--env", f"{k}={v}")], check=True)
    pane = tab["root_pane"]["pane_id"]
    try:
        fc_herdr.herdr_start_crew(agent, "--kind", kind_, "--pane", pane, "--timeout", "60000", "--", *argv)
    except RuntimeError as e:
        raise StartFailed(str(e), tab=tab["tab"]["tab_id"], pane=pane) from e
    return {"tab": tab["tab"]["tab_id"], "pane": pane}

PLACING = threading.Lock()  # `run` starts its tasks on threads: the first makes the implementers tab, the rest split it, never one tab each

def start_implementer(proj, space, cwd, env, name, title, argv, model):
    """One task's interactive pi as the agent `name` in a pane of the project's `implementers` tab, labelled `title`, with `env`.
    A previous run's agent of that name is closed first. {"pane", "tab", "sfile"}: the pi session file when the host reports it."""
    if old := agent(name):
        close_pane(old.get("pane_id"))
    if (b := backend()):
        with PLACING:
            made = b.start(space, IMPLEMENTERS_TAB, cwd, env, name, ["pi", *argv], join=True, title=title)
        b.wait(name, timeout=60)
        b.pane_label(made["pane"], tokens={"model": model.split("/")[-1].split("@")[0]})
        return {**made, "sfile": None}
    with PLACING:
        pane = fc_herdr.herdr_implementer_pane(space, cwd, env, tab_label(proj, IMPLEMENTERS_TAB))
    fc_herdr.herdr("pane", "rename", pane, title)
    started = fc_herdr.herdr_agent_start(name, "--kind", "pi", "--pane", pane, "--timeout", "60000", "--", *argv)
    fc_herdr.herdr("pane", "report-metadata", pane, "--source", "compose", "--title", title, "--token", f"model={model.split('/')[-1].split('@')[0]}")
    sfile = (((started or {}).get("agent") or {}).get("agent_session") or {}).get("value")
    return {"pane": pane, "tab": None, "sfile": sfile}

def session_file(session, argv=()):
    """The pi session file of an implementer's session: what the host reported, else the file pi named after the `--session-id` in `argv`
    inside its `--session-dir`, once it exists."""
    if session.get("sfile"):
        return session["sfile"]
    if not backend():
        session["sfile"] = ((agent(session["name"]) or {}).get("agent_session") or {}).get("value")
    elif "--session-id" in argv and "--session-dir" in argv:
        sid, sdir = argv[list(argv).index("--session-id") + 1], Path(argv[list(argv).index("--session-dir") + 1])
        session["sfile"] = next((str(p) for p in sorted(sdir.glob(f"*{sid}*.jsonl"))), None)
    return session.get("sfile")

def pane_label(pane, title=None, state_labels=None, tokens=None, clear_tokens=(), source="compose"):
    """Display-only metadata on a pane: its title, a state label per agent state ({"working": "attempt 2/3"}), and tokens ({"cost": "$0.01"})."""
    if not pane:
        return
    if (b := backend()):
        b.pane_label(pane, title=title, state=next(iter((state_labels or {}).values()), None), tokens=tokens, clear=clear_tokens)
        return
    args = ["pane", "report-metadata", pane, "--source", source]
    if title is not None:
        args += ["--title", title]
    for k, v in (state_labels or {}).items():
        args += ["--state-label", f"{k}={v}"]
    for k, v in (tokens or {}).items():
        args += ["--token", f"{k}={v}"]
    for k in clear_tokens:
        args += ["--clear-token", k]
    fc_herdr.herdr(*args)

def label(session, text, cost, state="working"):
    """The state label the product owner reads next to an implementer, and its running cost."""
    if session and session.get("pane"):
        pane_label(session["pane"], state_labels={state: text, "done": text}, tokens={"cost": f"${cost:.4f}"})

stage = fc_herdr.herdr_stage  # a verify stage's state: told to the observers (the hold file) and drawn on its pane where the host has one

def verify_panes(proj, space, wt, names, label_="verify"):
    """The project's `verify` tab, rebuilt per run. In herdr one pane per stage, each reported through `stage`; in the other hosts one
    pane following the run (`compose stages`), the stages read from the hold file. {stage: pane} for the panes `stage` draws on."""
    if (b := backend()):
        if not space or not names:
            return {}
        close_tabs(space, label_)
        try:
            b.start(space, label_, wt, {}, agent_name(Path(proj).name, "verify"), ["compose", "stages", "--repo", str(wt), "--project", Path(proj).name], title=label_)
        except RuntimeError:
            pass
        return {}
    return fc_herdr.herdr_verify_panes(space, wt, names, label_)

def close_pane(pane):
    if not pane:
        return
    if (b := backend()):
        b.close_pane(pane)
    else:
        fc_herdr.herdr("pane", "close", pane)

def close_tab(tab):
    if not tab:
        return
    if (b := backend()):
        b.close_tab(tab)
    else:
        fc_herdr.herdr("tab", "close", tab)

def tab_gone(tab):
    """Whether the host says the tab is no longer there. A host that cannot say leaves it open."""
    if (b := backend()):
        return bool(tab) and b.tab_gone(tab)
    return fc_herdr.herdr_error("tab", "get", tab or "", timeout=10)[1] == "tab_not_found"

def close_tabs(space, label_):
    """Closes the tabs `label_` names: its own and the older `<label> HH:MM` ones."""
    if (b := backend()):
        for old, t in tabs(space).items():
            if re.fullmatch(re.escape(label_) + r"( \d\d:\d\d)?", old or ""):
                b.close_tab(t["tab_id"])
    else:
        fc_herdr.herdr_close_tabs(space, label_)

def close_task(proj, tid):
    """Closes the pane of a task's pi agent, whichever session the task ended on: only the agent's name still reaches the pane an earlier attempt left."""
    if got := agent(agent_name(Path(proj).name, tid)):
        close_pane(got.get("pane_id"))

def close_space(space):
    if (b := backend()):
        b.close_space(space)
    else:
        fc_herdr.herdr("workspace", "close", space)

def space_is(space, name):
    """Only the project's own space is ever closed by its clean: a checkout path can be a pool slot another project now uses."""
    if (b := backend()):
        return b.space_is(space, name)
    return fc_herdr.herdr_space_is(space, name)

def focus(space=None, tab=None, pane=None):
    """Take the person to the space, tab and pane. None when done, else a line saying what the host did not show."""
    if (b := backend()):
        return b.focus(space=space, tab=tab, pane=pane)
    if space:
        fc_herdr.herdr("workspace", "focus", space)
    if tab:
        fc_herdr.herdr("tab", "focus", tab)

# ---------- the pane's processes ----------

def pane_processes(pane):
    """The processes running in `pane` in front of its shell, each {"pid"}: empty when the pane holds nothing but an idle shell."""
    if (b := backend()):
        return [{"pid": p} for p in b.pane_pids(pane)]
    return fc_herdr.pane_processes(pane)

def agent_pids(pane):
    if backend():
        return sorted(p["pid"] for p in pane_processes(pane) if fc_proc.nice_floor(p["pid"]) is not None)
    return fc_herdr.agent_pids(pane)

def pane_suspended(pane):
    """True when a process in `pane` is stopped (Ctrl-Z, or SIGSTOP after a sleep): the host keeps listing such an agent with its last status."""
    for p in pane_processes(pane):
        try:
            stat = subprocess.run(["ps", "-o", "stat=", "-p", str(p["pid"])], capture_output=True, text=True, timeout=5, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired, KeyError):
            continue
        if stat.startswith("T"):
            return True
    return False

def pane_launching(name, pane):
    """True when `name` is registered, listed on `pane`, or the pane runs a process that is still becoming it."""
    if not backend():
        return fc_herdr.pane_launching(name, pane)
    return bool(agent(name)) or any(ag.get("pane_id") == pane for ag in live_agents()) or bool(pane_processes(pane))

def pane_kill(pane):
    """Stop whatever runs in `pane` in front of its shell, so closing it leaves nothing unregistered behind."""
    for p in pane_processes(pane):
        try:
            os.kill(p["pid"], signal.SIGTERM)
        except (OSError, KeyError):
            pass

# ---------- a session's first moments ----------

def ready(name, timeout=60):
    """Return once a Claude session's input box (its `❯` line) is on screen, or it is already on a turn, blocked on a dialog, or done.
    A host reports a Claude session ready seconds before its UI exists: text typed then is kept in the box unsent, or dropped."""
    for _ in range(max(1, timeout // 2)):
        got = agent(name) or {}
        if not got or got.get("agent_status") in ("working", "blocked", "done"):
            return
        if any(ln.lstrip().startswith("❯") for ln in read(name, lines=40, source="visible").splitlines()):
            return
        time.sleep(2)

def dialog(name):
    """The dialog a blocked Claude session shows (its title, text, and choices, on one line): everything below the last rule line,
    without the key hints. "unknown" when the pane cannot be read."""
    lines = read(name, lines=40, source="visible").splitlines()
    rules = [i for i, ln in enumerate(lines) if ln.strip() and set(ln.strip()) <= set("─━-")]
    body = [ln.strip().removeprefix("❯").strip() for ln in lines[rules[-1] + 1 if rules else 0:]]
    text = " / ".join(ln for ln in body if ln and not ln.startswith(("Enter to confirm", "Esc to")))
    return text[:400] or "unknown"

def login_screen(name):
    """The sign-in text on the agent's pane: the first `login_screens` text of any fc_harness row visible on it, else ""."""
    text = read(name, lines=40, source="visible")
    for key in fc_harness.rows():
        for screen in fc_harness.HARNESSES[key].get("login_screens", ()):
            if screen in text:
                return screen
    return ""

def await_start(name, first=None, tries=3):
    """Return once the agent has started on its brief, the first prompt it gets. This only confirms the submission landed (status
    working, blocked, or done); when it did not, presses Enter for a box that kept the text without submitting, then sends `first`
    again for a box that lost it, and fails loudly with the agent name when it still did not start. A pane on a sign-in screen
    gets neither: before each Enter and each re-send `login_screen` is asked, and one there raises naming its text."""
    def started():  # `done` counts too: a short first turn can finish before the check
        return agent_status(name) in ("working", "blocked", "done")
    def settles(secs):
        for _ in range(secs // 2):
            if started():
                return True
            time.sleep(2)
        return False
    for attempt in range(tries + 1):
        if attempt:
            if screen := login_screen(name):
                raise RuntimeError(f"{name} is on a sign-in screen ({screen}): sign in there, then run compose again")
            send_keys(name, "enter")
            if settles(6):
                return
            if first:
                if screen := login_screen(name):
                    raise RuntimeError(f"{name} is on a sign-in screen ({screen}): sign in there, then run compose again")
                prompt(name, first)
        if settles(20):
            return
    raise RuntimeError(f"{name} did not start on its brief after {tries} retries: look in its tab"
                       + (f", and send it again with: compose crew prompt --agent {name} {shlex.quote(first)}" if first else ""))

PROMPT_START_WAIT = fc_herdr.PROMPT_START_WAIT

def stalled(session, text, deadline, argv=()):
    """Follow a prompt the host answered with agent_prompt_stalled. The turn is the one the host sees working or blocked, waited for like
    any other, or the one the pi session file records whole while the host missed it. A prompt pi never wrote down goes once more
    into the same session after PROMPT_START_WAIT. Returns None when the turn ended, else "timeout", or "agent_prompt_stalled" when it
    never started."""
    name, sent, again = session["name"], time.time(), False
    while (left := deadline - time.time()) > 0:
        if agent_status(name) in ("working", "blocked"):
            return wait(name, timeout=left)
        delivered, ended = fc_models.pi_turn_state(session_file(session, argv), session["seen"])
        if ended:
            return None
        if not delivered and time.time() - sent >= PROMPT_START_WAIT:
            if again:
                return "agent_prompt_stalled"
            if backend():
                prompt(name, text, timeout=60)
            else:
                fc_herdr.herdr_error("agent", "prompt", name, text, timeout=60)  # the resend answers with herdr's own code, never raises
            sent, again = time.time(), True
        time.sleep(1)
    return "timeout"

# ---------- the engineering manager's seat ----------

MANAGER = fc_herdr.MANAGER
manager_record = fc_herdr.herdr_manager_record

def manager_projects(text):
    """The projects a pane's label names it the engineering manager of: [] for an unlabelled pane, None for a label that is not
    compose's (the product owner's own, never touched)."""
    if not text:
        return []
    if text == MANAGER or text.startswith(MANAGER + ": "):
        return [p for p in text[len(MANAGER) + 2:].split(", ") if p]
    return None

def manager_label(pane):
    if (b := backend()):
        return b.manager_label(pane)
    return ((fc_herdr.herdr("pane", "get", pane) or {}).get("pane") or {}).get("label")

def set_manager_label(pane, text):
    if (b := backend()):
        b.set_manager_label(pane, text)
    else:
        fc_herdr.herdr("pane", "rename", pane, text or "--clear")

def manager_panes():
    """{pane: label} over the panes that carry a label."""
    if (b := backend()):
        return b.manager_panes()
    return {p["pane_id"]: p.get("label") for p in (fc_herdr.herdr("pane", "list") or {}).get("panes", []) if p.get("label")}

def name_manager(proj):
    """Label the engineering manager's pane `manager: <slug>, ...`, every project it runs, so the product owner tells the pane they
    started compose in from their others. Only a session with no crew role names its pane, and a label the product owner gave it is kept."""
    pane = fc_core.session_id()
    if not inside() or not pane or os.environ.get("COMPOSE_ROLE"):
        return
    projects, slug = manager_projects(manager_label(pane)), Path(proj).name
    if projects is not None and slug not in projects:
        set_manager_label(pane, f"{MANAGER}: {', '.join(projects + [slug])}")

def unname_manager(slug):
    """Take a cleaned project off every engineering manager pane's label; a label left naming no project is cleared."""
    if not inside():
        return
    for pane, text in manager_panes().items():
        if slug in (projects := manager_projects(text) or []):
            rest = [x for x in projects if x != slug]
            set_manager_label(pane, f"{MANAGER}: {', '.join(rest)}" if rest else None)

def manager_live(pane):
    """Whether a live agent still sits in the pane. A host unable to say counts it as gone: it must not lock a project."""
    if (b := backend()):
        return b.live(pane)
    return fc_herdr.herdr_manager_live(pane)

def manager_claim(proj, takeover=False):
    """One engineering manager session drives a project. The first `crew start`, `crew stop`, or continuous `watch` of a session with no
    crew role claims it; the same from another session while the owner's pane still holds a live agent refuses, naming it, unless
    `takeover`, which moves the claim and the owner's teammates (`started_by`) here. An owner whose pane is gone is replaced silently.
    Outside a host sessions cannot be told apart, so nothing is claimed. Returns None when nothing was written (not a manager session,
    or already the owner), else {"from": the live pane the claim was taken from, else None}; manager.json keeps that as `from`, so
    the session that lost the project can be told (fc_manager)."""
    mine, proj = fc_core.session_id(), Path(proj)
    if os.environ.get("COMPOSE_ROLE") or not mine or not inside():
        return None
    rec = manager_record(proj)
    owner = rec.get("session")
    if owner == mine:
        return None
    taken = None
    if owner and manager_live(owner):
        if not takeover:
            sys.exit(f"{proj.name} is driven by another engineering manager session ({host_name()} pane {owner}, since {rec.get('since', '?')}): two sessions on "
                     f"one project send prompts to the same teammates and take each other's events. From here you may look (`compose status --digest`, "
                     f"`compose watch --now`). To drive it from this session instead, run the same command with --takeover.")
        taken = owner
        for f in sorted((proj / "results" / "crew").glob("*.json")):
            try:
                c = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            if isinstance(c, dict) and c.get("started_by") == owner:
                fc_core.atomic_write(f, json.dumps({**c, "started_by": mine}, indent=2))
    (proj / "results").mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    fc_core.atomic_write(proj / "results" / "manager.json", json.dumps({"session": mine, "since": now, **({"from": {"session": taken, "since": rec.get("since")}} if taken else {})}))
    return {"from": taken}

# ---------- bare `compose` ----------

def attach():
    """Bare `compose` on a terminal outside any host: open the host this machine has (the Herdr app, else herdr's session `compose`,
    else Rex) and say what to do there. Returns when a condition fails (no terminal, a host's variables, CI, or COMPOSE_NO_HERDR set);
    exits 1 when none is installed."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or any(os.environ.get(k) for k in ("HERDR_ENV", "HC_TERMINAL_ID", "REX_SESSION", "CI", "COMPOSE_NO_HERDR")):
        return
    have = fc_host.installed()
    if fc_host.HC in have:
        fc_hc.attach()
    elif fc_host.HERDR in have:
        fc_herdr.herdr_attach()
    elif fc_host.REX in have:
        fc_rex.attach()
    else:
        sys.exit("herdr is not installed: curl -fsSL https://herdr.dev/install.sh | sh\n"
                 "(or the Herdr app from https://herdr.dev, or Rex from https://www.superlogical.com/rex with its rex CLI on PATH)\nor run compose setup")
