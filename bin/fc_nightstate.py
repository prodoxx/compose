"""Night mode's record: its limits (an end time and a dollar budget), what the night has spent, the cap that drops to zero when it drains, and its notes."""
from __future__ import annotations
import json, math, re, subprocess, sys, time
from pathlib import Path

import fc_core, fc_config, fc_factorystate, fc_factoryturn, fc_models, fc_plan, fc_stall, fc_tasks

def until_epoch(hhmm: str, now: float) -> float:
    """The next local HH:MM after `now`: today's if it is still ahead, else tomorrow's."""
    hour, minute = (int(p) for p in hhmm.split(":"))
    t = time.localtime(now)
    at = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))
    if at <= now:
        at = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, hour, minute, 0, 0, 0, -1))
    return at

def new_night(now, until, budget, stop_when) -> dict:
    return {"since": now, "until": until, "budget": budget, "stop_when": stop_when, "draining": None, "ended": None, "stops": 0,
            "spend": 0.0, "wait": None, "asked": {}, "morning": [], "waits": 0, "waited": 0.0}

def night_spend(repo, since) -> float:
    """The cost of every task result written since the night began, over every project."""
    total = 0.0
    for _wt, proj in fc_core.all_projects(repo).values():
        for path in Path(proj, "results").glob("[0-9][0-9][0-9].json"):
            try:
                if path.stat().st_mtime >= since:
                    cost = float(json.loads(path.read_text()).get("cost") or 0)
                    total += cost if math.isfinite(cost) else 0.0
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    return total

def drain_reason(night, now) -> str | None:
    if now >= night["until"]:
        return "morning"
    if night["spend"] >= night["budget"]:
        return "budget"
    return None

def night_cap(state) -> int:
    night = state.get("night")
    if not night or night.get("draining") or night.get("wait") or night.get("ended"):
        return 0
    return fc_config.FACTORY["night_tickets"]

fc_factorystate.PACE["night"] = night_cap

def notes_path(repo) -> Path:
    return fc_core.repo_link(repo).with_name("night-notes.md")

def add_note(repo, line) -> None:
    path = notes_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as out:
        out.write(line.rstrip("\n") + "\n")

def notes_tail(repo, n=8000) -> str:
    try:
        return notes_path(repo).read_text()[-n:]
    except OSError:
        return ""

def spawn_once(repo, args: list[str], log: str) -> int:
    """compose with `args` as a process of its own, out of this session; its output is appended to .compose/<log>. Returns its pid."""
    path = fc_core.repo_link(repo).with_name(log)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as out:
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "compose.py"), *args],
                             start_new_session=True, stdin=subprocess.DEVNULL, stdout=out, stderr=out)
    return p.pid

def unmerged_depth(plan, feature, seen=()) -> int:
    """How many verified-but-unmerged (In Review) levels the feature sits on: 0 with none, else 1 plus the deepest such blocker's own depth."""
    seen = {*seen, feature}
    deeper = [b for b in (plan["features"].get(feature) or {}).get("blockers") or []
              if fc_plan.base_built(b["state"], b.get("labels") or []) and not fc_tasks.blocker_finished(b["state"], b.get("labels") or [])]
    return 1 + max((unmerged_depth(plan, b["id"], seen) for b in deeper if b["id"] not in seen), default=0) if deeper else 0

def unmerged_stories(plan, entry, seen=()) -> int:
    """How many verified-but-unmerged (In Review) stories of its own feature the entry builds on, one above another: 0 with none."""
    stories = {s["id"]: s for s in (plan["features"].get(entry["feature"]) or {}).get("stories") or []}
    seen = {*seen, entry["id"]}
    deeper = [b["id"] for b in (stories.get(entry["id"]) or {}).get("blockers") or [] if b["id"] in stories and b["id"] not in seen
              and fc_plan.base_built(b["state"], b.get("labels") or []) and not fc_tasks.blocker_finished(b["state"], b.get("labels") or [])]
    return 1 + max(unmerged_stories(plan, {**entry, "id": b}, seen) for b in deeper) if deeper else 0

