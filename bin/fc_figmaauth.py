"""Before a design role starts: will its session have a Figma write tool? (#493)

A session with the Figma plugin gets the plugin's own server, `plugin:figma:figma`, signed in with a token Claude Code keeps on this machine.
When that token is gone (`claude mcp list` says `Needs authentication`) the session is offered only `authenticate` and `complete_authentication`,
and the claude.ai Figma connector, signed in through the account and the same server URL, does not load beside it. A sign-in made in a running session
(`/mcp`) does not write the token a new session reads. So a design builder came up with no `use_figma` and could only report `blocked`.

`check` asks `claude mcp list` once, and when the plugin server needs a sign-in asks again with that server denied: a signed-in claude.ai connector
then shows, and the session starts with the plugin server denied (the plugin's skills stay), or else the start refuses and says how to sign in.
A pass is kept for TTL seconds in the probe cache (~/.compose/model-probe.json), a refusal never is, and a check that cannot run never blocks."""
import json, time
import fc_config, fc_core, fc_mcp

PLUGIN, CONNECTOR = "plugin:figma:figma", "claude.ai Figma"
KEY, TTL = "figma-signin", 600  # the probe-cache key; seconds a pass is trusted
DENY_PLUGIN = json.dumps({"deniedMcpServers": [{"serverName": PLUGIN}]})
_ROUTE = {}  # in this process: the answer per directory


def refusal(cwd):
    return (f"{PLUGIN} needs a sign-in, so a Figma builder would start with no `use_figma` and could only report blocked; no claude.ai Figma connector is signed in either. "
            f"Nothing was started. Sign in to the Figma plugin so new sessions see it (a sign-in made with `/mcp` inside a running session may not reach them), "
            f"or connect Figma in claude.ai; then `claude mcp list` run in {cwd} shows `{PLUGIN}` as Connected (or `{CONNECTOR}`) and the same crew start goes ahead.")


def look(cwd):
    """([servers to deny], refusal or None) from `claude mcp list`: the plugin server is fine, or a connector replaces it, or neither is.
    (None, None) when a list could not be had: not knowing."""
    lines = fc_mcp.server_lines(cwd)
    if lines is None:
        return None, None
    states = fc_mcp.server_states(lines)
    if states.get(PLUGIN) != "needs authentication" or states.get(CONNECTOR) == "connected":
        return [], None
    lines = fc_mcp.server_lines(cwd, DENY_PLUGIN)
    if not lines:  # failed, or printed nothing (as `claude mcp list --settings` did in #493): that proves no connector is missing
        return None, None
    return ([PLUGIN], None) if fc_mcp.server_states(lines).get(CONNECTOR) == "connected" else ([], refusal(cwd))


def kept():
    """The cached pass `deny` list, or None."""
    try:
        hit = json.loads(fc_config.PROBE_CACHE.read_text()).get(KEY) or {}
    except (OSError, ValueError, AttributeError):
        return None
    return hit.get("deny") if time.time() - hit.get("at", 0) < TTL and isinstance(hit.get("deny"), list) else None


def check(role, cwd):
    """([servers to deny in the session], refusal or None) for `role` starting in `cwd`."""
    if not fc_mcp.writes_figma(role):
        return [], None
    if (cwd := str(cwd)) in _ROUTE:
        return _ROUTE[cwd], None
    deny = kept()
    if deny is None:
        deny, why = look(cwd)
        if why or deny is None:  # a refusal and a check that did not run are never kept
            return [], why
        try:
            cache = json.loads(fc_config.PROBE_CACHE.read_text())
        except (OSError, ValueError):
            cache = {}
        cache[KEY] = {"at": time.time(), "deny": deny}
        fc_config.PROBE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        fc_core.atomic_write(fc_config.PROBE_CACHE, json.dumps(cache, indent=2))
    _ROUTE[cwd] = deny
    return deny, None
