"""compose review: the diff review of one task's change, or of a commit range, outside the full verify."""

import json, re, sys, time
from pathlib import Path
import fc_core, fc_tasks, fc_worktree, fc_models, fc_ledger, fc_review, fc_verify


def resolve(repo, ref):
    """The commit `ref` names, or None."""
    p = fc_core.sh(["git", "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], repo, check=False)
    return p.stdout.strip() if p.returncode == 0 else None

def task_range(repo, proj, cfg, tid):
    """(base, tip) of task `tid`: its branch, and the branch it was built on (results/NNN.json `base`, the highest layer below it)
    while that still exists, else the review base."""
    tip = fc_worktree.task_branch(repo, proj, tid)
    if not resolve(repo, tip):
        sys.exit(f"review: task {tid} has no branch {tip}; review a change that is merged with --range <base>..<tip>")
    f = proj / "results" / f"{tid}.json"
    built_on = (json.loads(f.read_text()).get("base") if f.exists() else None) or "HEAD"
    base = built_on if built_on != "HEAD" and resolve(repo, built_on) else fc_worktree.review_base(repo, cfg)
    if not base:
        sys.exit(f"review: no base branch for task {tid}; pass --range <base>..<tip>")
    return base, tip

def given_range(repo, text):
    """(base, tip) of a `<base>..<tip>` argument whose two ends are commits."""
    base, _, tip = text.partition("..")
    if not (base and tip) or tip.startswith(".") or not (resolve(repo, base) and resolve(repo, tip)):
        sys.exit(f"review: --range wants <base>..<tip>, two commits that exist here; got {text!r}")
    return base, tip

def review_event(proj, meta, sha, rv):
    """The ledger event of one review, for `meta`'s task or (None) the feature: pass, fail on a confirmed high, error when only a
    confirm call failed."""
    result = "pass" if rv["pass"] else "fail" if any(i["severity"] == "high" for i in rv["confirmed"]) else "error"
    run, ids = (fc_ledger.run_id(proj, meta["id"]), fc_ledger.criterion_ids(meta)) if meta else ("", [])
    return fc_ledger.event(run, sha, [meta["id"]] if meta else [], ids, "review", "review", result, None,
                           f"{len(rv['confirmed'])} confirmed of {len(rv['issues'])} raised")

def cmd_review(a):
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    only = fc_core.task_ids(a.only)
    if not only and not a.range:
        sys.exit("review needs --only <ids> (each task's own change) or --range <base>..<tip>")
    cfg_path = proj / "verify.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    tasks = fc_tasks.load_tasks(proj, required=bool(only))
    metas = {m["id"]: m for m in tasks}
    if [x for x in only if x not in metas]:
        sys.exit(f"review: no task {', '.join(x for x in only if x not in metas)} in the last sync; `compose tasks --project {proj.name}` lists them")
    ranged = given_range(repo, a.range) if a.range else None
    # One review of the range (the tasks named only brief it), else one review per task over its own change.
    scopes = [(ranged, [metas[x] for x in only])] if ranged else [(task_range(repo, proj, cfg, x), [metas[x]]) for x in only]
    path = proj / "linear.json"
    feature = json.loads(path.read_text()).get("feature") if path.exists() else None
    (proj / "results").mkdir(exist_ok=True)
    ok = True
    for (base, tip), group in scopes:
        t0 = time.time()
        name = ",".join(m["id"] for m in group) or "the range"
        spec = f"{base}..{tip}" if ranged else f"{base}...{tip}"
        diff = fc_core.sh(["git", "diff", spec], repo, check=False).stdout
        if not diff.strip():
            sys.exit(f"review: {name} has no change in {spec}; a task already merged into {base} is reviewed with --range <base>..<tip>")
        diff, plan, docs, _ = fc_verify.review_inputs(repo, proj, cfg, group, diff)
        fc_core.say(f"review start {name}  {spec}")
        try:
            rv = fc_review.llm_review(cfg, diff, plan, docs, False)
        except fc_models.PaymentRequired as e:
            sys.exit(f"review stopped on PAYMENT REQUIRED ({e}); nothing was recorded for {name}")
        rv["seconds"] = round(time.time() - t0, 1)
        sha = resolve(repo, tip)
        fc_core.atomic_write(proj / "results" / (f"review-{group[0]['id']}.json" if not ranged else f"review-range-{resolve(repo, base)[:7]}-{sha[:7]}.json"),
                             json.dumps({"scope": spec, "tip": sha, "tasks": [m["id"] for m in group], "review": rv, "cost": fc_core.LEDGER.snapshot()}, indent=2))
        ok &= rv["pass"]
        fc_core.say(f"review {'ok  ' if rv['pass'] else 'FAIL' if any(i['severity'] == 'high' for i in rv['confirmed']) else 'TOOL ERROR'} {fc_core.fmt(rv['seconds']):>8}  "
                    f"{name}: {len(rv['confirmed'])} confirmed of {len(rv['issues'])} raised"
                    + (f", {rv['malformed']} malformed dropped" if rv.get("malformed") else ""))
        if rv.get("error"):
            print(f"           {rv['error']}")
        for i in rv["confirmed"]:
            print(f"           confirmed {fc_verify.issue_line(i)}\n             Evidence: {i.get('evidence', '')}"
                  + (f"\n             Must read: {i['fix'] or '(remove the line)'}" if "fix" in i else ""))
        # The review lands where Jev's sufficiency judgment reads it: each reviewed task's issue, else the feature's.
        for m in group:
            fc_ledger.record_task(repo, proj, m, [review_event(proj, m, sha, rv)])
        if not group:
            fc_ledger.record(feature, [review_event(proj, None, sha, rv)])
    for issue in list(dict.fromkeys(i for i, _ in fc_ledger.UNSAVED)):
        fc_ledger.add_row(issue, [])
    lost = fc_ledger.unsaved()
    if lost:
        ok = False
        fc_core.say("review FAIL  evidence not saved on Linear: " + ", ".join(r["event_id"] for r in lost))
    fc_core.say(fc_core.LEDGER.line())
    sys.exit(0 if ok else 1)
