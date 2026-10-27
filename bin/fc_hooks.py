import hashlib, json, os, re, shlex, sys
from pathlib import Path
import fc_core, fc_config, fc_harness, fc_worktree

# ---------- Claude Code hooks: `guard` keeps the team lead and planner out of code, `skillcheck` names changed skill files ----------

GUARDED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "Bash")
GUARDED_ROLES = ("orchestrator", "planner")
CLI_FILE = re.compile(r"/\.compose/(linear\.json|[^/]+/(linear|verify)\.json|[^/]+/scratch/.+|[^/]+/pr/[^/]+\.md)$")  # the team lead's own: the CLI's config and the layers' PR text

def env_role(role):
    """The COMPOSE_ROLE of a teammate's tab. `guard` keeps the team lead and every planner, `planner-<ticket>` too, off the code."""
    return role if role in GUARDED_ROLES else "planner" if role.startswith("planner-") else "crew"

def restore_crew_env(proj):
    """A teammate's session that comes back without the env its tab was created with (herdr rebuilds the pane after a computer
    restart) is found by its pane in the project's crew records, and gets that env again: otherwise it is taken for an engineering
    manager. A session with a role, outside herdr, or in a pane no record names is left alone."""
    pane = os.environ.get("HERDR_PANE_ID")
    if os.environ.get("COMPOSE_ROLE") or not pane:
        return
    for f in sorted((Path(proj) / "results" / "crew").glob("*.json")):
        try:
            c = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(c, dict) and c.get("pane") == pane and c.get("role") and c.get("name"):
            os.environ.update(COMPOSE_ROLE=env_role(c["role"]), COMPOSE_TEAMMATE=c["name"], COMPOSE_PROJECT=Path(proj).name, COMPOSE_REPO=str(Path(proj).resolve().parents[1]))
            return

def restore_pane_env(cwd):
    """restore_crew_env for a hook: the project is found by the hook's cwd, over every worktree of its repo."""
    if os.environ.get("HERDR_PANE_ID") and not os.environ.get("COMPOSE_ROLE"):
        for _, proj in fc_core.all_projects(cwd or ".").values():
            restore_crew_env(proj)
            if os.environ.get("COMPOSE_ROLE"):
                break

def guard_decision(tool, path, role):
    """None to allow, else why not. The team lead writes Linear, the CLI's JSON, and the layers' PR text; code, tests, and docs
    are teammate work. The toolsmith edits in its own worktree of the compose repo, never in a shared checkout of it."""
    if role not in (*GUARDED_ROLES, "toolsmith") or tool not in GUARDED_TOOLS or not path:
        return None
    p = Path(path).expanduser().resolve()
    d = next((x for x in [p.parent, *p.parents] if x.exists()), Path("/"))
    if role == "toolsmith":
        return f"A toolsmith never edits {why}: fix on a branch in your own worktree of the compose repo." if (why := fc_worktree.shared_checkout(d, fc_core.CLI_CHECKOUT)) else None
    if CLI_FILE.search(str(p)):
        return None
    if fc_core.sh(["git", "rev-parse", "--is-inside-work-tree"], d, check=False).returncode != 0:
        return None  # scratch outside any repository, e.g. a brief in /tmp
    return (f"The team lead and the planner never edit code, tests, config, or docs ({p.name}). Hand it to a teammate: "
            f"`compose crew start --role <job> --brief -`, or put it in a task issue for `run`.")

def toplevel(path):
    """The worktree root `path` is in, or None."""
    c = fc_core.checkout(path)
    return c.top if c else None

