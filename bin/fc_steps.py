"""The setup steps table: one row per step, read by doctor and the setup flow.
A row is (name, check, install, level, needs_claude, mode); level is required, default, or designer; mode is run, interactive,
manual (never run), write (compose's own writer; install is `compose setup`), key, or perms (rules for the user's Claude Code settings,
written by fc_perms.offer only on the user's yes)."""
import json, re, shutil
from pathlib import Path
import fc_core, fc_auth, fc_config, fc_worktree, fc_checks, fc_harness, fc_pi, fc_keys, fc_perms


FRAMELINK_ENV = Path.home() / ".config" / "figma-context-mcp" / ".env"
FRAMELINK_ADD = f"claude mcp add framelink-figma --scope user -- npx -y figma-developer-mcp --stdio --env {FRAMELINK_ENV} --no-telemetry"
HERDR_MIN = "0.9.3"  # `herdr agent start` and `agent prompt` (herdr_start_crew, pi_herdr_turn): the version they were proven on
TREEHOUSE_MIN = "3.1.2"  # `treehouse get --lease --json --lease-holder` (slot_on): the version it was proven on
FLOORS = {"herdr": HERDR_MIN, "treehouse": TREEHOUSE_MIN}

def user_mcp_servers():
    """The MCP servers Claude Code has at user scope, read from ~/.claude.json without starting any (moved from fc_init)."""
    try:
        return json.loads((Path.home() / ".claude.json").read_text()).get("mcpServers") or {}
    except (OSError, ValueError):
        return {}

def version_of(tool):
    """`<tool> --version` as a tuple of ints, or None (moved from fc_init)."""
    p = fc_core.sh([tool, "--version"], None, timeout=20, check=False)
    m = re.search(r"\d+(?:\.\d+)+", p.stdout + p.stderr)
    return tuple(map(int, m.group().split("."))) if m else None

def owned_link(name, checkout):
    """(ok, detail) for one of the links compose owns (fc_checks.owned_links): ok when the link resolves to its place in this or any compose checkout (a directory holding bin/compose.py and SKILL.md); a dangling link names link and raw target."""
    link, target = {n: (l, t) for n, l, t in fc_checks.owned_links(checkout)}[name]
    state, seen = fc_checks.link_state(link)
    if state == "dangling":
        return False, f"dangling link {link} -> {seen}"
    if state == "missing":
        return False, f"{fc_core.tilde(link)} is missing"
    if state == "ok":
        rel = Path(target).relative_to(Path(checkout))
        seen = Path(seen)
        if any((c / "bin/compose.py").exists() and (c / "SKILL.md").exists() and c / rel == seen for c in (seen, *seen.parents)):
            return True, ""
    return False, f"{link} is not a link to {target}"

def key_ok(kind):
    """(ok, detail) for one key row: a non-empty key; never a key value."""
    status = fc_keys.key_status(fc_auth.read_auth(fc_config.AUTH_PATH), fc_config.WORKSPACE, FRAMELINK_ENV)
    return (True, "") if status[kind] else (False, f"no {kind} key")

def herdr_integration(agent):
    """(ok, detail) for one herdr integration: `herdr integration status` shows it current."""
    try:
        p = fc_core.sh(["herdr", "integration", "status"], None, timeout=20, check=False)
    except FileNotFoundError:
        return False, ""
    line = next((l for l in p.stdout.splitlines() if l.startswith(f"{agent}:")), "")
    return ("current" in line, "" if not line or "current" in line else line[len(agent) + 1:].strip())

def check(row, checkout):
    """(ok, detail) for one row: detail names a floor miss, a dangling link, or a key's absence, never a key value."""
    how = row[1]
    if callable(how):
        return how(checkout)
    if isinstance(how, (list, tuple)):
        try:
            return fc_core.sh(list(how), None, timeout=20, check=False).returncode == 0, ""
        except FileNotFoundError:
            return False, ""
    if "/" in how:  # a file under home, or a glob there
        return (any(Path.home().glob(how)) if "*" in how else (Path.home() / how).exists()), ""
    if not shutil.which(how):
        return False, ""
    floor = FLOORS.get(how)
    if floor:
        v = version_of(how)
        if v and v < tuple(map(int, floor.split("."))):
            return False, f"{'.'.join(map(str, v))} is older than {floor}"
    return True, ""

CLAUDE_ONLY = ("claude", "herdr skill", "superpowers", "compose skill link")

def other_harness(checkout):
    """The first installed harness whose skill link points at this checkout (e.g. "codex"), else None."""
    for key in fc_harness.installed():
        if owned_link(f"{fc_harness.HARNESSES[key]['name']} skill link", checkout)[0]:
            return key
    return None

def required(row, other):
    """Whether the row must be set up: level required, unless it feeds only Claude Code and another harness is set up."""
    return row[3] == "required" and not (other and row[0] in CLAUDE_ONLY)

