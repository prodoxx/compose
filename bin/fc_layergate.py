"""The gate a stack's layers pass before they are submitted, and now also when a verify passes (#456): each review layer is checked on its own
tree, bottom up, and every layer that fails is named. `fc_ship` runs it for a submit, `fc_verify` for a verify. Imports only lower modules."""
import hashlib, json, re, subprocess, time
from pathlib import Path
import fc_core, fc_proc, fc_tasks, fc_worktree, fc_layers, fc_stack, fc_green, fc_gate, fc_jev, fc_prrisk

def at_root(value, olds, root):
    """`value` with every absolute path under one of `olds` (the project worktree, the main checkout) moved under `root`, in
    every string, inside dicts and lists too. Longest prefix first, each matched as a whole path: a path already at `root` (the
    slot lives under the main checkout) is never moved again, so a second call changes nothing, and ~/code/app never matches ~/code/app-2/x.
    A project's `.compose/` is git-ignored: a path in it moves only when it exists under `root` (the link stack_worktree makes),
    else it stays where the file is, so an e2e driver kept in the project worktree is still found."""
    olds = sorted({str(o) for o in olds} - {str(root)}, key=len, reverse=True)
    if not olds:
        return value
    path = re.compile(r"(?<![\w.~-])(?:" + "|".join(map(re.escape, olds)) + r")(?=/|$|[\s\"'`;:&|()<>,=])")
    if isinstance(value, dict):
        return {k: at_root(v, olds, root) for k, v in value.items()}
    if isinstance(value, list):
        return [at_root(v, olds, root) for v in value]
    def moved(m):
        if re.match(re.escape(str(root)) + r"(?=/|$|[\s\"'`;:&|()<>,=])", m.string[m.start():]):
            return m.group(0)  # already at `root`, which lives under an old one: moving it again doubles it
        tail = re.match(r"/\.compose(?:/[^\s\"'`;:&|()<>,=]*)?", m.string[m.end():])
        return m.group(0) if tail and not Path(str(root) + tail.group(0)).exists() else str(root)
    return path.sub(moved, value) if isinstance(value, str) else value

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
        fc_worktree.ready_checkout(repo, wt, install=cfg.get("install"), generate=cfg.get("generate"))
    except (RuntimeError, OSError) as e:
        lines = [l for l in fc_core.scrub(str(e)).splitlines()[1:] if l.strip()] or [str(e)]
        raise InstallFailed("\n".join(lines[-12:]))

def install_refusal(name, e, base="", again="compose stack --submit"):
    return (f"{name}: install failed" + (f" on the base {base[:9]}, where its failing checks are compared" if base else "") +
            f" (verify.json `install`):\n{e}\n" + ("Nothing was pushed. " if again == "compose stack --submit" else "") + f"Make the install pass on {'the base' if base else 'this layer'}"
            f" (a lockfile in step with its package files, the tool on PATH), then run `{again}` again")

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

def layer_refusal(name, c, out, why="", again="compose stack --submit"):
    """The reason a layer is refused for the check `c`: `why` says which tests, `out` is the tail of its output. A submit pushed nothing
    (`again` is the command to run once it is fixed); a verify has nothing to push (`again` is `compose verify`)."""
    return (f"{name} fails `{c}` on its own tree" + (f" ({why})" if why else "") + (f":\n{out[-800:]}\n" if out.strip() else ". ") +
            ("Nothing was pushed. " if again == "compose stack --submit" else "") +
            f"A layer must pass without the layers above it: put what it needs in this layer or below "
            f"(a task's `after`), fix it, then run `{again}` again")

NEEDS_ABOVE = "it passes on the top layer: this layer needs a commit from above it"

def unchanged_layer(seen, branch, stamp, lean, fresh=False):
    """True when the layer's checks passed before and nothing changed since: `seen` (the memo kept in layer-checks.json) holds its `stamp` (tip and commands) or its `lean` one (also the base and top it leaned on). Never on a `fresh` run."""
    return not fresh and seen.get(branch) in (stamp, lean)

