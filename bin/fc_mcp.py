"""Per-role MCP server policy."""
import re
import subprocess

DESIGNER = ("figma", "framelink-figma")

ROLE_MCP = {
    "toolsmith": (),
    "implementer": (),
    "designer": DESIGNER,
    "design-review": DESIGNER,
}

COVERS = {
    "linear": ("claude_ai_Linear",),
    "figma": ("claude_ai_Figma", "plugin_figma_figma"),
    "framelink-figma": ("framelink-figma",),
}


def reads_figma(role):
    """A role that works with Figma: the designer, its builders and reviewer, and a helper started to read a file (`--role figma`)."""
    return bool(re.fullmatch(r"design-\d+", role)) or role in ("designer", "design-review", "figma")


def writes_figma(role):
    """A role that writes to Figma through the official connector: the designer and its `design-NNN` builders (the reviewer and a `figma` helper read through Framelink)."""
    return bool(re.fullmatch(r"design-\d+", role)) or role == "designer"


def servers_for(role):
    return DESIGNER if reads_figma(role) else ROLE_MCP.get(role, ())


def keeps_skills(role):
    return reads_figma(role)

def keeps_commands(role):
    """A Claude teammate that starts with Claude Code's skills and commands: the design roles, which need the Figma skills, and the team lead,
    into which the compactor types `/compact`. `--disable-slash-commands` hides the built-in commands too: a session started with it answers
    `Unknown command: /compact` and reads the focus as a prompt, so the lead would never compact except by Claude's own auto-compaction."""
    return keeps_skills(role) or role == "orchestrator"


def server_lines(cwd, settings=None):
    """The server lines `claude mcp list` prints in `cwd` (under `claude --settings <settings>` when given), or None when it cannot run
    (no claude, a failure, a hang): a caller treats that as not knowing, never as no servers."""
    try:
        p = subprocess.run(["claude", *(["--settings", settings] if settings else []), "mcp", "list"], cwd=str(cwd), capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    out = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            if out:
                break
            continue
        if ":" in line:
            out.append(line)
    return out


def installed_servers(cwd):
    return [re.sub(r"[^A-Za-z0-9_-]", "_", line.split(": ", 1)[0]) for line in server_lines(cwd) or []]


def server_states(lines):
    """{server name: `connected`, `needs authentication`, or `other`} from `server_lines`."""
    states = {}
    for line in lines:
        name, _, rest = line.partition(": ")
        status = rest.rsplit(" - ", 1)[-1].lower()
        states[name] = "needs authentication" if "needs authentication" in status else "connected" if "connected" in status and "failed" not in status else "other"
    return states


def disallowed(role, servers):
    allowed = set()
    for s in servers_for(role):
        allowed.update(COVERS.get(s, (s,)))
    return ["mcp__%s__*" % s for s in servers if s not in allowed]
