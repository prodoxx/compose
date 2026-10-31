"""The evidence ledger's write side: evidence events, written as JSON lines in a fenced block of one Linear comment per
batch on the task or feature issue, retried, and kept in memory while unsaved. Stages hand it events and never write
Linear themselves; fc_evidence reads the rows back and derives the `## Evidence` section."""

import hashlib, json, secrets, time

import fc_core, fc_evidence, fc_linear, fc_tasks

KEYS = ("event_id", "run_id", "commit_sha", "task_ids", "criterion_id", "phase", "source", "result", "fails_on_base", "detail")

def event(run_id, commit_sha, task_ids, criterion_id, phase, source, result, fails_on_base, detail):
    """One evidence event: a dict with the keys of KEYS in order and an event_id hashed from the rest."""
    d = {"run_id": run_id, "commit_sha": commit_sha, "task_ids": list(task_ids), "criterion_id": list(criterion_id), "phase": phase,
         "source": source, "result": result, "fails_on_base": fails_on_base,
         "detail": " ".join(fc_core.scrub(detail).split())[:160]}
    return {"event_id": hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12], **d}

def criterion_ids(meta):
    """`<task id>.<order>:<hash of the text>` for each criterion of the task, in order."""
    return [f"{meta['id']}.{n}:{hashlib.sha256(c.encode()).hexdigest()[:8]}"
            for n, c in enumerate(fc_tasks.parse_criteria(meta.get("body", "")), 1)]

def run_id(proj, tid, fresh=False):
    """The task's run id, kept in results/runs.json; a new one when it has none or `fresh` is true."""
    f = proj / "results" / "runs.json"
    runs = json.loads(f.read_text()) if f.exists() else {}
    if fresh or tid not in runs:
        runs[tid] = secrets.token_hex(6)
        (proj / "results").mkdir(exist_ok=True)
        fc_core.atomic_write(f, json.dumps(runs, indent=2))
    return runs[tid]

UNSAVED = []
SAVED = set()

def comment_text(rows):
    """One comment holding a batch of rows in an `evidence` fenced block."""
    phases = []
    for r in rows:
        if r["phase"] not in phases:
            phases.append(r["phase"])
    return f"Evidence: {rows[0]['source']} {', '.join(phases)}\n\n```evidence\n" + \
        "\n".join(json.dumps(r) for r in rows) + "\n```"

def landed(issue, batch):
    """The event ids of `batch` already in the issue's comments, none when they cannot be read: a write that timed out may have
    landed after all, and writing it again would add a second identical row."""
    try:
        return {r["event_id"] for r in fc_evidence.rows(issue)} & {r["event_id"] for r in batch}
    except Exception:
        return set()

def add_row(issue, rows):
    """Write the issue's unsaved rows and these as one comment, retried; False while anything stays unsaved. Before every try
    but a first one on fresh rows, the rows already on the issue are left out (see landed), so a retry never repeats a write
    that went through. A write still in flight when its timeout is given up on can land beside the retry: the reading side
    keeps one row per event id."""
    if not issue or not fc_linear.linear_ready():
        return True
    seen, batch = set(), []
    unsure = any(t[0] == issue for t in UNSAVED)  # an earlier call gave up on rows whose last write may have landed
    for r in [t[1] for t in UNSAVED if t[0] == issue] + list(rows):
        if (issue, r["event_id"]) in SAVED or r["event_id"] in seen:
            continue
        seen.add(r["event_id"])
        batch.append(r)
    if not batch:
        return True
    err = ""
    for n in range(3):
        try:
            if n or unsure:
                there = landed(issue, batch)
                SAVED.update((issue, e) for e in there)
                batch = [r for r in batch if r["event_id"] not in there]
            if batch:
                fc_linear.linear_comment(fc_linear.linear_issue(issue), comment_text(batch))
                SAVED.update((issue, r["event_id"]) for r in batch)
            UNSAVED[:] = [t for t in UNSAVED if t[0] != issue]
            return True
        except Exception as e:
            err = str(e)
            if n < 2:
                time.sleep(2)
    UNSAVED[:] = [t for t in UNSAVED if t[0] != issue] + [(issue, r) for r in batch]
    fc_core.say(f"ledger {issue}: evidence not saved after 3 tries, kept in memory: "
                f"{', '.join(r['event_id'] for r in batch)} ({err[:200]})")
    return False

def unsaved():
    """The rows not saved yet, every issue, in order."""
    return [r for _, r in UNSAVED]

def record(issue, events):
    """Write the events on the issue, then refresh its `## Evidence` section; the write's result."""
    ok = add_row(issue, events)
    if ok:
        fc_evidence.refresh(issue)
    return ok

def run_events(proj, repo, meta, result):
    """The evidence events of one finished task's run result, in order."""
    if result["status"] == "held":
        return []
    sha = lambda ref: fc_core.sh(["git", "rev-parse", "--verify", "-q", ref + "^{commit}"], repo, check=False).stdout.strip()
    run, ids = run_id(proj, meta["id"]), criterion_ids(meta)
    ev = lambda s, phase, res, base, detail: event(run, s, [meta["id"]], ids, phase, "run", res, base, detail)
    if result["status"] == "already_green":
        return [ev(sha("HEAD"), "red", "pass", False, "test already passes before the change")]
    out = [ev(sha(result["base"]), "red", "fail_expected", True, "test fails before the change")] if "base" in result else []
    for e in result.get("log") or []:
        if "test_exit" in e:
            tampered = e.get("tampered") or []
            lines = [ln for ln in (e.get("test_output") or "").splitlines() if ln.strip()]
            detail = f"attempt {e['attempt']}: test exit {e['test_exit']}" + (f", tampered {', '.join(tampered)}" if tampered else "") + \
                (f": {lines[-1]}" if lines else "")
            out.append(ev("", "attempt", "fail" if e["test_exit"] or tampered else "pass", None, detail))
        elif e.get("error") and not e.get("rate_limited"):
            out.append(ev("", "attempt", "error", None, f"attempt {e['attempt']}: {e['error']}"))
    status = result["status"]
    final = "pass" if status == "passed" else "fail" if status == "failed" or status.startswith("rejected_") else "error"
    out.append(ev(sha(result["branch"]) if status == "passed" else "", "final", final, None, f"{status} after {result['attempts']} attempts"))
    return out

