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


def servers_for(role):
    return DESIGNER if reads_figma(role) else ROLE_MCP.get(role, ())


def keeps_skills(role):
    return reads_figma(role)


def installed_servers(cwd):
    try:
        p = subprocess.run(["claude", "mcp", "list"], cwd=str(cwd), capture_output=True, text=True, check=False)
    except OSError:
        return []
    if p.returncode != 0:
        return []
    out = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            if out:
                break
            continue
        if ":" not in line:
            continue
        name = line.split(": ", 1)[0]
        out.append(re.sub(r"[^A-Za-z0-9_-]", "_", name))
    return out



def disallowed(role, servers):
    allowed = set()
    for s in servers_for(role):
        allowed.update(COVERS.get(s, (s,)))
    return ["mcp__%s__*" % s for s in servers if s not in allowed]
