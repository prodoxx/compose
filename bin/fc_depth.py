"""Verify at the stack's depth: the deepest route among the tasks the last passed verify did not cover, the verify settings for it,
and the stages it skips on the ledger."""


def verified_tasks(proj, repo, tasks):
    """The ids of the code tasks whose branch is already in the head of the last passed verify (`results/verify.json`), so their depth
    was paid then; empty without that file, its `head`, or a pass."""
    import json
    import fc_core
    import fc_tasks
    import fc_worktree
    f = proj / "results" / "verify.json"
    try:
        last = json.loads(f.read_text())
    except (OSError, ValueError):
        return set()
    head = last.get("head")
    if not last.get("passed") or not head:
        return set()
    prefix = fc_worktree.task_prefix(repo, proj)
    return {m["id"] for m in fc_tasks.runnable(tasks)
            if fc_core.sh(["git", "merge-base", "--is-ancestor", prefix + m["id"], head], repo, check=False).returncode == 0}


def stack_depth(proj, tasks, skip=()):
    """(depth, task id) of the stack's deepest code task outside `skip`; ("STANDARD", None) with none."""
    import fc_routerec
    import fc_tasks
    issues = fc_tasks.linear_cache(proj).get("issues") or {}
    best, best_id = "STANDARD", None
    for i, m in enumerate(t for t in fc_tasks.runnable(tasks) if t["id"] not in skip):
        route = fc_routerec.parse_route((issues.get(m.get("linear")) or {}).get("description") or "")
        depth = route.get("depth") if route else None
        if depth not in fc_tasks.DEPTHS:
            depth = "STANDARD"
        if best_id is None or fc_tasks.DEPTHS.index(depth) > fc_tasks.DEPTHS.index(best):
            best, best_id = depth, m["id"]
    return best, best_id


def depth_config(cfg, proj, tasks, skip=()):
    """(new cfg, depth, task id): the verify cfg at the stack's depth, the tasks in `skip` not counted, `cfg` left unchanged."""
    import fc_routerec
    import fc_tasks
    depth, task_id = stack_depth(proj, tasks, skip)
    issues = fc_tasks.linear_cache(proj).get("issues") or {}
    code = fc_tasks.code_tasks(tasks)
    reqs = []
    for m in code:
        route = fc_routerec.parse_route((issues.get(m.get("linear")) or {}).get("description") or "")
        value = (route or {}).get("verification requirements") or ""
        reqs += [p.strip() for p in value.split(",") if p.strip() and p.strip() != "none"]
    tests = [m["test"] for m in code] if depth == "FAST" else []
    new = {**cfg, "depth": depth, "checks": list(dict.fromkeys([*cfg.get("checks", []), *tests, *reqs]))}
    if "e2e" in cfg:
        new["e2e"] = list(cfg["e2e"])
    if depth == "FAST":
        named = {line.strip()[5:].strip() for m in code for line in m["body"].splitlines() if line.strip().startswith("case:")}
        new["review"] = False
        new["e2e"] = [c for c in cfg.get("e2e", []) if c["name"] in named]
    if depth == "DEEP":
        new["acceptance_gate"] = True
    return new, depth, task_id


def depth_events(cfg, run_cfg, depth, task_id, sha):
    """One `skipped` ledger event per stage `cfg` runs and `run_cfg` does not."""
    import fc_ledger
    ev = lambda phase, stage: fc_ledger.event("", sha, [], [], phase, "verify", "skipped", None, f"{stage} skipped at depth {depth} (deepest task {task_id})")
    events = []
    if cfg.get("review", True) and not run_cfg.get("review", True):
        events.append(ev("review", "review"))
    if depth == "FAST" and cfg.get("jev_acceptance", True):
        events.append(ev("acceptance", "acceptance"))
    names = {c["name"] for c in run_cfg.get("e2e", [])}
    for c in cfg.get("e2e", []):
        if c["name"] not in names:
            events.append(ev("e2e", f"e2e {c['name']}"))
    return events


def record_depth(proj, repo, cfg, run_cfg, depth, task_id):
    """Write the skipped stages on the feature issue's ledger; add_row's answer, True with none."""
    import fc_core
    import fc_ledger
    import json
    sha = fc_core.sh(["git", "rev-parse", "HEAD"], repo, check=False).stdout.strip()
    events = depth_events(cfg, run_cfg, depth, task_id, sha)
    if not events:
        return True
    feature = json.loads((proj / "linear.json").read_text()).get("feature")
    return fc_ledger.add_row(feature, events)
