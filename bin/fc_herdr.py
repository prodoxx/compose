import shlex, json, os, re, shutil, signal, subprocess, sys, time
from pathlib import Path
import fc_core, fc_harness, fc_models, fc_proc


# ---------- herdr ----------
# Inside herdr (HERDR_ENV=1) the fleet is visible: one space per project, a `status` tab, one tab per task holding a named
# interactive pi agent the team lead can steer with `herdr agent prompt`, and a verify tab of stage panes. Every call
# here is best-effort: outside herdr, or when herdr refuses, the run continues exactly as the headless path would.

HERDR_SPACES = {}  # project name -> workspace id, filled by herdr_space
PI_HERDR_EXT = Path.home() / ".pi" / "agent" / "extensions" / "herdr-agent-state.ts"

def herdr_env():
    """Inside herdr, unless COMPOSE_NO_HERDR asks for the headless path anyway."""
    return os.environ.get("HERDR_ENV") == "1" and bool(os.environ.get("HERDR_SOCKET_PATH")) and not os.environ.get("COMPOSE_NO_HERDR")

PANE_MAKERS = (("tab", "create"), ("pane", "split"), ("workspace", "create"))
# herdr types the agent's command into the new pane's interactive shell (`agent start` cannot launch it as the pane's process), so a
# prompt that shell asks first takes the command's first letters as its answer. oh-my-zsh's update prompt is the known one; both
# variables are its old-style switches (an explicit `zstyle ':omz:update' mode` in the user's files outranks them: shell_startup_check).
SHELL_GUARD = (("DISABLE_AUTO_UPDATE", "true"), ("DISABLE_UPDATE_PROMPT", "true"))
def herdr_argv(args):
    """The herdr command line for `args`. A new pane gets the herdr server's env, not ours, so a call that makes one passes the
    caller's COMPOSE_CONFIG and HOME on: a scratch config stays scratch in the CLI, agent, or teammate that pane runs. Each harness
    row's home variable goes too, when set in the caller: the pane's harness must use the home compose wrote hooks, trust and skills
    into, and its sign-in. PATH too: compose decides a harness is installed by its own PATH, so the pane must run with that PATH.
    They go last: herdr reads `pane split`'s pane id only as the argument right after the subcommand. A new pane's shell also
    gets SHELL_GUARD, so a startup prompt does not swallow the agent command herdr types."""
    args = [str(a) for a in args]
    if tuple(args[:2]) in PANE_MAKERS:
        homes = [h for k in fc_harness.rows() if (h := fc_harness.HARNESSES[k].get("home_env")) and os.environ.get(h)]
        args += [x for k, v in SHELL_GUARD for x in ("--env", f"{k}={v}")]
        args += [x for k in ("COMPOSE_CONFIG", "HOME", *homes, "PATH") if os.environ.get(k) for x in ("--env", f"{k}={os.environ[k]}")]
    return ["herdr", *args]

