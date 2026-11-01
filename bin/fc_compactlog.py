import calendar, hashlib, json, re, time
import fc_config, fc_core

# The on-disk side of judged compaction: an append-only measurement log, the prompts compose typed into a pane, one in-flight marker per
# pane, and the judge's per-pane state. Everything lives under the config home's compact/ folder. Nothing here judges or touches a terminal.

INFLIGHT_TTL = 900  # seconds a marker holds a pane; a compaction that died must not block its pane forever
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def state_dir():
    """Where the compaction state lives; created by the first write."""
    return fc_config.CONFIG_PATH.parent / "compact"

def log_path():
    return state_dir() / "log.jsonl"

def safe(pane):
    """A pane id as a file name part."""
    return re.sub(r"[^\w-]", "-", pane)

def append(kind, **fields):
    """Add one record to the log: its kind, the UTC time and the fields, the line scrubbed of known secrets. Returns the record."""
    rec = {"kind": kind, "at": time.strftime(TIME_FORMAT, time.gmtime()), **fields}
    state_dir().mkdir(parents=True, exist_ok=True)
    with open(log_path(), "a") as f:
        f.write(fc_core.scrub(json.dumps(rec, ensure_ascii=False)) + "\n")
    return rec

def epoch(at):
    """A log time as epoch seconds, or None when it is not one."""
    try:
        return calendar.timegm(time.strptime(at, TIME_FORMAT))
    except (TypeError, ValueError):
        return None

def read(since=None):
    """The log's records in order, or those at or after `since` (epoch seconds). A line that is not a record is skipped."""
    try:
        lines = log_path().read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if since is not None and not (epoch(rec.get("at")) or 0) >= since:
            continue
        out.append(rec)
    return out

def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def typed(pane, text):
    """Record a prompt compose typed into a pane, so the transcript's copy of it is not taken for the product owner's."""
    append("typed", pane=pane, text=text[:200], sha=digest(text))

def was_typed(pane, text, since):
    """Whether compose typed this text into this pane at or after `since` (an ISO UTC time)."""
    sha = digest(text)
    return any(r.get("kind") == "typed" and r.get("pane") == pane and r.get("sha") == sha and str(r.get("at", "")) >= since for r in read())


# ---------- in-flight markers: one compaction per pane ----------
def marker_path(pane):
    return state_dir() / f"inflight-{safe(pane)}.json"

def inflight(pane):
    """The pane's marker (pane, pid, step, mode, at), or None when there is none, it is unreadable, or it is older than INFLIGHT_TTL."""
    try:
        marker = json.loads(marker_path(pane).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict) or not isinstance(marker.get("at"), (int, float)) or time.time() - marker["at"] > INFLIGHT_TTL:
        return None
    return marker

def set_inflight(pane, step, pid, mode):
    state_dir().mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(marker_path(pane), json.dumps({"pane": pane, "pid": pid, "step": step, "mode": mode, "at": time.time()}))

def clear_inflight(pane):
    marker_path(pane).unlink(missing_ok=True)


# ---------- per-pane judge state ----------
def state_path(pane):
    return state_dir() / f"pane-{safe(pane)}.json"

def pane_state(pane):
    """What the judge remembers about a pane (baseline, last_asked_at, failures, ...); {} when nothing readable."""
    try:
        state = json.loads(state_path(pane).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}

def save_pane_state(pane, state):
    state_dir().mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(state_path(pane), json.dumps(state))
