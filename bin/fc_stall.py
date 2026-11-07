import json, re, subprocess, time
import fc_core, fc_herdr, fc_fleet


# A banner a coding agent leaves while it waits out a limit or retries a refused call: "Retrying in 4s ... 401", "usage limit reached",
# "try again at 3pm", "rate limited", "429", "402 Payment Required". Prose that merely mentions a rate limit does not match.
LIMIT_LINE = re.compile(r"retrying\b.*\b(?:401|402|403|429|5\d\d)\b|usage limit|limit reached|try again (?:at|in|after)\b|rate.?limit(?:ed|\s+(?:reached|exceeded|hit))"
                        r"|(?:error|status|http|code)\W{0,3}(?:401|402|429)\b|payment required|out of (?:credits|extra usage)", re.I)
LIMIT_READ = 30  # seconds between two reads of one pane
LIMIT_HOLD = 60  # seconds a pane must keep showing such a line before it is a stall: a diff or a log that mentions a limit scrolls away
AGENT_STALL_SECONDS = 1800  # a teammate herdr shows working this long, with no report and no commit, is stuck; verify.json `agent_stall_seconds` changes it

def limit_line(pane):
    """The last line near the bottom of a pane that says a limit or a retry loop, else None."""
    for line in reversed(pane.strip().splitlines()[-8:]):
        if LIMIT_LINE.search(line):
            return line.strip()[:160]
    return None

def limit_spells(snap, spells, now_s):
    """[(key, what)] for each pane that has shown a limit or retry line for LIMIT_HOLD seconds, once per spell. `spells`
    ({agent: {"line", "since", "read"}}) is the watcher's memory: a spell ends the first time the pane stops matching, and the
    banner's own text (a countdown, an attempt count) may change while it lasts."""
    out, closed = [], set(snap.get("closed", ()))
    for n in snap["agents"]:
        r = snap.get("crew", {}).get(n) or []
        sp = spells.get(n) or {}
        if n in closed or (r and r[-1]["state"] == "done"):
            spells.pop(n, None)
        elif now_s - sp.get("read", 0) >= LIMIT_READ:
            line = limit_line(fc_fleet.read(n, lines=12))
            if not line:
                spells.pop(n, None)
                continue
            spells[n] = sp = {"line": line, "since": sp.get("since", now_s), "read": now_s}
            if now_s - sp["since"] >= LIMIT_HOLD:
                out.append((f"limit:{n}:{int(sp['since'])}", f"limit agent={n} line={line!r} on its pane since {time.strftime('%H:%M:%S', time.localtime(sp['since']))}: "
                            f"a retry loop or a usage limit, not work; read it: compose crew read --project <slug> --agent {n} --lines 60"))
    return out

def last_commit(proj, name):
    """Epoch seconds of the newest commit in the worktree a teammate's record names, 0 when there is none to read."""
    try:
        c = json.loads((proj / "results" / "crew" / f"{name}.json").read_text())
        return int(fc_core.sh(["git", "log", "-1", "--format=%ct"], c["worktree"] if c.get("worktree") else c["cwd"], timeout=10, check=False).stdout.strip())
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return 0

def silent_stalls(proj, snap, seen, now_s, limit=AGENT_STALL_SECONDS, awake_from=0):
    """[(key, what)] for each teammate herdr has shown `working` for `limit` seconds without a report and without a commit
    (`seen`: {agent: [status, since]}, kept by the watcher). Time asleep does not count: `awake_from` is the end of the last sleep."""
    out, closed = [], set(snap.get("closed", ()))
    for n, st in snap["agents"].items():
        r = (snap.get("crew", {}).get(n) or [None])[-1]
        if st != "working" or n in closed or (r and r["state"] in ("done", "needs-user")):
            continue
        since = max(seen.get(n, ["", now_s])[1], awake_from, fc_core.report_time(r["at"]) if r else 0)
        if now_s - since < limit:
            continue
        since = max(since, last_commit(proj, n))  # the git call waits until the rest says it may be stuck
        if now_s - since >= limit:
            out.append((f"silent:{n}:{int(since)}", f"silent agent={n} herdr shows it working, with no report and no commit for {fc_core.fmt(now_s - since)}; "
                        f"read it: compose crew read --project <slug> --agent {n} --lines 60"))
    return out
