"""Framelink, the Figma REST reader compose uses for every Figma read: one node or file as JSON, through its CLI, in the fleet's Figma pace."""
import re, subprocess
from pathlib import Path
import fc_pace, fc_steps, fc_figmaplan


FRAMELINK_SETUP = (f"Framelink is not set up. Put a Figma personal access token in {fc_steps.FRAMELINK_ENV} as one line `FIGMA_API_KEY=<token>` (chmod 600), then run\n"
                   f"  {fc_steps.FRAMELINK_ADD}\n"
                   "The token never goes on a command line; only --env names the file.")

FRAMELINK_TIMEOUT = 300  # seconds for one fetch; a node Framelink read in over 120s has been seen to finish

def framelink_fetch(key, node=None, depth=None, env_file=None):
    """Framelink's JSON for a Figma file or node via the REST API, which does not count against the MCP read cap. The fetch takes
    its turn in the fleet's Figma pace (fc_figmaplan.figma_pace), and a fetch Figma refused for its rate holds the fleet a minute.
    Never echoes the token."""
    env_file = Path(env_file or fc_steps.FRAMELINK_ENV)
    if not env_file.exists():
        raise RuntimeError(FRAMELINK_SETUP)
    fc_pace.wait("figma", fc_figmaplan.figma_pace, fc_figmaplan.FIGMA_CALL_SECONDS, "Figma")
    cmd = ["npx", "-y", "figma-developer-mcp", "fetch", "--env", str(env_file), "--no-telemetry", "--file-key", key, "--format", "json"]
    if node:
        cmd += ["--node-id", node]
    if depth is not None:
        cmd += ["--depth", str(depth)]
    for attempt in (1, 2):  # a fetch that outlasts the timeout is tried once more before the task stops
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=FRAMELINK_TIMEOUT)
            break
        except subprocess.TimeoutExpired as e:
            if attempt == 2:
                raise RuntimeError(f"framelink fetch failed: {e}")
        except OSError as e:
            raise RuntimeError(f"framelink fetch failed: {e}")
    secrets = [v.strip().strip("\"'") for line in env_file.read_text().splitlines() if "=" in line and not line.startswith("#")
               for v in [line.split("=", 1)[1]] if len(v.strip()) > 3]
    if r.returncode or not r.stdout.strip():
        err = (r.stderr or r.stdout or "no output").strip()[-500:]
        for sec in secrets:
            err = err.replace(sec, "***")
        if re.search(r"\b429\b|too many requests|rate limit", err, re.I):
            fc_pace.hold("figma", 60)
            err += " (Figma lets calls through again in about 60s; run it again then)"
        raise RuntimeError(f"framelink fetch failed (exit {r.returncode}): {err}")
    return r.stdout

def empty_file(key):
    """True when the Figma REST API reads the file and no page has a frame yet, which Framelink's whole-file fetch cannot parse
    ("Cannot read properties of undefined (reading 'children')"); false when the file has content or REST cannot read it either."""
    try:
        pages = fc_figmaplan.figma_api(f"/files/{key}?depth=2", setup=FRAMELINK_SETUP)["document"]["children"]
    except (RuntimeError, KeyError, TypeError):
        return False
    return not any(p.get("children") for p in pages)
