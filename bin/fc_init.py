import json, os, re, shutil, subprocess, sys, urllib.error, urllib.request
from pathlib import Path
import fc_auth, fc_checks, fc_config, fc_core, fc_harness, fc_steps, fc_tasks, fc_linear, fc_providers, fc_herdr, fc_worktree, fc_layers, fc_crew, fc_pin


TRAIL_INIT = ["trail", "init", "--no-agents", "--no-global", "--no-build"]  # Claude Code wiring only, nothing outside the repo

def existing_repo(wt):
    """True when the repository already has code: a tracked file beyond a README, a license, or git's own dotfiles."""
    files = fc_core.sh(["git", "ls-files"], wt, check=False).stdout.split()
    return any(not re.match(r"(?i)(readme|licen[cs]e|\.git)", Path(f).name) for f in files)

def setup_trail(wt):
    """An existing repo gets trail before anyone reads its code: wired for Claude Code (unless that would touch a tracked
    file) and built. trail's files stay out of git through info/exclude. Safe to run again."""
    if os.environ.get("COMPOSE_NO_TRAIL"):
        return fc_core.say("trail    skipped: COMPOSE_NO_TRAIL is set")
    if not existing_repo(wt):
        return fc_core.say("trail    skipped: a new repository has no code to map yet")
    if not shutil.which("trail"):
        return fc_core.say(f"trail    not installed: `{TRAIL_INSTALL}`, then `compose trail --project <slug>`")
    if not (wt / ".trail" / "hook.cjs").exists():
        p = fc_core.sh(TRAIL_INIT + ["--dry-run", str(wt)], wt, timeout=120, check=False)
        dry = p.stdout + p.stderr  # trail prints the list on stderr
        section = dry.split("this repo:", 1)[1].split("\n\n", 1)[0] if "this repo:" in dry else ""
        paths = sorted(set(re.findall(r"^\s+(\S+)", section, re.M)))
        tracked = [f for f in paths if fc_core.sh(["git", "ls-files", "--error-unmatch", f], wt, check=False).returncode == 0]
        if tracked:
            fc_core.say(f"trail    init skipped: it would edit tracked {', '.join(tracked)}; building the graph only")
        else:
            fc_core.sh(TRAIL_INIT + [str(wt)], wt, timeout=300, check=False)
            fc_worktree.exclude_paths(wt, ["/" + f for f in paths])
            fc_core.say(f"trail    wired for Claude Code ({len(paths)} files, kept out of git)")
    fc_worktree.exclude_paths(wt, ["/graft/", "/.trail/", *("/" + w for w in fc_worktree.WIRING)])  # the graph and wiring, also when trail runs without init
    fc_worktree.stamp_trail_wiring(wt)
    (wt / ".trail" / "cache").mkdir(parents=True, exist_ok=True)  # trail build puts the map in graft/ unless .trail/cache/ exists first
    fc_core.sh(["trail", "build", "--no-gitignore", "--no-ignore", str(wt)], wt, timeout=600, check=False)
    fc_core.say("trail    graph built")

def cmd_trail(a):
    """Set trail up in the project's worktree again (after installing trail, or to rebuild the graph)."""
    setup_trail(fc_herdr.project_worktree(fc_core.project_dir(a)))

TRAIL_INSTALL = "npm i -g @trailhq/trail-code"

def check_deps(role=None):
    """Print one `MISSING: <name>[: <detail>] (install: <command>)` line per setup step that is not ok; the setup steps make
    no network call, but each local provider row pings its server once (a 5 s GET). a key row names the key and its fix,
    never a value. herdr's config is one more row (loud toasts or sounds are the detail), optional, fixed by `compose setup`,
    and the local provider rows follow it. While `claude` itself is missing, the rows that need it share one `waiting on Claude Code:`
    line instead of a MISSING line each (they still count as missing when required). Returns the required steps that are missing:
    those stop `init`; the optional ones only warn. With no role the designer rows are opt-in: a miss among them gets one
    `designer extras not set up: compose setup, choose the designer rows` line instead, never the waiting line, never returned.
    With a role, only what that role needs is checked, and it is required (Framelink stops the designer's start)."""
    bad, waiting, designer = [], [], False
    checkout = Path(__file__).resolve().parents[1]
    claude = bool(shutil.which("claude"))
    other = fc_steps.other_harness(checkout)
    for row in (*fc_steps.STEPS, ("herdr config", lambda co: herdr_loud(), "compose setup", "default", False, "run"), *fc_providers.doctor_rows()):
        name, _, install, needed, needs_claude, mode = row
        if role and needed != role:
            continue
        required = fc_steps.required(row, other) or (role is not None and needed == role)
        ok, detail = fc_steps.check(row, checkout)
        if not ok:
            if needed == "designer" and role is None:
                designer = True
                continue
            if needs_claude and not claude:
                waiting.append(name)
                if required:
                    bad.append(name)
                continue
            note = "" if required else " - optional"
            if not required and other and name in fc_steps.CLAUDE_ONLY:
                note = f" - optional, {fc_harness.HARNESSES[other]['name']} is set up"
            why = "" if mode == "key" else f": {detail}" if detail else ""
            print(f"MISSING: {name}{why} (install: {install}){note}")
            if required:
                bad.append(name)
    if designer:
        print("designer extras not set up: compose setup, choose the designer rows")
    if waiting:
        print("waiting on Claude Code: " + ", ".join(waiting))
    return bad

