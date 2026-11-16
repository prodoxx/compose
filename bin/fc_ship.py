"""Shipping a verified stack: check every layer before it is submitted, reword its commits, land it on the base."""
import json, os, subprocess, sys, time
from pathlib import Path
import fc_core, fc_tasks, fc_worktree, fc_layers, fc_stack, fc_layergate, fc_verify, fc_update, fc_crew, fc_herdr
from types import SimpleNamespace

LAND_POLL = 20      # seconds between looks at a pull request that is not ready yet
LAND_SETTLE = 15    # seconds after a push before its required checks are read: GitHub registers them a moment later
LAND_METHODS = ("squash", "merge", "rebase")

def layer_changed(info, tip):
    """True when a layer's PR (`info`, or None) is open and GitHub holds a head other than the local `tip`."""
    return bool(info) and info["state"] == "OPEN" and info["headRefOid"] != tip

def change_cause(own, moved):
    """Why a layer moved since its PR: the files it owns (`own`, its diff against the layer below) that changed between GitHub's head and the tip (`moved`), else a layer below it or the base moved."""
    hit = sorted(set(own) & set(moved))
    return f"its own files changed: {', '.join(hit)}" if hit else "a layer below it or the base moved"

def changed_stop(i, b, info, tip, cause):
    """The STOP of a submit that finds layer `i` (branch `b`) changed since its PR `info`."""
    return (f"layer {i} ({b}) changed since PR #{info['number']}: GitHub has {info['headRefOid'][:9]}, here {tip[:9]}; {cause}. "
            "Nothing was pushed. `compose stack --submit --update` pushes it with --force-with-lease")

def lease_push(b, head):
    """The git command that pushes layer `b` over the `head` GitHub holds, and nothing else."""
    return ["git", "push", "-q", f"--force-with-lease={b}:{head}", "origin", f"{b}:{b}"]

def layer_gate(a, repo, proj, layers, trunk, wt):
    """The gate of a submit, run once the layers stand and before anything is pushed: fc_layergate.layer_checks, then every layer whose open PR
    holds another head than its tip stops the submit; with --update it is pushed with a lease on that head, bottom up, and the first push refused stops it. "" when the submit may go on."""
    if why := fc_layergate.layer_checks(repo, proj, layers, trunk, wt, getattr(a, "fresh", False)):
        return why
    below = fc_stack.layer_base(repo, proj, trunk)
    for i, l in enumerate(layers, 1):
        b = l["branch"]
        info = pr_info(b, wt)
        tip = fc_core.sh(["git", "rev-parse", b], repo).stdout.strip()
        prev, below = below, b
        if not layer_changed(info, tip):
            continue
        head = info["headRefOid"]
        own = fc_core.sh(["git", "diff", "--name-only", prev, b], repo).stdout.split()
        cause = change_cause(own, fc_core.sh(["git", "diff", "--name-only", head, tip], repo, check=False).stdout.split())
        if not a.update:
            return changed_stop(i, b, info, tip, cause)
        q = fc_core.sh(lease_push(b, head), repo, check=False)
        if q.returncode:
            return f"push of {b} refused: {q.stderr.strip()[-200:]}"
        fc_core.say(f"submit update {b} {tip[:9]} (was {head[:9]})")
    return ""

def cmd_stack(a):
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    trunk = a.trunk or fc_core.current_branch(repo)
    fc_update.refuse_while_verifying(proj, repo)
    gate = lambda repo, proj, layers, trunk, wt: layer_gate(a, repo, proj, layers, trunk, wt)
    if not fc_stack.register_stack(repo, proj, trunk, submit=a.submit, preview=not a.submit, gate=gate):
        if fc_layers.STACK_STOP:
            sys.exit(1)  # the STOP line already said why and what to do
        print("stack  nothing to register: no unmerged task branches")

def verify_passed_now(proj, since):
    """True when a full verify wrote a passing results/verify.json at or after `since` (smoke and review-only runs write other files)."""
    f = proj / "results" / "verify.json"
    try:
        return f.stat().st_mtime >= since and json.loads(f.read_text()).get("passed") is True
    except (OSError, ValueError, AttributeError):
        return False

def submit_stop_state(why):
    """`needs-user` when the stop is a push only the product owner may allow (a changed layer needs `--update`, a force push; a base
    ahead of its remote needs its own push), else `blocked`."""
    return "needs-user" if "--update" in why or " ahead of " in why else "blocked"

