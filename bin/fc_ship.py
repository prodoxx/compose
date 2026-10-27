"""Shipping a verified stack: check every layer before it is submitted, reword its commits, land it on the base."""
import hashlib, json, os, re, subprocess, sys, time
from pathlib import Path
import fc_core, fc_proc, fc_tasks, fc_worktree, fc_layers, fc_stack, fc_green, fc_gate, fc_jev, fc_verify, fc_update

LAND_POLL = 20      # seconds between looks at a pull request that is not ready yet
LAND_SETTLE = 15    # seconds after a push before its required checks are read: GitHub registers them a moment later
LAND_METHODS = ("squash", "merge", "rebase")

def run_check(wt, c):
    """(exit code, scrubbed output) of the check `c` in `wt`: 124 past CHECKS_TIMEOUT, and one more run after a tool error."""
    for attempt in (1, 2):
        try:
            p = fc_core.sh(c, wt, timeout=fc_gate.CHECKS_TIMEOUT, check=False, env=fc_core.clean_env())
            code, out = p.returncode, fc_core.scrub(p.stdout + p.stderr)
        except subprocess.TimeoutExpired:
            code, out = 124, f"timed out after {fc_gate.CHECKS_TIMEOUT}s"
        if not (code and attempt == 1 and fc_jev.tool_error(out[-3000:])):
            return code, out
        fc_core.say(f"layers tool error; retrying once  {c}")

def checked(label, outs):
    """A `run` for fc_gate.check_state that runs each check with run_check, says how it went under `label`, and keeps its output in `outs`."""
    def run(c, wt, limit, env=None):
        t1 = time.time()
        code, outs[c] = run_check(wt, c)
        fc_core.say(f"layers {'ok  ' if code == 0 else 'FAIL'} {fc_core.fmt(time.time() - t1):>8}  {label}  {c}")
        return subprocess.CompletedProcess(c, code, outs[c], "")
    return run

def layer_commands(proj, layer, checks, top):
    """What the gate runs on a layer: the `test` of each task it is the lowest layer for (build_layers' `tested`: the task's code and
    all its test commits are there, which can be above the layer with its code), then every check on the top layer or only the cheap ones
    (all but a whole test suite) below it, so a layer is held to its own tasks and the full suite runs once, on the stack as a whole."""
    tests = {m["id"]: m.get("test") for m in fc_tasks.code_tasks(fc_tasks.load_tasks(proj))}
    return list(dict.fromkeys([c for c in checks if top or not fc_proc.suite(c)] + [tests[t] for t in layer["tested"] if tests.get(t)]))

class InstallFailed(RuntimeError):
    """verify.json `install` failed in the CLI's worktree: the message is the scrubbed tail of what it printed."""

def ready(repo, wt, cfg):
    """fc_worktree.ready_checkout with verify.json `install`, its failure (a broken lockfile, a tool not on PATH) raised as InstallFailed."""
    try:
        fc_worktree.ready_checkout(repo, wt, install=cfg.get("install"))
    except (RuntimeError, OSError) as e:
        lines = [l for l in fc_core.scrub(str(e)).splitlines()[1:] if l.strip()] or [str(e)]
        raise InstallFailed("\n".join(lines[-12:]))

def install_refusal(name, e, base=""):
    return (f"{name}: install failed" + (f" on the base {base[:9]}, where its failing checks are compared" if base else "") +
            f" (verify.json `install`):\n{e}\nNothing was pushed. Make the install pass on {'the base' if base else 'this layer'}"
            " (a lockfile in step with its package files, the tool on PATH), then run `compose stack --submit` again")

def against_base(repo, cfg, wt, base, top, failed, ran):
    """{command: (new, touched, accepted, unknown)} for the failing ids of a layer (`failed`: command -> ids), judged by what the same
    command does on `base`, the commit the stack grows from. `new`: ids that pass there; `touched`: ids that fail there too, in a test or
    module the stack's diff (base to `top`) changes; `accepted`: ids that fail there too and the stack does not touch; `unknown`: the
    base run timed out or named no failing test, so nothing can be told apart. The base runs each command once (`ran`).
    InstallFailed when the install fails on the base."""
    todo = [c for c in failed if c not in ran]
    if todo:
        fc_core.sh(["git", "checkout", "-q", "--detach", base], wt)
        ready(repo, wt, cfg)
        ran.update(fc_gate.check_state(checked(f"base {base[:9]}", {}), todo, wt, 0))
    touched, verdict = fc_gate.touches(wt, base, fc_core.sh(["git", "diff", "--name-only", base, top], repo).stdout.split()), {}
    for c, ids in failed.items():
        old = [t for t in ids if t in ran[c]["failing"]]
        hit = [t for t in old if touched(t)]
        verdict[c] = ([t for t in ids if t not in old], hit, [t for t in old if t not in hit], ran[c]["unknown"])
    return verdict