def herdr_toml(path):
    """The `[section] key = value` pairs of herdr's config.toml as {"section.key": "value"}, quotes stripped: the few flat keys
    compose reads, without tomllib (the Python 3.9 macOS ships has none). Unreadable is empty."""
    vals, section = {}, ""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return vals
    for line in lines:
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if m := re.match(r"\[([\w.-]+)\]$", line):
            section = m.group(1)
        elif m := re.match(r"([\w.-]+)\s*=\s*(.+)$", line):
            vals[f"{section}.{m.group(1)}".lstrip(".")] = m.group(2).strip().strip("\"'")
    return vals

def herdr_loud():
    """(ok, detail) for the herdr config row: not ok when herdr's own toasts or sounds would reach the product owner from every
    agent, with the loud keys as the detail; `[ui.toast] delivery` off is herdr's default, `[ui.sound] enabled` false is not, and an
    absent file counts as the defaults."""
    cfg = herdr_toml(Path.home() / ".config" / "herdr" / "config.toml")
    loud = [(key, cfg.get(key, default)) for key, quiet, default in (("ui.toast.delivery", "off", "off"), ("ui.sound.enabled", "false", "true"))
            if cfg.get(key, default) != quiet]
    return (False, ", ".join(f"{k}={v}" for k, v in loud)) if loud else (True, "")

def herdr_toast_warning():
    """One line when herdr's own toasts or sounds are on, for the product owner to read: herdr then alerts them for every agent
    that finishes or needs input, in every pane, while compose already sends the engineering manager's one alert itself
    (desktop_notify). None when quiet."""
    ok, loud = herdr_loud()
    if ok:
        return None
    return ("WARNING: herdr alerts the product owner for every agent, not only the engineering manager (" + loud
            + '): set [ui.toast] delivery = "off" and [ui.sound] enabled = false in ~/.config/herdr/config.toml; '
            "compose sends the engineering manager's alert itself")

def key_problems():
    """(problems, warnings) for the workspace's OpenRouter and Linear keys, found with a call that costs nothing: OpenRouter's
    key-info endpoint and Linear's `viewer` query. A key its service turns away (or an OpenRouter key whose credit is used up)
    is a problem with its fix; a key that is not set is the setup row's business, and a service that does not answer is a
    warning, never a verdict. COMPOSE_NO_PREFLIGHT=1 makes no call (offline work)."""
    if os.environ.get("COMPOSE_NO_PREFLIGHT"):
        return [], []
    def ask(req):
        """(HTTP status or None, parsed JSON or {}) of one request; None when nothing answered."""
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, {}
        except (OSError, ValueError):  # no network, a timeout, a body that is not JSON
            return None, {}
    swap = lambda where, flag="": f"get a new key at {where}, then: pbpaste | compose workspace key {fc_config.WORKSPACE}{flag}"
    checks = []
    if fc_config.OPENROUTER_KEY:
        checks.append(("OpenRouter", urllib.request.Request("https://openrouter.ai/api/v1/auth/key", headers={"Authorization": "Bearer " + fc_config.OPENROUTER_KEY}),
                       swap("openrouter.ai/keys")))
    if fc_config.LINEAR_KEY:
        checks.append(("Linear", urllib.request.Request("https://api.linear.app/graphql", data=b'{"query": "{ viewer { id } }"}',
                       headers={"Content-Type": "application/json", "Authorization": fc_config.LINEAR_KEY}),
                       swap("linear.app/settings/api", " --linear")))
    problems, warnings = [], []
    for name, req, fix in checks:
        status, out = ask(req)
        left = ((out.get("data") or {}).get("limit_remaining") if isinstance(out, dict) else None) if name == "OpenRouter" else None
        if status in (400, 401, 403):
            problems.append(f"the {name} key is rejected (HTTP {status}): {fix}")
        elif status == 200 and left is not None and left <= 0:
            problems.append(f"the {name} key has no credit left: raise its limit or add credit at openrouter.ai/settings/credits")
        elif status != 200:
            warnings.append(f"WARNING: could not check the {name} key ({'no answer' if status is None else 'HTTP ' + str(status)}): the work may stop later if it is bad")
    return problems, warnings