def submit_after_verify(a, proj):
    """The project's standing go (verify.json submit_after_verify) after a passing verify: one plain `compose stack --submit`, which pushes
    new branches and opens draft PRs. Never --update (a force push), never land. A stop is reported to the team lead and tried no more."""
    fc_core.say("verify passed and submit_after_verify is on: submitting the stack with a plain push (no --update, no land)")
    ns = SimpleNamespace(repo=a.repo, project=getattr(a, "project", None), trunk=getattr(a, "trunk", None), submit=True, update=False, fresh=False)
    fc_layers.STACK_STOP = ""
    try:
        cmd_stack(ns)
        return
    except SystemExit as e:
        why = fc_layers.STACK_STOP or f"compose stack exited {e.code}"
    state = submit_stop_state(why)
    ask = " A force push (`compose stack --submit --update`, --force-with-lease) needs your go." if "--update" in why else ""
    message = f"verified stack not pushed: {why}.{ask}"
    name = fc_herdr.agent_name(proj.name, "orchestrator")
    if fc_crew.crew_record(proj, name):
        fc_crew.write_report(proj, name, state, message)
        fc_crew.crew_attention(proj, name, state, message)
    fc_core.say(f"submit_after_verify {state}: {message}")

def cmd_verify(a):
    """`compose verify`, then the project's standing go for the push: with verify.json submit_after_verify on, a full verify that passed submits the stack."""
    proj = fc_core.project_dir(a)
    started = time.time()
    code = 0
    try:
        fc_verify.cmd_verify(a)
    except SystemExit as e:
        code = e.code
    if code in (0, None) and fc_tasks.submit_after_verify(proj) and verify_passed_now(proj, started):
        submit_after_verify(a, proj)
    sys.exit(code)

def layer_commits(repo, proj, trunk):
    """[(branch, [commit, ...])] bottom first: each layer's own commits, oldest first."""
    out, below = [], fc_stack.layer_base(repo, proj, trunk)
    for b in fc_layers.layer_branches(repo, proj):
        out.append((b, fc_core.sh(["git", "rev-list", "--reverse", f"{below}..{b}"], repo).stdout.split()))
        below = b
    return out

def cmd_reword(a):
    """List the stack's commits with the ones that break the commit rules flagged; with --set SHA=MESSAGE, choose a commit's
    message and rebuild the layers with it. The choice is kept by the commit (author, date, files), so every later rebuild
    applies it. Layers are rebuilt in compose's own worktree; nothing is pushed."""
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    trunk = a.trunk or fc_core.current_branch(repo)
    conv = fc_core.commit_convention(proj)
    layers = layer_commits(repo, proj, trunk)
    if not layers:
        fc_core.say("reword nothing to reword: no layers; `compose stack` builds them")
        sys.exit(1)
    subject = lambda c: fc_core.sh(["git", "show", "-s", "--format=%s", c], repo).stdout.strip()
    prefix = fc_layers.layer_prefix(repo, proj)
    show = lambda: [print(f"  {b.removeprefix(prefix)}  {c[:9]}  {subject(c)}"
                          + (f"   <- {why}" if (why := fc_layers.message_problem(fc_core.sh(['git', 'show', '-s', '--format=%B', c], repo).stdout, conv)) else ""))
                    for b, cs in layers for c in cs]
    if not a.set:
        show()
        print(f"reword set one with `compose reword --project {proj.name} --set <sha>=<message>`; `compose stack` also rewrites a flagged message on its own")
        return
    fc_update.refuse_while_verifying(proj, repo)
    every = [c for _, cs in layers for c in cs]
    chosen = dict(fc_layers.rewords(proj))
    for item in a.set:
        sha, _, message = item.partition("=")
        hits = [c for c in every if sha and c.startswith(sha)]
        if len(hits) != 1:
            fc_core.say(f"reword FAIL  {sha!r} names {len(hits)} commits of the stack; `compose reword --project {proj.name}` lists them")
            sys.exit(1)
        why = "" if a.as_is else fc_layers.message_problem(message, conv)
        if why:
            fc_core.say(f"reword FAIL  {message.splitlines()[0] if message.strip() else '(empty)'!r}: {why}")
            sys.exit(1)
        chosen[fc_layers.commit_key(repo, hits[0])] = {"message": message.strip(), "was": subject(hits[0])}
    fc_core.atomic_write(Path(proj) / "rewords.json", json.dumps(chosen, indent=2))
    if not fc_stack.register_stack(repo, proj, trunk):
        fc_core.say("reword the choice is kept, but the layers were not rebuilt" + (f": {fc_layers.STACK_STOP}" if fc_layers.STACK_STOP else ""))
        sys.exit(1)
    layers = layer_commits(repo, proj, trunk)
    show()
    print("reword layers rebuilt, nothing pushed. A layer that already has a PR is pushed with its new commits by the next `compose stack --submit`")

