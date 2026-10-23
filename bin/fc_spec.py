import json, re, sys
from pathlib import Path
import fc_core, fc_routerec, fc_tasks, fc_worktree, fc_status


def file_clashes(tasks):
    """Files two still-open tasks own without one being reachable from the other through `after:`: {file: [task ids]}."""
    deps = {m["id"]: {d.strip() for d in str(m.get("after") or "").split(",") if d.strip()} for m in tasks}
    def ordered(a, b):  # b is reached from a through `after:` links, so one runs before the other
        seen, todo = set(), [a]
        while todo:
            for d in deps.get(todo.pop(), ()):
                if d == b:
                    return True
                if d not in seen:
                    seen.add(d); todo.append(d)
        return False
    shared = {}
    for m in tasks:
        for f in m["files"]:
            shared.setdefault(f, []).append(m["id"])
    clashes = {}
    for f, ids in shared.items():
        loose = [i for i in ids if any(j != i and not ordered(i, j) and not ordered(j, i) for j in ids)]
        if loose:
            clashes[f] = loose
    return clashes

def cmd_tasks(a):
    """The task list as the user should review it before dispatch: id, title, files it owns, what it builds on, its test."""
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    tasks = fc_tasks.load_tasks(proj, required=True)
    if not tasks:
        sys.exit("no tasks on Linear under this feature carry a compose block")
    # Only tasks still to land are layers or can clash; a later wave may legitimately touch a file an already-merged task owned,
    # or a page a passed design task built (a design task never merges, so passed is its finish).
    trunk_head = fc_status.project_head(repo, proj)
    facts = fc_status.branch_facts(repo, proj, trunk_head)
    finished = lambda m, st: st in ("merged", "hand_done", "already_green") or (m.get("kind") == "design" and st == "passed")
    open_tasks = [m for m in tasks if not finished(m, fc_status.task_state(repo, proj, m, trunk_head, facts)["state"].split(" ")[0])]
    # The layers in the order the chain stacks them once each has a branch: the rule stack_order applies to the branches.
    layers = fc_worktree.layer_order(fc_tasks.code_tasks(open_tasks))
    issues = fc_tasks.linear_cache(proj).get("issues") or {}
    print(f"project {proj.name}   {len(tasks)} tasks   stack order (bottom first): {' <- '.join(layers) or '-'}")
    print(f"  {'id':4} {'route':33} {'title':40} {'after':10} {'linear':9} files | protected | test")
    for m in tasks:
        r = fc_routerec.parse_route((issues.get(m.get("linear")) or {}).get("description") or "")
        route = f"{r['depth']} {r['workflow']}" if r else "-"
        after = str(m.get("after") or "").strip() or "-"
        linear = str(m.get("linear") or "").strip() or "-"
        owned = ', '.join(m['nodes']) if m.get('kind') == 'design' else ', '.join(m['files'])
        print(f"  {m['id']:4} {route:33} {m.get('title', '')[:40]:40} {after:10} {linear:9} {owned or '-'} | {', '.join(m['protected']) or '-'} | {m['test']}")
    clashes = file_clashes(open_tasks)
    if clashes:
        print("\n  WARNING two tasks own the same file (they must be one task, or one must be `after:` the other):")
        for f, ids in clashes.items():
            print(f"    {f}: {', '.join(ids)}")
    pages = {}
    for m in (t for t in open_tasks if t.get("kind") == "design"):
        for n in m["nodes"]:
            pages.setdefault(n.split("/")[0], set()).add(m["id"])
    page_clashes = {p: sorted(ids) for p, ids in pages.items() if len(ids) > 1}
    if page_clashes:
        print("\n  WARNING two design tasks own the same page (one page per task):")
        for p, ids in page_clashes.items():
            print(f"    {p}: {', '.join(ids)}")
    big = [(m["id"], why) for m in open_tasks if (why := too_big(m))]
    if big:
        print("\n  WARNING TASK TOO BIG: split it before `run`; a task is one tiny change a fast model finishes in one short turn:")
        for tid, why in big:
            print(f"    {tid}: {why}")
    big_stories = [(k, len(fc_tasks.parse_criteria(v.get("description") or "")))
                   for k, v in issues.items()
                   if (v.get("status") or "").lower() not in fc_tasks.CANCELED and not fc_tasks.task_from_issue(dict(v, id=k))
                   and not fc_tasks.hand_task(v)]
    if any(n > 5 for _, n in big_stories):
        print("\n  WARNING STORY TOO BIG: split it into thinner stories before cutting its tasks:")
        for k, n in big_stories:
            if n > 5:
                print(f"    {k}: {n} criteria (max 5)")
    # Waves are what `run` still dispatches: an unmerged passed layer can still clash above, but it never runs again.
    to_run = [m for m in open_tasks if not fc_tasks.task_finished(proj, m)]
    waves = run_waves(to_run)
    if waves:
        print(f"\n  run waves (each wave in parallel): {'  then  '.join(', '.join(w) for w in waves)}")
    if len(to_run) >= 3 and len(waves) > len(to_run) / 2:
        print(f"\n  WARNING SERIAL CHAIN: {len(to_run)} tasks run in {len(waves)} waves, mostly one after another. Keep an `after:` "
              "(blocked-by) only where a task needs another task's code or file; the stack puts a task above its `after:` tasks and orders the rest by id, so review order needs no `after:`.")
        sys.exit(1)  # the team lead cuts every `after:` that is not a real code need before 3b