def one_level(plan, entries, state) -> list:
    """At night, only stories at most one unmerged level deep, counting the features and the stories of its own feature they
    build on, plus those already in flight."""
    live = set(fc_factoryturn.in_flight(plan, state))
    return [e for e in entries if e["id"] in live or unmerged_depth(plan, e["feature"]) + unmerged_stories(plan, e) <= 1]

fc_factoryturn.ORDER_FILTER["night"] = one_level

RESUME_BUFFER = 60  # seconds past a stated reset before the leads are told to carry on
MIN_WAIT = 60  # seconds of the first wait when a limit line states no reset
MAX_FALLBACK = 1800  # the longest such wait
MAX_RESET = 6 * 3600  # a stated reset further off than this is a misread (a timestamp, a count): use the fallback instead
PAY_PAUSE = 60  # seconds apart two looks must be for a billing banner to end the night
CONTINUE = "night: the usage window has reset; carry on from where you stopped."
PANE_PAY = re.compile(r"(?:api )?error\W{0,3}402|credit balance is too low|insufficient (?:credits|funds|balance)|out of credits", re.I)
IN_HOURS = re.compile(r"\bin (\d+)\s*h(?:ours?)?\b(?:\s*(\d+)\s*m)?", re.I)
IN_MINUTES = re.compile(r"\bin (\d+)\s*m(?:in(?:utes)?)?\b", re.I)
AT_AMPM = re.compile(r"\b(?:resets?|at|until|after)\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", re.I)
AT_CLOCK = re.compile(r"\b(?:resets?|at|until|after)\s+(\d{1,2}):(\d{2})\b")

def reset_at(line, now) -> float | None:
    """When the usage window a limit line names resets (plus RESUME_BUFFER), else None."""
    m = IN_HOURS.search(line)
    if m:
        return now + int(m.group(1)) * 3600 + int(m.group(2) or 0) * 60 + RESUME_BUFFER
    m = IN_MINUTES.search(line)
    if m:
        return now + int(m.group(1)) * 60 + RESUME_BUFFER
    m = AT_AMPM.search(line)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        return until_epoch(f"{hour}:{m.group(2) or '00'}", now) + RESUME_BUFFER
    m = AT_CLOCK.search(line)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return until_epoch(f"{m.group(1)}:{m.group(2)}", now) + RESUME_BUFFER
    return None

def fallback_wait(n) -> float:
    return min(MIN_WAIT * 2 ** (n - 1), MAX_FALLBACK)

def no_credit(repo, since) -> str | None:
    """Why the night must end now: a project halted on a payment since it began, else None."""
    for slug, (_wt, proj) in sorted(fc_core.all_projects(repo).items()):
        path = Path(proj, "results", "resume.json")
        try:
            if path.stat().st_mtime >= since:
                halt = json.loads(path.read_text())
                return f"{slug}: payment halt ({(halt.get('provider') if isinstance(halt, dict) else None) or 'unknown'})"
        except (OSError, ValueError):
            continue
    return None

def is_pay_banner(line) -> bool:
    return bool(PANE_PAY.search(line)) and not line.lstrip().startswith(("+", "-", "\u2713", "expect("))

def wait_line(until, names, line) -> str:
    return (f'event factory=usage-wait until={time.strftime("%H:%M", time.localtime(until))} agents={",".join(names)} line="{line.replace(chr(34), chr(39))}"'
            "  (a usage window is exhausted: nothing new starts until it resets)")

