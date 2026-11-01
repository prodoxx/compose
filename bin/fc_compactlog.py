import calendar, hashlib, json, os, re, statistics, time
from pathlib import Path
import fc_config, fc_core, fc_transcript

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
    line = (fc_core.scrub(json.dumps(rec, ensure_ascii=False)) + "\n").encode("utf-8")
    fd = os.open(log_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)  # one write on an O_APPEND descriptor: the compactor and the Stop hook append at once, and a long line must not interleave
    finally:
        os.close(fd)
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
        text = log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.split("\n"):  # not splitlines(): U+2028, U+0085 and friends sit unescaped inside records
        if not line:
            continue
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


# ---------- the weekly measurement: every Claude Code transcript on this Mac against the log ----------
WHERE_WERE_WE = ("focus", "where were we", "how are we", "status", "what next", "something is wrong")
MATCH_WINDOW = 600  # seconds between a transcript boundary and the log record written just after it
TRIGGER_ORDER = ("compose", "ceiling", "native", "manual")

def where_were_we(text):
    """Whether a prompt reads like asking where things stand: a phrase from the list in its first 200 characters, any case."""
    head = str(text)[:200].lower()
    return any(phrase in head for phrase in WHERE_WERE_WE)

def transcripts(root=None):
    """Every session transcript under the Claude Code projects folder, sorted."""
    root = root or Path.home() / ".claude" / "projects"
    return sorted(root.rglob("*.jsonl"))

def boundary_epoch(at):
    """A transcript time (`2026-11-01T10:00:00.000Z`) as epoch seconds, or None when it is not one."""
    try:
        return calendar.timegm(time.strptime(at[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None

def claim(done, path, when):
    """The nearest unused log record of a finished compaction for a boundary at `when` in this transcript, marked used; None when there is none.
    The log record is written after the boundary lands and names the transcript, a record without one matches on the time alone."""
    best = None
    for item in done:
        rec, stamp = item["rec"], item["epoch"]
        if item["used"] or abs(stamp - when) > MATCH_WINDOW or rec.get("transcript") not in (None, str(path)):
            continue
        rank = (rec.get("transcript") is None, abs(stamp - when))  # a record that names this transcript beats one that does not
        if best is None or rank < best[0]:
            best = (rank, item)
    if best is None:
        return None
    best[1]["used"] = True
    return best[1]["rec"]

def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)

def week(root, since, log):
    """The window's compactions by trigger (a log record decides `compose` or `ceiling`; else the boundary's own `manual`, else `native`),
    the median context before each trigger's, input tokens per (session, date), and (hits, total) of first prompts after a boundary that ask where things stand."""
    done = [{"rec": r, "epoch": e, "used": False} for r in log
            if r.get("kind") == "compaction" and r.get("outcome") == "done" and (e := epoch(r.get("at"))) is not None]
    counts, pre, hits, total, input_by_day = {}, {}, 0, 0, {}
    first_day = time.strftime("%Y-%m-%d", time.gmtime(since))
    for path in transcripts(root):
        rows = fc_transcript.records(path)
        for boundary in fc_transcript.compactions(rows):
            when = boundary_epoch(boundary["at"])
            if when is None or when < since:
                continue
            matched = claim(done, path, when)
            trigger = matched.get("trigger") if matched and matched.get("trigger") in ("compose", "ceiling") else None
            trigger = trigger or ("manual" if boundary["trigger"] == "manual" else "native")
            counts[trigger] = counts.get(trigger, 0) + 1
            if numeric(boundary["pre"]):
                pre.setdefault(trigger, []).append(boundary["pre"])
            first = fc_transcript.owner_prompts(rows, after=boundary["at"])[:1]
            total += 1
            hits += bool(first and where_were_we(first[0]["text"]))
        for date, tokens in fc_transcript.daily_input(rows).items():
            if date >= first_day:
                input_by_day[(path.stem, date)] = tokens
    return {"compactions": counts, "median_pre": {t: int(statistics.median(v)) for t, v in pre.items()},
            "input_per_session_day": input_by_day, "where_were_we": (hits, total)}

def cmd_compactions(a):
    """Print the window's compactions by trigger, input per session per day and the where-were-we share."""
    if a.since:
        since, label = calendar.timegm(time.strptime(a.since, "%Y-%m-%d")), f"since {a.since}"
    else:
        since, label = time.time() - a.days * 86400, f"in the last {a.days} days"
    w = week(Path.home() / ".claude" / "projects", since, read(since - MATCH_WINDOW))
    print(f"compactions {label} (where-were-we is a phrase heuristic)")
    for trigger in sorted(w["compactions"], key=lambda t: TRIGGER_ORDER.index(t) if t in TRIGGER_ORDER else len(TRIGGER_ORDER)):
        median = w["median_pre"].get(trigger)
        print(f"  {trigger}: {w['compactions'][trigger]}, median before {median if median is not None else 'unknown'}")
    for (session, date), tokens in sorted(w["input_per_session_day"].items(), key=lambda kv: (kv[0][1], kv[0][0])):
        print(f"  {date} {session[:8]}: {tokens} input tokens")
    hits, total = w["where_were_we"]
    print(f"  where were we: {hits}/{total} first prompts after a compaction")
