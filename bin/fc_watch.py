import argparse, fcntl, json, os, re, sys, time
from pathlib import Path
import fc_core, fc_proc, fc_tasks, fc_herdr, fc_change, fc_reports, fc_crew, fc_hooks, fc_load, fc_status, fc_briefs, fc_sleep, fc_stall, fc_shed, fc_checks


YOLO_HINT = "  {m}=on (answer with the recommendation; log it: compose {m} decided ...)"
NIGHT_HINT = "  night=on (the night answerer takes it: do not answer; wait for factory=answered|waiting|answer-failed)"

# ---------- watch ----------
FINAL_STATES = ("passed", "already_green", "failed", "contradiction", "unproven", "broken_base", "rejected_size", "rejected_docs", "rejected_scope", "rejected_regression", "rejected_tampered", "payment_required", "interrupted", "hand_done", "unchained", "paused")

AGENTS_INTERVAL = 10.0  # seconds between `herdr agent list` calls in one watch loop: a teammate's status moves at the pace of its turns

class Agents:
    """`herdr agent list` for one watch loop, asked at most once per AGENTS_INTERVAL: one process per poll from every parked watch is too many.
    Asked again when the crew `records` changed since the last list: a teammate `crew_up` just marked as listed is judged by a list
    taken after that, never by one from before herdr listed it, which would count it as closed."""
    def __init__(self):
        self.at, self.listed, self.records = None, None, None

    def __call__(self, records=None):
        now = time.monotonic()
        if self.at is None or now - self.at >= AGENTS_INTERVAL or records != self.records:
            self.listed, self.at, self.records = fc_herdr.herdr("agent", "list"), now, records
        return self.listed

def watch_snapshot(repo, proj, agents=None):
    """Everything a team lead decides on: each task's state, each herdr agent's status, run and verify markers."""
    try:
        rows = fc_status.board(repo, proj)["rows"]
    except json.JSONDecodeError:  # a writer that is not ours caught mid-write: one more look, then the real error
        time.sleep(0.5)
        rows = fc_status.board(repo, proj)["rows"]
    # The marker alone is not enough: a run that crashes leaves it behind, and watch then waits on a dead run forever.
    snap = {"tasks": {}, "agents": {}, "run": fc_tasks.run_alive(proj), "ended": fc_tasks.run_ended(proj), "verify": None, "halt": None, "fixing": fc_tasks.fixing(proj),
            "crew": {f.stem: fc_crew.crew_reports(proj, f.stem) for f in sorted((proj / "results" / "crew").glob("*.status"))}
            if (proj / "results" / "crew").exists() else {},
            "refused": {f.stem: fc_reports.crew_refusals(proj, f.stem) for f in sorted((proj / "results" / "crew").glob("*.refused"))}
            if (proj / "results" / "crew").exists() else {}}
    try:
        h = json.loads((proj / "results" / "resume.json").read_text())
        snap["halt"] = [h["halted_at"], h["provider"], len(h.get("remaining") or []), h.get("top_up"), h.get("resume")]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    try:
        snap["ready"] = {i["id"]: i["title"] for i in json.loads((proj / "results" / "ready.json").read_text())}
    except (OSError, json.JSONDecodeError):
        snap["ready"] = {}
    for r in rows:
        snap["tasks"][r["id"]] = {"state": r["state"], "attempts": r["attempts"], "cost": r["cost"], "note": r["note"], "linear": r["linear"], "title": r["title"]}
    records = [json.loads(f.read_text()) for f in sorted((proj / "results" / "crew").glob("*.json"))]  # before the list: it must be as new as they are
    listed = agents(records) if agents else fc_herdr.herdr("agent", "list")
    agents = None if listed is None else listed.get("agents", [])
    for ag in agents or []:
        n = ag.get("name") or ""
        if fc_herdr.agent_id(proj.name, n):
            snap["agents"][n] = ag.get("agent_status")
    snap["closed"] = [c["name"] for c in records if fc_crew.tab_closed(c, agents)]  # whatever their last report says, not working
    snap["owner"] = crew_owners(records, agents)  # which engineering manager session each teammate is: watch_scope
    pis = [c for c in records if "skill" in c and c.get("agent", c["name"]) in snap["agents"]]
    try:
        cur = fc_hooks.skill_hashes() if pis else {}
    except OSError:
        pis = []
    snap["skill"] = {c["name"]: changed for c in pis if (changed := fc_hooks.skill_changed(c["skill"], cur))}  # live pi teammates reading an old skill
    v = proj / "results" / "verify.json"
    if v.exists():
        try:
            vd = json.loads(v.read_text())
            snap["verify"] = [int(v.stat().st_mtime), bool(vd.get("passed"))]  # a list: snapshots round-trip JSON
            snap["verify_why"] = fc_status.verify_why(vd)
        except (json.JSONDecodeError, OSError):
            pass
    # The newest sign of life anywhere in the project, and when the last run ended: the stall rules read these.
    marks = [(fc_core.report_time(r[-1]["at"]), f"teammate {n} reported {r[-1]['state']}") for n, r in snap["crew"].items() if r]
    marks += [(f.stat().st_mtime, f"result {f.name}") for f in (proj / "results").glob("*.json") if not f.name.startswith("watch") and f.name != "manager.json"]
    snap["last"] = list(max(marks)) if marks else [0, ""]
    cost = proj / "results" / "run-cost.json"
    snap["run_end"] = cost.stat().st_mtime if cost.exists() and not snap["run"] else 0
    cut = fc_change.evidence_cut(proj)  # a screen a closed change or a dismissal already answered is not news, not even to `--now`
    snap["reflects"] = {}
    for f in sorted((proj / "results").glob("reflect-*.json")):
        try:
            r = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if r.get("at", "") > cut:
            snap["reflects"][str(f)] = {"trigger": r.get("trigger"), "error": r.get("error"), "counts": fc_change.reflect_counts(r)}
    return snap

