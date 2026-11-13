"""The FAST route (fix-then-regression-test): its holds before a run, its green-base check, and its run's ledger events."""


def workflow(proj, meta):
    """The task's routed workflow from its issue description in the sync cache; None without a route."""
    import fc_routerec, fc_tasks
    issue = fc_tasks.linear_cache(proj).get("issues", {}).get(meta.get("linear"))
    if not issue:
        return None
    route = fc_routerec.parse_route(issue.get("description") or "")
    return route["workflow"] if route else None


def run_holds(repo, proj, tasks, routes):
    """{id: why} for each FAST task with no expected behavior; prints that an implement-then-test task runs as test-first."""
    import fc_core, fc_tasks, fc_routectx
    held = {}
    for meta in tasks:
        route = routes.get(meta["id"])
        if not route:
            continue
        if route["workflow"] == "implement-then-test":
            fc_core.say(f"{meta['id']} route   implement-then-test is not implemented yet: it runs as test-first")
        elif route["workflow"] == "fix-then-regression-test":
            c = fc_routectx.route_context(repo, proj, meta, tasks, fc_tasks.linear_cache(proj)["issues"])
            if not c["criteria"] and not c["bug"]["documented"] and not c["bug"]["confirmed"]:
                held[meta["id"]] = "no expected behavior: add a Reproduction section or acceptance criteria"
    return held


def broken_base(proj, meta, red, base_name, branch, wt):
    """The `broken_base` result of a FAST task whose test fails on the base before any change."""
    import json
    import fc_core, fc_linear
    fc_core.say(f"{meta['id']} broken  test exits {red.returncode} on {base_name} before any change: an environment problem, so no implementer turn")
    result = {"id": meta["id"], "title": meta.get("title"), "status": "broken_base", "model": meta["model"], "attempts": 0,
              "seconds": 0.0, "cost": 0.0, "branch": branch, "worktree": str(wt), "diffstat": "",
              "log": [{"base_exit": red.returncode, "test_output": fc_core.excerpt(red.stdout + red.stderr)}]}
    (proj / "results").mkdir(exist_ok=True)
    fc_core.atomic_write((proj / "results" / f"{meta['id']}.json"), json.dumps(result, indent=2))
    fc_linear.linear_event(proj, meta, "failed", result)
    return result


def run_events(proj, repo, meta, result):
    """The task's run events for the ledger: a FAST task's carry no `red` event and end `unproven` when it is."""
    import fc_ledger
    flow = workflow(proj, meta)
    if flow == "check-only":
        events = fc_ledger.run_events(proj, repo, meta, result)
        if not events:
            return []
        events = [fc_ledger.event(e["run_id"], e["commit_sha"], e["task_ids"], e["criterion_id"], e["phase"], e["source"], e["result"], e["fails_on_base"], f"check-only: {meta['test']}: {e['detail']}") for e in events]
        first = events[0]
        events.append(fc_ledger.event(first["run_id"], "", first["task_ids"], first["criterion_id"], "red", "run", "skipped", None, "regression test skipped (check-only)"))
        return events
    if flow != "fix-then-regression-test":
        return fc_ledger.run_events(proj, repo, meta, result)
    unproven = result["status"] == "unproven"
    events = fc_ledger.run_events(proj, repo, meta, {**result, "status": "passed"} if unproven else result)
    events = [e for e in events if e["phase"] != "red"]
    if unproven:
        e = events[-1]
        events[-1] = fc_ledger.event(e["run_id"], e["commit_sha"], e["task_ids"], e["criterion_id"], "final", "run", "unproven", None, f"unproven after {result['attempts']} attempts")
    return events
