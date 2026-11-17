"""The waiting list: what waits on the product owner, one file per project (results/waiting.json). An item is opened under a key
(one open item per key), may be seen, and is closed by what settled it; its id is never reused. Every write holds a lock on
results/waiting.lock. Imports only lower modules."""

import fcntl, json, re, time
from contextlib import contextmanager

import fc_core, fc_tasks, fc_herdr, fc_fleet, fc_ask

KINDS = ("answer", "go", "attend", "confirm", "review")
STOPS = ("answer", "go", "attend")  # the kinds that hold work until the owner acts
LINE_MAX = 120

def _path(proj):
    """The waiting list file of a project."""
    return proj / "results" / "waiting.json"

def _empty():
    """A store with no items."""
    return {"next": 1, "items": []}

def load(proj):
    """results/waiting.json as {"next": int, "items": [item]}; the empty store when the file is missing, unreadable or not a store
    (a dict with an int `next` and a list `items` of dicts that each have `id` and `key`). A read never writes."""
    try:
        data = json.loads(_path(proj).read_text())
    except (OSError, ValueError):
        return _empty()
    ok = isinstance(data, dict) and type(data.get("next")) is int and isinstance(data.get("items"), list)
    if not ok or not all(isinstance(i, dict) and "id" in i and "key" in i for i in data["items"]):
        return _empty()
    return data