def start_problems(repo, tree=True, hint="commit or discard them", proj=None):
    """(problems, warnings) to settle before long work starts, all at once so they are fixed in one pass: a work tree with changes
    of the user's (fc_worktree.dirty_paths ignores compose's and the tools' own files), a key its service rejects (key_problems),
    docker down or unhealthy where the project's compose file needs it (fc_checks.docker_problems), a database target that is not
    the one recorded in `proj`'s verify.json (fc_checks.database_problems), and the machine warnings (fc_checks.machine_warnings).
    `tree=False` skips the tree, for verify, which tolerates it. A problem is a line with its fix; a warning starts with WARNING.
    COMPOSE_NO_PREFLIGHT=1 drops everything but the tree."""
    problems, warnings = key_problems()
    if not os.environ.get("COMPOSE_NO_PREFLIGHT"):
        warnings += fc_checks.machine_warnings()
        problems += fc_checks.docker_problems(repo)
        if proj is not None and (recorded := verify_config(proj).get("database")) is not None:
            problems += fc_checks.database_problems(fc_checks.database_targets(repo), recorded)
    if tree and (dirty := fc_worktree.dirty_paths(repo)):
        shown = ", ".join(dirty[:8]) + (f" and {len(dirty) - 8} more" if len(dirty) > 8 else "")
        problems.insert(0, f"repo has uncommitted changes ({shown}); {hint}")
    return problems, warnings

def verify_config(proj):
    try:
        return json.loads((Path(proj) / "verify.json").read_text())
    except (OSError, ValueError):
        return {}

def record_databases(proj, repo):
    """Record the database targets the project runs on, once: the first start after init (or after this check arrived) writes
    `database` into verify.json, and from then on a target that changes or appears stops a start until the user confirms it."""
    cfg, targets = verify_config(proj), fc_checks.database_targets(repo)
    if "database" not in cfg and (Path(proj) / "verify.json").exists():
        fc_core.atomic_write(Path(proj) / "verify.json", json.dumps({**cfg, "database": targets}, indent=2))
    return targets

def start_checks(repo, stack=True, tree=True, hint="commit or discard them", force=False, proj=None):
    """The start of run, resume and verify: print the warnings and stop with every problem at once (nothing is cloned or run
    yet), so the work never stops later to ask. With `stack` (the work will be submitted as a gh stack), a gh that is not
    logged in is one more problem; a Mac on battery is one too, unless `force` (--force). Then, with `proj`, the startup checklist
    (fc_checks.startup_lines): power, docker, the database targets (recorded on the first start), sleep.
    COMPOSE_NO_PREFLIGHT=1 skips that with the calls and machine checks."""
    problems, warnings = start_problems(repo, tree, hint, proj)
    if low := fc_checks.battery_stop(force):
        problems.append(low)
    if stack and not os.environ.get("COMPOSE_NO_PREFLIGHT") and shutil.which("gh") and not fc_checks.gh_authed():
        problems.append("gh is not logged in, so the stack cannot be submitted: gh auth login (or run with --no-stack)")
    for w in warnings:
        fc_core.say(w)
    if problems:
        sys.exit("preflight stopped, nothing was cloned or run. Fix " + ("this" if len(problems) == 1 else f"all {len(problems)} of these")
                 + ", then run again:\n  " + "\n  ".join(problems))
    if proj is not None and not os.environ.get("COMPOSE_NO_PREFLIGHT"):
        for line in fc_checks.startup_lines(repo, record_databases(proj, repo)):
            fc_core.say(line)

