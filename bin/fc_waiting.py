"""The waiting list: what waits on the product owner, one file per project (results/waiting.json). An item is opened under a key
(one open item per key), may be seen, and is closed by what settled it; its id is never reused. Every write holds a lock on
results/waiting.lock. Imports only lower modules."""

import fcntl, json, time
from contextlib import contextmanager

import fc_core

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