STEPS = (  # the rows, in setup order
    ("Homebrew", "brew", '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"', "default", False, "interactive"),
    ("git", "git", "xcode-select --install", "required", False, "manual"),
    ("npx", "npx", "brew install node", "required", False, "run"),
    ("gh", "gh", "brew install gh", "required", False, "run"),
    ("gh auth", lambda co: (fc_checks.gh_authed(), ""), "gh auth login", "required", False, "interactive"),
    ("gh stack", ["gh", "stack", "--version"], "gh extension install github/gh-stack", "required", False, "run"),
    ("claude", "claude", "curl -fsSL https://claude.ai/install.sh | bash", "required", False, "manual"),
    ("herdr", "herdr", "curl -fsSL https://herdr.dev/install.sh | sh", "required", False, "run"),
    ("herdr skill", ".claude/skills/herdr/SKILL.md", "mkdir -p ~/.claude/skills/herdr && herdr --skill > ~/.claude/skills/herdr/SKILL.md", "required", False, "run"),
    ("superpowers", ".claude/plugins/cache/*/superpowers/*/skills/brainstorming/SKILL.md", "claude plugin install superpowers@claude-plugins-official", "required", True, "run"),
    ("pi", "pi", "npm install -g --ignore-scripts @earendil-works/pi-coding-agent", "required", False, "run"),
    ("pi openrouter-fast provider", lambda co: (fc_pi.provider_declared(fc_pi.PI_MODELS), ""), "compose setup", "required", False, "write"),
    ("compose CLI link", lambda co: owned_link("compose CLI link", co), "compose setup", "required", False, "write"),
    ("compose skill link", lambda co: owned_link("compose skill link", co), "compose setup", "required", False, "write"),
    *[row for k in fc_harness.rows() for r in (fc_harness.HARNESSES[k],) for row in (
        (f"{r['name']} skill link",
         lambda co, r=r: owned_link(f"{r['name']} skill link", co) if shutil.which(r["exe"]) else (True, ""),
         "compose setup", "default", False, "write"),
        (f"{r['name']} commands", lambda co, k=k: fc_checks.commands_ok(k, co), "compose setup", "default", False, "write"),
        *(((f"{r['name']} hooks", lambda co, k=k: fc_checks.hooks_ok(k, co), "compose setup", "default", False, "write"),) if r.get("hooks") else ()))],
    ("~/.local/bin on PATH", lambda co: (fc_checks.local_bin_on_path(), ""), 'put `export PATH="$HOME/.local/bin:$PATH"` in your shell profile', "required", False, "manual"),
    ("OpenRouter key", lambda co: key_ok("openrouter"), "get one at openrouter.ai/keys; compose setup stores it", "required", False, "key"),
    ("TypeSafe key", lambda co: key_ok("typesafe"), "get one from TypeSafe; compose setup stores it", "required", False, "key"),
    ("Linear key", lambda co: key_ok("linear"), "get one at linear.app/settings/api; compose setup stores it", "required", False, "key"),
    ("uv", "uv", "curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh", "default", False, "run"),
    ("jev", lambda co: (fc_checks.jev_synced(co), ""), f"uv sync --locked --project {fc_config.JEV_DIR}", "default", False, "run"),
    ("trail", "trail", "npm i -g @trailhq/trail-code", "default", False, "run"),
    ("treehouse", "treehouse", fc_worktree.TREEHOUSE_INSTALL, "default", False, "run"),
    ("bp link", lambda co: owned_link("bp link", co), "compose setup", "default", False, "write"),
    ("Chromium", lambda co: (fc_checks.chromium_installed(), ""), "npx playwright install chromium", "default", False, "run"),
    ("herdr pi integration", lambda co: herdr_integration("pi"), "herdr integration install pi", "default", False, "run"),
    ("herdr claude integration", lambda co: herdr_integration("claude"), "herdr integration install claude", "default", True, "run"),
    ("TEXT_MODEL lines", lambda co: (fc_pi.text_model_set(fc_config.ENV_FILE), ""), "compose setup", "default", False, "write"),
    ("git rerere", lambda co: (fc_checks.rerere_set(), ""), "git config --global rerere.enabled true", "default", False, "run"),
    ("pi auth.json override", lambda co: (not fc_pi.pi_auth_overrides(fc_pi.PI_AUTH), ""), f"remove the openrouter and openrouter-fast entries from {fc_pi.PI_AUTH}: pi reads those entries before the workspace key, so they override the workspace key", "default", False, "manual"),
    (fc_perms.NAME, fc_perms.check, "compose setup", "default", False, "perms"),
    ("framelink-figma", lambda co: ("framelink-figma" in user_mcp_servers(), ""), FRAMELINK_ADD, "designer", True, "run"),
    ("framelink-figma token", lambda co: key_ok("figma"), "put a Figma personal access token in ~/.config/figma-context-mcp/.env as one line FIGMA_API_KEY=<token>, chmod 600", "designer", False, "key"),
)