def switch_decision(command, cwd, role, shared):
    """None to allow, else why not. A crew session started without its own worktree runs in the project worktree `shared` with the
    team lead, so a `git checkout` or `git switch` there moves the lead's HEAD, and the stack then reads the wrong task branches.
    Restoring files (`git checkout -- path`) and any checkout in another worktree (`cd`, `git -C`) are fine."""
    if role != "crew" or not command or not shared or not toplevel(shared):
        return None
    here = Path(cwd or ".")
    for segment in re.split(r"&&|\|\||[;|\n]", command):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        if words[:1] == ["cd"] and len(words) > 1:
            here = here / Path(words[1]).expanduser()
        if words[:1] != ["git"]:
            continue
        i, where = 1, here
        while i < len(words) and words[i].startswith("-"):  # git's own options; -C and -c take a value
            if words[i] == "-C" and i + 1 < len(words):
                where = where / Path(words[i + 1]).expanduser()
            i += 2 if words[i] in ("-C", "-c") else 1
        verb, rest = (words[i] if i < len(words) else ""), words[i + 1:]
        if verb in ("checkout", "switch") and not restores_files(rest, where) and toplevel(where) == toplevel(shared):
            return (f"`git {verb}` here would move the team lead's HEAD: {shared} is the project worktree, shared with the team lead. Work in a "
                    f"worktree of your own (ask for `crew start --work-repo` or `--cwd`) and run git there with `git -C <worktree>`.")
    return None

def restores_files(rest, where):
    """True when `git checkout <rest>` restores files and leaves HEAD alone: a `--` anywhere (`git checkout <tree-ish> -- <paths>`),
    `-p`/`--patch`, or every argument an existing path under `where` (`git checkout app/page.test.tsx`). An empty `rest` is a
    plain `git checkout`, which moves nothing, and is left to the branch rule's caller: it is denied as the conservative reading."""
    if "--" in rest or rest[:1] in (["-p"], ["--patch"]):
        return True
    args = [a for a in rest if not a.startswith("-")]
    return bool(args) and len(args) == len(rest) and all((Path(where) / a).expanduser().exists() for a in args)

HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*([\"']?)(\w+)\1")

def without_heredoc_bodies(cmd):
    """`cmd` minus the text of its heredocs: that text is data for the command, never shell, so a `>` in it is no redirection."""
    kept, end = [], None
    for line in cmd.split("\n"):
        if end is not None:
            end = None if line.strip() == end else end
            continue
        kept.append(line)
        if m := HEREDOC.search(line):
            end = m.group(2)
    return "\n".join(kept)

ASSIGN = re.compile(r"([A-Za-z_]\w*)=(.*)", re.S)
SEPARATORS = ("&&", "||", ";", "|", "&", "\n", "(", ")")  # the tokens a new command starts after
VAR = re.compile(r"\$(\w+)|\$\{(\w+)\}")

def expand(word, assigned):
    """`word` as the shell reads it: a leading ~, then each $NAME and ${NAME} from the command's own assignments, else the
    environment. A name set nowhere stays as written."""
    return VAR.sub(lambda m: assigned.get(n := m.group(1) or m.group(2), os.environ.get(n, m.group(0))), os.path.expanduser(word))

def edit_paths(ev):
    """The paths a tool call writes, relative ones resolved against the event's cwd, moved by any `cd` before them in the command."""
    ti = ev.get("tool_input") or {}
    cmd = ti.get("command") or ""
    found = [ti.get("file_path") or ti.get("notebook_path")]
    if ev.get("tool_name") == "apply_patch":
        found += re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", cmd, re.M)
    elif ev.get("tool_name") == "Bash":
        lex = shlex.shlex(without_heredoc_bodies(cmd), posix=True, punctuation_chars="();<>|&\n")  # a newline is a separator, never blank
        lex.whitespace, lex.whitespace_split = " \t\r", True
        try:
            words = list(lex)
        except ValueError:  # an unclosed quote: the shell does not run it either
            words = []
        assigned, here, at_start = {}, Path(ev.get("cwd") or "."), True
        for i, w in enumerate(words):
            if m := ASSIGN.fullmatch(w):
                assigned[m.group(1)] = expand(m.group(2), assigned)
            elif at_start and w == "cd" and i + 1 < len(words) and words[i + 1] not in SEPARATORS:  # a `cd` moves every later relative target
                here = here / expand(words[i + 1], assigned)
            elif w in (">", ">>", ">|", "&>", "&>>") and i + 1 < len(words):
                found.append(str(here / expand(words[i + 1], assigned)))
            at_start = w in SEPARATORS
    return [str(Path(ev.get("cwd") or ".") / p) for p in found if p]

def cmd_guard(a):
    """Claude Code PreToolUse hook (the plugin's hooks.json): deny the team lead's file edits inside a repository, and a crew
    session's branch switch in the shared project worktree."""
    try:
        ev = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    ti = ev.get("tool_input") or {}
    restore_pane_env(ev.get("cwd"))  # a restored pane has lost its env (compose.py does the same for commands)
    role = os.environ.get("COMPOSE_ROLE")
    why = (switch_decision(ti.get("command"), ev.get("cwd"), role, os.environ.get("COMPOSE_REPO")) if ev.get("tool_name") == "Bash" else None)
    why = why or next(filter(None, (guard_decision(ev.get("tool_name"), p, role) for p in edit_paths(ev))), None)
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": why}}))

SKILL_DIR = Path.home() / ".claude" / "skills" / "compose"  # the main checkout every live session reads: a merge changes it
SKILL_SEEN = fc_config.CONFIG_PATH.parent / "skill-seen"  # one file per session: the skill files' blob hashes it last saw
# ponytail: never pruned, a few hundred bytes per session; prune by mtime if the directory grows
SKILL_FILES = ("SKILL.md", "sections/*.md", "commands/*.md")

def skill_hashes():
    """Each skill file's git blob hash, by absolute path."""
    files = sorted(f for g in SKILL_FILES for f in SKILL_DIR.resolve().glob(g))
    if not files:
        raise FileNotFoundError(SKILL_DIR)
    return {str(f): hashlib.sha1(b"blob %d\0" % len(b := f.read_bytes()) + b).hexdigest() for f in files}

def skill_changed(seen, cur):
    """The skill files whose hash differs between two skill_hashes() results: changed, added, or removed."""
    return sorted(p for p in cur.keys() | seen.keys() if cur.get(p) != seen.get(p))

def skill_notice(session):
    """Record the skill version this session sees; the notice naming what changed since it last saw one, else None."""
    if not session:  # no session id: every such caller would share one file
        return None
    cur, f = skill_hashes(), SKILL_SEEN / f"{session}.json"
    seen = json.loads(f.read_text()) if f.exists() else None
    if seen != cur:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cur))
    changed = skill_changed(seen or {}, cur)
    if seen is None or not changed:
        return None
    return f"compose's skill changed since you read it: {', '.join(changed)}. Re-read only those files before acting; nothing else changed."

def cmd_skillcheck(a):
    """Claude Code UserPromptSubmit and PostToolUse(Skill) hook: a session with a role, or one that loaded the compose skill
    (the Skill tool, or a typed /compose command), hears once when the skill's files changed. Any error prints nothing."""
    try:
        ev = json.load(sys.stdin)
        session = re.sub(r"[^\w-]", "", ev.get("session_id") or "")
        if not session:
            return
        loads = "/" + (ev.get("tool_input") or {}).get("skill", "") if ev.get("hook_event_name") == "PostToolUse" else ev.get("prompt", "")
        sigils = "|".join(re.escape(h["sigil"]) for h in fc_harness.HARNESSES.values() if h.get("sigil"))
        if re.match(rf"/compose(:|\s|$)|(?:{sigils or '(?!)'})compose(-|\s|$)", loads):
            skill_notice(session)  # the version it loads now is the one it read
        elif os.environ.get("COMPOSE_ROLE") or (SKILL_SEEN / f"{session}.json").exists():
            if ev.get("hook_event_name") == "UserPromptSubmit" and (note := skill_notice(session)):
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note}}))
    except Exception:
        pass