def gh(args, cwd):
    """A gh call that can never stop to ask: no prompt, no stdin."""
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env={**os.environ, "GH_PROMPT_DISABLED": "1"})

def pr_view(which, cwd):
    """(the pull request for a PR number or a head branch or None, gh's error). The error is empty when gh found the PR
    or said there is none; any other failure of `gh pr view` is named in it."""
    p = gh(["pr", "view", str(which), "--json", "number,baseRefName,state,isDraft,headRefOid,url,mergeStateStatus"], cwd)
    said = (p.stderr or p.stdout).strip()
    if p.returncode:
        if "no pull requests found" in said.lower() or "could not resolve to a pullrequest" in said.lower():
            return None, ""
        return None, said.splitlines()[-1][:300] if said else f"gh exited {p.returncode}"
    try:
        return json.loads(p.stdout), ""
    except ValueError:
        return None, "gh printed no JSON"

def pr_info(which, cwd):
    """The pull request for a PR number or a head branch, or None."""
    return pr_view(which, cwd)[0]

def required_checks(n, cwd):
    """'pass' | 'pending' | 'none' (no required checks) | 'fail: <names>' | 'error: <gh's words>' for a PR's required checks."""
    p = gh(["pr", "checks", str(n), "--required", "--json", "name,bucket"], cwd)
    try:
        rows = json.loads(p.stdout)
    except ValueError:
        text = (p.stdout + p.stderr).strip()
        return "none" if "no required checks" in text.lower() or "no checks reported" in text.lower() else f"error: {text[-200:]}"
    bad = [r.get("name", "?") for r in rows if r.get("bucket") in ("fail", "cancel")]
    if bad:
        return "fail: " + ", ".join(bad)
    return "pending" if any(r.get("bucket") == "pending" for r in rows) else ("pass" if rows else "none")

def wait(probe, deadline):
    """probe() until it returns something but None, LAND_POLL seconds apart; None once the deadline passes."""
    while True:
        r = probe()
        if r is not None:
            return r
        if time.time() >= deadline:
            return None
        time.sleep(LAND_POLL)

def in_stack(n, cwd):
    """True when GitHub holds PR n in a native stack (`gh stack`): such a PR merges only through the asynchronous merge API,
    and GitHub itself retargets and rebases the layer above a merged one. False when gh cannot say."""
    p = gh(["api", "graphql", "-F", "owner={owner}", "-F", "name={repo}", "-F", f"n={n}", "-f",
            "query=query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){pullRequest(number:$n){stack{size}}}}"], cwd)
    try:
        return bool(json.loads(p.stdout)["data"]["repository"]["pullRequest"]["stack"])
    except (ValueError, KeyError, TypeError):
        return False

def merge_async(n, sha, method, cwd, deadline):
    """Merge PR n, which is in a stack, through the asynchronous merge API at head sha, and poll it until GitHub is done. "" when
    it merged, else why not."""
    url = f"repos/{{owner}}/{{repo}}/pulls/{n}/merge-async"
    p = gh(["api", "-X", "PUT", url, "-f", f"sha={sha}", "-f", f"merge_method={method}", "-f", "merge_action=direct_merge"], cwd)
    try:  # a 409 (a merge already pending) carries the pending request's uuid too, so a re-run joins it
        body = json.loads(p.stdout)
    except ValueError:
        body = {}
    uuid = (body.get("details") or {}).get("uuid")
    if p.returncode and not uuid:
        return f"merge of #{n} refused: {(p.stderr or p.stdout).strip()[-300:]}"
    while True:
        status, details = body.get("status"), body.get("details") or {}
        if status == "merged":
            return ""
        if status == "failed":
            return f"merge of #{n} failed: {details.get('message', 'no reason given')}"
        if status == "enqueued":
            return f"#{n} went into a merge queue, which compose land does not wait for"
        if time.time() >= deadline:
            return f"#{n} was asked to merge but GitHub has not finished after the timeout; re-run to resume"
        time.sleep(LAND_POLL)
        g = gh(["api", f"{url}/{uuid}"], cwd)
        if g.returncode:
            return f"merge of #{n}: cannot read its result: {(g.stderr or g.stdout).strip()[-200:]}"
        try:
            body = json.loads(g.stdout)
        except ValueError:
            return f"merge of #{n}: gh printed no JSON for its result"