def cmd_doctor(a):
    """Check every tool and skill file compose depends on; print every line first, then exit 1 when a required one is missing.
    Warns, never stops, when a key still sits in config.json or .env: it differs from the key auth.json holds, so it was left
    there - remove the one you do not want. herdr's loud toasts or sounds are one optional row of check_deps, fixed by `compose setup`.
    Then the preflight run and verify repeat at their start (start_problems): a `PROBLEM:` line with its fix for each key its
    service rejects and, in the work tree of --project, for changes of the user's that run refuses;
    WARNING lines for a Mac on battery, a loaded machine and docker containers in a crash loop. A problem exits 1 as well."""
    bad = check_deps()
    if shutil.which("gh") and fc_core.sh(["gh", "auth", "status"], Path.home(), check=False, timeout=20).returncode != 0:
        print("WARNING: gh is not logged in: compose bug reports are saved, not filed on GitHub (run: gh auth login)")
    for name, path in fc_auth.old_keys(fc_config.CONFIG_PATH, fc_config.ENV_FILE):
        print(f"WARNING: {name} in {fc_core.tilde(path)} differs from the key auth.json holds, so it was kept: remove the one you do not want")
    wt, missing = None, []
    if getattr(a, "project", None):  # the tree is the named project's; without one (and under `update`) there is none to check
        pick = fc_core.all_projects(Path(a.repo).resolve()).get(a.project)
        wt = pick[0] if pick else None
        missing = [] if pick else [f"no project {a.project!r} in {Path(a.repo).resolve()}: check the name with `compose projects`"]
    problems, warnings = start_problems(wt or Path.cwd(), tree=wt is not None, proj=wt / ".compose" / a.project if wt else None)
    problems += missing
    for line in warnings:
        print(line)
    for line in problems:
        print("PROBLEM: " + line)
    if bad:
        modes = {row[0]: row[5] for row in fc_steps.STEPS}  # setup fixes every mode but manual
        print(f"{len(bad)} required step{'s' if len(bad) > 1 else ''} missing"
              + (": run compose setup" if any(modes.get(n) != "manual" for n in bad) else ""))
        sys.exit(1)
    if problems:
        print(f"{len(problems)} problem{'s' if len(problems) > 1 else ''} to fix before work starts")
        sys.exit(1)
    print("every step compose needs is ready")

def created_from(repo, branch):
    """The commit `branch` was created on: its reflog's oldest entry when that entry is the creation, else None (an expired
    reflog starts later, at a commit that may be the branch's own)."""
    log = fc_core.sh(["git", "reflog", "show", "--format=%H %gs", f"refs/heads/{branch}"], repo, check=False).stdout.splitlines()
    sha, _, why = (log[-1] if log else "").partition(" ")
    return sha if why.startswith("branch: Created from") else None

def move_onto(wt, proj, after, after_name):
    """Build an existing project on another project's branch (`init --after`): allowed while the project has no commits of its own
    (init's own "Ignore local tool directories" one aside) and its worktree is clean but for agent wiring (fc_worktree.dirty_paths),
    then its branch is reset to `after` and verify.json stores the review base and `after`. A commit of its own is one in neither
    its review base (fc_worktree.review_base) nor the commit the branch was created on, so a project made before init recorded the
    branch it started from is read right too. Exits with the reason otherwise, so the flag is never silently ignored."""
    cfg = fc_core.project_config(proj)
    if cfg.get("review_base") == after:
        return
    branch = fc_core.project_branch(proj)
    if fc_core.sh(["git", "branch", "--show-current"], wt, check=False).stdout.strip() != branch:
        sys.exit(f"{wt} is not on {branch}: check it out, then run init --after again")
    since = [r for r in (fc_worktree.review_base(wt, cfg), created_from(wt, branch)) if r]
    log = fc_core.sh(["git", "log", "--format=%s", branch, "--not", *since], wt, check=False) if since else None
    if not log or log.returncode != 0:
        sys.exit(f"cannot tell which commits on {branch} are its own, so --after {after_name} cannot move it"
                 + (f": {log.stderr.strip()}" if log else ": neither its review base nor the commit it was created on resolves"))
    own = [s for s in log.stdout.splitlines() if not s.endswith("Ignore local tool directories")]
    if own:
        sys.exit(f"project {proj.name} has {len(own)} commit{'s' if len(own) > 1 else ''} of its own on {branch}, so --after {after_name} cannot move it: "
                 f"rebase it onto {after} yourself, or start the project again")
    if dirty := fc_worktree.dirty_paths(wt):
        shown = ", ".join(dirty[:8]) + (f" and {len(dirty) - 8} more" if len(dirty) > 8 else "")
        sys.exit(f"{wt} has uncommitted changes ({shown}), so --after {after_name} cannot move {branch}: commit or discard them first")
    if fc_core.sh(["git", "reset", "-q", "--keep", after], wt, check=False).returncode:  # --keep holds a session hook's wiring rewrite
        fc_core.sh(["git", "reset", "-q", "--hard", after], wt)  # `after` changed that file too: the hook writes it again next session
    fc_core.atomic_write(proj / "verify.json", json.dumps({**cfg, "review_base": after, "after": after_name}, indent=2))
    print(f"branch {branch} moved onto {after}")

