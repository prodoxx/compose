"""The waiting list: what waits on the product owner, one file per project (results/waiting.json). An item is opened under a key
(one open item per key), may be seen, and is closed by what settled it; its id is never reused. Every write holds a lock on
results/waiting.lock. Imports only lower modules."""

import fcntl, json, re, sys, time
from pathlib import Path
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
    """True when the team lead wrote this status file, or its crew record says it reports to the manager (a record that is not there names
    nobody). Raises OSError or ValueError when the record exists and cannot be read or parsed."""
    if name == lead:
        return True
    try:
        record = json.loads((crew / f"{name}.json").read_text())
    except FileNotFoundError:
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
    changes nothing more and returns the open items it can read, else [] (a waiting-list failure never stops a watch); it says why on
    stderr in one line (stdout stays clean: a hook's stdout is parsed as JSON)."""
    try:
        return _sync(proj, events)
    except Exception as e:  # noqa: BLE001
        print(f"waiting list not updated ({e})", file=sys.stderr)
        try:
            return open_items(proj)
        except Exception:  # noqa: BLE001
            return []


# ---------- reading across projects, and the two commands ----------

def _scope(repo, project=None):
    """[(slug, project dir)] the commands read: the one project named, else every project of the repo except those whose manager record
    names a session other than this one (outside a terminal host, with no session id, all of them). An unknown project exits naming the known ones."""
    projects = fc_core.all_projects(Path(repo))
    if project:
        if project not in projects:
            sys.exit(f"no project {project!r}; known: {sorted(projects)}")
        return [(project, projects[project][1])]
    mine = fc_core.session_id()
    return [(slug, p) for slug, (_, p) in sorted(projects.items()) if not (mine and (theirs := fc_fleet.manager_record(p).get("session")) and theirs != mine)]

def gather(repo, project=None):
    """The open items of every project in scope (see _scope), each synced first and given "project": <slug>, in open_items order."""
    items = [{**i, "project": slug} for slug, p in _scope(repo, project) for i in sync(p)]
    return sorted(items, key=_order)

def _age(item):
    """How long the item has waited, as `5m`, `2h 10m` or `3d` ("?" when its opened stamp cannot be read)."""
    try:
        minutes = max(0, int((time.time() - fc_core.report_time(item["opened"])) // 60))
    except (KeyError, TypeError, ValueError):
        return "?"
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m" if minutes < 24 * 60 else f"{minutes // (24 * 60)}d"

def _recommended(item, gap=" "):
    """The item's line followed, after `gap`, by its recommendation in brackets when it has one."""
    return item["line"] + (f"{gap}(recommendation: {item['recommendation']})" if item.get("recommendation") else "")

def listing(items, cap=7):
    """One line per item (`W3  hw  answer  12m  <line>  (recommendation: <rec>)`, ` [seen]` when seen); more than `cap` lines: the first cap - 1
    and a count line (`cap=None` prints all); no item: one line saying so."""
    if not items:
        return ["nothing waits on you"]
    lines = [f"{i['id']}  {i['project']}  {i['kind']}  {_age(i)}  {_recommended(i, '  ')}" + (" [seen]" if i.get("seen") else "") for i in items]
    if cap is not None and len(lines) > cap:
        return lines[:cap - 1] + [f"+{len(lines) - cap + 1} more: compose waiting --all"]
    return lines

def footer(items):
    """What a session's footer says: [] unless an item is both unseen and stopping work; else `Waiting on you:`, the unseen stoppers
    (all of them when there are at most three and nothing else is open, else the first two) and a count line for the rest."""
    stoppers = [i for i in items if i.get("stops") and not i.get("seen")]
    if not stoppers:
        return []
    shown = stoppers if len(stoppers) <= 3 and len(items) == len(stoppers) else stoppers[:2]
    lines = ["Waiting on you:"] + [f"- {i['id']} {i['project']}: {_recommended(i)}" for i in shown]
    return lines + ([f"+{len(items) - len(shown)} more: compose waiting"] if len(items) > len(shown) else [])

def _locate(a, wid, command):
    """(project dir, open item) for the id, in the project --project names, else the one project in scope where it is open. Exits when it is open
    nowhere, or in several (naming a `<command> --project <slug> <id>` for each)."""
    wid = wid.upper()
    found = [i for i in gather(a.repo, a.project) if i["id"] == wid]
    if not found:
        sys.exit(f"no open item {wid}; compose waiting lists them")
    if len(found) > 1:
        sys.exit(f"{wid} is open in {len(found)} projects; say which: " + "; ".join(f"{command} --project {i['project']} {wid}" for i in found))
    return dict(_scope(a.repo, found[0]["project"]))[found[0]["project"]], found[0]

def _reporter_pane(proj, key):
    """The pane of the teammate a `report:<name>:<at>` item came from, from results/crew/<name>.json; None when the key is another kind, or the
    record or its pane is missing or unreadable."""
    if not str(key).startswith("report:"):
        return None
    try:
        record = json.loads((proj / "results" / "crew" / f"{key.split(':', 2)[1]}.json").read_text())
    except (OSError, ValueError):
        return None
    return record.get("pane") if isinstance(record, dict) else None

def cmd_ack(a):
    """Mark an item seen: it stays on the list until it is closed; a report's pane stops showing `blocked`."""
    proj, item = _locate(a, a.id, "compose ack")
    mark_seen(proj, item["id"])
    if pane := _reporter_pane(proj, item["key"]):
        fc_fleet.pane_label(pane, state_labels={"blocked": "seen"})
    print(f"seen {item['id']} ({item['project']}): {item['line']}; it stays on the list until it is closed")

def cmd_waiting(a):
    """Print what waits on the product owner (at most seven lines; --all prints every item, --footer the session footer), or close a go item
    with `no W<n> --quote "<their words>"`."""
    if a.words:
        if len(a.words) != 2 or a.words[0] != "no":
            sys.exit('usage: compose waiting [--all | --footer] | compose waiting no W<n> --quote "<what the product owner said>"')
        if not (a.quote or "").strip():
            sys.exit('no needs the product owner\'s words: compose waiting no W<n> --quote "<what they said>"')
        proj, item = _locate(a, a.words[1], "compose waiting no")
        if item["kind"] != "go":
            sys.exit(f"{item['id']} is a {item['kind']} item; only a go can be answered with no")
        close_item(proj, item["key"], f"no: {a.quote.strip()}")
        print(f'closed {item["id"]} ({item["project"]}): the product owner said no: "{a.quote.strip()}"')
        return
    items = gather(a.repo, a.project)
    lines = footer(items) if a.footer else listing(items, None if a.all else 7)
    if lines:
        print("\n".join(lines))