def ids_text(ids, limit=8):
    return ", ".join(ids[:limit]) + (f" and {len(ids) - limit} more" if len(ids) > limit else "")

MISSING_SCRIPT = re.compile(r'Script not found "([^"]+)"|Missing script:\s*"?([^"\s]+)"?|Command "([^"]+)" not found|'
                            r"Couldn't find a script named \"([^\"]+)\"")  # bun; npm and pnpm; pnpm and yarn 1; yarn 2+

def missing_script(out):
    """The package script a check's output says is not defined, else None."""
    m = MISSING_SCRIPT.search(out or "")
    return next(g for g in m.groups() if g) if m else None

def layer_refusal(name, c, out, why=""):
    """The reason a layer is refused for the check `c`: `why` says which tests, `out` is the tail of its output."""
    return (f"{name} fails `{c}` on its own tree" + (f" ({why})" if why else "") + (f":\n{out[-800:]}\n" if out.strip() else ". ") +
            f"Nothing was pushed. A layer must pass without the layers above it: put what it needs in this layer or below "
            f"(a task's `after`), fix it, then run `compose stack --submit` again")

def layer_checks(repo, proj, layers, trunk, wt, fresh=False):
    """The gate on a submit: verify.json `checks` (the cheap ones, never e2e) on each layer's own tree, bottom up, in the CLI's
    worktree. Each layer runs the tests of the tasks it is the lowest layer for (layer_commands); the top layer also every check, a layer below it only the cheap ones (layer_commands).
    A failing test that fails on the stack's base too, in a test the stack does not touch, was red before this work: it is accepted
    and reported (against_base). One that passes on the base, or that the stack touches, refuses the submit, as does a failure that
    names no test. A check below the top whose package script the layer does not define yet waits for the layer that adds it (a
    stack that brings in a tool cuts its groundwork below the tool's setup); the top runs every check, so a script no layer adds still refuses. The first layer refused is the reason returned, with nothing pushed; "" when every layer passes. A layer that
    passed once is not run again while its tip and its commands stay the same, and one that leaned on the base also the base and top.
    A command already green on a layer's tree (fc_green: verify, the run or an earlier submit left it) is not run again either, and the
    layer says so; one that goes red is forgotten. `fresh` runs everything."""
    cfg = fc_core.project_config(proj)
    checks = cfg.get("checks") or []
    if not checks:
        fc_core.say("layers no checks in verify.json: the layers are not checked before submit")
        return ""
    roots = (repo, fc_core.worktrees(repo)[0])
    checks = fc_verify.at_root(checks, roots, wt)
    kept = Path(proj) / "results" / "layer-checks.json"
    seen = json.loads(kept.read_text()) if kept.exists() else {}
    top, below = fc_core.sh(["git", "rev-parse", layers[-1]["branch"]], repo).stdout.strip(), fc_stack.layer_base(repo, proj, trunk)
    base = fc_core.sh(["git", "merge-base", below, top], repo, check=False).stdout.strip() or fc_core.sh(["git", "rev-parse", below], repo).stdout.strip()
    on_base, accepted, own = {}, 0, set(checks)
    for i, l in enumerate(layers, 1):
        tip = fc_core.sh(["git", "rev-parse", l["branch"]], repo).stdout.strip()
        name = f"layer {i} of {len(layers)} ({l['branch']})"
        cmds = fc_verify.at_root(layer_commands(proj, l, checks, i == len(layers)), roots, wt)
        stamp = f"{tip}:{hashlib.sha1(json.dumps(cmds).encode()).hexdigest()[:12]}"
        lean = f"{stamp}:{base}:{top}"
        if not fresh and seen.get(l["branch"]) in (stamp, lean):
            fc_core.say(f"layers ok    {name}  unchanged since its checks passed")
            continue
        fc_core.say(f"layers scope {name}  " + ("its tasks' tests and every check" if i == len(layers) else "its tasks' tests and the cheap checks") + (": " + "; ".join(cmds) if cmds else ": nothing to run"))
        tree, checked_out = fc_green.tree_of(repo, tip), False
        failed, outs = {}, {}
        for c in cmds:
            if line := fc_green.taken(proj, tree, c, fresh):
                fc_core.say(f"layers {name}  {line}")
                continue
            if not checked_out:  # a layer whose commands are all green on its tree is not even checked out
                fc_core.sh(["git", "checkout", "-q", "--detach", tip], wt)
                try:
                    ready(repo, wt, cfg)
                except InstallFailed as e:
                    return install_refusal(name, e)
                checked_out = True
            state = fc_gate.check_state(checked(name, outs), [c], wt, 0)[c]
            fc_green.record(proj, tree, c, state["exit"])
            if state["exit"] and i < len(layers) and c in own and (script := missing_script(outs[c])):
                fc_core.say(f"layers later {name}  `{c}`: this layer does not define the script {script} yet: it waits for the layer that adds it, and the top runs every check")
                continue
            if state["unknown"]:
                return layer_refusal(name, c, outs[c], "" if state["exit"] == 124 else "it names no failing test, so it cannot be compared with the base")
            if state["exit"]:
                failed[c] = state["failing"]
        leaned = False
        try:
            verdicts = against_base(repo, cfg, wt, base, top, failed, on_base) if failed else {}
        except InstallFailed as e:
            return install_refusal(name, e, base)
        for c, (new, hit, ok, unknown) in verdicts.items():
            if unknown or new or hit:
                why = (f"cannot tell whether {ids_text(new)} fail on the base too: its run on the base {base[:9]} timed out or names no failing test" if unknown else
                       "; ".join(x for x in (f"passes on the base {base[:9]}, fails here: {ids_text(new)}" if new else "",
                                             f"fails on the base too, in what this stack changes: {ids_text(hit)}" if hit else "",
                                             f"{len(ok)} more already fail on the base and are ignored" if ok else "") if x))
                return layer_refusal(name, c, outs[c], why)
            accepted, leaned = accepted + len(ok), True
            fc_core.say(f"layers base  {name}  `{c}` fails on the base {base[:9]} too and the stack does not touch it, accepted: {ids_text(ok)}")
        seen[l["branch"]] = lean if leaned else stamp
        fc_core.atomic_write(kept, json.dumps(seen, indent=2))
    fc_core.say(f"layers ok    {len(layers)} layer(s): every check on the top, the cheap ones below, each layer's own tasks' tests" + (f"; {accepted} failing test(s) already red on the base accepted" if accepted else ""))
    return ""