@contextmanager
def _locked(proj):
    """Hold the project's waiting lock (creating results/ when it is not there yet) for one read-modify-write."""
    (proj / "results").mkdir(parents=True, exist_ok=True)
    with open(proj / "results" / "waiting.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield

def _save(proj, data):
    """Write the store atomically."""
    fc_core.atomic_write(_path(proj), json.dumps(data, indent=2) + "\n")

def _stamp():
    """The time as items record it."""
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _one_line(text):
    """The text with its whitespace collapsed and cut to LINE_MAX characters (117 and `...`)."""
    text = " ".join(str(text).split())
    return text if len(text) <= LINE_MAX else text[:LINE_MAX - 3] + "..."

def _is_open(item):
    """True when the item has not been closed."""
    return not item.get("closed")

def open_item(proj, kind, key, line, recommendation="", ids=()):
    """The open item with that key when one exists, else a new open item with the next id (W<n>)."""
    with _locked(proj):
        data = load(proj)
        for item in data["items"]:
            if item["key"] == key and _is_open(item):
                return item
        item = {"id": f"W{data['next']}", "key": key, "kind": kind, "line": _one_line(line), "recommendation": _one_line(recommendation),
                "ids": [str(i) for i in ids], "stops": kind in STOPS, "opened": _stamp(), "seen": None, "closed": None, "closed_by": None}
        data["items"].append(item)
        data["next"] += 1
        _save(proj, data)
        return item

def close_item(proj, key, by):
    """Close the open item with that key, recording what closed it (`by`). True when one was closed, False when none was open."""
    with _locked(proj):
        data = load(proj)
        for item in data["items"]:
            if item["key"] == key and _is_open(item):
                item["closed"], item["closed_by"] = _stamp(), by
                _save(proj, data)
                return True
    return False

def mark_seen(proj, wid):
    """Stamp the item as seen (it stays open) and return it; None when there is no such item."""
    with _locked(proj):
        data = load(proj)
        for item in data["items"]:
            if item["id"] == wid:
                item["seen"] = item.get("seen") or _stamp()
                _save(proj, data)
                return item
    return None

def _order(item):
    """The sort key of an open item: stoppers first, then the oldest opened, then the lowest id as a number."""
    num = str(item["id"])[1:]
    return (not item.get("stops"), item.get("opened") or "", int(num) if num.isdigit() else 0)

def open_items(proj):
    """The open items, the ones that stop work first, then the oldest opened, then the lowest id."""
    return sorted((i for i in load(proj)["items"] if _is_open(i)), key=_order)

def answered(proj, ids):
    """Take `ids` off every open answer item; an item that had ids and has none left is closed as `decided`.
    Returns the ids of the items closed."""
    gone, closed, changed = {str(i) for i in ids}, [], False
    with _locked(proj):
        data = load(proj)
        for item in data["items"]:
            have = item.get("ids")
            if item.get("kind") != "answer" or not _is_open(item) or not have or not gone.intersection(have):
                continue
            item["ids"], changed = [i for i in have if i not in gone], True
            if not item["ids"]:
                item["closed"], item["closed_by"] = _stamp(), "decided"
                closed.append(item["id"])
        if changed:
            _save(proj, data)
    return closed

RECOMMENDATION = re.compile(r"\s*[;,.]?\s*\brecommendation:?\s+", re.I)
BLOCKED_EVENT = re.compile(r"\bagent=(\S+)\s+status=blocked\b")

def _cond(key, kind, line, recommendation="", ids=()):
    """One thing that waits, as `conditions` returns it."""
    return {"key": key, "kind": kind, "line": line, "recommendation": recommendation, "ids": list(ids)}

def _last_line(path):
    """The last non-empty line of a file parsed as JSON (an object); None when the file has none. Raises OSError or ValueError when it cannot be read or parsed."""
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    if not lines:
        return None
    data = json.loads(lines[-1])
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the last line is not an object")
    return data

def _reaches_manager(crew, name, lead):
    """True when the team lead wrote this status file, or its crew record says it reports to the manager (a record that is missing or unreadable names nobody)."""
    if name == lead:
        return True
    try:
        record = json.loads((crew / f"{name}.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("reports_to") == "manager"

def _report_conditions(proj):
    """The answer and attend conditions: the newest report of each teammate that reaches the product owner, when it is a needs-user or a blocked."""
    crew, lead, out = proj / "results" / "crew", fc_herdr.agent_name(proj.name, "orchestrator"), []
    for f in sorted(crew.glob("*.status")):
        name = f.name[:-len(".status")]
        if not _reaches_manager(crew, name, lead):
            continue
        last = _last_line(f)
        state = (last or {}).get("state")
        if state not in ("needs-user", "blocked"):
            continue
        message = str(last.get("message") or "")
        parts = RECOMMENDATION.split(message, maxsplit=1)
        line, recommendation = (parts[0], parts[1]) if len(parts) == 2 else (message, "")
        ids = fc_ask.question_ids(message) if state == "needs-user" else ()
        out.append(_cond(f"report:{name}:{last.get('at') or ''}", "answer" if state == "needs-user" else "attend", line, recommendation, ids))
    return out

def _json_file(path):
    """The JSON object in a file; None when the file is not there. Raises OSError or ValueError when it exists and cannot be read or parsed."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not an object")
    return data

def conditions(proj):
    """What waits on the product owner right now, read from the crew reports, results/resume.json and results/verify.json:
    [{"key", "kind", "line", "recommendation", "ids"}]. Raises OSError or ValueError when a source that exists cannot be read or parsed."""
    out = _report_conditions(proj)
    if (halt := _json_file(proj / "results" / "resume.json")) is not None:
        left = len(halt.get("remaining") or [])
        out.append(_cond(f"halt:{halt.get('halted_at')}", "attend", f"payment halt at {halt.get('provider')}: {left} task(s) left", "top up, then compose resume"))
    verify = proj / "results" / "verify.json"
    if (ver := _json_file(verify)) is not None and ver.get("passed") is True and fc_tasks.push_stance(proj) == "ask":
        layers = proj / "layers.json"
        if not layers.exists() or layers.stat().st_mtime < verify.stat().st_mtime:
            head = ver.get("head") or int(verify.stat().st_mtime)
            out.append(_cond(f"go:{head}", "go", "the verified stack waits for your go to push it as draft PRs", f"compose stack --submit --project {proj.name}"))
    return out

def _sync(proj, events):
    """Bring the list up to date and return the open items; lets a failure through."""
    wanted = conditions(proj)  # first: a source that cannot be read changes nothing
    for c in wanted:
        if not any(i["key"] == c["key"] and (_is_open(i) or i.get("closed_by") != "settled") for i in load(proj)["items"]):
            open_item(proj, c["kind"], c["key"], c["line"], c["recommendation"], c["ids"])
    for e in events:
        if m := BLOCKED_EVENT.search(e):
            open_item(proj, "attend", f"agent:{m.group(1)}", f"{m.group(1)} waits at a prompt only you can answer (a sign-in or an Allow)")
    keys = {c["key"] for c in wanted}
    for item in [i for i in load(proj)["items"] if _is_open(i)]:
        if str(item["key"]).startswith(("report:", "halt:", "go:")) and item["key"] not in keys:
            close_item(proj, item["key"], "settled")
    for item in [i for i in load(proj)["items"] if _is_open(i) and str(i["key"]).startswith("agent:")]:
        if fc_fleet.agent_status(item["key"][len("agent:"):]) != "blocked":
            close_item(proj, item["key"], "settled")
    return open_items(proj)

def sync(proj, events=()):
    """Bring the list up to date from the files and the watch's events, and return the open items. Opens each condition not already open
    (one closed as `settled` may open again; one closed by a no or by `decided` stays closed), closes the `report:`, `halt:` and `go:` items that
    are no longer a condition and the `agent:` items whose agent is no longer blocked. A decision or review item is never touched. A failure
    changes nothing more and returns the open items it can read, else [] (a waiting-list failure never stops a watch)."""
    try:
        return _sync(proj, events)
    except Exception:  # noqa: BLE001
        try:
            return open_items(proj)
        except Exception:  # noqa: BLE001
            return []