def undo_worktree(here, wt, branch):
    """Give back the worktree a failed init made, and delete `branch` (None when it existed before init), so the same command runs again."""
    if fc_worktree.use_treehouse():
        fc_worktree.park_slot(wt, "HEAD")  # detached where it stands, so the branch is free to delete
        fc_worktree.release_slot(here, wt)
    else:
        fc_core.sh(["git", "worktree", "remove", "--force", str(wt)], here, check=False)
    if branch:
        fc_core.sh(["git", "branch", "-D", branch], here, check=False)

def branch_prefix(cfg):
    """The prefix of a project's branch: the active workspace's `branch_prefix` over the top level's, else `compose/`."""
    return fc_config.settings(cfg)[1].get("branch_prefix", "compose/")

def project_branch_name(prefix, name, branch=None):
    """The branch of a project's own worktree: --branch when given, else the prefix and the project's name."""
    return branch or prefix + name

def after_branch(prefix, after_name, known, tip):
    """The branch `init --after <after_name>` builds on: `tip` (its top layer, else its stored branch) when that project exists
    (`known`), else the prefix and the name, None without --after."""
    return tip if known else (prefix + after_name if after_name else None)

def branch_exists(here, branch):
    """Whether `branch` names a commit in the repository at `here`."""
    return fc_core.sh(["git", "rev-parse", "--verify", "-q", branch], here, check=False).returncode == 0

def unknown_after(after, after_name):
    """Why `--after` cannot start when `after`, the other project's branch, is not there."""
    return f"no branch {after}: `compose init --project {after_name}` that project first"