def counted(repo, proj, meta, rows):
    """Copies of the rows that count for the task: its run, its revision at the task branch tip, and its current criteria."""
    sha = lambda ref: fc_core.sh(["git", "rev-parse", "--verify", "-q", ref + "^{commit}"], repo, check=False).stdout.strip()
    f = proj / "results" / f"{meta['id']}.json"
    b = (json.loads(f.read_text()).get("branch") or "HEAD") if f.exists() else "HEAD"
    tip = sha(b) or sha("HEAD")
    same = lambda s, paths: bool(paths) and fc_core.sh(["git", "diff", "--quiet", s, tip, "--", *paths], repo, check=False).returncode == 0
    run, ids = run_id(proj, meta["id"]), criterion_ids(meta)
    out = []
    for r in rows:
        if r["task_ids"] and meta["id"] not in r["task_ids"]:
            continue
        if not r.get("shared") and r["run_id"] != run:
            continue
        if r["commit_sha"]:
            if r.get("fails_on_base"):
                if not same(r["commit_sha"], meta.get("protected") or []):
                    continue
            elif r["commit_sha"] != tip and not same(r["commit_sha"], meta.get("files") or []):
                continue
        kept = [c for c in r["criterion_id"] if c in ids]
        if r["criterion_id"] and not kept:
            continue
        out.append({**r, "criterion_id": kept})
    return out

def record_task(repo, proj, meta, events):
    """`record` for a task issue: write the events, then refresh its `## Evidence` section from the rows that count. No events,
    no Linear round trip: the section already shows the rows that count."""
    if not events:
        return True
    issue = meta.get("linear")
    path = proj / "linear.json"
    feature = json.loads(path.read_text()).get("feature") if path.exists() else None
    ok = add_row(issue, events)
    if ok:
        fc_evidence.refresh(issue, lambda i: counted(repo, proj, meta, fc_evidence.task_rows(i, feature, meta["id"])))
    return ok

def verify_record(proj, repo, report):
    """Record one verify round's evidence: checks, e2e, review on the feature issue, scoped and acceptance on each task issue in
    one batch; True when all saved. An acceptance row the round reused from an earlier verify is on its issue already and
    still counts (the task's files are unchanged since), so it is not written again."""
    path = proj / "linear.json"
    feature = json.loads(path.read_text()).get("feature") if path.exists() else None
    sha = fc_core.sh(["git", "rev-parse", "HEAD"], repo, check=False).stdout.strip()

    def result(e):
        if e.get("tool_error") or "JEV_GAVE_UP" in e.get("output", "") + e.get("stdout", "") + e.get("stderr", ""):
            return "error"
        return "pass" if e["exit"] == 0 else "fail"

    ev = lambda phase, r, detail: event("", sha, [], [], phase, "verify", r, None, detail)
    checks = [ev("checks", result(c), f"{c['cmd']}: exit {c['exit']}") for c in report.get("checks", [])]
    e2e = [ev("e2e", result(c), f"{c['name']}: exit {c['exit']}") for c in report.get("e2e", [])]
    rv = report.get("review")
    if rv is None:
        review = [ev("review", "skipped", "review did not run")]
    else:
        review = [ev("review", "pass" if rv["pass"] else "fail", f"{len(rv['confirmed'])} confirmed of {len(rv['issues'])} raised")]
    acc = report.get("acceptance")
    rows = (acc or {}).get("criteria")
    oks = [record(feature, checks or [ev("checks", "skipped", "no checks ran")]),
           record(feature, e2e or [ev("e2e", "skipped", "no e2e cases ran")]),
           record(feature, review)]
    if not rows:
        if acc is None:
            oks.append(record(feature, [ev("acceptance", "skipped", "acceptance did not run")]))
        elif "error" in acc:
            oks.append(record(feature, [ev("acceptance", "error", acc["error"])]))
        else:
            oks.append(record(feature, [ev("acceptance", "skipped", acc.get("note", ""))]))
    for t in fc_tasks.code_tasks(fc_tasks.load_tasks(proj)):
        ids, rid = criterion_ids(t), run_id(proj, t["id"])
        texts = fc_tasks.parse_criteria(t.get("body", ""))
        tev = lambda cid, phase, r, detail: event(rid, sha, [t["id"]], cid, phase, "verify", r, None, detail)
        scoped = [tev(ids, "scoped", result(c), f"{c['cmd']}: exit {c['exit']}") for c in report.get("scoped", []) if c["cmd"] == t["test"]]
        acceptance = []
        for row in (rows or []):
            if row["task"] != t["id"] or row.get("reused"):
                continue
            r = "error" if row.get("judge_error") or row["choice"] is None else "pass" if row["choice"] == "MET" or row.get("proven_by") else "fail" if row.get("confirmed") else "unclear"
            acceptance.append(tev([ids[texts.index(row["criterion"])]], "acceptance", r, f"{row['choice']}: p_met {row['p_met']}, p_not_met {row['p_not_met']}" + (f", proven by test event {row['proven_by']}" if row.get("proven_by") else "")))
        oks.append(record_task(repo, proj, t, scoped + acceptance))
    return all(oks)