def herdr(*args, check=False, timeout=120):
    """One herdr CLI call. Returns the JSON `result`, or None when not inside herdr or the call failed (raises if check).
    `pane read` answers plain text, so it returns that text."""
    if not herdr_env():
        return None
    try:
        p = subprocess.run(herdr_argv(args), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        if check:
            raise RuntimeError(f"herdr {' '.join(map(str, args[:2]))}: {e}")
        return None
    if tuple(map(str, args[:2])) == ("pane", "read"):
        return p.stdout if p.returncode == 0 else None
    try:
        out = json.loads((p.stdout or p.stderr or "{}").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        out = {}
    if p.returncode or "error" in out:
        if check:
            raise RuntimeError(f"herdr {' '.join(map(str, args[:2]))}: {(out.get('error') or {}).get('code') or p.stderr.strip()[-200:]}")
        return None
    return out.get("result", out)

def herdr_error(*args, timeout=120):
    """Like herdr() but returns the error code string on failure ("timeout", "agent_blocked", ...) instead of None.
    In its own session: a SIGINT to the run's process group must not kill a `--wait` client before the run set its stop."""
    p = subprocess.run(herdr_argv(args), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        out = json.loads((p.stdout or p.stderr or "{}").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        out = {}
    if p.returncode or "error" in out:
        return None, ((out.get("error") or {}).get("code") or p.stderr.strip()[-200:] or "error")
    return out.get("result", out), None

def herdr_text(*args, timeout=30):
    """One herdr CLI call whose answer is plain text (`agent read`): the text, or "" outside herdr or when the call failed."""
    if not herdr_env():
        return ""
    try:
        p = subprocess.run(herdr_argv(args), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout if p.returncode == 0 else ""

SHELL_PROMPTS = (  # (pattern for the last visible line of a pane stopped before its command prompt, the key that declines it, "" when unknown)
    (re.compile(r"Would you like to update\? \[Y/n\]"), "n"),  # oh-my-zsh: it reads one key, so "n" alone answers it
    (re.compile(r"[\[(]y(es)?/n(o)?[\])]", re.I), ""),  # any other yes/no question: named in the error, never answered blind
)

def pane_screen(pane):
    """The non-blank lines on `pane`'s screen; empty outside herdr or when it cannot be read. Read through herdr()
    so the callers that already stand in for it (a fake herdr answering {}) get an empty screen, not a call to the real one."""
    out = herdr("pane", "read", pane, "--source", "visible", "--lines", "40")
    return [ln.strip() for ln in out.splitlines() if ln.strip()] if isinstance(out, str) else []

def pane_blocker(pane):
    """(line, key) for the prompt the shell in `pane` is stopped on, else None: only the last line on screen counts, so an
    answered prompt above a command prompt is no blocker. `key` declines a prompt SHELL_PROMPTS knows, else ""."""
    lines = pane_screen(pane)
    for pattern, key in SHELL_PROMPTS if lines else ():
        if pattern.search(lines[-1]):
            return lines[-1], key
    return None

def pane_unblock(pane):
    """Answer the prompt `pane`'s shell is stopped on when SHELL_PROMPTS knows the key that declines it; True when it did."""
    blocker = pane_blocker(pane)
    if not (blocker and blocker[1]):
        return False
    herdr("pane", "send-text", pane, blocker[1])
    time.sleep(1)  # the shell echoes the answer and goes on to its command prompt
    return True

def pane_diagnosis(pane):
    """Why a start in `pane` timed out, as one sentence for the error: the prompt its shell waits on (its first typed letters were the
    answer, so the agent never started) or else the last lines it shows; "" when it shows nothing."""
    if blocker := pane_blocker(pane):
        return (f"the new pane's shell is waiting on a prompt: '{blocker[0]}' - the typed agent command went into it as the answer; "
                "answer it in the pane, and run `compose doctor` for the setting that stops it")
    shown = pane_screen(pane)
    return "the pane shows: " + " / ".join(shown[-3:]) if shown else ""

ZSH_STARTUP = (".zshenv", ".zprofile", ".zshrc", ".zlogin")  # what a pane's zsh reads, login or not (herdr's shell_mode)
OMZ_QUIET_MODES = ("disabled", "auto", "reminder", "background-alpha")  # the update modes that never ask

def shell_startup_check(home):
    """(ok, detail) for the zsh startup files in `home`: not ok when one sets oh-my-zsh's update mode to one that asks, with the file
    and line of each. That `zstyle` outranks SHELL_GUARD, so a pane compose makes would stop on `Would you like to update? [Y/n]`."""
    found = []
    for name in ZSH_STARTUP:
        try:
            lines = (Path(home) / name).read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(lines, 1):
            m = re.match(r"\s*zstyle\s+['\"]:omz:update['\"]\s+mode\s+(\S+)", re.sub(r"(^|\s)#.*$", "", line))
            if m and m.group(1) not in OMZ_QUIET_MODES:
                found.append(f"~/{name}:{n} sets the oh-my-zsh update mode to {m.group(1)}")
    if not found:
        return True, ""
    return False, ("; ".join(found) + ", which asks 'Would you like to update? [Y/n]' in every new shell and takes the first letter of "
                   "the agent command compose types into a teammate's pane, so its start times out")

def herdr_ready(agent, timeout=60):
    """Return once a Claude session's input box (its `❯` line) is on screen, or it is already on a turn, blocked on a dialog, or done.
    `herdr agent start` reports a Claude session ready seconds before its UI exists, while the pane still shows the shell command
    (`herdr agent explain`: idle, rule none): text typed then is kept in the box unsent, or dropped outright, so a brief waits for the box."""
    for _ in range(max(1, timeout // 2)):
        got = (herdr("agent", "get", agent) or {}).get("agent") or {}
        if not got or got.get("agent_status") in ("working", "blocked", "done"):
            return
        if any(ln.lstrip().startswith("❯") for ln in herdr_text("agent", "read", agent, "--source", "visible", "--lines", "40").splitlines()):
            return
        time.sleep(2)

def herdr_dialog(agent):
    """The dialog a blocked Claude session shows (its title, text, and choices, on one line), read from its pane: everything below
    the last rule line, without the key hints. "unknown" when the pane cannot be read."""
    lines = herdr_text("agent", "read", agent, "--source", "visible", "--lines", "40").splitlines()
    rules = [i for i, ln in enumerate(lines) if ln.strip() and set(ln.strip()) <= set("─━-")]
    body = [ln.strip().removeprefix("❯").strip() for ln in lines[rules[-1] + 1 if rules else 0:]]
    text = " / ".join(ln for ln in body if ln and not ln.startswith(("Enter to confirm", "Esc to")))
    return text[:400] or "unknown"

def herdr_login_screen(agent):
    """The sign-in text on `agent`'s pane: the first `login_screens` text of any fc_harness row visible on it, else ""."""
    text = herdr_text("agent", "read", agent, "--source", "visible", "--lines", "40")
    for key in fc_harness.rows():
        for screen in fc_harness.HARNESSES[key].get("login_screens", ()):
            if screen in text:
                return screen
    return ""

def herdr_await_start(agent, first=None, tries=3):
    """Return once `agent` has started on its brief, the first prompt it gets. This only confirms the submission landed (status
    working, blocked, or done); when it did not, presses Enter for a box that kept the text without submitting, then sends `first`
    again for a box that lost it, and fails loudly with the agent name when it still did not start. A pane on a sign-in screen
    gets neither: before each Enter and each re-send `herdr_login_screen` is asked, and one there raises naming its text."""
    def started():  # `done` counts too: a short first turn can finish before the check
        return ((herdr("agent", "get", agent) or {}).get("agent") or {}).get("agent_status") in ("working", "blocked", "done")
    def settles(secs):
        for _ in range(secs // 2):
            if started():
                return True
            time.sleep(2)
        return False
    for attempt in range(tries + 1):
        if attempt:
            if screen := herdr_login_screen(agent):
                raise RuntimeError(f"{agent} is on a sign-in screen ({screen}): sign in there, then run compose again")
            herdr("agent", "send-keys", agent, "enter")
            if settles(6):
                return
            if first:
                if screen := herdr_login_screen(agent):
                    raise RuntimeError(f"{agent} is on a sign-in screen ({screen}): sign in there, then run compose again")
                herdr("agent", "prompt", agent, first)
        if settles(20):
            return
    raise RuntimeError(f"{agent} did not start on its brief after {tries} retries: look in its tab"
                       + (f", and send it again with: herdr agent prompt {agent} {shlex.quote(first)}" if first else ""))

def herdr_start_crew(*args, tries=8):
    """`herdr agent start` for a teammate, retried while its pane is still coming up: starts running in parallel (designcrews,
    testcrews) race each other and the new pane answers agent_pane_busy until its shell is there, or agent_name_not_found while herdr
    has not yet registered the name the start waits for (the session may be up under its pane's default name: it is renamed).
    A shell prompt on the new pane takes the typed command's first letters, so one on screen is answered first (pane_unblock), and a
    start that times out on one is answered once and started again; a timeout that stays says what the pane shows (pane_diagnosis)."""
    pane = args[args.index("--pane") + 1] if "--pane" in args else None
    answered = bool(pane and pane_unblock(pane))
    for i in range(tries):
        try:
            r = herdr("agent", "start", *args, check=True, timeout=90)
            if "--pane" in args:
                agent_pids(args[args.index("--pane") + 1])
            return r
        except RuntimeError as e:
            if ("timeout" in str(e) or "agent_name_not_found" in str(e)) and "--pane" in args:  # the session can come up after the call gave up: name it rather than lose it
                pane = args[args.index("--pane") + 1]
                for _ in range(10):  # it may still be registering with herdr when the call gives up
                    if any(ag.get("pane_id") == pane for ag in live_agents()):
                        print(f"herdr agent start timed out but {args[0]} came up; continuing")
                        return herdr("agent", "rename", pane, args[0], check=True)
                    time.sleep(2)
                if pane_launching(args[0], pane):  # one last look by name and by pane: an agent that can still come up is never declared dead
                    print(f"herdr agent start timed out but {args[0]} is still starting in its pane; continuing")
                    return None
                if "timeout" in str(e) and not answered and pane_unblock(pane):  # the shell was stopped on a prompt: it is answered, so the command is typed again
                    answered = True
                    continue
                if "timeout" in str(e) and (why := pane_diagnosis(pane)):
                    raise RuntimeError(f"{e}: {why}") from e
            if not any(c in str(e) for c in ("agent_pane_busy", "agent_name_not_found")) or i == tries - 1:
                raise
            time.sleep(0.5 * (i + 1))

def pane_processes(pane):
    """The processes running in `pane` in front of its shell: empty when the pane holds nothing but an idle shell."""
    info = (herdr("pane", "process-info", "--pane", pane) or {}).get("process_info") or {}
    return [p for p in info.get("foreground_processes", []) if p.get("pid") != info.get("shell_pid")] \
        if info.get("foreground_process_group_id") != info.get("shell_pid") else []

def pane_suspended(pane):
    """True when a process in `pane` is stopped (Ctrl-Z, or SIGSTOP after a sleep): herdr keeps listing such an agent with its last
    status, and after `fg` its terminal can stay wedged, so it is neither working nor finished."""
    for p in pane_processes(pane):
        try:
            stat = subprocess.run(["ps", "-o", "stat=", "-p", str(p["pid"])], capture_output=True, text=True, timeout=5, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired, KeyError):
            continue
        if stat.startswith("T"):
            return True
    return False

def agent_pids(pane):
    return sorted(p["pid"] for p in pane_processes(pane) if fc_proc.nice_floor(p["pid"]) is not None)

def pane_launching(agent, pane):
    """True when `agent` is registered by name, listed on `pane`, or the pane runs a process that is still becoming it."""
    return bool(herdr("agent", "get", agent)) or any(ag.get("pane_id") == pane for ag in live_agents()) or bool(pane_processes(pane))

def pane_kill(pane):
    """Stop whatever runs in `pane` in front of its shell, so closing the tab leaves nothing unregistered behind."""
    for p in pane_processes(pane):
        try:
            os.kill(p["pid"], signal.SIGTERM)
        except (OSError, KeyError):
            pass

def agent_name(slug, tid):
    """herdr agent names: [a-z][a-z0-9_-]{0,31}, unique among live agents. `<slug>-<id>`, slug trimmed to fit."""
    s = re.sub(r"[^a-z0-9_-]+", "-", slug.lower()).strip("-")
    if not re.match(r"[a-z]", s):
        s = "fc-" + s
    return f"{s[:32 - len(tid) - 1]}-{tid}"

def agent_id(slug, name):
    """The id `agent_name(slug, id)` built `name` from, else None: a cut name no longer starts with the whole slug.
    The shortest id wins: `clean-recut-branche-orchestrator` is `orchestrator`, not `recut-branche-orchestrator` on a 5-character cut."""
    return next((name[i + 1:] for i in range(len(name) - 1, -1, -1) if name[i] == "-" and agent_name(slug, name[i + 1:]) == name), None)

def agent_ref(slug, ref):
    """The registered agent a command names: a role, the name herdr holds, or `<slug>-<role>` spelled out in full."""
    return agent_name(slug, agent_id(slug, ref) or ref.removeprefix(agent_name(slug, "")))

def herdr_main_repo(wt):
    c = fc_core.checkout(wt)
    return str(c.common.parent) if c else str(wt)

def herdr_find_space(wt):
    """The workspace id whose checkout is this path (`worktree.checkout_path` in `workspace list`)."""
    wt = str(Path(wt).resolve())
    for w in (herdr("workspace", "list") or {}).get("workspaces", []):
        cp = (w.get("worktree") or {}).get("checkout_path")
        if cp and str(Path(cp).resolve()) == wt:
            return w["workspace_id"]
    return None

def project_worktree(proj):
    """The project's own checkout: proj is `<worktree>/.compose/<name>`."""
    return Path(proj).resolve().parents[1]

def live_agents():
    return (herdr("agent", "list") or {}).get("agents", [])

def space_record(proj):
    return Path(proj) / "results" / "herdr.json"

def tab_label(proj, name):
    """A project tab's label. In a space it shares (the one compose was started from, when no space of its repo was open) the
    project's name goes first, so two projects there never mistake each other's tabs; in a space of its own the bare name."""
    return f"{Path(proj).name} {name}" if space_record(proj).exists() else name

MANAGER = "manager"

def manager_projects(label):
    """The projects a pane's label names it the engineering manager of: [] for an unlabelled pane, None for a label that is not
    compose's (the product owner's own, never touched)."""
    if not label:
        return []
    if label == MANAGER or label.startswith(MANAGER + ": "):
        return [p for p in label[len(MANAGER) + 2:].split(", ") if p]
    return None

def herdr_name_manager(proj):
    """Label the engineering manager's pane `manager: <slug>, ...`, every project it runs, so the product owner tells the pane
    they started compose in from their others. Only a session with no crew role names its pane, and a label the product owner
    gave the pane is kept."""
    pane = os.environ.get("HERDR_PANE_ID")
    if not herdr_env() or not pane or os.environ.get("COMPOSE_ROLE"):
        return
    projects, slug = manager_projects(((herdr("pane", "get", pane) or {}).get("pane") or {}).get("label")), Path(proj).name
    if projects is not None and slug not in projects:
        herdr("pane", "rename", pane, f"{MANAGER}: {', '.join(projects + [slug])}")

def herdr_unname_manager(slug):
    """Take a cleaned project off every engineering manager pane's label; a label left naming no project is cleared."""
    for p in (herdr("pane", "list") or {}).get("panes", []):
        if slug in (projects := manager_projects(p.get("label")) or []):
            rest = [x for x in projects if x != slug]
            herdr("pane", "rename", p["pane_id"], f"{MANAGER}: {', '.join(rest)}" if rest else "--clear")

def herdr_manager_record(proj):
    """results/manager.json: the engineering manager session (herdr pane) that drives the project, set by its first `crew start` or `watch`."""
    try:
        return json.loads((Path(proj) / "results" / "manager.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}

def herdr_manager_live(pane):
    """Whether a live agent still sits in herdr pane `pane`. herdr unable to say counts as gone: it must not lock a project."""
    listed = herdr("agent", "list")
    return listed is not None and any(ag.get("pane_id") == pane for ag in listed.get("agents", []))

def herdr_manager_claim(proj, takeover=False):
    """One engineering manager session drives a project. The first `crew start`, `crew stop`, or continuous `watch` of a session with no
    crew role claims it; the same from another session while the owner's pane still holds a live agent refuses, naming it, unless
    `takeover`, which moves the claim and the owner's teammates (`started_by`) here. An owner whose pane is gone is replaced silently.
    Outside herdr sessions cannot be told apart, so nothing is claimed."""
    mine, proj = fc_core.session_id(), Path(proj)
    if os.environ.get("COMPOSE_ROLE") or not mine or not herdr_env():
        return
    rec = herdr_manager_record(proj)
    owner = rec.get("session")
    if owner == mine:
        return
    if owner and herdr_manager_live(owner):
        if not takeover:
            sys.exit(f"{proj.name} is driven by another engineering manager session (herdr pane {owner}, since {rec.get('since', '?')}): two sessions on "
                     f"one project send prompts to the same teammates and take each other's events. From here you may look (`compose status --digest`, "
                     f"`compose watch --now`). To drive it from this session instead, run the same command with --takeover.")
        for f in sorted((proj / "results" / "crew").glob("*.json")):
            try:
                c = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            if isinstance(c, dict) and c.get("started_by") == owner:
                fc_core.atomic_write(f, json.dumps({**c, "started_by": mine}, indent=2))
    (proj / "results").mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(proj / "results" / "manager.json", json.dumps({"session": mine, "since": time.strftime("%Y-%m-%d %H:%M:%S")}))

def herdr_tab(space, label, cwd, *args, check=False):
    """A new tab labelled `label` whose one pane carries the same name. The `tab create` result, {} when herdr refused."""
    r = herdr("tab", "create", "--workspace", space, "--label", label, "--cwd", str(cwd), *args, "--no-focus", check=check) or {}
    if pane := (r.get("root_pane") or {}).get("pane_id"):
        herdr("pane", "rename", pane, label)
    return r

def herdr_project_space(proj):
    """The project's space: the shared one `herdr_space` recorded, while it is open, else the space whose checkout is the project
    worktree. None when neither exists."""
    if space_record(proj).exists():
        space = json.loads(space_record(proj).read_text()).get("space")
        return space if any(w.get("workspace_id") == space for w in (herdr("workspace", "list") or {}).get("workspaces", [])) else None
    return herdr_find_space(project_worktree(proj))

def herdr_project_tabs(proj, space):
    """The project's tabs in its space: in a shared space only those labelled with its name."""
    tabs = (herdr("tab", "list", "--workspace", space) or {}).get("tabs", [])
    return [t for t in tabs if (t.get("label") or "").startswith(tab_label(proj, ""))]

def herdr_repo_space(wt):
    """The open space of the repo's own checkout, the only one herdr groups the repo's worktree spaces under. None when no space
    of the repo is open. `worktree list` names it even when `workspace list` has not yet learned the space's repo."""
    return ((herdr("worktree", "list", "--cwd", str(wt)) or {}).get("source") or {}).get("source_workspace_id")

def herdr_space(proj):
    """The project's herdr space, idempotent: the project worktree opened as a worktree space of its own, labelled `<name>` with
    a `shell` tab, grouped under the repo's open space. When no space of the repo is open, the space compose was
    started from (HERDR_WORKSPACE_ID) instead, never a new empty repo space: there its shell tab is labelled `<name> shell`,
    and the space is recorded so every later command finds it whoever runs it. A recorded space is kept while
    it is open. Outside any space herdr opens the repo's space itself. Keyed on the project, never on where a command runs
    (verify runs in the stack worktree)."""
    if not herdr_env():
        return None
    slug, wt = Path(proj).name, project_worktree(proj)
    if slug in HERDR_SPACES:
        return HERDR_SPACES[slug]
    space, caller = herdr_project_space(proj), os.environ.get("HERDR_WORKSPACE_ID")
    if not space:
        space_record(proj).unlink(missing_ok=True)  # a recorded space that was closed: the project's tabs move to a space of its own
        if repo := herdr_repo_space(wt):
            r = herdr("worktree", "open", "--workspace", repo, "--path", str(wt), "--label", slug, "--no-focus") or {}
            space = (r.get("workspace") or {}).get("workspace_id")
        elif caller:
            space = caller
            fc_core.atomic_write(space_record(proj), json.dumps({"space": space}))
    if space and space_record(proj).exists():
        HERDR_SPACES[slug] = space
        if tab_label(proj, "shell") not in herdr_tabs(space):
            herdr_tab(space, tab_label(proj, "shell"), wt)
        return space
    if not space:
        r = herdr("worktree", "open", "--cwd", herdr_main_repo(wt), "--path", str(wt), "--label", slug, "--no-focus") or {}
        space = (r.get("workspace") or {}).get("workspace_id") or r.get("workspace_id")
    if not space:
        r = herdr("workspace", "create", "--cwd", str(wt), "--label", slug, "--no-focus") or {}
        space = (r.get("workspace") or {}).get("workspace_id")
    if space:
        HERDR_SPACES[slug] = space
        herdr("workspace", "rename", space, slug)
        for label, t in herdr_tabs(space).items():
            if label and label.isdigit():  # the tab herdr opened with the space: a shell in the project worktree
                herdr("tab", "rename", t["tab_id"], "shell")
                for p in (herdr("pane", "list", "--workspace", space) or {}).get("panes", []):
                    if p.get("tab_id") == t["tab_id"]:
                        herdr("pane", "rename", p["pane_id"], "shell")
    return space


def herdr_tabs(space):
    return {t.get("label"): t for t in (herdr("tab", "list", "--workspace", space) or {}).get("tabs", [])}

def herdr_label(session, label, cost, state="working"):
    """Display-only pane metadata: the state label the product owner reads next to the agent, and the running cost."""
    if session and session.get("pane"):
        herdr("pane", "report-metadata", session["pane"], "--source", "compose", "--state-label", f"{state}={label}",
              "--state-label", f"done={label}", "--token", f"cost=${cost:.4f}")

IMPLEMENTERS_TAB = "implementers"

def herdr_implementer_pane(space, cwd, env, label=IMPLEMENTERS_TAB):
    """A fresh pane for one implementer in the project's single `implementers` tab: the tab's root pane when the tab is new,
    else a split of its newest pane, alternating right and down so the panes tile instead of thinning into strips."""
    env_args = [x for k, v in env.items() for x in ("--env", f"{k}={v}")]
    tab = herdr_tabs(space).get(label)
    if not tab:
        r = herdr("tab", "create", "--workspace", space, "--label", label, "--cwd", str(cwd), *env_args, "--no-focus", check=True)
        return r["root_pane"]["pane_id"]
    panes = [p for p in (herdr("pane", "list", "--workspace", space) or {}).get("panes", []) if p.get("tab_id") == tab["tab_id"]]
    direction = "right" if len(panes) % 2 else "down"
    r = herdr("pane", "split", panes[-1]["pane_id"], "--direction", direction, "--cwd", str(cwd), *env_args, "--no-focus", check=True)
    return r["pane"]["pane_id"]

def herdr_agent_start(*args, tries=8):
    """`herdr agent start`, retried while the pane is still coming up: a pane split a moment ago answers agent_pane_busy
    until its shell prompt is there, which is the harness racing itself and never a reason to fail a task. A shell prompt on the
    pane is answered like herdr_start_crew does, and a timeout that stays says what the pane shows."""
    pane = args[args.index("--pane") + 1] if "--pane" in args else None
    answered = bool(pane and pane_unblock(pane))
    for i in range(tries):
        started, err = herdr_error("agent", "start", *args, timeout=90)
        if err == "timeout" and pane and not answered and pane_unblock(pane):
            answered = True
            continue
        if err not in ("agent_pane_busy", "agent_name_not_found") or i == tries - 1:
            break
        time.sleep(0.5 * (i + 1))
    if err:
        if err == "timeout" and pane and (why := pane_diagnosis(pane)):
            err = f"{err}: {why}"
        raise RuntimeError(f"herdr agent start: {err}")
    if "--pane" in args:
        agent_pids(args[args.index("--pane") + 1])
    return started


PROMPT_START_WAIT = 30  # seconds a stalled prompt gets to show up as a turn before it is sent again; pi starts one in 1 to 2 s

def herdr_stalled(session, prompt, deadline):
    """Follow a prompt herdr answered with agent_prompt_stalled. The turn is the one herdr sees working or blocked, waited for like
    any other, or the one the session file records whole while herdr missed it. A prompt pi never wrote down goes once more into
    the same session after PROMPT_START_WAIT. Returns None when the turn ended, else "timeout", or "agent_prompt_stalled" when it
    never started."""
    name, sent, again = session["name"], time.time(), False
    while (left := deadline - time.time()) > 0:
        got = (herdr("agent", "get", name) or {}).get("agent") or {}
        session["sfile"] = session.get("sfile") or (got.get("agent_session") or {}).get("value")
        if got.get("agent_status") in ("working", "blocked"):
            return herdr_error("agent", "wait", name, "--timeout", str(int(left * 1000)), timeout=left + 30)[1]
        delivered, ended = fc_models.pi_turn_state(session["sfile"], session["seen"])
        if ended:
            return None
        if not delivered and time.time() - sent >= PROMPT_START_WAIT:
            if again:
                return "agent_prompt_stalled"
            herdr_error("agent", "prompt", name, prompt, timeout=60)
            sent, again = time.time(), True
        time.sleep(1)
    return "timeout"


def herdr_close_tabs(space, label):
    """Closes the tabs `label` names: its own and the older `<label> HH:MM` ones."""
    for old, t in herdr_tabs(space).items():
        if re.fullmatch(re.escape(label) + r"( \d\d:\d\d)?", old or ""):
            herdr("tab", "close", t["tab_id"])

def herdr_close_task(proj, tid):
    """Closes the pane of a task's pi agent, whichever session the task ended on: a fallback turn runs headless, and a hand-done
    task has no session, so only the agent's name still reaches the pane an earlier attempt left."""
    got = herdr("agent", "get", agent_name(Path(proj).name, tid)) or {}
    if pane := (got.get("agent") or got).get("pane_id"):
        herdr("pane", "close", pane)

def herdr_verify_panes(space, wt, names, label="verify"):
    """The project's one `verify` tab, rebuilt per run, with one pane per stage, each a plain shell the CLI reports states for. {name: pane_id}."""
    if not space or not names:
        return {}
    herdr_close_tabs(space, label)  # the previous run's tab (and the older `verify HH:MM` ones)
    r = herdr("tab", "create", "--workspace", space, "--label", label, "--cwd", str(wt), "--no-focus") or {}
    pane = (r.get("root_pane") or {}).get("pane_id")
    if not pane:
        return {}
    panes, prev = {names[0]: pane}, pane
    for n in names[1:]:
        s = herdr("pane", "split", prev, "--direction", "down", "--cwd", str(wt), "--no-focus") or {}
        prev = (s.get("pane") or {}).get("pane_id") or prev
        panes[n] = prev
    for n, pid in panes.items():
        herdr("pane", "rename", pid, n)
        herdr_stage(panes, n, "idle", "pending")
    return panes

def herdr_stage(panes, name, state, message=""):
    if panes.get(name):
        herdr("pane", "report-agent", panes[name], "--source", "compose", "--agent", name, "--state", state, "--message", message, "--seq", str(time.time_ns()))


def herdr_space_is(space, name):
    """Only the project's own space is ever closed by its clean: a checkout path can be a pool slot another project now uses."""
    return any(w.get("workspace_id") == space and w.get("label") == name for w in (herdr("workspace", "list") or {}).get("workspaces", []))

def herdr_attach():
    """Bare `compose` on a terminal outside herdr: print the hint and exec `herdr session attach compose` in this directory.
    Returns when a condition fails (no terminal, HERDR_ENV, CI, or COMPOSE_NO_HERDR set); exits 1 when herdr is not installed."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or any(os.environ.get(k) for k in ("HERDR_ENV", "CI", "COMPOSE_NO_HERDR")):
        return
    if not shutil.which("herdr"):
        sys.exit("herdr is not installed: curl -fsSL https://herdr.dev/install.sh | sh\nor run compose setup")
    print("opening herdr session compose: run claude in a pane and type /compose", flush=True)
    os.execvp("herdr", ["herdr", "session", "attach", "compose"])