def land_layer(repo, wt, i, b, prev, rec, base, method, minutes):
    """Land layer b (index i). "" when it is merged, else why it stopped. A layer above the first has lost its parent to the
    merge: it is rebased onto the base (from the tip its parent had when the land began), its PR retargeted, and pushed with a
    lease on the commit compose last saw there. Every remote change waits for the layer's own required checks. A PR in a GitHub
    stack is merged through the asynchronous merge API instead, and GitHub moves the layer above it onto the base itself, so
    compose touches nothing there and merges whatever head GitHub left."""
    n, deadline = rec["pr"][b], time.time() + minutes * 60
    tip = lambda: fc_core.sh(["git", "rev-parse", b], repo).stdout.strip()
    info = pr_info(n, repo)
    if not info:
        return f"{b}: gh cannot read PR #{n}"
    if info["state"] == "MERGED":
        fc_core.say(f"land   skip  #{n} {b} is already merged")
        return ""
    if info["state"] != "OPEN":
        return f"PR #{n} ({b}) is {info['state'].lower()}, not open"
    old, pushed = tip(), False
    stacked = in_stack(n, repo)
    if stacked and prev:
        if wait(lambda: True if (pr_info(n, repo) or {}).get("baseRefName") == base else None, deadline) is None:
            return f"PR #{n} ({b}) never moved onto {base} after its parent merged"
        info, pushed = pr_info(n, repo) or info, True  # GitHub rewrites the head as it moves the layer
        old = new = info["headRefOid"]
    elif prev:
        if fc_core.sh(["git", "merge-base", "--is-ancestor", prev, old], repo, check=False).returncode == 0:  # not rebased yet
            fc_core.sh(["git", "fetch", "-q", "origin", base], wt)
            fc_core.sh(["git", "checkout", "-q", "--detach", b], wt)
            r = fc_core.sh(["git", "rebase", "-q", "--onto", f"origin/{base}", prev], wt, check=False)
            if r.returncode:
                files = fc_core.sh(["git", "diff", "--name-only", "--diff-filter=U"], wt, check=False).stdout.split()
                fc_core.sh(["git", "rebase", "--abort"], wt, check=False)
                return f"{b} conflicts with {base} in {', '.join(files) or 'files git does not name'} after its parent merged; resolve it by hand, nothing of it was pushed"
            fc_core.sh(["git", "branch", "-f", b, fc_core.sh(["git", "rev-parse", "HEAD"], wt).stdout.strip()], repo)
    new = old if stacked and prev else tip()
    if info["headRefOid"] not in (old, new):
        return f"PR #{n} ({b}) has commits compose did not push ({info['headRefOid'][:9]}, local {new[:9]}); `compose stack --submit` first"
    if prev and not stacked and info["baseRefName"] != base:
        fc_core.say(f"land   edit  #{n} base {info['baseRefName']} -> {base}")
        q = gh(["pr", "edit", str(n), "--base", base], repo)
        if q.returncode:
            return f"gh pr edit #{n}: {(q.stderr or q.stdout).strip()[-200:]}"
    if info["headRefOid"] != new:
        fc_core.say(f"land   push  {b} {new[:9]} (was {info['headRefOid'][:9]})")
        q = fc_core.sh(["git", "push", "-q", f"--force-with-lease={b}:{info['headRefOid']}", "origin", f"{b}:{b}"], repo, check=False)
        if q.returncode:
            return f"push of {b} refused: {q.stderr.strip()[-200:]}"
        pushed = True
    if info["isDraft"]:
        fc_core.say(f"land   ready #{n}")
        gh(["pr", "ready", str(n)], repo)
    for _ in range(2):  # a second round when the base moved on while the checks ran
        if wait(lambda: True if (pr_info(n, repo) or {}).get("headRefOid") == new else None, deadline) is None:
            return f"PR #{n} ({b}) never showed {new[:9]} as its head"
        if pushed:
            time.sleep(LAND_SETTLE)
        status = wait(lambda: None if (s := required_checks(n, repo)) == "pending" else s, deadline)
        if status is None:
            return f"required checks on #{n} ({b}) still pending after {minutes} minutes; re-run to resume"
        if status.startswith(("fail", "error")):
            return f"required checks on #{n} ({b}): {status}"
        fc_core.say(f"land   checks #{n} {status}")
        if stacked or (pr_info(n, repo) or {}).get("mergeStateStatus") != "BEHIND":
            break
        fc_core.say(f"land   update #{n} is behind {base}; gh pr update-branch")
        q = gh(["pr", "update-branch", str(n), "--rebase"], repo)
        if q.returncode:
            return f"gh pr update-branch #{n}: {(q.stderr or q.stdout).strip()[-200:]}"
        moved = lambda: (h if (h := (pr_info(n, repo) or {}).get("headRefOid")) != new else None)
        new = wait(moved, deadline) or new
        pushed = True
    if stacked:
        if why := merge_async(n, (pr_info(n, repo) or {}).get("headRefOid") or new, method, repo, deadline):
            return f"{why} ({b})"
    elif (q := gh(["pr", "merge", str(n), f"--{method}"], repo)).returncode:
        return f"gh pr merge #{n} ({b}): {(q.stderr or q.stdout).strip()[-300:]}"
    if wait(lambda: True if (pr_info(n, repo) or {}).get("state") == "MERGED" else None, deadline) is None:
        return f"#{n} ({b}) was asked to merge but is not merged after {minutes} minutes; re-run to resume"
    fc_core.say(f"land   merged #{n} {b} ({method})")
    return ""