def check_outcome(state, top, c, own, out):
    """What a command's `state` (fc_gate.check_state's) means for its layer: ("later", script) when it fails below the `top` on a package script this layer does not define yet (`c` is one of the project's `own` checks, `out` its output); ("refuse", why) when no failing test is named; ("fail", failing ids) when it fails; else ("pass", None)."""
    if state["exit"] and not top and c in own and (script := missing_script(out)):
        return "later", script
    if state["unknown"]:
        return "refuse", "" if state["exit"] == 124 else "it names no failing test, so it cannot be compared with the base"
    return ("fail", state["failing"]) if state["exit"] else ("pass", None)

def base_why(new, hit, ok, unknown, base):
    """Why a command's failures, judged against the base (against_base's `new, touched, accepted, unknown`), refuse the layer; "" when they are all accepted."""
    if not (unknown or new or hit):
        return ""
    return (f"cannot tell whether {ids_text(new)} fail on the base too: its run on the base {base[:9]} timed out or names no failing test" if unknown else
            "; ".join(x for x in (f"passes on the base {base[:9]}, fails here: {ids_text(new)}" if new else "",
                                  f"fails on the base too, in what this stack changes: {ids_text(hit)}" if hit else "",
                                  f"{len(ok)} more already fail on the base and are ignored" if ok else "") if x))

def layer_checks(repo, proj, layers, trunk, wt, fresh=False):
    """layer_refusals as the one reason a submit stops with: "" when every layer passes, else the refusal of every layer that fails."""
    return "\n\n".join(layer_refusals(repo, proj, layers, trunk, wt, fresh))

