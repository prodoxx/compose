import json, os, sys, time
from pathlib import Path
import fc_cost, fc_core, fc_load, fc_tasks, fc_worktree, fc_layers, fc_blockers, fc_budget, fc_change, fc_linear, fc_ledger, fc_tierpin, fc_tierspend, fc_update, fc_routecost, fc_shed, fc_history, fc_herdr


def project_head(repo, proj):
    """The commit a layer counts as merged into: the project branch's tip, so checking out anything else in the project's
    worktree (peeking at the stack top) changes no task's state. A project without its own branch uses HEAD."""
    for ref in (fc_core.project_branch(proj), "HEAD"):
        r = fc_core.sh(["git", "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], repo, check=False)
        if r.returncode == 0:
            return r.stdout.strip()
    return ""

def branch_facts(repo, proj, trunk_head):
    """What git says about the project's task branches: the task prefix, the branches that exist (from the repo's branch list,
    which several projects on one repo read once per process), and those merged into trunk_head."""
    prefix = fc_worktree.task_prefix(repo, proj)
    def refs(*opts):
        out = fc_core.sh(["git", "for-each-ref", "--format=%(refname)", *opts, f"refs/heads/{prefix}*"], repo, check=False).stdout
        return {l.removeprefix("refs/heads/") for l in out.split()}
    return {"prefix": prefix, "have": {b for b in fc_worktree.heads(repo) if b.startswith(prefix)},
            "merged": refs(f"--merged={trunk_head}") if trunk_head else set()}

def task_state(repo, proj, meta, trunk_head, facts=None):
    """One row for `status`: state, attempts, seconds, cost, branch note. State comes from the result file, the branch, and trunk
    (`facts`: branch_facts for the whole project; computed here when a caller reads one task). A design task has no branch, so
    Linear is its record: In Review or Done there is passed whatever this checkout holds, and it is never merged."""
    tid = meta["id"]
    res = proj / "results" / f"{tid}.json"
    r = json.loads(res.read_text()) if res.exists() else {}
    facts = facts or branch_facts(repo, proj, trunk_head)
    branch = facts["prefix"] + tid
    has_branch = branch in facts["have"]
    merged = has_branch and branch in facts["merged"]
    state = r.get("status") or "pending"
    design, linear_note = meta.get("kind") == "design", ""
    if design and state == "hand_done":
        state = "passed (hand)"
    elif design and (meta.get("linear_state") or "").lower() in ("in review", "done") and state != "passed":
        # a judge pass in another checkout, or a judge failure the designer overruled by hand: what blocks on it may start
        state, linear_note = "passed", f"{meta['linear_state']} on Linear"
    elif state == "pending" and fc_tasks.linear_done(meta):
        state = "merged"  # done on Linear (another story's task, or finished earlier): never run again, never a stall
    elif state == "running" and not fc_tasks.run_alive(proj):
        state = "interrupted"  # the run died without writing a final state; `compose resume` re-runs it
    elif state == "passed" and (merged or not has_branch) and meta.get("kind") != "design":
        state = "merged"  # an already_green task keeps its own state: its check passed with nothing to merge
    elif state == "hand_done":
        state = "hand_done" if has_branch and not merged else "merged (hand)"
    note = branch if has_branch and not merged else linear_note
    deps = [d.strip() for d in str(meta.get("after") or "").split(",") if d.strip()]
    if state == "pending" and deps:
        note = "after " + ", ".join(deps)
    hold = fc_tasks.held(proj).get(tid) or fc_tasks.waiting([meta], (facts or {}).get("released", ())).get(tid)
    if state == "pending" and hold:
        state, note = "held", hold["reason"]
    if state == "paused" and r.get("paused"):
        k = r["paused"]
        note = f"{k.get('reason') or 'paused'}; work kept ({len(k.get('files', []))} files)"
        if k.get("choice") == "part":
            note += f"; part kept ({', '.join(k.get('keep') or [])})"
    wf, dp = fc_routecost.route(proj, meta)
    n = fc_routecost.counts(r, fc_routecost.rounds(proj), tid)
    return {"id": tid, "title": meta.get("title", ""), "state": state, "attempts": r.get("attempts", 0), "seconds": r.get("seconds", 0.0),
            "cost": r.get("cost", 0.0), "note": note, "model": (r.get("model") or ("" if design else meta.get("model", ""))).split("/")[-1],
            "linear": str(meta.get("linear") or ""), "workflow": wf, "depth": dp,
            "escalations": n["escalations"], "fix_rounds": n["fix_rounds"]}

def git_dirs(repo):
    """(git dir, common dir) of a checkout: the refs live in the common dir, HEAD in the git dir."""
    c = fc_core.checkout(repo)
    return (c.git_dir, c.common) if c else (Path(repo).resolve() / ".git",) * 2

def board_stamp(repo, proj):
    """Everything the board is computed from, as stat results: the project's state files, whether its run is alive, and the
    repository's refs. Git writes a ref by renaming a lock file into place, which changes its directory's mtime, so the
    directories under refs/heads plus packed-refs and HEAD see every branch created, moved, or deleted."""
    def st(p):
        try:
            s = os.stat(p)
            return (s.st_mtime_ns, s.st_size)
        except OSError:
            return None
    files = sorted((f.name, st(f)) for f in (*proj.glob("*.json"), *(proj / "results").glob("*.json")) if not (f.parent.name == "results" and f.name.startswith("watch-")))
    gitdir, common = git_dirs(repo)
    refs = sorted((d, st(d)) for d, _, _ in os.walk(common / "refs" / "heads"))
    return (tuple(files), fc_tasks.run_alive(proj), st(gitdir / "HEAD"), st(common / "packed-refs"), tuple(refs))

_board = {}

def same_stamp(kept, proj, stamp):
    """The board `kept` (project -> (stamp, board)) holds for `proj` when nothing it was computed from changed since (its stamp is `stamp`), else None."""
    hit = kept.get(proj)
    return hit[1] if hit and hit[0] == stamp else None

def board(repo, proj):
    """The project's board: every task's row, the trunk, and the stack layers. Computed with git only when its stamp changed
    since this process last read it: every parked `watch` reads it every two seconds, from many loops at
    once, and while nothing changed that must cost no process."""
    repo, proj = Path(repo).resolve(), Path(proj)
    stamp = board_stamp(repo, proj)
    if (same := same_stamp(_board, proj, stamp)) is not None:
        return same
    tasks = fc_tasks.load_tasks(proj)
    trunk_head = project_head(repo, proj)
    facts = branch_facts(repo, proj, trunk_head)
    facts["released"] = fc_blockers.released(repo, proj, tasks, trunk_head)[0] if trunk_head else {}  # outside blockers whose code the trunk holds
    b = {"trunk": fc_core.current_branch(repo), "trunk_head": trunk_head,
         "rows": [task_state(repo, proj, m, trunk_head, facts) for m in tasks],
         "layers": {x: i + 1 for i, x in enumerate(fc_worktree.stack_order(repo, proj, tasks))}}
    lay = json.loads((proj / "layers.json").read_text()) if (proj / "layers.json").exists() else {}
    prs = json.loads((proj / "results" / "prs.json").read_text()) if (proj / "results" / "prs.json").exists() else {}
    for i, (br, keys) in enumerate(lay.items()):
        if isinstance(prs.get(br), int):
            for r in b["rows"]:
                if r["id"] in keys:
                    r["note"] = f"{facts['prefix']}{r['id']}  layer {i + 1}  PR #{prs[br]}"
    blist = fc_layers.layer_branches(repo, proj)
    b["stack"] = f"  stack {sum(isinstance(prs.get(x), int) for x in blist)}/{len(blist)} submitted" if blist else ""
    _board[proj] = (stamp, b)
    return b

FAILED_STATES = ("failed", "contradiction", "rejected_size", "rejected_docs", "rejected_scope", "rejected_regression", "rejected_tampered", "interrupted")

def verify_why(vd):
    """The few words behind a failed verify, from the last round of its verify.json: which stage failed and how many cases, and
    whether every failure was the tooling (browser, harness) rather than the code. "" when the file says nothing."""
    rounds = vd.get("rounds") or []
    last = rounds[-1] if rounds and isinstance(rounds[-1], dict) else {}
    why, failed, tool = [], 0, 0
    for stage in ("checks", "e2e"):
        rows = last.get(stage) or []
        bad = [r for r in rows if r.get("exit")]
        if bad:
            why.append(f"{stage} {len(bad)}/{len(rows)} failed")
            failed += len(bad)
            tool += sum(bool(r.get("tool_error")) for r in bad)
    rv = last.get("review") or {}
    if rv and not rv.get("pass", True):
        why.append(f"review {len(rv.get('confirmed') or [])} confirmed")
        failed += 1
    if vd.get("rejected_fixes"):
        nxt = next((r["next"] for r in vd["rejected_fixes"] if isinstance(r, dict) and r.get("next")), None)
        why.append(f"a fix needs a lower layer's file: {nxt}" if nxt else "a fix touched files outside the stack")
    text = ", ".join(why) + (" - tool errors, not the code" if failed and tool == failed else "")
    return text + (f" after {len(rounds) - 1} fix round(s)" if text and len(rounds) > 1 else "")

def digest(repo, proj):
    """The project's honest state in one line, for the product owner who asks "where are we": tasks done of all, the run, the last
    verify and why it failed, a halt, and every question waiting on them. `status --digest` prints it; `watch` sends its changes."""
    rows = board(repo, proj)["rows"]
    done = sum(r["state"].split(" ")[0] in ("merged", "passed", "hand_done", "already_green") for r in rows)
    bad = [r["id"] for r in rows if r["state"] in FAILED_STATES]
    out = [f"{proj.name}: {done}/{len(rows)} tasks done" + (f", failed {','.join(bad)}" if bad else "")]
    m = fc_tasks.run_marker(proj)
    out.append("run live" if m and fc_tasks.pid_alive(m.get("pid")) else "run interrupted" if m else "no run")
    v = proj / "results" / "verify.json"
    if v.exists():
        try:
            vd = json.loads(v.read_text())
            at = time.strftime("%H:%M", time.localtime(v.stat().st_mtime))
            why = verify_why(vd)
            out.append(f"verify PASS at {at}" if vd.get("passed") else f"verify FAIL at {at}" + (f" ({why})" if why else ""))
        except (OSError, json.JSONDecodeError):
            pass
    if (proj / "results" / "resume.json").exists():
        out.append("HALTED on payment")
    for f in sorted((proj / "results" / "crew").glob("*.status")) if (proj / "results" / "crew").exists() else []:
        try:
            last = json.loads(f.read_text().strip().splitlines()[-1])
        except (OSError, IndexError, json.JSONDecodeError):
            continue
        if last.get("state") == "needs-user":
            out.append(f"{f.stem} asks: {last.get('message', '')[:100]}")
    return "; ".join(out)

def load_lines():
    s = fc_load.update()
    lim = fc_load.limits()
    low = f"{lim['low']:.1f}"
    fmt = lambda t: time.strftime("%H:%M", time.localtime(t))
    if s["held"]:
        out = [f"load: {s['avg']:.1f} on {os.cpu_count()} cores, held since {fmt(s['since'])}: nothing new starts until it falls below {low}"]
    else:
        rel = [e for e in s["log"] if e["kind"] == "released"]
        out = [f"load: {s['avg']:.1f} on {os.cpu_count()} cores, released at {fmt(rel[-1]['at'])} (below {low})"] if rel and time.time() - rel[-1]["at"] < 600 else []
        warn = [e for e in s["log"] if e["kind"] == "warn"]
        if s.get("warn") and warn:
            out.append(f"load: {s['avg']:.1f} on {os.cpu_count()} cores is above the warn mark {lim['warn']:.1f} since {fmt(warn[-1]['at'])}: a hold starts above {lim['high']:.1f}")
    if (shed := fc_shed.state())["hold"]:
        n = sum(e["at"] >= shed["hold"]["at"] for e in shed["freed"])
        if n:
            out.append(f"shed: freed {n} since {fmt(shed['hold']['at'])}")
        if shed["hot"]:
            out.append(f"shed: hottest not started by compose: {shed['hot']['command']} (pid {shed['hot']['pid']}, {shed['hot']['cpu']:.0f}% cpu), left alone")
    if shed["paused"]:
        out.append(f"shed: paused {len(shed['paused'])}: " + ", ".join(f"{e['kind']} {e['pid']} ({e['project']})" for e in sorted(shed["paused"], key=lambda e: e["at"])))
    return out

def submit_line(proj):
    """Whether a passed verify submits the stack without asking (verify.json submit_after_verify), on the project's status."""
    if fc_tasks.submit_after_verify(proj):
        return "  submit_after_verify on: a passed verify pushes the stack as draft PRs; --update and land still need your go"
    return "  submit_after_verify off: the verified stack waits for your go (verify.json submit_after_verify: true turns it on)"

def render_status(repo, projects):
    out = []
    for proj in projects:
        b = board(repo, proj)
        trunk, rows, layers = b["trunk"], b["rows"], b["layers"]
        counts = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        v = proj / "results" / "verify.json"
        s = proj / "results" / "smoke.json"
        vtxt = ""
        if s.exists() and (not v.exists() or s.stat().st_mtime > v.stat().st_mtime):
            sd = json.loads(s.read_text())
            vtxt = f"  smoke {'PASS' if sd.get('passed') else 'FAIL'} ({', '.join('no ' + x for x in sd.get('left_out', []))}) {fc_core.fmt(sd.get('seconds', 0))} at {time.strftime('%H:%M', time.localtime(s.stat().st_mtime))}"
        elif v.exists():
            vd = json.loads(v.read_text())
            vtxt = f"  verify {'PASS' if vd.get('passed') else 'FAIL'} {fc_core.fmt(vd.get('seconds', 0))} at {time.strftime('%H:%M', time.localtime(v.stat().st_mtime))}"
        halt = proj / "results" / "resume.json"
        htxt = ""
        if halt.exists():
            hd = json.loads(halt.read_text())
            htxt = f"  HALTED {hd['halted_at'][11:16]} payment required at {hd['provider']} ({len(hd.get('remaining', []))} task(s) left) -> compose resume"
        m = fc_tasks.run_marker(proj)
        rtxt = f"  run live since {m['started_at'][11:16]} (pid {m['pid']})" if m and fc_tasks.pid_alive(m.get("pid")) else \
               (f"  run INTERRUPTED (started {m['started_at'][11:16]}) -> compose resume" if m else "")
        summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        on = f" (after {fc_core.project_after(proj)})" if fc_core.project_after(proj) else ""
        out.append(f"project {proj.name}   trunk {trunk}{on}   {len(rows)} tasks: {summary}{rtxt}{vtxt}{b['stack']}{htxt}{fc_change.change_line(proj)}")
        ready = proj / "results" / "ready.json"
        if ready.exists():
            out.append("  " + fc_linear.ready_line(json.loads(ready.read_text())))
        if y := fc_tasks.yolo_state(proj):
            out.append(f"  {y['mode']} on{' (push)' if y.get('push') else ''} since {y['since']}" + (f", set by {by}" if (by := fc_tasks.yolo_by(y)) != "unknown" else ""))
        t = fc_cost.total(fc_cost.rows(proj))
        out.append(f"  cost  {t['input']}in  {t['cache_created']}cc  {t['cache_read']}cr  {t['output']}out  ({t['teammates']} teammates)")
        out.append(submit_line(proj))
        out.append(f"  {'id':4} {'title':38} {'state':16} {'route':33} {'att':>3} {'esc':>3} {'fix':>3} {'time':>8} {'cost':>8}  {'model':14} note")
        for r in rows:
            note = r["note"]
            if note in layers:
                note = f"{note}  layer {layers[note]}"
            route = "-" if r["depth"] == "-" and r["workflow"] == "-" else f"{r['depth']} {r['workflow']}"
            out.append(f"  {r['id']:4} {r['title'][:38]:38} {r['state']:16} {route:33} {r['attempts']:>3} {r['escalations']:>3} {r['fix_rounds']:>3} {fc_core.fmt(r['seconds']):>8} {'$' + format(r['cost'], '.4f'):>8}  {r['model'][:14]:14} {note}")
        out.extend(fc_routecost.summary(rows))
        out.extend(fc_tierpin.lines(proj))
        out.extend(fc_tierspend.lines(proj, fc_budget.limit(proj)))
        out.append("")
    return "\n".join(out)

def project_summary(repo, proj):
    """One line per project: name, the feature's title, task counts by state, trunk, last activity."""
    rows = board(repo, proj)["rows"]
    counts = {}
    for r in rows:
        st = r["state"].split(" ")[0]
        counts[st] = counts.get(st, 0) + 1
    title = fc_tasks.linear_cache(proj)["feature"].get("title", "") or "(not synced from Linear yet)"
    stamps = [f.stat().st_mtime for f in (proj / "results").glob("*.json")] if (proj / "results").exists() else []
    last = time.strftime("%Y-%m-%d %H:%M", time.localtime(max(stamps))) if stamps else "never run"
    done = counts.get("merged", 0) + counts.get("passed", 0) + counts.get("hand_done", 0) + counts.get("already_green", 0)
    halted = (proj / "results" / "resume.json").exists()
    ready = proj / "results" / "ready.json"
    return {"name": proj.name, "title": title, "tasks": len(rows), "done": done, "counts": counts, "last": last, "halted": halted,
            "ready": json.loads(ready.read_text()) if ready.exists() else None,
            "after": fc_core.project_after(proj)}


def cmd_projects(a):
    """Every project (feature) across this repository's worktrees with its task progress; `status --project <name>` drills in."""
    repo = Path(a.repo).resolve()
    projects = fc_core.all_projects(repo)
    if not projects:
        sys.exit("no compose projects in this repository; `compose init --project <name>` creates one")
    print(f"{'project':24} {'done':>9}  {'last activity':17} {'worktree':34} title / states")
    for name, (wt, proj) in projects.items():
        ps = project_summary(wt, proj)
        states = ", ".join(f"{n} {k}" for k, n in sorted(ps["counts"].items(), key=lambda kv: -kv[1]))
        flag = "  HALTED (payment) -> compose resume" if ps["halted"] else ""
        ready = f"  {fc_linear.ready_line(ps['ready'])}" if ps["ready"] is not None else ""
        after = f"  (after {ps['after']})" if ps["after"] else ""
        print(f"{ps['name']:24} {ps['done']:>4}/{ps['tasks']:<4}  {ps['last']:17} {str(wt)[-34:]:34} {ps['title'] or '-'}  [{states}]{after}{flag}{ready}")

def cmd_cost(a):
    """Tokens per teammate session and per run provider; --json prints the rows and their total."""
    proj = fc_core.project_dir(a)
    runs = [e for e in fc_history.read(a.repo, proj.name, 0) if e["kind"] == "run-end"]  # every run of the project, not only the latest
    rows = fc_cost.rows(proj, runs)
    if a.json:
        print(json.dumps({"rows": rows, "total": fc_cost.total(rows)}, indent=2))
    else:
        print(fc_cost.render(rows))

def cmd_status(a):
    """Every project across the repository's worktrees with every task's state.
    --digest prints one honest line per project instead: the answer to "where are we"."""
    repo = Path(a.repo).resolve()
    if getattr(a, "history", None) is not None:  # past runs, from the history the worktrees do not hold
        print(fc_history.render(fc_history.read(repo, a.project, a.history)))
        return
    projects = fc_core.all_projects(repo)
    if a.project:
        projects = {a.project: projects[a.project]} if a.project in projects else {}
    if not projects:
        sys.exit("no compose projects in this repository")
    if a.digest:
        for wt, proj in projects.values():
            print(digest(wt, proj))
        return
    for l in load_lines():
        print(l)
    print("\n".join(render_status(wt, [proj]) for wt, proj in projects.values()), end="")

def cmd_done(a):
    """Record that the team lead finished tasks by hand (status hand_done) so `status` tells the truth, and move each one In
    Review on Linear with the reason, rolling its story and feature up as a passed task does. Requires a reason."""
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    ids = [x.strip() for x in a.only.split(",") if x.strip()]
    (proj / "results").mkdir(exist_ok=True)
    unsaved = False
    for tid in ids:
        meta = next((m for m in fc_tasks.load_tasks(proj) if m["id"] == tid), None)
        if not meta:
            print(f"{tid}: no such task (run `compose sync` if it is new on Linear)")
            continue
        res = proj / "results" / f"{tid}.json"
        prev = json.loads(res.read_text()) if res.exists() else {}
        prev.update({"id": tid, "title": meta.get("title"), "status": "hand_done", "note": a.note, "model": "orchestrator",
                     "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"), "attempts": prev.get("attempts", 0), "seconds": prev.get("seconds", 0.0),
                     "cost": prev.get("cost", 0.0), "branch": prev.get("branch", fc_worktree.task_branch(repo, proj, tid))})
        clone = Path(prev["worktree"]) if prev.get("worktree") else None
        if clone and (clone / ".git").exists():  # a task that missed finished by hand there: bring its branch in as a pass does, or the layers above start without it
            fetched = fc_core.sh(["git", "fetch", "-q", str(clone), f"{prev['branch']}:{prev['branch']}"], repo, check=False)
            if fetched.returncode:
                print(f"{tid} note: its branch {prev['branch']} was not brought in from {clone}: {fetched.stderr.strip()[-200:]}")
        sha = fc_core.sh(["git", "rev-parse", "--verify", "-q", prev["branch"] + "^{commit}"], repo, check=False).stdout.strip()
        if not fc_ledger.record_task(repo, proj, meta, [fc_ledger.event(fc_ledger.run_id(proj, tid), sha, [tid], fc_ledger.criterion_ids(meta), "done", "done", "pass", None, f"hand done: {a.note}")]):
            print(f"{tid} not done: its done event is not saved on Linear; run compose done again")
            unsaved = True
            continue
        fc_core.atomic_write(res, json.dumps(prev, indent=2))
        print(f"{tid} hand_done  {meta.get('title', '')}  ({a.note})")
        fc_linear.linear_event(proj, meta, "hand_done", prev)
        fc_herdr.herdr_close_task(proj, tid)  # the pi pane of its earlier attempts
    if unsaved:
        sys.exit(1)

def cmd_top(a):
    """Check out the project's stack top in the CLI worktree and print its path, for manual testing of the whole feature
    (nothing is merged, so the user's checkout never contains it). `--detach` returns the worktree to the trunk."""
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    trunk = fc_core.current_branch(repo)
    fc_update.refuse_while_verifying(proj, repo)
    wt = fc_worktree.stack_worktree(repo, proj, trunk)
    if a.detach:
        fc_core.sh(["git", "checkout", "-q", "--detach", trunk], wt, check=False)
        print(f"{wt} detached at {trunk}")
        return
    chain = fc_layers.linearize(repo, proj, trunk, wt)
    if fc_layers.STACK_STOP:
        sys.exit(1)  # the STOP line already said why and what to do
    if not chain:
        sys.exit("no unmerged layers; the trunk already is the top")
    fc_core.sh(["git", "checkout", "-q", "--detach", chain[-1]], wt)
    fc_worktree.ready_checkout(repo, wt, install=fc_core.project_config(proj).get("install"))
    print(f"stack top {chain[-1]} ({len(chain)} layers on {trunk}) checked out at:\n{wt}")