def run_waves(tasks):
    """The tasks grouped as `run` dispatches them when every task passes: a task waits only for its `after:` tasks in the list."""
    ids = {m["id"] for m in tasks}
    deps = {m["id"]: {d.strip() for d in str(m.get("after") or "").split(",") if d.strip() in ids} for m in tasks}
    waves, done = [], set()
    while len(done) < len(deps):
        wave = sorted(t for t, d in deps.items() if t not in done and d <= done)
        if not wave:  # a cycle in `after:`; `run` would mark these blocked
            break
        waves.append(wave)
        done |= set(wave)
    return waves

def spec_check(data, checks=()):
    """(ok, issue id, level, gaps) for the feature, then every non-canceled issue: a story or a task. `checks` are
    verify.json's: a task whose `test:` is one of them runs the whole suite after every implementer turn."""
    suite = {" ".join(c.split()) for c in checks}
    issues = [i for i in data["issues"] if (i.get("status") or "").lower() not in fc_tasks.CANCELED]
    tasks = [t for t in (fc_tasks.task_from_issue(i) for i in issues) if t]
    out = []
    desc = (data["feature"].get("description") or "")
    gaps = []
    m = re.search(r"## Not yet specified\n+([^\n]*)", desc)
    if not m or m.group(1).strip().rstrip(".").strip().lower() != "none":
        gaps.append(f"`## Not yet specified` holds {m.group(1).strip() if m else 'nothing'}: say `None` when the plan is settled")
    if "### Codebase brief" not in desc:
        gaps.append("no `### Codebase brief` section")
    if "## Plan" not in desc:
        gaps.append("no `## Plan` section")
    by_issue = {t["linear"]: t["id"] for t in tasks}
    tasks = [dict(t, after=",".join(by_issue[b["id"]] for b in t["blocked_by"] if b["id"] in by_issue)) for t in tasks]
    clashes = file_clashes(tasks)
    if clashes:
        detail = "; ".join(f"{f}: {', '.join(ids)}" for f, ids in clashes.items())
        gaps.append(f"two tasks own the same file (they must be one task, or one must be `after:` the other): {detail}")
    to_run = [t for t in tasks if (t["linear_state"] or "").lower() not in ("in review", "done")]  # a passed or merged layer never runs again
    waves = run_waves(to_run)
    if len(to_run) >= 3 and len(waves) > len(to_run) / 2:
        gaps.append(f"serial chain: {len(to_run)} tasks run in {len(waves)} waves, mostly one after another")
    out.append((not gaps, data["feature"]["id"], "feature", gaps))
    story_task = any("parentId" in i for i in data["issues"])
    for i in issues:
        meta = fc_tasks.task_from_issue(i)
        desc = i.get("description") or ""
        bullets = fc_tasks.parse_criteria(desc)
        if meta:
            gaps = []
            if too_big(meta):
                gaps.append(f"too big: {too_big(meta)}")
            if len(bullets) > 5:
                gaps.append(f"{len(bullets)} bullets under `## Acceptance criteria` (max 5)")
            if meta["kind"] == "design":  # it names its Figma page and frames, and its test is the judge
                if not meta["nodes"]:
                    gaps.append("empty `nodes:`")
            else:
                if not meta["files"] and not meta["delete"]:
                    gaps.append("empty `files:`")
                # `protected:` may stay empty: a task whose proof is its `test:` command alone names no test file.
                # testcrews then starts no writer for it (fc_briefs), and run checks only the paths a task names.
            if " ".join(meta["test"].split()) in suite:
                gaps.append(f"`test:` is the verify check `{meta['test']}`, the whole suite: run only this task's test (its file, or a name filter)")
            if re.search(r"(?i)contract\.md|\b(?:from|in|per|see) the (?:groundwork )?contract\b", desc):
                gaps.append("the brief points at the contract, which implementers never see: state its signatures and exact lines in the brief")
            out.append((not gaps, i["id"], "task", gaps))
        elif fc_tasks.hand_task(i):
            out.append((True, i["id"], "hand task", []))
        else:
            gaps = []
            if not bullets:
                gaps.append("no bullet under `## Acceptance criteria`")
            if len(bullets) > 5:
                gaps.append(f"too big: {len(bullets)} criteria under `## Acceptance criteria` (max 5)")
            if story_task and not any(j.get("parentId") == i["id"] for j in issues if fc_tasks.task_from_issue(j) or fc_tasks.hand_task(j)):
                gaps.append("no task carries this story as its parentId (cut its code, dependency and toolchain changes into tasks)")
            out.append((not gaps, i["id"], "story", gaps))
    return out