def layer_refusals(repo, proj, layers, trunk, wt, fresh=False, again="compose stack --submit"):
    """The gate on a submit: verify.json `checks` (the cheap ones, never e2e) on each layer's own tree, bottom up, in the CLI's
    worktree. Each layer runs the tests of the tasks it is the lowest layer for (layer_commands); the top layer also every check, a layer below it only the cheap ones (layer_commands).
    A failing test that fails on the stack's base too, in a test the stack does not touch, was red before this work: it is accepted
    and reported (against_base). One that passes on the base, or that the stack touches, refuses the submit, as does a failure that
    names no test. A check below the top whose package script the layer does not define yet waits for the layer that adds it (a
    stack that brings in a tool cuts its groundwork below the tool's setup); the top runs every check, so a script no layer adds still refuses. Every layer is checked, a refused one
    too, and the refusal of each is returned (a stack with three layers that fail on their own tree costs one fix-and-run, not three); [] when every layer passes. A refusal whose
    command is green on the top layer says the layer needs a commit from above it (NEEDS_ABOVE). `again` is the command the refusals name for the run after the fix: a verify
    runs this gate too (#456) and pushes nothing. A layer that
    passed once is not run again while its tip and its commands stay the same, and one that leaned on the base also the base and top.
    A command already green on a layer's tree (fc_green: verify, the run or an earlier submit left it) is not run again either, and the
    layer says so; one that goes red is forgotten. `fresh` runs everything."""
    cfg = fc_core.project_config(proj)
    checks = cfg.get("checks") or []
    if not checks:
        fc_core.say("layers no checks in verify.json: the layers are not checked before submit")
        return []
    roots = (repo, fc_core.main_checkout(repo))
    checks = at_root(checks, roots, wt)
    kept = Path(proj) / "results" / "layer-checks.json"
    seen = json.loads(kept.read_text()) if kept.exists() else {}
    top, below = fc_core.sh(["git", "rev-parse", layers[-1]["branch"]], repo).stdout.strip(), fc_stack.layer_base(repo, proj, trunk)
    base = fc_core.sh(["git", "merge-base", below, top], repo, check=False).stdout.strip() or fc_core.sh(["git", "rev-parse", below], repo).stdout.strip()
    on_base, accepted, own, refused = {}, 0, set(checks), []
    top_tree = fc_green.tree_of(repo, top)
    def refusal(name, c, out, why, below):
        """layer_refusal, and when the layer is below the top and the command is green on the top layer's tree, why: the layer lacks a commit that sits above it."""
        above = below and fc_green.taken(proj, top_tree, c, fresh) is not None
        return layer_refusal(name, c, out, "; ".join(x for x in (why, NEEDS_ABOVE if above else "") if x), again)
    for i, l in enumerate(layers, 1):
        before = len(refused)
        tip = fc_core.sh(["git", "rev-parse", l["branch"]], repo).stdout.strip()
        name = f"layer {i} of {len(layers)} ({l['branch']})"
        cmds = at_root(layer_commands(proj, l, checks, i == len(layers)), roots, wt)
        stamp = f"{tip}:{hashlib.sha1(json.dumps(cmds).encode()).hexdigest()[:12]}"
        lean = f"{stamp}:{base}:{top}"
        if unchanged_layer(seen, l["branch"], stamp, lean, fresh):
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
                except InstallFailed as e:  # the install is the environment's: every layer after it would fail on it the same way
                    return [*refused, install_refusal(name, e, again=again)]
                checked_out = True
            state = fc_gate.check_state(checked(name, outs), [c], wt, 0)[c]
            fc_green.record(proj, tree, c, state["exit"])
            kind, detail = check_outcome(state, i == len(layers), c, own, outs[c])
            if kind == "later":
                fc_core.say(f"layers later {name}  `{c}`: this layer does not define the script {detail} yet: it waits for the layer that adds it, and the top runs every check")
                continue
            if kind == "refuse":
                refused.append(refusal(name, c, outs[c], detail, i < len(layers)))
                break
            if kind == "fail":
                failed[c] = detail
        if len(refused) > before:  # refused on a command that names no failing test: this layer is done, the layers above it are still checked
            continue
        leaned = False
        try:
            verdicts = against_base(repo, cfg, wt, base, top, failed, on_base) if failed else {}
        except InstallFailed as e:
            return [*refused, install_refusal(name, e, base, again)]
        for c, (new, hit, ok, unknown) in verdicts.items():
            if why := base_why(new, hit, ok, unknown, base):
                refused.append(refusal(name, c, outs[c], why, i < len(layers)))
                break
            accepted, leaned = accepted + len(ok), True
            fc_core.say(f"layers base  {name}  `{c}` fails on the base {base[:9]} too and the stack does not touch it, accepted: {ids_text(ok)}")
        if len(refused) > before:
            continue
        seen[l["branch"]] = lean if leaned else stamp
        fc_core.atomic_write(kept, json.dumps(seen, indent=2))
    if refused:
        fc_core.say(f"layers FAIL  {len(refused)} of {len(layers)} layer(s) fail on their own tree")
        return refused
    fc_core.say(f"layers ok    {len(layers)} layer(s): every check on the top, the cheap ones below, each layer's own tasks' tests" + (f"; {accepted} failing test(s) already red on the base accepted" if accepted else ""))
    return []

def layer_pass(main, proj, a, findings=()):
    """The per-layer gate a submit runs, run by a passing verify (#456): the review layers are stood as a submit would stand them (held tasks left
    out) and each is checked on its own tree, bottom up, by layer_refusals, so every layer that fails (lint on a stub a later task implements,
    a test whose setup arrives above it) is named now, in one run, and not one per `compose stack --submit`. Returns the refusals, one per layer
    that fails, [] when all pass or the layers did not stand (the STOP line says why; the submit stops there too). Nothing is pushed or registered.
    When every layer passes and the project has its review on, each layer's risk is judged for its PR (fc_prrisk.assess), told the review's
    confirmed `findings`."""
    found, fresh = [], getattr(a, "fresh", False)
    def gate(repo, proj, layers, trunk, wt):
        found.extend(layer_refusals(repo, proj, layers, trunk, wt, fresh, again="compose verify"))
        if not found and fc_core.project_config(proj).get("review", True):
            fc_prrisk.assess(repo, proj, layers, fc_stack.pr_names(layers), fc_stack.layer_base(repo, proj, trunk), fc_tasks.code_tasks(fc_tasks.load_tasks(proj)), findings, fresh)
    fc_layers.STACK_STOP = ""
    if not fc_stack.register_stack(main, proj, fc_core.current_branch(main), check_only=True, gate=gate):
        fc_core.say("layers not checked: the layers did not stand (see the line above); a submit stops there too")
    return found
