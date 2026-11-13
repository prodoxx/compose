"""`compose route`: each task's workflow and depth, project policies first, Jev for the rest; recorded on the task issue
when the workspace has a Linear key, printed only without one or with COMPOSE_NO_POLICIES=1."""
import json, os, sys
from pathlib import Path

import fc_config, fc_core, fc_linear, fc_policy, fc_routejev, fc_routectx, fc_routerec, fc_tasks


def route_tasks(repo, proj, tasks, recalculate=False):
    """Route each task, print one line per task, record it with a Linear key. Returns ({id: route}, {id: why held})."""
    fc_linear.linear_ready()
    try:
        policies, note = fc_policy.read_policies(repo)
    except RuntimeError as e:
        return {}, {m["id"]: str(e) for m in tasks}
    cfg = fc_core.project_config(proj)
    env = {**fc_core.load_env(cfg.get("env_file", fc_config.ENV_FILE)), **os.environ}
    plain = [p for p in policies if not p["globs"]]
    issues = fc_tasks.linear_cache(proj).get("issues", {})
    routes = {}
    for m in tasks:
        state = fc_routectx.route_context(repo, proj, m, tasks, issues)
        h = fc_routectx.context_hash(repo, m, policies)
        rec = fc_routerec.parse_route((issues.get(m.get("linear")) or {}).get("description") or "")
        if rec and rec.get("context hash") == h and not recalculate and not rec["reason"].startswith("fallback"):  # a whole fallback is no answer to keep
            route = {"workflow": rec["workflow"], "depth": rec["depth"], "reason": rec["reason"], "hash": h}
            routes[m["id"]] = route
            print(f"{m['id']} {route['depth']} {route['workflow']}  reused {route['reason']}")
            continue
        route = fc_routejev.decide(env, cfg, state, fc_policy.glob_matches(policies, m["files"]), plain, lambda: fc_routectx.dependency_context(repo, m))
        route["note"] = note
        route["hash"] = h
        route["reason"] = fc_routerec.route_reason(route, state)
        routes[m["id"]] = route
        print(f"{m['id']} {route['depth']} {route['workflow']}  {route['reason']}")
        if fc_config.LINEAR_KEY and m.get("linear") and not fc_policy.skipped():
            try:
                record_route(proj, m, fc_routerec.route_section(route))
            except Exception as e:
                fc_core.say(f"linear {m['linear']}: route not recorded ({e})")
    return routes, {}


def record_route(proj, meta, section):
    """Write the section into the task issue's description, post it as a comment, and update the sync cache."""
    issue = fc_linear.linear_gql("query($id: String!) { issue(id: $id) { id description } }", {"id": meta["linear"]})["issue"]
    new = fc_routerec.with_route(issue.get("description") or "", section)
    fc_linear.linear_gql("mutation($id: String!, $in: IssueUpdateInput!) { issueUpdate(id: $id, input: $in) { success } }",
                         {"id": meta["linear"], "in": {"description": new}})
    fc_linear.linear_comment({"id": issue["id"]}, section)
    path = proj / "results" / "tasks.json"
    cache = json.loads(path.read_text())
    cache["issues"][meta["linear"]]["description"] = new
    fc_core.atomic_write(path, json.dumps(cache, indent=2))


def cmd_route(a):
    """Route the project's tasks: --only NNN[,NNN], --recalculate."""
    proj = fc_core.project_dir(a)
    repo = Path(a.repo).resolve()
    tasks = fc_tasks.runnable(fc_tasks.load_tasks(proj, required=True))
    if a.only:
        ids = set(fc_core.task_ids(a.only))
        tasks = [m for m in tasks if m["id"] in ids]
    if not tasks:
        line = "no tasks on Linear under this feature carry a compose block"
        sys.exit(line + (f" with ids {a.only}" if a.only else ""))
    routes, held = route_tasks(repo, proj, tasks, a.recalculate)
    if held:
        sys.exit(f"route stopped: {next(iter(held.values()))}; nothing recorded")
