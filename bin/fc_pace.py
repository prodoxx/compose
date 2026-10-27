"""One pace for every compose process on this machine that calls a shared API (Linear, Figma): each call claims its turn from a
state file per service, so the fleet's calls together stay under the service's limit, and a limit one process runs into holds
every other until the service lets calls through again. The state lives beside the config in use (`pace/` under its directory),
so a test's temp config never paces against the user's real calls."""
import contextlib, fcntl, json, os, sys, time
from pathlib import Path
import fc_core, fc_config

LONG_WAIT = 10  # a wait of this many seconds or more is said on stderr, so no caller waits silently

def pace_dir():
    return Path(os.environ.get("COMPOSE_CONFIG") or fc_config.CONFIG_PATH).parent / "pace"

@contextlib.contextmanager
def state(service):
    """The service's state (a dict), held under a cross-process lock for the block, and written back after it."""
    d = pace_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{service}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = d / f"{service}.json"
        try:
            s = json.loads(path.read_text())
        except (OSError, ValueError):
            s = {}
        s = s if isinstance(s, dict) else {}
        yield s
        fc_core.atomic_write(path, json.dumps(s))

def claim(service, policy, limit=None):
    """Seconds until the next call to `service` may run, that turn claimed. policy(state, now) gives (not_before, spacing): the
    earliest moment any call may run, and the gap kept after this one. A turn more than `limit` seconds away is not claimed,
    and its distance is returned, so the caller can give up without holding a slot it will never use."""
    with state(service) as s:
        now = time.time()
        not_before, spacing = policy(s, now)
        slot = max(now, s.get("next_at") or 0, s.get("hold_until") or 0, not_before)
        if limit is not None and slot - now > limit:
            return slot - now
        s["next_at"] = slot + spacing
        return slot - now

def hold(service, seconds):
    """No call to `service` for `seconds`: what the service said when it refused one. The latest word from the service wins."""
    with state(service) as s:
        s["hold_until"] = time.time() + max(0, seconds)

def learn(service, cost=None, **facts):
    """Keep what a reply said about the budget (a limit, what is left of it, when it resets) for the next claim; `cost` is this
    call's price, kept as a running average."""
    with state(service) as s:
        s.update({k: v for k, v in facts.items() if v is not None})
        if cost is not None:
            s["cost"] = cost if s.get("cost") is None else 0.8 * s["cost"] + 0.2 * cost

def about(seconds):
    """A wait in round words: 45s, 3m, 2h, 3d."""
    seconds = max(0, seconds)
    if seconds < 90:
        return f"{round(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    if seconds < 129600:
        return f"{round(seconds / 3600)}h"
    return f"{round(seconds / 86400)}d"

def retry_after(headers, default):
    """The seconds a reply's Retry-After header asks for, else `default`."""
    try:
        return max(1, float((headers or {}).get("Retry-After") or default))
    except (TypeError, ValueError):
        return default

def wait(service, policy, limit, who):
    """Claim the next turn at `service` and sleep until it; a long wait is said on stderr. A turn past `limit` seconds away is not
    taken: it raises, naming when the service lets calls through again. COMPOSE_NO_PACE=1 (the test suite) switches the pace off."""
    if os.environ.get("COMPOSE_NO_PACE"):
        return 0
    pause = claim(service, policy, limit)
    if limit is not None and pause > limit:
        raise RuntimeError(f"{who} lets calls through again in about {about(pause)}; run it again then")
    if pause >= LONG_WAIT:
        print(f"{who}: waiting {about(pause)} for a turn (every compose process paces its {who} calls to stay under the limit)", file=sys.stderr)
    if pause > 0:
        time.sleep(pause)
    return pause