def cmd_stack(a):
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    trunk = a.trunk or fc_core.current_branch(repo)
    fc_update.refuse_while_verifying(proj, repo)
    def gate(repo, proj, layers, trunk, wt):
        if why := layer_checks(repo, proj, layers, trunk, wt, getattr(a, "fresh", False)):
            return why
        below = fc_stack.layer_base(repo, proj, trunk)
        for i, l in enumerate(layers, 1):
            b = l["branch"]
            info = pr_info(b, wt)
            tip = fc_core.sh(["git", "rev-parse", b], repo).stdout.strip()
            prev, below = below, b
            if not info or info["state"] != "OPEN" or info["headRefOid"] == tip:
                continue
            head = info["headRefOid"]
            own = set(fc_core.sh(["git", "diff", "--name-only", prev, b], repo).stdout.split())
            hit = sorted(own & set(fc_core.sh(["git", "diff", "--name-only", head, tip], repo, check=False).stdout.split()))
            cause = f"its own files changed: {', '.join(hit)}" if hit else "a layer below it or the base moved"
            if not a.update:
                return (f"layer {i} ({b}) changed since PR #{info['number']}: GitHub has {head[:9]}, here {tip[:9]}; {cause}. "
                        "Nothing was pushed. `compose stack --submit --update` pushes it with --force-with-lease")
            q = fc_core.sh(["git", "push", "-q", f"--force-with-lease={b}:{head}", "origin", f"{b}:{b}"], repo, check=False)
            if q.returncode:
                return f"push of {b} refused: {q.stderr.strip()[-200:]}"
            fc_core.say(f"submit update {b} {tip[:9]} (was {head[:9]})")
        return ""
    if not fc_stack.register_stack(repo, proj, trunk, submit=a.submit, preview=not a.submit, gate=gate):
        if fc_layers.STACK_STOP:
            sys.exit(1)  # the STOP line already said why and what to do
        print("stack  nothing to register: no unmerged task branches")

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