def crew_owners(records, agents):
    """Each teammate an engineering manager session started, mapped to that session (`started_by`) while a live agent still sits in
    its pane; the team lead's own teammates follow their team lead. A teammate whose session ended has no owner, so any engineering
    manager session adopts it, and so does one from before `started_by` existed. `agents` None (herdr cannot say) keeps every session live."""
    live = None if agents is None else {ag.get("pane_id") for ag in agents}
    own = {c["name"]: c["started_by"] for c in records
           if c.get("reports_to") == "manager" and c.get("started_by") and (live is None or c["started_by"] in live)}
    lead = next((own[c["name"]] for c in records if c.get("role") == "orchestrator" and c["name"] in own), None)
    if lead:
        own |= {c["name"]: lead for c in records if c.get("reports_to") == "orchestrator"}
    return own

NET_ERROR = re.compile(r"API Error|ENOTFOUND|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|Unable to connect to API|fetch failed|socket hang up", re.I)
NET_IDLE = 60  # seconds a working teammate sits idle before its pane is read for a dropped connection
NET_NUDGE = "The network dropped and is back; continue exactly where you stopped; re-check any write that may not have landed."

STALL_SECONDS = 240  # verify.json `stall_seconds` or `watch --stall` change it

def watch_mem(proj, key):
    """What one watcher (key: watch_key) last reported and which stalls it fired, in results/watch-<key>.json."""
    try:
        return json.loads((proj / "results" / f"watch-{key}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}

def watch_save(proj, key, base=None, **kv):
    f = proj / "results" / f"watch-{key}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    if base is not None:
        b = f.with_suffix(".base")
        fc_core.atomic_write(b.with_suffix(".tmp"), json.dumps(base))
        b.with_suffix(".tmp").replace(b)
    mem = {k: v for k, v in watch_mem(proj, key).items() if k != "base"} | kv
    fc_core.atomic_write(f.with_suffix(".tmp"), json.dumps(mem))
    f.with_suffix(".tmp").replace(f)  # atomic: a reader never sees half a file

def watch_base(proj, key):
    try:
        return json.loads((proj / "results" / f"watch-{key}.base").read_text())
    except (OSError, json.JSONDecodeError):
        return watch_mem(proj, key).get("base")

def load_events(proj, key):
    return load_lines(proj, key) + fc_shed.tick()

def load_lines(proj, key):
    st = fc_load.update()
    mem = watch_mem(proj, key)
    if mem.get("load_n") is None:
        watch_save(proj, key, load_n=st["n"])
        return []
    new = [e for e in st["log"] if e["n"] > mem["load_n"]]
    if not new:
        return []
    lim = fc_load.limits()
    pane = fc_herdr.herdr_manager_record(proj).get("session")
    lines = []
    for e in new:
        if e["kind"] == "held":
            lines.append(f"event load={e['kind']} avg={e['avg']:.1f} high={lim['high']:.1f} cores={lim['cores']}  (compose starts nothing new until the load falls below {lim['low']:.1f})")
        elif e["kind"] == "released":
            lines.append(f"event load={e['kind']} avg={e['avg']:.1f} low={lim['low']:.1f}")
        elif e["kind"] == "warn":
            tell = f"load {e['avg']:.1f} on {lim['cores']} cores is above the warn mark {lim['warn']:.1f}: compose holds new starts above {lim['high']:.1f}"
            if pane:
                fc_herdr.herdr("pane", "report-metadata", pane, "--source", "compose-load", "--token", f"load={e['avg']:.1f} on {lim['cores']} cores")
            if fc_load.claim(e["n"]):
                fc_crew.desktop_notify("compose: load", tell)
            lines.append(f"event tell={tell!r}")
        else:
            tell = f"load cleared: {e['avg']:.1f} on {lim['cores']} cores, below {lim['low']:.1f}"
            if pane:
                fc_herdr.herdr("pane", "report-metadata", pane, "--source", "compose-load", "--clear-token", "load")
            if fc_load.claim(e["n"]):
                fc_crew.desktop_notify("compose: load", tell)
            lines.append(f"event tell={tell!r}")
    watch_save(proj, key, load_n=new[-1]["n"])
    return lines

def watch_lock(proj, key):
    """One live watch per watcher, held until the returned file closes; its pid is the file's content. None when another holds it."""
    (proj / "results").mkdir(parents=True, exist_ok=True)
    f = open(proj / "results" / f"watch-{key}.lock", "a+")  # not "w": that would wipe the pid of the watch that holds it
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.truncate(0)
    f.write(str(os.getpid()))
    f.flush()
    return f

def watch_holder(proj, key):
    """The pid of the watch that holds the lock of `key`, as far as its file says."""
    try:
        return (proj / "results" / f"watch-{key}.lock").read_text().strip() or "unknown"
    except OSError:
        return "unknown"

def at_work(snap, names):
    """Of the teammates whose last report is working, those herdr does not list as idle or done: a test writer whose task passed sits idle in its
    pane with that report until collect closes it, and the tail of a stall line says who is really working."""
    return [n for n in names if snap["agents"].get(n) not in ("idle", "done")]

def watch_stalls(proj, snap, limit, now_s=None, key="all", woke=None, agent_limit=None):
    """The silences an event diff never shows: work is pending but nothing moves. Each stall fires once per watcher
    (results/watch-<key>.json remembers it), so a supervisor that nudges is not woken again for the same silence.
    `woke` is the [start, end] of a sleep: for `limit` seconds after it the silence rules wait, since nothing could move while the Mac slept.
    `agent_limit` is how long a working teammate may show no report and no commit (fc_stall.AGENT_STALL_SECONDS)."""
    now_s = now_s or time.time()
    orch = fc_herdr.agent_name(proj.name, "orchestrator")
    last = {n: (r[-1]["state"], fc_core.report_time(r[-1]["at"])) for n, r in snap.get("crew", {}).items() if r}
    closed = set(snap.get("closed", ()))
    pending = [t for t, v in snap["tasks"].items() if v["state"] in ("pending", "running", "interrupted")]
    on_hold = [t for t, v in snap["tasks"].items() if v["state"] in ("held", "paused")]  # waiting on a change of course: still work to move
    working = [n for n, (st, _) in last.items() if st == "working" and n != orch and n not in closed]
    mem = watch_mem(proj, key)
    was = json.dumps([sorted(mem.get("fired", [])), mem.get("seen", {}), mem.get("spells", {})])
    fired, seen = set(mem.get("fired", [])), mem.get("seen", {})
    lead = orch in snap.get("crew", {}) or orch in snap["agents"]  # a project started without a team lead (a designer started directly) never has one
    who = f" agent={orch}" if lead else ""
    status = snap["agents"].get(orch, "gone")
    if (seen.get(orch) or [None])[0] != status:
        seen[orch] = [status, now_s]
    ev = []
    hm = lambda t: time.strftime("%H:%M:%S", time.localtime(t))
    tail = f"{f'team lead {status}' if lead else 'no team lead'}, pending tasks {len(pending)}{f', held {len(on_hold)}' if on_hold else ''}, teammates working {','.join(at_work(snap, working)) or 'none'}"
    if lead:
        tail += f"  (read: herdr agent read {orch} --lines 60; nudge: herdr agent prompt {orch} \"...\")"
    def fire(key, what):
        if key not in fired:
            fired.add(key)
            ev.append(f"event stall={what}; {tail}")
    for n, st in snap["agents"].items():  # a teammate whose last turn ended on an API or network error sits idle with its work unreported
        if (seen.get(n) or [None])[0] != st:
            seen[n] = [st, now_s]
        r = snap.get("crew", {}).get(n) or []
        since = seen[n][1]
        if st == "idle" and n not in closed and r and r[-1]["state"] == "working" and now_s - since >= NET_IDLE and f"netchk:{n}:{since}" not in fired:
            fired.add(f"netchk:{n}:{since}")  # the pane is read once per idle spell
            pane = "\n".join(fc_herdr.herdr_text("agent", "read", n, "--source", "recent-unwrapped", "--lines", "12").strip().splitlines()[-6:])
            if m := NET_ERROR.search(pane):
                fire(f"net:{n}:{since}", f"network agent={n} its last turn ended on an API or network error ({m.group(0)}) and it has been idle since {hm(since)}; "
                     f"once the network is back, nudge: herdr agent prompt {n} \"{NET_NUDGE}\"")
        if st == "idle" and n != orch and n not in closed and r and r[-1]["state"] == "working" and now_s - since >= limit and f"net:{n}:{since}" not in fired \
                and not (re.search(r"-tests-\d+$", n) and fc_briefs.test_ready(r[-1])):  # collect closes a writer after its test ready
            fire(f"closable:{n}:{since}", f"closable agent={n} its turn ended and it has been idle since {hm(since)} with its last report working; "
                 f"if its work is finished, nudge: herdr agent prompt {n} \"If nothing is left for you, report done\" (its tab then closes)")
    spells = mem.get("spells", {})
    for what_key, what in fc_stall.limit_spells(snap, spells, now_s) + fc_stall.silent_stalls(proj, snap, seen, now_s, agent_limit or fc_stall.AGENT_STALL_SECONDS, woke[1] if woke else 0):
        fire(what_key, what)  # a pane stuck on a limit or a retry loop, a teammate working for ages with nothing to show
    for n in snap.get("closed", ()):
        r = snap.get("crew", {}).get(n) or []
        if r and r[-1]["state"] != "done" and not (re.search(r"-tests-\d+$", n) and fc_briefs.test_ready(r[-1])):  # collect closes a writer after its test ready
            fire(f"ended:{n}:{r[-1]['at']}", f"ended agent={n} its tab closed with its last report {r[-1]['state']}")
    just_woke = bool(woke) and now_s - woke[1] < limit
    if not (just_woke or last.get(orch, ("",))[0] == "done" or any(st == "needs-user" for st, _ in last.values())):  # finished, or waiting on the product owner: silence is expected
        lt, what = snap.get("last") or (0, "")
        if (pending or on_hold or working or snap["run"]) and lt and now_s - lt >= limit:
            fire(f"quiet:{lt}", f"quiet{who} nothing moved for {fc_core.fmt(now_s - lt)}, last: {what} at {hm(lt)}")
        rep = (snap.get("crew", {}).get(orch) or [None])[-1]  # the team lead's newest report: working with nothing parallel is a lead under-applying SKILL.md's parallel rule
        if lead and rep and rep["state"] == "working" and orch not in closed and (gap := fc_crew.parallel_gap(rep["message"])):
            fire(f"serial:{rep['at']}", f"serial agent={orch} its working report of {rep['at']} {gap}; nudge it to start what can start now")
        tests = {n: v for n, v in last.items() if re.search(r"-tests-\d+$", n)}
        if tests and all(st == "done" or n in closed for n, (st, _) in tests.items()) and not snap["run"] and pending \
                and all(snap["tasks"][t]["state"] == "pending" for t in pending):
            done_at = max(t for _, t in tests.values())
            if now_s - done_at >= limit:
                fire(f"undispatched:{done_at}", f"undispatched{who} every test-writing teammate was done by {hm(done_at)} and nothing is dispatched")
        ended = max(snap.get("run_end") or 0, (snap.get("verify") or (0,))[0])
        if lead and ended and not snap["run"] and last.get(orch, ("", 0))[1] < ended and now_s - ended >= limit:
            fire(f"after:{ended}", f"no-next-step agent={orch} the last run or verify ended at {hm(ended)} and the team lead has not reported since")
        since = seen[orch][1]
        if lead and status in ("idle", "done") and (pending or on_hold) and now_s - since >= limit:
            fire(f"idle:{since}", f"idle agent={orch} the team lead has been {status} since {hm(since)} with {len(pending + on_hold)} task(s) pending")
    if json.dumps([sorted(fired), seen, spells]) != was:
        try:
            watch_save(proj, key, fired=sorted(fired), seen=seen, spells=spells)
        except OSError:
            pass
    return ev

RUN_ENDED = {"crashed": "  CRASHED: the run process died without finishing; read results/run.log, fix the cause, then `compose resume`",
             "interrupted": "  INTERRUPTED: the run was stopped by Ctrl-C; `compose resume` continues it"}

def watch_events(before, after, started=False):
    """The actionable differences between two snapshots, one line each. Attempt counts, a task starting, and idle/done flaps are not events.
    A teammate's `started` report wakes no one; `started` (for --now, which lists what is running) keeps it."""
    ev = []
    for tid, t in after["tasks"].items():
        b = before["tasks"].get(tid, {})
        if t["state"] != b.get("state") and t["state"] in FINAL_STATES:  # `running` is the team lead's own run: no wake
            ev.append(f"event task={tid} state={t['state']} attempts={t['attempts']} cost=${t['cost']:.4f}"
                      + (f" branch={t['note']}" if t["note"] else "") + (f" linear={t['linear']}" if t["linear"] else "") + f" title={t['title']!r}")
    for n, st in after["agents"].items():
        if st == "blocked" and before["agents"].get(n) != "blocked":
            ev.append(f"event agent={n} status=blocked  (read: herdr agent read {n} --lines 60; steer: herdr agent prompt {n} \"...\")")
    if before["run"] and not after["run"]:
        passed = sum(t["state"] in ("passed", "already_green", "merged") for t in after["tasks"].values())
        ev.append(f"event run=ended passed={passed}/{len(after['tasks'])}"
                  + RUN_ENDED.get(after.get("ended"), ""))
    if after.get("halt") and after["halt"] != before.get("halt"):
        at_, provider, left, top_up, resume = (after["halt"] + [None, None])[:5]
        ev.append(f"event halt=payment provider={provider} remaining={left} at={at_!r}"
                  + (f" top_up={top_up}" if top_up else "") + (f" resume={resume!r}" if resume else "")
                  + "  (the product owner's stop: tell them, wait for their word, then run the resume command)")
    new = {i: t for i, t in after.get("ready", {}).items() if i not in before.get("ready", {})}
    if new:
        import fc_linear
        items = [{"id": i, "title": t} for i, t in new.items()]
        ev.append(f"event ready={','.join(new)}  ({fc_linear.ready_line(items)}; tell the product owner)")
    if after["verify"] and after["verify"] != before["verify"]:
        ev.append(f"event verify={'PASS' if after['verify'][1] else 'FAIL'}")
    for n, reports in after.get("crew", {}).items():
        for r in reports[len(before.get("crew", {}).get(n, [])):]:
            if started or r["message"] != "started":
                ev.append(f"event crew={n} state={r['state']} message={r['message']!r}")
    for n, refusals in after.get("refused", {}).items():
        for r in refusals[len(before.get("refused", {}).get(n, [])):]:
            ev.append(f"event crew={n} report=refused state={r['state']} why={r['why']!r}  (its report never arrived: read it, then steer the teammate)")
    for n, changed in after.get("skill", {}).items():
        if changed != before.get("skill", {}).get(n):
            ev.append(f"event crew={n} skill=stale changed={','.join(changed)}")
    for path, r in after.get("reflects", {}).items():
        if path in before.get("reflects", {}):
            continue
        if r["error"]:
            ev.append(f"event reflect={path} error={r['error']!r}  (the screen did not run: judge the evidence yourself)")
        elif sum(r["counts"]):
            ev.append(f"event reflect={path} broken={r['counts'][0]} unclear={r['counts'][1]} trigger={r['trigger']}")
    return ev

VERIFIED_PUSH = {
    "ask": "; the stack waits for your go to push it as draft PRs (`compose stack --submit`)",
    "auto": "; submitting the stack now as draft PRs (submit_after_verify is on)",
    "posture": "; the team lead submits the stack as draft PRs (push is on)",
}

def owner_lines(slug, before, after, push=None):
    """What the product owner must hear when the project's state changes, as ready-to-send one-liners (`event tell=...`): a verify
    that passed or failed and why, a run that ended, a fix loop starting after a failed verify, the team lead done. Only changes:
    nothing here repeats, and nothing says "still working" or "waiting on you". `push` (fc_tasks.push_stance) puts who pushes
    the verified stack in the passed line, so that line is the one ask for the push (a later report does not repeat it) and says
    nothing to ask when the project submits by itself."""
    out, v, was = [], after.get("verify"), before.get("verify")
    if v and v != was:
        why = after.get("verify_why")
        out.append("verify passed - the feature is In Review" + VERIFIED_PUSH.get(push, "") if v[1] else f"verify failed{': ' + why if why else ''}")
    if before.get("run") and not after.get("run"):
        tasks = after["tasks"]
        bad = [t for t, x in tasks.items() if x["state"] in fc_status.FAILED_STATES]
        ok = sum(x["state"].split(" ")[0] in ("merged", "passed", "hand_done", "already_green") for x in tasks.values())
        out.append({"crashed": "run crashed before finishing", "interrupted": "run stopped by Ctrl-C - compose resume continues it"}[after["ended"]] if after.get("ended") else f"run ended: {ok}/{len(tasks)} passed" + (f", failed {','.join(bad)}" if bad else ""))
    if after.get("fixing") and not before.get("fixing"):
        out.append("fix loop started: verify is fixing its findings")
    orch = fc_herdr.agent_name(slug, "orchestrator")
    for r in after.get("crew", {}).get(orch, [])[len(before.get("crew", {}).get(orch, [])):]:
        if r["state"] == "done":
            out.append(f"done: {r['message']}")
    return [f"event tell={line!r}" for line in out]

def is_info(e):
    """A progress ping that wakes nobody alone: a teammate's `working` report (but a test writer's `test ready`, which the team lead
    collects). It travels with the next wake, or alone after INFO_HOLD."""
    m = re.match(r"event crew=\S+ state=working message=(.*)$", e)
    return bool(m) and "test ready" not in m.group(1).lower()

URGENT = re.compile(r"event (?:stall=|halt=|sleep=(?:resumed|run-lost)|agent=\S+ status=blocked|crew=\S+ (?:state=(?:needs-user|blocked|contradiction|tool-bug|fallback)|report=refused) )|CRASHED")

ATTENTION = re.compile(r"event (?:crew=\S+ state=done |tell=|ready=|sleep=woke|load=|shed=|power=ac)|sign-in", re.I)

def is_quiet(a, proj, sources):
    """Whether this watch wakes only for what needs attention: the engineering manager's `--from orchestrator` watch, or any watch given
    `--attention`; `--all` turns it off. Then only an URGENT or ATTENTION line wakes it, and the progress lines before it (task states, a
    verify result, a run's end, working pings) are printed in the same wake, never alone."""
    if getattr(a, "all", False):
        return False
    return bool(getattr(a, "attention", False)) or (not os.environ.get("COMPOSE_ROLE") and fc_herdr.agent_name(proj.name, "orchestrator") in sources)

def yolo_mark(proj, events):
    """Under the posture on (yolo or away), a question line tells the engineering manager it answers it itself, and who set the posture when another session did.
    Under night's posture it tells it the night answerer takes the question."""
    if not (y := fc_tasks.yolo_state(proj)):
        return events
    by = fc_tasks.yolo_by(y)
    base = YOLO_HINT.format(m=y["mode"])
    hint = NIGHT_HINT if y["mode"] == "night" else base if by in ("this session", "unknown") else base.replace(f"{y['mode']}=on (", f"{y['mode']}=on set by {by} (", 1)
    return [e + hint if re.match(r"event crew=\S+ state=needs-user ", e) else e for e in events]

def watch_filter(events, slug, sources, direct=(), others=()):
    """`--from` names the teammates whose reports wake you (the engineering manager watches only its team lead, lead_sources when it
    names nobody); without it you get everything except the team lead's own reports, which go up to the engineering manager. `direct` names the teammates that report to
    this watcher (crew_reporting_to): a planner or designer the engineering manager started itself passes whatever `--from` says, so
    its needs-user and blocked reach the product owner (manager_attention) while the `--from orchestrator` watch runs. `others` names
    another engineering manager session's teammates (watch_scope): none of their lines wakes this one unless `--from` names them."""
    if others := [n for n in others if n not in sources]:
        events = [e for e in events if not any(f"crew={n} " in e or f"agent={n} " in e for n in others)]
    if sources:  # a tool-bug from any teammate wakes the engineering manager too: it owns fixing the tool; a stall is its to supervise
        return [e for e in events if " state=tool-bug " in e or " state=fallback " in e or e.startswith(("event stall=", "event ready=", "event tell=", "event sleep=", "event power="))
                or any(f"crew={n} " in e or f"agent={n} " in e for n in (*sources, *direct))]
    me = fc_herdr.agent_name(slug, "orchestrator")
    return [e for e in events if f"crew={me} " not in e and not e.startswith(f"event stall=idle agent={me} ")]  # idle: it waits on this watch

WATCH_INTERVAL = 2.0
WATCH_SETTLE = 10.0  # seconds a wake waits for the rest of its burst (a wave of tasks finishing together is one wake, not N)
INFO_HOLD = 300.0  # seconds progress pings (is_info) wait for a wake to travel with before they wake alone
POWER_EVERY = 30.0  # seconds between two looks for a battery stop the Mac's charger now lifts

def watch_key():
    """Whose watch this is: COMPOSE_ROLE, else the engineering manager, one per session (`manager-<herdr pane>`; bare `manager`
    outside herdr). Two engineering manager sessions on one project never share a lock or a memory, so neither waits on the
    other's watch or has its events marked seen by it."""
    if role := os.environ.get("COMPOSE_ROLE"):
        return role
    return "manager-" + re.sub(r"[^\w-]", "-", fc_core.session_id()) if fc_core.session_id() else "manager"

def watch_scope(proj, snap):
    """(direct, others) for an engineering manager's watch: the teammates this session started (crew_owners), plus every one that
    reports to an engineering manager whose session ended or never recorded one; and those another live session started."""
    if os.environ.get("COMPOSE_ROLE"):
        return (), ()
    mine, owner = fc_core.session_id(), snap.get("owner") or {}
    return ([n for n in fc_crew.crew_reporting_to(proj, "manager") if owner.get(n, mine) == mine],
            [n for n, by in owner.items() if by != mine])

def lead_sources(proj, others=()):
    """What an engineering manager's watch without `--from` listens to: its team lead, once the project has one (the `--from orchestrator`
    watch), so a watch started bare never drops the team lead's needs-user, blocked and done. Not a team lead another live session
    drives (`others`, watch_scope). The team lead's own watch runs with COMPOSE_ROLE, or from the pane its record names after a restart
    lost that env, and keeps everything but its own reports."""
    lead = fc_herdr.agent_name(proj.name, "orchestrator")
    c = fc_crew.crew_record(proj, lead)
    if os.environ.get("COMPOSE_ROLE") or not c or c.get("closed") or lead in others or crew_in_pane(proj, os.environ.get("HERDR_PANE_ID")) == lead:
        return []
    return [lead]

def cmd_watch(a, rewake=False):
    """Sleep on the project until something a team lead must act on happens, print it, exit. Zero tokens while waiting:
    run it in the background and let its exit wake you. `--now` prints the current state as events and exits.
    Each watcher (watch_key: COMPOSE_ROLE, else this engineering manager session; --from only filters what is printed) remembers what it last
    reported, so a watch started later still reports what happened while none ran. `rewake` is the Stop hook's mode: the event goes to stderr with exit 2, which wakes the session."""
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    sources = [fc_herdr.agent_ref(proj.name, x.strip()) for x in (a.from_ or "").split(",") if x.strip()]
    key = watch_key()
    quiet = is_quiet(a, proj, sources or lead_sources(proj))
    if a.now:
        cur = watch_snapshot(repo, proj)
        empty = {"tasks": {}, "agents": {}, "run": cur["run"] or cur.get("ended"), "verify": None, "halt": None,
                 "crew": {n: r[:-1] for n, r in cur["crew"].items()}}  # each teammate's latest report, once
        direct, others = watch_scope(proj, cur)
        lines = yolo_mark(proj, watch_filter(watch_events(empty, cur, started=True), proj.name, sources or lead_sources(proj, others), direct, others))
        if y := fc_tasks.yolo_state(proj):
            lines.insert(0, f"event {y['mode']}=on push={'yes' if y.get('push') else 'no'} since={y['since']!r} by={fc_tasks.yolo_by(y)!r} words={y.get('words', '')!r}")
        if fc_tasks.submit_after_verify(proj):
            lines.insert(0, "event submit_after_verify=on a passed verify submits the stack with a plain push (no --update, no land); do not ask for that push")
        if cur["run"]:
            lines.append(f"event run=live pid={(fc_tasks.run_marker(proj) or {}).get('pid')}  (a run is in progress: supervise it with watch, "
                         f"read results/run.log; do not start a second)")
        rec = {} if os.environ.get("COMPOSE_ROLE") else fc_herdr.herdr_manager_record(proj)
        if rec.get("session") and rec["session"] != fc_core.session_id() and fc_herdr.herdr_manager_live(rec["session"]):
            lines.append(f"event owner=other pane={rec['session']} since={rec.get('since')!r}  (another engineering manager session drives this project: look "
                         f"here, do not start teammates or a watch; --takeover on `crew start` or `watch` moves it to this session)")
        for line in lines or ["event idle  nothing running"]:
            print(line)
        watch_save(proj, key, base=cur)  # the reconcile is the acknowledgement: the next watch reports only what changes after it
        return
    if not os.environ.get("COMPOSE_ROLE"):
        fc_herdr.herdr_manager_claim(proj, getattr(a, "takeover", False))  # one engineering manager session per project
    lock = watch_lock(proj, key)  # a second hook leaves it to the parked one; a second watch is refused, never parked behind it
    if not lock:
        if rewake:
            return
        sys.exit(f"another compose watch of yours already runs on {proj.name} (pid {watch_holder(proj, key)}): its exit wakes you with the events, "
                 "and a second one would sit behind it printing nothing. Start `watch` again after it exits; `watch --now` shows the state meanwhile")
    with lock:  # held until this watch returns or exits
        fc_proc.keep_awake()  # a sleeping Mac sleeps the team it watches
        before = watch_base(proj, key) or watch_snapshot(repo, proj)
        emit = (lambda s: print(s, file=sys.stderr)) if rewake else print
        parent = os.getppid()
        deadline = time.time() + a.timeout if a.timeout else None
        try:
            cfg = json.loads((proj / "verify.json").read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
        limit = a.stall if a.stall is not None else (cfg.get("stall_seconds") or STALL_SECONDS)
        settle, held_since = getattr(a, "settle", 0), None
        agents, tick = Agents(), time.time()
        woke = watch_mem(proj, key).get("woke")  # the last sleep this watcher saw: a run found dead after it is the sleep's
        power_seen, power_tick = watch_mem(proj, key).get("power_seen"), 0.0  # the last battery stop this watcher announced
        scope = lambda snap: watch_scope(proj, snap)
        def look(after, stalls):
            lines = watch_events(before, after) + (owner_lines(proj.name, before, after, fc_tasks.push_stance(proj)) if not os.environ.get("COMPOSE_ROLE") else [])
            direct, others = scope(after)
            return yolo_mark(proj, watch_filter(lines + stalls, proj.name, sources or lead_sources(proj, others), direct, others))
        while True:
            time.sleep(a.interval)
            if rewake and os.getppid() != parent:
                return  # the session that parked this hook is gone
            after = watch_snapshot(repo, proj, agents)
            now = time.time()
            slept, woke_was = fc_sleep.wake_events(repo, proj, woke, tick, now)
            tick = now
            if woke_was != woke:
                woke = woke_was
                watch_save(proj, key, woke=woke)
            stalls = slept + watch_stalls(proj, after, limit, key=key, woke=woke, agent_limit=cfg.get("agent_stall_seconds")) + load_events(proj, key)  # lines handed out once: kept for the settle's second look
            if now - power_tick >= POWER_EVERY:
                power_tick = now
                line, at = fc_checks.power_back(power_seen)
                if line:
                    stalls, power_seen = stalls + [line], at
                    watch_save(proj, key, power_seen=at)
            ev = look(after, stalls)
            if ev and all(is_info(e) or quiet and not (URGENT.search(e) or ATTENTION.search(e)) for e in ev):  # progress alone: it waits for a wake to travel with, `before` stays put
                held_since = held_since or time.time()
                if quiet or time.time() - held_since < INFO_HOLD:  # a quiet watch holds it until something needs attention
                    ev = []
            if ev:
                if settle and not any(URGENT.search(e) for e in ev):  # the rest of the burst joins this wake
                    time.sleep(settle)
                    after = watch_snapshot(repo, proj, agents)
                    stalls += watch_stalls(proj, after, limit, key=key, woke=woke, agent_limit=cfg.get("agent_stall_seconds"))
                    ev = look(after, stalls)
                fc_crew.manager_attention(proj, ev)
                if rewake:
                    emit("compose watch woke you. When this turn ends the Stop hook parks the next watch; do not start one yourself.")
                    try:
                        if note := fc_hooks.skill_notice(a.session):  # a wake is a prompt the UserPromptSubmit hook never sees
                            emit(note)
                    except Exception:
                        pass
                for line in ev:
                    emit(f"[{fc_core.now()}] {line}")
                watch_save(proj, key, base=after)  # after printing: a watch killed in between reports it again rather than never
                if rewake:
                    sys.exit(2)
                return
            if held_since is None:
                before = after
            if deadline and time.time() > deadline:
                watch_save(proj, key, base=after)
                pending = sum(t["state"] in ("pending", "running", "interrupted") for t in after["tasks"].values())
                working = at_work(after, [n for n, r in after["crew"].items() if r and r[-1]["state"] == "working" and n not in after.get("closed", ())])
                print(f"[{fc_core.now()}] event timeout  nothing changed in {fc_core.fmt(a.timeout)}: pending tasks {pending}, teammates working "
                      f"{','.join(working) or 'none'}, run {'live' if after['run'] else 'not live'}, verify "
                      f"{'PASS' if after['verify'] and after['verify'][1] else 'FAIL' if after['verify'] else 'not run'}")
                sys.exit(2)

def cmd_rewake(a):
    """Claude Code Stop hook (the plugin's hooks.json, asyncRewake): when a team lead's turn ends, park a watch whose event
    wakes the idle session. Its herdr tab carries the role, project, and worktree. Every other session returns at once,
    a headless one (`claude -p`, which Claude Code marks CLAUDE_CODE_ENTRYPOINT=sdk-*) too. A wake names skill files changed since the last one."""
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
        return
    try:
        ev = json.load(sys.stdin)
    except Exception:
        ev = {}
    fc_hooks.restore_pane_env(ev.get("cwd"))  # a team lead restored after a computer restart has lost the env its tab was created with
    project = os.environ.get("COMPOSE_PROJECT")
    if os.environ.get("COMPOSE_ROLE") != "orchestrator" or not project:
        return
    session = re.sub(r"[^\w-]", "", ev.get("session_id") or "")
    cmd_watch(argparse.Namespace(repo=os.environ.get("COMPOSE_REPO") or ".", project=project, now=False, from_=None,
                                 timeout=0, stall=None, interval=WATCH_INTERVAL, settle=WATCH_SETTLE, session=session), rewake=True)

def crew_in_pane(proj, pane):
    """The teammate whose record says it runs in herdr pane `pane`, else None."""
    for f in sorted(fc_crew.crew_dir(proj).glob("*.json")) if pane else ():
        try:
            c = json.loads(f.read_text())
        except ValueError:
            continue
        if isinstance(c, dict) and c.get("pane") == pane:
            return c.get("name")
    return None

def cmd_reenter(a):
    """Claude Code SessionStart hook (matcher compact): after a compaction the session continues by re-entering through 0r, not from scratch.
    Its herdr tab carries the role, the teammate's name, project, and worktree. COMPOSE_ROLE is `crew` for every teammate but the
    guarded ones, so the brief is found by the name (a session started before its tab carried one: by the pane its record names).
    Every other session returns at once, a headless one (`claude -p`, which Claude Code marks CLAUDE_CODE_ENTRYPOINT=sdk-*) too."""
    project, role = os.environ.get("COMPOSE_PROJECT"), os.environ.get("COMPOSE_ROLE")
    if not role or not project or os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
        return
    proj = fc_core.project_dir(argparse.Namespace(repo=os.environ.get("COMPOSE_REPO") or ".", project=project))
    name = os.environ.get("COMPOSE_TEAMMATE") or crew_in_pane(proj, os.environ.get("HERDR_PANE_ID")) or fc_herdr.agent_name(project, role)
    print("Your context was just compacted; the session continues by re-entering through 0r, not from scratch.")
    print(f"Re-read your brief file {fc_crew.crew_dir(proj) / f'{name}.brief.md'} now.")
    print(f"Run `compose status --repo {os.environ.get('COMPOSE_REPO') or '.'} --project {project}` and "
          f"`compose watch --repo {os.environ.get('COMPOSE_REPO') or '.'} --project {project} --now` to see the board.")
    print("Re-read the feature's Decisions so far and Plan on Linear before acting.")
