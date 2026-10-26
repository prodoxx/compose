"""The build loop's state: one JSON file and one numbered event log per repo, and the pace dial that caps tickets at once."""
import contextlib, copy, fcntl, json, time

import fc_core, fc_config

EMPTY = {"pid": None, "started": None, "stopped": None, "mode": "day", "day_tickets": None, "dispatched": {}, "prints": {}, "offer": None,
         "declined_until": None, "planning": None, "out_of_order": [], "said": {}, "turns": 0, "backoff": {}, "manager_pane": None,
         "night": None, "prep": {}, "prep_checked": None, "prd": {}}

def state_path(repo):
    return fc_core.repo_link(repo).with_name("factory.json")

def events_path(repo):
    return fc_core.repo_link(repo).with_name("factory-events.jsonl")

def load_state(repo):
    """The state file over EMPTY; a missing or unreadable file reads as EMPTY."""
    state = copy.deepcopy(EMPTY)
    try:
        saved = json.loads(state_path(repo).read_text())
    except (OSError, ValueError):
        return state
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k in EMPTY and (EMPTY[k] is None or type(v) is type(EMPTY[k])):  # a None default takes any value
                state[k] = v
    return state

def save_state(repo, state):
    path = state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(path, json.dumps(state, indent=2) + "\n")

@contextlib.contextmanager
def edit(repo):
    """The state under a short lock, saved on exit: a verb and the loop's end-of-turn merge never write over each other."""
    state_path(repo).parent.mkdir(parents=True, exist_ok=True)
    with open(state_path(repo).with_name("factory-state.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        state = load_state(repo)
        yield state
        save_state(repo, state)

def _read_events(repo):
    try:
        lines = events_path(repo).read_text().splitlines()
    except OSError:
        return []
    events = []
    for l in lines:
        try:
            e = json.loads(l)
        except ValueError:
            continue
        if isinstance(e, dict) and isinstance(e.get("n"), int):
            events.append(e)
    return events

def add_event(repo, line):
    """Append one event and return its number; a lock keeps the loop and a CLI verb from writing the same number."""
    path = events_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name("factory-events.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        events = _read_events(repo)
        n = max([e["n"] for e in events], default=0) + 1
        torn = path.exists() and path.stat().st_size and not path.read_bytes().endswith(b"\n")
        with open(path, "a") as f:
            f.write(("\n" if torn else "") + json.dumps({"n": n, "at": time.time(), "line": line}) + "\n")
    return n

def events_since(repo, n):
    """The events numbered above n, oldest first."""
    return [e for e in _read_events(repo) if e["n"] > n]

def say_once(state, key, line, out):
    """Append line to out unless it is what was last said under key."""
    if state["said"].get(key) != line:
        state["said"][key] = line
        out.append(line)

def turn_failed(why, story=None, then="the next turn tries again"):
    """The one turn-failed event line: a turn, or one story's dispatch, that did not go through."""
    return f'event factory=turn-failed{f" story={story}" if story else ""} why="{why}"  ({then})'

def day_cap(state):
    return state["day_tickets"] or fc_config.FACTORY["day_tickets"]

PACE = {"day": day_cap}

def pace(state):
    mode = state["mode"] if state["mode"] in PACE else "day"
    return {"mode": mode, "cap": PACE[mode](state)}