def cmd_spec_check(a):
    """The feature, its stories, and its tasks against the spec: one PASS or FAIL line each, exit 1 on any FAIL."""
    if a.stdin:
        try:
            d = json.loads(sys.stdin.read())
        except ValueError as e:
            sys.exit(f"spec-check: stdin is not JSON ({e}); expected {fc_tasks.SYNC_SHAPE}")
        checks = []
    else:
        proj = fc_core.project_dir(a)
        fc_tasks.load_tasks(proj, required=True)
        cache = fc_tasks.linear_cache(proj)
        d = {"feature": cache.get("feature") or {},
             "issues": [dict(v, id=k) for k, v in (cache.get("issues") or {}).items()]}
        checks = fc_core.project_config(proj).get("checks", [])
    bad = False
    for ok, ident, level, gaps in spec_check(d, checks):
        print(f"{'PASS' if ok else 'FAIL'} {ident} {level}" + (f": {'; '.join(gaps)}" if gaps else ""))
        bad = bad or not ok
    if bad:
        sys.exit(1)

MAX_TASK_FILES, MAX_TASK_BRIEF = 2, 2500  # the size the fast implementers pass first try; bigger ones are where they miss

def too_big(meta):
    """Why a task is too big for one fast turn, or None. Cheap signals only: files it owns and the length of its brief.
    A lockfile it names is regenerated, not written, so it counts toward neither the cap nor the branch's lines."""
    why = []
    owned = [f for f in meta["files"] if not fc_worktree.GENERATED_RE.search(f)]
    if len(owned) > MAX_TASK_FILES:
        why.append(f"owns {len(owned)} files (max {MAX_TASK_FILES})")
    if len(meta.get("body") or "") > MAX_TASK_BRIEF:
        why.append(f"brief is {len(meta['body'])} chars (max {MAX_TASK_BRIEF})")
    return "; ".join(why) or None