def usage_step(night, leads, now, read, prompt) -> list[str]:
    """One look at the team leads' panes. A billing banner seen twice ends the night; a new limit line starts or extends a wait; at a due wait the leads are
    prompted to continue (the prompt is the probe: an idle agent keeps its banner until something is typed)."""
    wait = night.get("wait")
    if wait and now < wait["until"]:
        return []
    present = set(leads.values())
    seen, resumed = night.setdefault("pay_seen", {}), night.setdefault("resumed_lines", {})
    stale = (wait or {}).get("lines") or {}
    limited, empty = {}, False
    for name in sorted(present):
        pane = read(name) or ""
        if wait and name in wait["agents"] and not pane.strip():
            empty = True
        line = fc_stall.limit_line(pane)
        if line and is_pay_banner(line):
            first = seen.get(name)
            if first and first["line"] == line:
                if now - first["at"] >= PAY_PAUSE:
                    night["ended"] = {"state": "no-credit", "at": now, "why": line}
                    return [f'event factory=night-ended state=no-credit why="{line.replace(chr(34), chr(39))}"  (a provider says there is no credit, which is the product owner\'s call: send one PushNotification naming it)']
            else:
                seen[name] = {"line": line, "at": now}
        else:
            seen.pop(name, None)
        if line is None or line == resumed.get(name) or (wait and line == stale.get(name)):
            if line is None:
                resumed.pop(name, None)
            continue
        resumed.pop(name, None)
        limited[name] = line
    n = wait["n"] if wait else 0
    if limited:
        parsed = [t for t in (reset_at(ln, now) for ln in limited.values()) if t and t - now <= MAX_RESET]
        n = 0 if parsed else n + 1
        until = max(parsed) if parsed else now + fallback_wait(n)
        kept = {a: ln for a, ln in stale.items() if a in present and a not in limited}  # still waiting: prompted when the longer wait is due
        night["wait"] = {"until": until, "n": n, "since": wait["since"] if wait else now, "agents": sorted({*limited, *kept}), "lines": {**kept, **limited}}
        return [wait_line(until, sorted(limited), limited[sorted(limited)[-1]])]
    if not wait:
        return []
    names = [a for a in wait["agents"] if a in present]
    if not names:
        night["wait"] = None
        return []
    done = [] if empty else [a for a in names if prompt(a, CONTINUE)]
    if not done:
        until = now + fallback_wait(n + 1)
        night["wait"] = {**wait, "until": until, "n": n + 1}
        return [wait_line(until, names, "no pane read" if empty else "the continue prompt was not delivered")]
    night["waits"] += 1
    night["waited"] += now - wait["since"]
    night["resumed_lines"] = {**{a: ln for a, ln in resumed.items() if a in present}, **(wait.get("lines") or {})}
    night["wait"] = None
    return [f'event factory=usage-resumed agents={",".join(done)}  (the window reset: each waiting team lead was told to continue)']

def live(state) -> bool:
    """A night is on and has not ended."""
    night = state.get("night")
    return state.get("mode") == "night" and isinstance(night, dict) and not night.get("ended")

def night_since(repo):
    """When the live night on disk began, else None: a worker reads it as it starts."""
    state = fc_factorystate.load_state(repo)
    return state["night"].get("since") if live(state) else None

def still_on(repo, since) -> bool:
    """Whether the night a worker started under is still the live one: it checks this before it writes, so nothing is written after the night."""
    return since is not None and night_since(repo) == since

def stamp(t) -> str:
    """A night's start as its posture's `since` says it."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))

def archive_stem(since: str) -> str:
    """The name, before any -<n> and .json, a lifted posture with this `since` is archived under (as `compose yolo off` names it)."""
    return f"yolo-{str(since or '').replace(' ', 'T').replace(':', '')}"

def archived_posture(proj, since) -> dict | None:
    """The night posture lifted from the project for the night begun at `since`: the newest archive (highest -<n>) whose since is the night's."""
    text = stamp(since)
    stem = archive_stem(text)
    found = []
    for path in Path(proj, "results").glob(f"{stem}*.json"):
        m = re.fullmatch(rf"{re.escape(stem)}(?:-(\d+))?\.json", path.name)
        if m:
            found.append((int(m.group(1) or 1), path))
    for _n, path in sorted(found, reverse=True):
        try:
            y = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(y, dict) and y.get("since") == text and y.get("mode") == "night":
            return y
    return None
