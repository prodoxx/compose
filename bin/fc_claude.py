import json, os, tempfile
from pathlib import Path
import fc_mcp, fc_core, fc_proc, fc_readguard


def is_claude(model):
    return model == "claude" or str(model).startswith("claude:")

def claude_id(model):
    """The --model value for a Claude model id, or None for bare `claude` (Claude Code's own default)."""
    return (model.split(":", 1)[1] or None) if str(model).startswith("claude:") else None


def headless_env():
    """The environment for a Claude Code process the CLI runs headless: no COMPOSE_* but a scratch COMPOSE_CONFIG, above all
    not the herdr tab's role, project, and worktree, so the plugin's hooks treat it as no one's session (a team lead's Stop hook
    would park a watch in it, and its guard would block an implementer's edits). No secret vars either: its tools run project code.
    No ANTHROPIC_* either (an API key, an auth token, a base URL pointed at OpenRouter, a model override): it bills to the subscription."""
    return {k: v for k, v in fc_core.clean_env().items()
            if (not k.startswith("COMPOSE_") or k == "COMPOSE_CONFIG") and not k.startswith("ANTHROPIC_")}

# Every Claude teammate compacts at 150k: each turn re-sends the whole session, and `reenter` points a compacted one back at its brief.
# It goes in the `--settings` env because CLAUDE_CODE_AUTO_COMPACT_WINDOW outranks the --autocompact flag, so a user's own value would win.
AUTOCOMPACT_WINDOW = "150000"
NO_ATTRIBUTION = json.dumps({"attribution": {"commit": "", "pr": ""}})  # `--settings` outranks the user settings: no Co-Authored-By trailer or "Generated with" line on any commit a Claude session makes
def crew_settings(role, cwd):
    """The `--settings` JSON of a teammate session in `cwd`."""
    enabled = {}
    try:
        enabled = (json.loads((Path.home() / ".claude" / "settings.json").read_text()).get("enabledPlugins") or {})
    except (OSError, ValueError):
        pass
    keep = fc_mcp.keeps_skills(role)
    return json.dumps({"attribution": {"commit": "", "pr": ""}, "env": {"ENABLE_TOOL_SEARCH": "true", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": AUTOCOMPACT_WINDOW},
                       "enabledPlugins": {k: False for k in enabled if not k.startswith("compose@") and not (keep and k.startswith("figma@"))}})

REPO_INSTRUCTIONS = ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")
def repo_instructions(cwd):
    """The repo's own instruction files at `cwd`, for a session started in --restricted, which drops every CLAUDE.md with the
    user's: each one that exists, under its path, as one system-prompt addition, or None. `@` imports are not followed."""
    found = [(name, (Path(cwd) / name).read_text(errors="replace").strip()) for name in REPO_INSTRUCTIONS if (Path(cwd) / name).is_file()]
    return "\n\n".join(f"Contents of {name} (this repo's instructions):\n\n{text}" for name, text in found if text) or None

def claude_once(cwd, prompt, timeout=900, model=None, tools="Read,Edit,Write,Bash,Grep,Glob", effort=None, keep=1500):
    """One-shot baseline through Claude Code on the subscription, with only `tools`. Marginal cost $0; the CLI's own cost figure is recorded for reference.
    It starts lean, without the user's CLAUDE.md, skills, plugins, hooks, and MCP servers, which cost ~200K input tokens a call;
    the repo's own CLAUDE.md and AGENTS.md go into its system prompt instead."""
    # --model is explicit because ~/.claude/settings.json may carry a launcher-injected default that headless runs cannot use.
    guard = fc_readguard.guard_file(cwd)
    settings = NO_ATTRIBUTION
    if guard.exists():  # --restricted fires --settings hooks, --safe-mode does not; a PreToolUse hook blocks only on exit 2 or a deny
        settings = json.dumps({**json.loads(NO_ATTRIBUTION), "hooks": {"PreToolUse": [
            {"matcher": "Read|Grep|Glob|Bash", "hooks": [{"type": "command", "command": fc_readguard.command(guard) + " || exit 2"}]}]}})
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model or os.environ.get("COMPOSE_CLAUDE_MODEL", "claude-sonnet-5-5"),
           "--settings", settings, "--permission-mode", "acceptEdits", "--tools", tools, "--allowedTools", tools, "--restricted", "--strict-mcp-config"]
    if effort:
        cmd += ["--effort", effort]
    if own := repo_instructions(cwd):
        cmd += ["--append-system-prompt", own]
    p = fc_proc.run(cmd, cwd=cwd, env=headless_env(), timeout=timeout)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        out = {}
    if out.get("is_error"):
        raise RuntimeError(f"claude -p error: {str(out.get('result', ''))[:300]}")
    usage = out.get("usage", {})
    tokens_in = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    fc_core.LEDGER.add("claude-subscription", tokens_in, usage.get("output_tokens", 0), 0.0)
    return None, str(out.get("result", ""))[-keep:], out.get("num_turns", 0), {"tokens_in": tokens_in, "tokens_out": usage.get("output_tokens", 0),
                                                                                "cost": 0.0, "reference_cost": out.get("total_cost_usd")}

def claude_json(system, user, model, timeout=300, effort=None):
    """The reply text of one tool-less Claude Code call on the subscription, for a caller that wants JSON back: `system` replaces
    Claude Code's own system prompt and `user` goes in on stdin. --safe-mode leaves out the user's CLAUDE.md, skills, plugins, hooks
    and MCP servers, and an empty folder means no repo's instructions are read either."""
    with tempfile.TemporaryDirectory() as cwd:
        cmd = ["claude", "-p", "--safe-mode", "--tools", "", "--system-prompt", system, "--output-format", "json", "--no-session-persistence",
               "--model", claude_id(model) or os.environ.get("COMPOSE_CLAUDE_MODEL", "claude-sonnet-5-5"), "--settings", NO_ATTRIBUTION]
        if effort:
            cmd += ["--effort", effort]
        p = fc_proc.run(cmd, cwd=cwd, env=headless_env(), timeout=timeout, input=user)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        out = {}
    if not out or out.get("is_error"):
        raise RuntimeError(f"claude -p error: {str(out.get('result') or p.stderr or p.stdout)[:300]}")
    usage = out.get("usage", {})
    tokens_in = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    fc_core.LEDGER.add("claude-subscription", tokens_in, usage.get("output_tokens", 0), 0.0)
    return str(out.get("result") or "")

def claude_trusted(path):
    """Whether Claude Code has accepted its folder-trust dialog for `path` (None when it cannot tell), by Claude Code's own rule:
    in a repo, the repo's main checkout or a folder from `path` up to the repo's top accepted it, and a folder above the repo never
    counts, so a repo nested in a trusted folder still asks; outside git, `path` or any parent accepted it.
    An untrusted folder parks a new session at the trust prompt, so `crew start` says so instead of starting one."""
    try:
        projects = json.loads((Path.home() / ".claude.json").read_text()).get("projects") or {}
    except (OSError, json.JSONDecodeError):
        return None
    c = fc_core.checkout(path)
    top, main = (c.top, c.main) if c else (None, None)
    folders = [p for p in (path, *path.parents) if not top or p.is_relative_to(top)] + ([main] if main else [])
    return any((projects.get(str(p)) or {}).get("hasTrustDialogAccepted") for p in folders)

def untrusted(path):
    return (f"Claude Code has never trusted {path}, so a session there would wait at its trust prompt. Run `claude` in "
            f"{path} once and accept the prompt (the product owner decides that), then start this teammate again.")
