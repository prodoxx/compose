"""The Claude Code permission rules compose's roles need, proposed by setup and checked by doctor; written to the user's settings only on their yes."""
import json
from pathlib import Path
import fc_core, fc_prompt

# The narrowest rules for the Bash calls a role makes itself (a call compose makes inside its own process never reaches Claude Code).
# Never here: git push, force-push, `gh pr merge` (a toolsmith fix merged into compose is in 0h's never-set), `clean --delete-unmerged`, `clean --delete-remote`,
# `clean --abandon`, `crew stop --force`. The team lead's and planner's file-edit denial is `compose guard`, not a permission rule: it stays.
RULES = (
    ("Bash(gh pr view *)", "read a pull request's state and fields before acting on it"),
    ("Bash(compose land *)", "merge the project's submitted stack (`--yes` merges; without it the command only prints its plan)"),
    ("Bash(git branch -f compose/*)", "move a compose/* branch, e.g. drop a fixer commit from a layer (local only)"),
)
NAME = "Claude Code permissions"

def settings_path():
    return Path.home() / ".claude" / "settings.json"

def read_settings():
    """The user's Claude Code settings as a dict (empty when the file is absent), or None when it is not a JSON object."""
    try:
        data = json.loads(settings_path().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None

def missing():
    """The rules of RULES that permissions.allow does not hold yet, in order."""
    allow = ((read_settings() or {}).get("permissions") or {}).get("allow") or []
    return [rule for rule, _ in RULES if rule not in allow]

def check(checkout):
    """(ok, detail) for the setup row: detail names the rules still missing."""
    left = missing()
    return (not left, ", ".join(left))

def write():
    """Add the missing rules to permissions.allow, keeping every other setting; the line saying what happened. A settings file
    that is not valid JSON is left alone."""
    data = read_settings()
    if data is None:
        return f"left {fc_core.tilde(str(settings_path()))} alone: it is not valid JSON, add the rules by hand"
    left = missing()
    if left:
        perms = data.setdefault("permissions", {})
        perms["allow"] = [*(perms.get("allow") or []), *left]
        settings_path().parent.mkdir(parents=True, exist_ok=True)
        fc_core.atomic_write(settings_path(), json.dumps(data, indent=2) + "\n", keep_secrets=True)  # the file may hold the user's own tokens
    return f"added {len(left)} rule{'s' * (len(left) != 1)} to {fc_core.tilde(str(settings_path()))}"

def offer(yes, dry=False):
    """Show the missing rules and what each is for; with a yes from the user write them (never under --yes or without a terminal,
    where nobody answered, and never in a dry run). The Summary lines it adds."""
    left = missing()
    if not left:
        fc_prompt.step(NAME, "ok")
        return []
    why = dict(RULES)
    fc_prompt.note(NAME, [f"{rule}: {why[rule]}" for rule in left]
                   + ["These stay asking: git push, force-push, gh pr merge, clean --delete-unmerged, clean --delete-remote, clean --abandon, crew stop --force."])
    where = fc_core.tilde(str(settings_path()))
    if dry:
        return [f"would add {len(left)} permission rule{'s' * (len(left) != 1)} to {where}"]
    if yes:
        return [f"{NAME} not added: they need your yes, run compose setup in a terminal"]
    if not fc_prompt.confirm(f"Add these {len(left)} rules to {where}?", True):
        return [f"{NAME} not added; compose doctor warns until they are"]
    return [write()]