def cmd_init(a):
    """Create the project's worktree `../compose-<name>` on its branch (`compose/<name>`, or --branch; from the branch the user is on,
    or with --after from another project's branch, for a feature that needs that feature's code), with the
    `.compose/<name>/` config beside it, ignored and never committed. The user's checkout is never touched again by this project."""
    bad = check_deps()  # before anything is created: a missing required tool stops here
    tools = {row[0] for row in fc_steps.STEPS if not callable(row[1])}  # a tool row: command, command list, or file under home
    if stop := [n for n in bad if n in tools]:
        sys.exit(f"init stopped: {', '.join(stop)} missing; install what is missing above, then run it again (`compose doctor` checks again)")
    here = Path(a.repo).resolve()
    name = a.project
    prefix = branch_prefix(fc_config.read_config())
    branch = project_branch_name(prefix, name, a.branch)
    if a.branch and fc_core.sh(["git", "check-ref-format", "--branch", a.branch], here, check=False).returncode != 0:
        sys.exit(f"{a.branch!r} is not a valid branch name")
    if a.branch and a.here:
        sys.exit("--branch names the project's own worktree branch; it does not combine with --here")
    other = fc_core.all_projects(here).get(a.after) if a.after else None
    top = (fc_layers.layer_branches(here, other[1]) or [None])[-1] if other else None  # a project's code sits on its layers, not on its branch, once they exist
    after = after_branch(prefix, a.after, bool(other), (top or fc_core.project_branch(other[1])) if other else None)
    if after and a.here:
        sys.exit("--after builds a project on another project's branch in its own worktree; it does not combine with --here")
    if after and not branch_exists(here, after):
        sys.exit(unknown_after(after, a.after))
    existing, base, made = fc_core.all_projects(here).get(name), None, None  # base: the review base a new verify.json records; made: (worktree, branch init created)
    if existing:
        wt, proj = existing
        print(f"project {name} already exists at {wt}")
        old = fc_core.project_branch(proj)
        if a.branch and a.branch != old:  # move the project to a new branch name: rename in git, store it, repoint projects built on it
            if branch_exists(wt, a.branch):
                sys.exit(f"branch {a.branch} exists already")
            fc_core.sh(["git", "branch", "-m", old, a.branch], wt)
            for _, (_, p) in fc_core.all_projects(here).items():
                cfg = fc_core.project_config(p)
                if p == proj or cfg.get("review_base") == old:
                    fc_core.atomic_write((p / "verify.json"), json.dumps({**cfg, **({"branch": a.branch} if p == proj else {"review_base": a.branch, "after": fc_core.project_after(p)})}, indent=2))
            print(f"branch {old} renamed to {a.branch}")
        if after:
            move_onto(wt, proj, after, a.after)
    elif a.here:
        wt, proj = here, here / ".compose" / name
        base = fc_worktree.default_branch(here)  # the work lands on the branch the user is on, so it is reviewed against the trunk
    else:
        start = after or fc_core.sh(["git", "branch", "--show-current"], here).stdout.strip()  # empty on a detached HEAD
        base = start or fc_worktree.default_branch(here)
        new_branch = not branch_exists(here, branch)
        if new_branch:
            fc_core.sh(["git", "branch", branch, after or "HEAD"], here)
        if fc_worktree.use_treehouse():
            wt = fc_worktree.slot_on(here, branch, f"compose:{name}:project")
        else:
            if not shutil.which("treehouse"):
                print(f"treehouse is not installed, so the project gets a plain git worktree; for a reusable pool install it: {fc_worktree.TREEHOUSE_INSTALL}")
            wt = here.parent / f"compose-{name}"
            if wt.exists():
                sys.exit(f"{wt} exists already; remove it or pass --here")
            fc_core.sh(["git", "worktree", "add", "-q", str(wt), branch], here)
        made = (wt, branch if new_branch else None)
        proj = wt / ".compose" / name
        print(f"worktree {wt}  branch {branch} (from {start or 'HEAD'})")
    try:
        if fc_worktree.ignore_tool_files(wt):
            fc_core.sh(["git", "add", ".gitignore"], wt)
            fc_core.sh(["git", "commit", "-qm", fc_core.typed("Ignore local tool directories", "deps", fc_core.commit_convention(proj))], wt, check=False)
        (proj / "results").mkdir(parents=True, exist_ok=True)
        fc_pin.pin(proj, wt)  # the compose version this project runs on, until an update between phases renews it
        setup_trail(wt)
        link = fc_core.repo_link(here)  # the repo's Linear project (step 0): the main checkout's file, which every worktree reads
        if link.exists() and not fc_core.repo_link(wt).exists():  # written in a linked worktree only: the project worktree gets its own
            (wt / ".compose" / "linear.json").write_text(link.read_text())
        if not (proj / "verify.json").exists():
            databases = fc_checks.database_targets(wt)  # what the work reads and writes, recorded now: a start stops when it changes
            fc_core.atomic_write((proj / "verify.json"), json.dumps({
                "checks": [], "docs": ["README.md"], "e2e": [],
                "review": True, "perspectives": ["correctness", "security", "spec", "docs"],
                "jev_acceptance": True, "acceptance_threshold": 0.6, **({"review_base": base} if base else {}),
                **({"after": a.after} if a.after else {}), "branch": branch, "database": databases}, indent=2))
            for key, target in databases.items():
                print(f"database {key} -> {target}  (recorded in verify.json: a run or verify stops when it changes, until the user confirms the new target)")
    except BaseException:  # nothing half-made stays behind
        if made:
            undo_worktree(here, *made)
        raise
    if fc_herdr.herdr_space(proj):
        print(f"herdr space {fc_herdr.HERDR_SPACES[name]} with tabs {fc_herdr.tab_label(proj, 'shell')!r} and {fc_herdr.tab_label(proj, 'status')!r}")
        fc_herdr.herdr_name_manager(proj)
    print(f"initialised {proj}\nwork in {wt} from now on: tests and every command with --repo {wt} (or any checkout with --project {name}); "
          + f"the plan and tasks live on Linear: write {proj / 'linear.json'} as {{\"feature\": \"<issue id>\"}}")
    if (lead := fc_herdr.agent_name(name, "orchestrator")) != f"{name}-orchestrator":
        print(f"herdr agent names stop at 32 characters, so this project's are cut: its team lead is {lead}, not {name}-orchestrator; "
              f"use the names `compose crew list --project {name}` shows in every `herdr agent` command")