def cmd_land(a):
    """Merge the submitted stack into its base, bottom up, one `gh pr merge` per PR (the asynchronous merge API for a PR in a GitHub stack). It pushes and merges on GitHub, so without
    --yes it only prints the plan."""
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    trunk = a.trunk or fc_core.current_branch(repo)
    method = a.method or fc_core.project_config(proj).get("land_method") or "squash"
    names = fc_layers.layer_branches(repo, proj)
    stop = lambda why: (fc_core.say(f"land   STOP  {why}"), sys.exit(1))
    if not names:
        stop("no layers: `compose stack --submit` builds and submits them")
    base = fc_stack.layer_base(repo, proj, trunk)
    kept = Path(proj) / "results" / "land.json"
    rec = json.loads(kept.read_text()) if kept.exists() else {}
    if set(rec.get("orig", {})) != set(names):
        rec = {"orig": {b: fc_core.sh(["git", "rev-parse", b], repo).stdout.strip() for b in names}, "pr": {}}
    views = {b: pr_view(rec["pr"].get(b) or b, repo) for b in names}
    infos = {b: views[b][0] for b in names}
    for b in names:
        if views[b][1]:  # gh itself failed: the PR may exist, so say what gh said instead of "no pull request"
            stop(f"gh pr view failed for {b}: {views[b][1]}")
    missing = [b for b in names if not infos[b]]
    cut = next((i for i, b in enumerate(names) if not infos[b]), len(names))
    if not cut:
        stop(f"{', '.join(missing)} has no pull request: `compose stack --submit` first")
    m = len(names)
    names = names[:cut]
    rec["pr"] = {b: infos[b]["number"] for b in names}
    v = Path(proj) / "results" / "verify.json"
    verify = "not run" if not v.exists() else ("PASS" if json.loads(v.read_text())["passed"] else "FAIL")
    print(f"land   plan  {len(names)} of {m} layer(s) submitted, verify {verify}; landing {len(names)} into {base} by {method}, bottom up:")
    for i, b in enumerate(names, 1):
        i_ = infos[b]
        print(f"  {i}  {b}  #{i_['number']}  {i_['state'].lower()}{' (draft)' if i_['isDraft'] else ''}  {i_['url']}")
    print(f"  for each: mark it ready, wait up to {a.timeout:g} min for its required checks, `gh pr merge --{method}` (the async merge API for a PR in a GitHub stack);\n"
          f"  after each: rebase the next layer onto origin/{base}, retarget its PR to {base}, push it with --force-with-lease (not for a GitHub stack: GitHub does it)")
    if not a.yes:
        print(f"land   nothing was merged or pushed. Re-run with --yes once the user has said to land this stack")
        return
    fc_update.refuse_while_verifying(proj, repo)
    wt = fc_worktree.stack_worktree(repo, proj, trunk)
    fc_core.atomic_write(kept, json.dumps(rec, indent=2))
    try:
        for i, b in enumerate(names):
            if why := land_layer(repo, wt, i, b, rec["orig"][names[i - 1]] if i else None, rec, base, method, a.timeout):
                stop(f"{why}. Layers below it are merged; re-run `compose land` to resume")
    finally:
        fc_core.sh(["git", "checkout", "-q", "--detach", trunk], wt, check=False)
    kept.unlink(missing_ok=True)
    print(f"land   done  {len(names)} layer(s) merged into {base}")
    if len(names) < m:
        print(f"land   left  {m - len(names)} layer(s) not submitted: they rebuild on {base} at the next compose stack")
        print(f"next: compose stack --submit --project {proj.name}")
    else:
        print(f"next: compose clean --project {proj.name}")
