import itertools, json, os, re, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import fc_core, fc_proc, fc_config, fc_tasks, fc_ledger, fc_linear, fc_models, fc_herdr, fc_worktree, fc_layers, fc_change, fc_routecost, fc_implement, fc_green, fc_gate, fc_init, fc_jev, fc_accept, fc_reroute, fc_costevent, fc_review, fc_history, fc_load, fc_trailpack, fc_update, fc_fixround, fc_keepout

CHECK_TIMEOUT = 900  # seconds a check may run; past it the check fails, named as a timeout


def check_env():
    """A verify.json check's env: the clean one, marked COMPOSE_CHECK so compose's own `python3 -m tests.run` runs the whole suite."""
    return {**fc_core.clean_env(), "COMPOSE_CHECK": "1"}


def verify_failed(proj, rounds, findings):
    """verify ended FAIL after its fix rounds: screen the settled lines against the confirmed findings. Tool errors are not
    findings, so a FAIL with none is not screened."""
    if findings.strip():
        try:
            path, _ = fc_change.reflect(proj, "verify", f"verify failed after {len(rounds)} round(s); confirmed findings:\n{findings[:15000]}")
            fc_core.say(f"reflect {path.name}")
        except Exception as e:  # noqa: BLE001  the verdict stands without the screen
            fc_core.say(f"verify reflect failed ({str(e)[:200]})")

def review_inputs(repo, proj, cfg, tasks=None, diff=None):
    """(diff, plan, docs, protected) the reviewers read. `tasks` and `diff` default to every task and the project's diff against
    its base; `compose review` passes one task's (or a range's) own."""
    if diff is None:
        base = fc_worktree.review_base(repo, cfg) or "main"
        diff = fc_core.sh(["git", "diff", f"{base}...HEAD"], repo, check=False).stdout
    tasks = fc_tasks.load_tasks(proj) if tasks is None else tasks
    named = sorted({f for t in tasks for f in t["files"] + t["protected"]})
    if not diff.strip():
        # On the base branch itself there is no range; review the current content of every file the tasks name.
        # Whole files only, up to the budget: a file cut mid-line reads as a syntax error to a small reviewer.
        budget, parts, omitted = 140000, [], []
        for f in named:
            if not (repo / f).exists():
                continue
            text = (repo / f).read_text()
            block = f"==== {f} ====\n{text}"
            if sum(len(x) for x in parts) + len(block) > budget:
                omitted.append(f)
                continue
            parts.append(block)
        if omitted:
            parts.append("==== NOT INCLUDED IN THIS PACK (too large; not a finding) ====\n" + "\n".join(omitted))
        diff = "\n\n".join(parts)
    elif len(diff) > 150000:
        diff = diff[:150000] + "\n\n==== PACK TRUNCATED HERE BY THE TOOL (not a finding) ===="
    plan = fc_tasks.linear_cache(proj)["feature"].get("description", "")  # the feature issue's description is the plan
    plan += "\n\n" + "\n\n".join(f"# Task {t['id']}: {t['title']}\n{t['body']}" for t in tasks)
    if cfg.get("review_context"):
        # Design intent reviewers must not contradict (for example: test credentials come from env by design).
        plan = "DESIGN CONTEXT FROM THE PROJECT (authoritative; do not report these as findings):\n" + str(cfg["review_context"]) + "\n\n" + plan
    docs = "\n\n".join(f"==== {d} ====\n{(repo / d).read_text()}" for d in cfg.get("docs", []) if (repo / d).exists())
    protected = sorted({f for t in tasks for f in t["protected"]})
    return diff, plan, docs, protected

def issue_line(i):
    """One review finding on one line: its [severity/perspective], file:line, and note."""
    return f"[{i.get('severity')}/{i.get('perspective')}] {i.get('file')}{':' + str(i['line']) if i.get('line') else ''}: {' '.join(i.get('note', '').split())}"

def e2e_stage(repo, cfg, panes, report, findings, results=None, retry=True, keep=False):
    """Every e2e case against one shared setup. Returns whether all passed. Runs beside review and jev, which only call models.
    With `results` (the project's results/), a jev case that does not pass keeps its step screenshots in results/e2e/, and with
    `keep` one that passes does too. `retry` False runs a harness failure once: prove expects failures and reads them itself."""
    ok = True
    stage_setup = None
    # The stage owns its lifecycle: fresh ports each run (the config uses $E2E_PORT / $E2E_CLIENT_PORT), and whatever
    # it started (setup's process group, plus anything left listening on its ports that was not before) ends with it,
    # pass, fail, exception, Ctrl-C, SIGTERM or SIGHUP, so no server or browser outlives a round to answer the next one.
    # A verify killed outright ends nothing; the next stage ends that one's browser (reap_orphans).
    for gone in fc_jev.reap_orphans():
        fc_core.say(f"e2e    ended the browser of a verify that is gone: {gone}")
    env = {k: str(v) for k, v in (cfg.get("e2e_env") or {}).items()}
    env.update(E2E_PORT=str(fc_core.free_port()), E2E_CLIENT_PORT=str(fc_core.free_port()))
    env = fc_jev.expand_ports(env, env)
    # Never the product owner's Chrome: without BU_CDP_URL, browser-harness attaches to the running Chrome and waits for approval.
    # So the stage launches its own Chromium on a fresh port and profile, unless e2e_env names one or e2e_browser is false.
    profile = None if "BU_CDP_URL" in env or cfg.get("e2e_browser") is False else tempfile.mkdtemp(prefix=f"fc-e2e-browser-{os.getpid()}-")
    if profile:
        cdp = fc_core.free_port()
        env.update(BU_CDP_URL=f"http://127.0.0.1:{cdp}", BU_NAME=f"fc-e2e-{cdp}")
    if "BU_CDP_URL" in env:  # bp has no default port, so a driver's or assert's bp reaches this stage's browser
        env.setdefault("BP_PORT", env["BU_CDP_URL"].rstrip("/").rsplit(":", 1)[1])
    env.setdefault("BU_NAME", f"fc-e2e-{fc_core.free_port()}")  # every stage names its daemons, so it ends its own and no one else's
    cfg = {**cfg, "e2e": [fc_jev.expand_ports(c, env) for c in cfg.get("e2e", [])]}
    ports = fc_jev.e2e_ports(cfg, env)
    busy = {p for p in ports if fc_core.port_listening(p)}
    daemons = fc_jev.harness_daemons(env["BU_NAME"])
    shots = Path(results) / "e2e" if results else None
    if shots:
        shutil.rmtree(shots, ignore_errors=True)  # only the last run's cases

    def end_stage():
        """End what the stage started: from its `finally`, and from a SIGTERM or SIGHUP (fc_proc.HOOKS), which skips that."""
        if stage_setup:
            fc_jev.stop_group(stage_setup)
        fc_jev.kill_listeners(ports - busy)
        fc_jev.end_new_daemons(env["BU_NAME"], daemons)
        if profile:
            shutil.rmtree(profile, ignore_errors=True)
    hook = f"e2e stage {env['BU_NAME']}"
    fc_proc.HOOKS[hook] = end_stage
    try:
        if profile:
            # Headless unless the project asks to watch (e2e_headed): an unattended verify runs while the display sleeps, and a
            # headed Chromium then stops painting, so screenshots and clicks time out. compose's own test suite forces
            # headless (COMPOSE_E2E_HEADLESS=1), so testing the tool never pops a window on the product owner's screen.
            headless = not cfg.get("e2e_headed") or os.environ.get("COMPOSE_E2E_HEADLESS") == "1"
            if not headless:  # a watched window needs the display awake (cmd_verify already holds this for its whole run)
                fc_proc.keep_awake()
            p = fc_core.sh(["bp", "launch", "about:blank", "--profile", profile, *(["--headless"] if headless else [])], repo, timeout=30, check=False,
                   env={**os.environ, "BP_PORT": env["BP_PORT"]})
            if p.returncode:  # the cases then fail on the dead CDP port, which reads as a tool error, not the app's
                fc_core.say(f"e2e    browser launch failed: {(p.stderr or p.stdout).strip()[-300:]}")
        if cfg.get("e2e_setup"):
            # One setup (for example a local server) for the whole stage; per-case `setup` still works for one-offs.
            stage_setup = subprocess.Popen(cfg["e2e_setup"], shell=True, cwd=repo, env={**fc_core.clean_env(), **env}, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            fc_jev.await_setup(cfg["e2e_setup"], env, cfg.get("e2e_setup_wait", 3))
        for i, case in enumerate(cfg.get("e2e", [])):
            t1 = time.time()
            stage = f"e2e {case.get('name', i)}"[:40]
            fc_herdr.herdr_stage(panes, stage, "working")
            record = {"record_dir": str(shots / f"{i + 1:02d}-{re.sub(r'[^A-Za-z0-9._-]+', '-', str(case.get('name', 'case')))[:60]}"), "keep_record": keep} \
                if shots and not case.get("driver") else {}
            run_case = lambda: fc_jev.jev_e2e({**case, **record, "env": {**env, **(case.get("env") or {})}}, repo)
            r = run_case()
            if retry and r["exit"] and fc_jev.tool_error(r["stdout"] + r["stderr"]):
                fc_core.say(f"e2e    tool error in {case.get('name')}; retrying once")
                r = run_case()
            r["tool_error"] = bool(r["exit"]) and fc_jev.tool_error(r["stdout"] + r["stderr"])
            r["seconds"] = round(time.time() - t1, 1)
            report["e2e"].append(r)
            ok &= r["exit"] == 0
            verdict = "ok  " if r["exit"] == 0 else "TOOL ERROR" if r["tool_error"] and retry else "FAIL"  # prove expects its cases to fail: a blocked jev is not a tool error there
            fc_herdr.herdr_stage(panes, stage, "idle" if r["exit"] == 0 else "blocked", verdict.strip())
            fc_jev.say_case(case.get("name"), r, verdict)
            if r["exit"] and not r["tool_error"]:  # a tool crash is not the app's failure: no fixer gets it
                findings.append(f"E2E failed: {case.get('name')}\n{r['stdout']}\n{r['stderr']}")
    finally:
        if profile:  # browser-harness keeps one daemon per BU_NAME alive; end ours with the browser it drove
            fc_core.sh(["uv", "run", "--quiet", "python", "-c", f"from browser_harness.admin import restart_daemon; restart_daemon({env['BU_NAME']!r})"],
               fc_config.JEV_DIR, timeout=30, check=False)
        end_stage()
        fc_proc.HOOKS.pop(hook, None)  # only now: a SIGTERM during the slow daemon stop above still ends the rest
    return ok

def fix_tests(tasks, files):
    """The test commands that cover a fix: each code task's `test:` whose `files:` or `protected:` holds a file the fix
    changed, once each, in task order. None when a changed file is in no task: only the full checks cover it."""
    cover = [t for t in fc_tasks.code_tasks(tasks) if set(files) & set(t["files"] + t["protected"])]
    if not files or any(all(f not in t["files"] + t["protected"] for t in cover) for f in files):
        return None
    return list(dict.fromkeys(t["test"] for t in cover))

def save_output(results, cmd, started, text):
    """A check's whole output, in results/checks/ under a name from the start time and the command; returns its path.
    Two checks that start in one second with the same command prefix get `-2`, `-3`, ... so none overwrites another."""
    folder = Path(results) / "checks"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(started))}-{re.sub(r'[^A-Za-z0-9]+', '-', cmd).strip('-')[:40]}"
    for n in itertools.count(1):
        f = folder / (f"{stem}.log" if n == 1 else f"{stem}-{n}.log")
        try:
            with open(f, "x") as fh:
                fh.write(text)
            return str(f)
        except FileExistsError:
            pass

def failure_summary(full, tail, path, cap=6000):
    """What a reflect or fix round reads of a failed check: every failing test (unittest's FAIL:/ERROR: header lines, up to `cap`
    characters), then the output's tail, then where the whole output is."""
    ids = "\n".join(re.findall(r"^(?:FAIL|ERROR): .+$", full, re.M))
    ids = ids if len(ids) <= cap else ids[:cap].rsplit("\n", 1)[0] + "\n(more failing tests in the full output)"
    return (f"Failing tests:\n{ids}\n\n" if ids else "") + f"End of the output:\n{tail}\n\nThe whole output is in {path}"

def run_stages(repo, proj, cfg, a, tests=None):
    """One pass: checks (cheap, exact), then e2e in parallel with review and acceptance. With `tests` (a fix round's
    `fix_tests`) the first stage runs only those, and the full checks run once at the end, only when every stage passed.
    A review-only pass (`--review-only`, right after `run`) skips checks and e2e: just the review and acceptance."""
    report, ok, findings = {"checks": [], "scoped": [], "e2e": [], "review": None, "acceptance": None}, True, []
    home = fc_core.home_state()
    review_only = getattr(a, "review_only", False)
    e2e_cases = [] if a.skip_e2e or review_only else cfg.get("e2e", [])
    stages = (["checks"] if (cfg.get("checks") or tests) and not review_only else []) + [f"e2e {c.get('name', i)}"[:40] for i, c in enumerate(e2e_cases)] \
        + ([] if a.skip_review else (["review"] if cfg.get("review", True) else []) + (["jev"] if cfg.get("jev_acceptance", True) and cfg.get("depth") != "FAST" else []))
    panes = fc_herdr.herdr_verify_panes(fc_herdr.herdr_space(proj), repo, stages, fc_herdr.tab_label(proj, "verify"))

    def run_checks(cmds, into):
        for c in cmds:
            tree = fc_green.tree_of(repo)
            if line := fc_green.taken(proj, tree, c, getattr(a, "fresh", False)):  # green on these very files already: not run twice
                fc_core.say(f"check  {line}")
                into.append({"cmd": c, "exit": 0, "seconds": 0, "output": "", "tool_error": False})
                continue
            t1, clk = time.time(), fc_proc.clock()
            with fc_proc.slot(fc_core.say, not fc_proc.suite(c)) as held:
                env = {**check_env(), "COMPOSE_TEST_SLOT": held}
                p = fc_proc.run_limited(["/bin/sh", "-c", c], cwd=repo, env=env, timeout=CHECK_TIMEOUT)  # in a group of its own: a stop ends the whole suite
                full = fc_core.scrub(p.stdout + p.stderr)
                out = full[-3000:]  # the retry and the label read this one text: a suite that prints harness errors in passing tests is no tool error
                nap = fc_proc.slept(clk) if p.returncode else 0  # a check the machine slept through fails on the sleep: its network and its clocks stopped with it
                if p.returncode and (nap or fc_jev.tool_error(out)):
                    fc_core.say(f"check  {fc_proc.slept_text(nap) + '; run again' if nap else 'tool error; retrying once'}  {c}")
                    p = fc_proc.run_limited(["/bin/sh", "-c", c], cwd=repo, env=env, timeout=CHECK_TIMEOUT)
                    full = fc_core.scrub(p.stdout + p.stderr)
                    out = full[-3000:]
            tool = bool(p.returncode) and fc_jev.tool_error(out)
            fc_green.record(proj, tree, c, p.returncode)
            awake = time.time() - t1 - fc_proc.slept(clk)  # the time the check ran, not the time the lid was closed
            record = {"cmd": c, "exit": p.returncode, "seconds": round(awake, 1), "output": out, "tool_error": tool}
            if p.returncode:  # the cut hides most of a long suite's failures: the whole output stays in a file
                record["output_file"] = save_output(proj / "results", c, t1, full)
                if kept := fc_keepout.keep_artifacts(repo, cfg.get("check_artifacts", []), record["output_file"]):
                    record["artifacts"] = kept
            into.append(record)
            fc_core.say(f"check  {'ok  ' if p.returncode == 0 else 'TOOL ERROR' if tool else 'FAIL'} {fc_core.fmt(awake):>8}  {c}"
                        + (fc_keepout.tail(full) + f"\n           full output {record['output_file']}" + (f"\n           artifacts   {kept}" if record.get("artifacts") else "") if p.returncode else ""))
            if p.returncode and not tool:
                findings.append(f"Command failed: {c}\n{failure_summary(full, out, record['output_file'])}")
        passed = all(c["exit"] == 0 for c in into)
        fc_herdr.herdr_stage(panes, "checks", "idle" if passed else "blocked", "ok" if passed else "FAIL")
        return passed

    checks_pass = None  # no checks ran: the reviewers are not told the tests pass
    if not review_only:
        if cfg.get("checks") or tests:
            fc_load.wait(fc_core.say, getattr(a, "force", False))
        fc_herdr.herdr_stage(panes, "checks", "working", "" if tests is None else "the fix's tests")
        if tests is not None:
            fc_core.say(f"check  scope  {len(tests)} task test(s) for the fix; the full checks run once every stage passes")
        checks_pass = run_checks(cfg.get("checks", []) if tests is None else tests, report["checks"] if tests is None else report["scoped"])
        ok &= checks_pass
    # e2e drives a browser for minutes while review and jev only wait on model calls: run them side by side, after checks
    # (which may bind the same ports as e2e_setup). Findings from both land in the same list.
    pool = ThreadPoolExecutor(max_workers=1)
    if not a.skip_e2e and cfg.get("e2e") and not review_only:
        fc_load.wait(fc_core.say, getattr(a, "force", False))
    e2e = pool.submit(e2e_stage, repo, cfg, panes, report, findings, proj / "results") if not a.skip_e2e and cfg.get("e2e") and not review_only else None
    if not a.skip_review and cfg.get("review", True):
        t1 = time.time()
        fc_herdr.herdr_stage(panes, "review", "working")
        diff, plan, docs, _ = review_inputs(repo, proj, cfg)
        rv = fc_review.llm_review(cfg, diff, plan, docs, bool(checks_pass))
        rv["seconds"] = round(time.time() - t1, 1)
        report["review"] = rv
        ok &= rv["pass"]  # review always gates; the jev acceptance stage below is advisory unless asked to gate
        fc_herdr.herdr_stage(panes, "review", "idle" if rv["pass"] else "blocked", f"{len(rv['confirmed'])} confirmed")
        verdict = "ok  " if rv["pass"] else "FAIL" if any(i["severity"] == "high" for i in rv["confirmed"]) else "TOOL ERROR"  # no confirmed high: only a confirm call failed
        fc_core.say(f"review {verdict} {fc_core.fmt(rv['seconds']):>8}  {len(rv['confirmed'])} confirmed of {len(rv['issues'])} raised"
                    + (f", {rv['malformed']} malformed dropped" if rv.get("malformed") else ""))
        if rv.get("error"):
            print(f"           {rv['error']}")
        for i in rv["issues"]:
            mark = "confirmed" if i["confirmed"] else "judge err" if i.get("judge_error") else "unjudged " if i.get("unjudged") else "dropped  "
            print(f"           {mark} {issue_line(i)}")
        for i in rv["confirmed"]:
            findings.append(f"[{i['severity']}] {i['file']}: {i['note']}\nEvidence: {i.get('evidence', '')}"
                            + (f"\nMust read: {i['fix'] or '(remove the line)'}" if "fix" in i else ""))
    if not a.skip_review and cfg.get("jev_acceptance", True) and cfg.get("depth") != "FAST":
        t1 = time.time()
        fc_herdr.herdr_stage(panes, "jev", "working")
        diff, _, _, _ = review_inputs(repo, proj, cfg)
        try:
            acc = fc_accept.jev_acceptance(cfg, proj, diff, repo=repo)
        except Exception as e:  # noqa: BLE001
            acc = {"pass": False, "criteria": [], "failed": [], "error": str(e)}
        acc["seconds"] = round(time.time() - t1, 1)
        report["acceptance"] = acc
        gate = bool(cfg.get("acceptance_gate", False))  # tests already cover every criterion; the classifier is advisory unless asked to gate
        if gate:
            ok &= acc["pass"]
        n = len(acc.get("criteria", []))
        met = sum(r["choice"] == "MET" for r in acc.get("criteria", []))
        fc_herdr.herdr_stage(panes, "jev", "idle" if acc["pass"] or not gate else "blocked", f"{met}/{n} met")
        fc_core.say(f"jev    {'ok  ' if acc['pass'] else ('FAIL' if gate else 'warn')} {fc_core.fmt(acc['seconds']):>8}  {met}/{n} criteria met, "
            f"{len(acc.get('unclear', []))} unproven"
            + (f", {n_test} proven by their task's test" if (n_test := sum(bool(r.get("proven_by")) for r in acc.get("criteria", []))) else "")
            + (f"  error={acc['error'][:120]}" if acc.get("error") else ""))
        for r in acc.get("criteria", []):
            if r["choice"] != "MET":
                flag = "NOT MET " if r in acc.get("failed", []) else ("unproven" if r in acc.get("suspects", []) else "unclear ")
                print(f"           {flag} p_met={r['p_met']:.2f} p_not={r['p_not_met']:.2f} [{r['task']}] {r['criterion'][:110]}"
                      + (f"  (note: the task's test passed, ledger event {r['proven_by']})" if r.get("proven_by") else ""))
        for r in acc.get("failed", []):
            if gate:
                findings.append(f"[high] acceptance criterion not met (task {r['task']}, p={r['p_not_met']}): {r['criterion']}")
        for r in acc.get("unclear", []):
            if gate:
                findings.append(f"[high] acceptance criterion not proven, judged {r['choice']} and not confirmed as a violation (task {r['task']}): {r['criterion']}")
    if e2e:
        ok &= e2e.result()
    pool.shutdown()
    if ok and tests is not None and cfg.get("checks"):  # the fix passed its own tests and every stage: no regression anywhere else
        fc_core.say("check  full   every check, once, after the fix passed every stage")
        fc_herdr.herdr_stage(panes, "checks", "working", "full checks")
        ok &= run_checks(cfg["checks"], report["checks"])
    fc_core.flag_home(home, "verify's checks and e2e")
    return ok, report, "\n\n".join(findings)

def verify_root(repo, proj, cfg):
    """Where verify runs. Task branches are never merged locally, so with unmerged layers verify runs in the CLI worktree
    checked out at the stack's top branch (fix commits then land on that layer, reviewed like everything else); the review
    base is the trunk. With no open layers it runs on the user's checkout against fc_worktree.review_base. A chain whose
    rebase stopped fails verify here, before any stage and with no result recorded."""
    trunk = fc_core.current_branch(repo)
    if not fc_worktree.stack_order(repo, proj):
        return repo, fc_worktree.review_base(repo, cfg) or trunk, None
    wt = fc_worktree.stack_worktree(repo, proj, trunk)
    chain = fc_layers.linearize(repo, proj, trunk, wt)
    if fc_layers.STACK_STOP:  # the top of a partial chain lacks every layer above the stop: a pass there would claim the feature
        fc_core.say(f"verify FAIL  nothing verified or recorded: {fc_layers.STACK_STOP}")
        sys.exit(1)
    if not chain:
        return repo, fc_worktree.review_base(repo, cfg) or trunk, None
    fc_core.sh(["git", "checkout", "-q", chain[-1]], wt)
    fc_worktree.ready_checkout(repo, wt, install=cfg.get("install"))
    fc_core.say(f"verify at    stack top {chain[-1]} ({len(chain)} layer(s) on {trunk}); fixes commit on that layer")
    return wt, trunk, wt

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

def cmd_verify(a):
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    fc_update.refuse_while_building(proj, repo)
    fc_init.start_checks(repo, stack=False, tree=False, force=getattr(a, "force", False), proj=proj)  # keys and machine only: verify tolerates a dirty tree
    fc_proc.keep_awake()  # a display that sleeps fails every e2e check, and a verify takes long enough for the Mac to sleep
    cfg_path = proj / "verify.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    review_only = getattr(a, "review_only", False)
    if review_only:
        a.fix = 0  # it only reports: the fix loop belongs to the full verify
    t0 = time.time()
    # Review, acceptance, and a fixer read the plan, the criteria, and the protected files from the Linear sync; checks and
    # e2e read only verify.json, so groundwork proves them before planning with --skip-review --fix 0.
    tasks = fc_tasks.load_tasks(proj, required=not a.skip_review or a.fix > 0,
                                hint=f"; checks and e2e alone run without one: `compose verify --project {proj.name} --skip-review --fix 0`")
    import fc_depth
    base_cfg = cfg
    cfg, depth, deep = fc_depth.depth_config(cfg, proj, tasks)
    fc_core.say(f"verify depth {depth}" + (f" (deepest task {deep})" if deep and depth != "STANDARD" else ""))
    left_out = (["tasks"] if not tasks else []) + (["review"] if a.skip_review else []) + (["e2e"] if a.skip_e2e and not review_only else []) + (["checks", "e2e"] if review_only else [])
    fc_core.say(f"verify start project={proj.name} fix_rounds={a.fix}")
    rounds = []
    root, base, wt = verify_root(repo, proj, cfg)
    fc_depth.record_depth(proj, root, base_cfg, cfg, depth, deep)
    cfg = {**cfg, "review_base": base}
    if root != repo:
        # Checks and e2e cases (setup, driver, assert, env, every string) may name the main checkout or the project worktree
        # by absolute path; run them against the stack top.
        olds = (repo, fc_core.main_checkout(repo))
        cfg.update({k: at_root(cfg[k], olds, root) for k in ("checks", "e2e", "e2e_setup", "e2e_env") if k in cfg})
        cfg["main_repo"] = repo
    try:
        fc_reroute.before_verify(repo, proj, tasks, base)
        verify_rounds(root, proj, cfg, a, rounds, t0, left_out)
    except fc_models.PaymentRequired as e:
        fc_models.payment_halt(repo, proj, "verify", [], {"fix": a.fix, "skip_e2e": a.skip_e2e, "skip_review": a.skip_review}, e)
    finally:
        if wt is not None:
            fc_core.sh(["git", "checkout", "-q", "--detach", fc_core.current_branch(repo)], wt, check=False)  # free the top branch again

def fix_strays(touched, allowed, named=()):
    """Files a verify fix round may not touch. A fixer gets the same scope as the tasks: only files the stack already
    changes. With no stack diff (verify on a plain checkout) it is free except for lockfiles, which a fixer only rewrites
    when it is working around a broken environment, not fixing a finding. A lockfile a task names in `files:` is part of the work."""
    return [f for f in touched if f not in allowed and f not in named] if allowed else [f for f in touched if Path(f).name in fc_worktree.LOCKFILES and f not in named]

def wrong_layer(tasks, files, top):
    """The files of a fix that belong to another layer: some task's `files:` lists them and the top task's does not.
    A fix commits on the top layer, so it may change only what that layer owns (or what no task names)."""
    code = fc_tasks.code_tasks(tasks)
    owners = {f: sorted(t["id"] for t in code if f in t["files"]) for f in files}
    return {f: o for f, o in owners.items() if o and top not in o} if top else {}

def cut_top_task(foreign, top):
    """What to do about a fix that belongs to a lower layer: only a task above the stack can change those files."""
    return f"cut a new top task (id above {top}) whose files: lists {', '.join(sorted(foreign))}, run it, then verify again"

def fix_answers(repo, fixed, report, install=None):
    """Whether a fix round answers something reproduced: it removes the line a confirmed review finding quotes, an e2e case
    failed (the next round reruns it), a check that failed passes when run again, or it changes the file a confirmed finding
    names and adds a test of its own that fails without the fix and passes with it (a fix that only adds code, a guard)."""
    diff = fc_core.sh(["git", "--literal-pathspecs", "diff", "-U0", "HEAD", "--", *fixed], repo, check=False).stdout
    removed = " ".join(" ".join(l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")).split())
    quotes = [" ".join(str(i.get("evidence") or "").split()) for i in (report.get("review") or {}).get("confirmed", [])]
    failed = lambda rs: [r for r in rs if r["exit"] and not r.get("tool_error")]
    ran = [r["cmd"] for r in report.get("checks", []) + report.get("scoped", [])]
    named = [str(i.get("file") or "").lstrip("./") for i in (report.get("review") or {}).get("confirmed", [])]
    return any(q and q in removed for q in quotes) or bool(failed(report.get("e2e", []))) or \
        any(fc_proc.run_limited(["/bin/sh", "-c", r["cmd"]], cwd=repo, env=check_env(), timeout=CHECK_TIMEOUT).returncode == 0
            for r in failed(report.get("checks", []) + report.get("scoped", []))) or \
        (any(n and (f == n or f.endswith("/" + n)) and not fc_gate.TEST_FILE_RE.search(f) for n in named for f in fixed)
         and fc_gate.new_tests_prove(fc_implement.timed, repo, fixed, ran, install))

def fix_message(reply, files, conv=None):
    """The fixer's one-line description of its change; when it gave none, or one about the process, the files it changed.
    The tool's name counts only as a word of its own, so docker-compose, compose.yaml, or composer stay a real change."""
    line = next(iter((reply or "").strip().splitlines()), "").strip(" \"'`.")
    if line and len(line) <= 72 and not re.search(r"(?<![\w-])compose(?![\w.-])|verif|finding|wave|task|linear|round", line, re.I):
        return fc_core.typed(line, "fix", conv)
    return fc_core.typed("Fix " + ", ".join(Path(f).name for f in files[:3]), "fix", conv)

def changed_files(repo):
    """Each file differing from HEAD, tracked or untracked (not ignored), with its digest; never the agent wiring (fc_worktree.WIRING)."""
    out = lambda *c: fc_core.sh(["git", *c, "-z"], repo, check=False).stdout.split("\0")
    return fc_core.digests(repo, fc_fixround.unwired(out("diff", "--name-only", "--no-renames", "HEAD") + out("ls-files", "--others", "--exclude-standard")))

def verify_rounds(repo, proj, cfg, a, rounds, t0, left_out=()):
    base = fc_worktree.review_base(repo, cfg)
    allowed = set(fc_core.sh(["git", "diff", "--name-only", f"{base}...HEAD"], repo, check=False).stdout.splitlines()) if base else set()
    dirty = fc_worktree.dirty_paths(repo)  # the user's own changes, read before any stage: what a check leaves behind is verify's
    rejected, tests = [], None  # the first round runs the full checks; a fix round only the tests of what its fix changed
    branch = lambda: fc_core.sh(["git", "branch", "--show-current"], repo, check=False).stdout.strip()
    stands_on = branch()
    lap = time.time()
    def round_logged(ok):
        nonlocal lap
        fc_history.record(proj, repo, "verify-round", round=len(rounds), outcome="pass" if ok else "fail", seconds=round(time.time() - lap, 1))
        lap = time.time()
    for rnd in range(a.fix + 1):
        if cfg.get("main_repo"):  # a fix round may have changed the lockfile
            fc_worktree.ready_checkout(cfg["main_repo"], repo, install=cfg.get("install"))
        ok, report, findings = run_stages(repo, proj, cfg, a, tests)
        if fc_fixround.checkout_moved(stands_on, branch()):  # a fix commit stays on the branch; only a checkout moves it, and then the stages judged other code
            fc_core.say(fc_fixround.void_message(repo, stands_on, branch()))
            sys.exit(1)
        rounds.append(report)
        round_logged(ok)
        fc_ledger.verify_record(proj, repo, report)
        report["sufficiency"] = fc_accept.jev_sufficiency(cfg, proj, repo) if not a.skip_review and cfg.get("jev_acceptance", True) else None
        while True:
            raised, failed = fc_reroute.after_round(cfg.get("main_repo") or repo, proj, base)
            if failed or not raised:
                break
            ok, report, findings = run_stages(repo, proj, cfg, a, tests)
            rounds.append(report)
            round_logged(ok)
            fc_ledger.verify_record(proj, repo, report)
            report["sufficiency"] = fc_accept.jev_sufficiency(cfg, proj, repo) if not a.skip_review and cfg.get("jev_acceptance", True) else None
        if failed:
            ok = False
            break
        if ok or rnd == a.fix:
            break
        if not findings.strip():
            fc_core.say("fix    skip  only tool errors failed (see TOOL ERROR above); nothing for a fixer to change in the app")
            break
        # Fix loop: hand confirmed findings to the fast implementer in the repo itself, then re-run every stage.
        if dirty and rnd == 0:
            fc_core.say(f"fix    skip  repo has uncommitted changes ({', '.join(dirty[:5])}); commit first so a bad fix can be reverted")
            break
        _, _, _, protected = review_inputs(repo, proj, cfg)
        main = cfg.get("main_repo") or repo
        before, pending = fc_core.digests(repo, protected), changed_files(repo)
        t1 = time.time()
        model = cfg.get("fix_model") or os.environ.get("COMPOSE_FIX_MODEL") or fc_config.DEFAULT_MODEL
        if rnd and fc_models.fallback_for(model):  # the first fix did not clear the findings; the fallback takes later rounds
            model = fc_models.fallback_for(model)
        fc_core.say(f"fix    start round {rnd + 1}  model={model}")
        fc_tasks.mark_fixing(proj, rnd + 1)
        pack = fc_trailpack.trail_pack(repo, findings[:400])
        prompt = (f"REPOSITORY ROOT: {repo}\nEvery edit must be under that root.\n\n" +
                  (f"CONTEXT PACK from trail (exact file:line). Use it instead of exploring.\n{pack}\n\n" if pack else "") +
                  "Verification of this repository failed. Fix ONLY the findings below with the smallest change. "
                  f"Do not edit these files: {', '.join(protected) or 'none'}. Do not weaken or delete tests. "
                  "Keep behavior the tests already cover. Keep the code simple; do not restructure; " + fc_review.SIMPLICITY_BAR.replace("no hardening.", "no hardening beyond the findings.") + " "
                  + (f"DESIGN CONTEXT (authoritative; never contradict it): {cfg['review_context']} " if cfg.get("review_context") else "") +
                  "Fix the app for a real user, never for the test harness: do not add labels, text, attributes, or "
                  "shortcuts aimed at the test bot or the e2e script; if a finding is the harness's fault, change nothing. "
                  "Do not run tests or checks: after your turn verify runs the tests of what you changed, and every check once the rest passes. Do not explain, just edit; your whole reply is one line, "
                  "a plain description of the change you made (like 'Save submissions in one transaction'), never about this process.\n\nFINDINGS:\n" + findings[:30000])
        try:
            _, reply, tools, usage = fc_implement.implementer(repo, model, prompt)
        except fc_models.RateLimited as e:
            fc_core.say(f"fix    {e.why}; " + (f"retrying on {fc_models.fallback_for(model)}" if fc_models.fallback_for(model) else "waiting 30s and retrying once"))
            if not fc_models.fallback_for(model):
                time.sleep(30)
            try:
                _, reply, tools, usage = fc_implement.implementer(repo, fc_models.fallback_for(model) or model, prompt)
            except fc_models.PaymentRequired:
                raise
            except Exception as e2:  # noqa: BLE001
                reply, tools, usage = f"fix call failed: {e2}", 0, {"cost": 0.0}
        except fc_models.PaymentRequired:
            raise
        except Exception as e:  # noqa: BLE001
            reply, tools, usage = f"fix call failed: {e}", 0, {"cost": 0.0}
            if fc_models.fallback_for(model):
                fc_core.say(f"fix    {model} errored; retrying on {fc_models.fallback_for(model)}")
                try:
                    _, reply, tools, usage = fc_implement.implementer(repo, fc_models.fallback_for(model), prompt)
                except fc_models.PaymentRequired:
                    raise
                except Exception as e2:  # noqa: BLE001
                    reply, tools, usage = f"fix call failed: {e2}", 0, {"cost": 0.0}
        tampered = [f for f, d in fc_core.digests(repo, protected).items() if d != before[f]]
        if tampered:
            fc_core.sh(["git", "checkout", "--"] + tampered, repo, check=False)
            fc_core.say(f"fix    reverted protected files the fixer touched: {', '.join(tampered)}")
        fixed = [f for f, d in changed_files(repo).items() if f not in pending or pending[f] != d]  # its turn's change alone: no check's leftovers
        added = set(fc_core.sh(["git", "ls-files", "--others", "--exclude-standard", "-z"], repo).stdout.split("\0")) & set(fixed)
        new_docs = [f for f in fixed if f in added and fc_config.DOC_RE.search(f)]
        for f in new_docs:  # fixers, like implementers, never add documents
            (repo / f).unlink()
        if new_docs:
            fc_core.say(f"fix    removed documents the fixer added: {', '.join(new_docs)}")
        fixed = [f for f in fixed if f not in new_docs]
        tasks = fc_tasks.load_tasks(proj)
        stray = fix_strays(fixed, allowed, {f for t in fc_tasks.code_tasks(tasks) for f in t["files"]})
        order = fc_worktree.stack_order(main, proj)
        top = order[-1].removeprefix(fc_worktree.task_prefix(main, proj)) if order else None
        foreign = {} if stray else wrong_layer(tasks, fixed, top)
        # The whole round goes when it is out of scope (a stray edit means the fixer worked around something, an install or the
        # toolchain, instead of fixing a finding, and its other edits rest on that), on a layer that does not own what it changed
        # (a fix commits on the top layer), or when it answers nothing reproduced. The last stage run failed, so verify ends FAIL.
        why = (f"touched files outside the stack ({', '.join(stray[:8])})" if stray else
               f"changed a file of layer {', '.join(sorted({x for o in foreign.values() for x in o}))}, not the top layer {top}: {', '.join(foreign)}" if foreign else
               "changed no failing check and removed no line a confirmed finding cites" if fixed and not fix_answers(repo, fixed, report, cfg.get("install")) else None)
        if why:
            restore, drop = fc_fixround.revert_plan(fixed, added)
            if restore:
                fc_core.sh(["git", "--literal-pathspecs", "checkout", "-q", "HEAD", "--", *restore], repo)
            for f in drop:
                (repo / f).unlink()
            rejected.append({"round": rnd + 1, "files": stray or sorted(foreign) or fixed, **({} if stray else {"why": why}), **({"next": cut_top_task(foreign, top)} if foreign else {})})
            fc_core.say(f"fix    REJECTED round {rnd + 1}: {why}; reverted the round")
            if foreign:
                fc_core.say(f"fix    next  {rejected[-1]['next']}")
            break
        fc_core.say(f"fix    done  {fc_core.fmt(time.time() - t1):>8}  tools={tools} changed={'yes' if fixed else 'no'}  ${usage['cost']:.4f}")
        if fixed:
            fc_core.sh(["git", "--literal-pathspecs", "add", "-A", "--", *fixed], repo)
            fc_core.sh(["git", "--literal-pathspecs", "commit", "-qm", fix_message(reply, fixed, fc_core.commit_convention(proj)), "--", *fixed], repo, check=False)
            fc_core.say("fix    committed on " + (fc_core.sh(["git", "branch", "--show-current"], repo, check=False).stdout.strip() or "HEAD"))
            rounds[-1]["fix_tasks"] = fc_routecost.fix_tasks(fc_tasks.load_tasks(proj), fixed)
            tests = fix_tests(fc_tasks.load_tasks(proj), fixed)
            if tests is None:
                fc_core.say("fix    scope  the fix changed a file no task names; the next round runs the full checks")
            unrouted = fc_reroute.catch_up(main, proj, fc_tasks.load_tasks(proj), [order[-1].removeprefix(fc_worktree.task_prefix(main, proj))], base, ("scope",)) if order else {}
            fc_reroute.say_unrouted(unrouted)
            if unrouted:
                break
        else:
            break
    final = {"rounds": rounds, "passed": ok, "rejected_fixes": rejected, "seconds": round(time.time() - t0, 1), "cost": fc_core.LEDGER.snapshot()}
    if left_out:
        final["left_out"] = left_out
    (proj / "results").mkdir(exist_ok=True)
    review_only = getattr(a, "review_only", False)  # its own record: neither the last verify's nor a smoke run's
    results = proj / "results" / ("review.json" if review_only else "smoke.json" if left_out else "verify.json")
    fc_core.atomic_write(results, json.dumps(final, indent=2))
    if results.name == "verify.json":
        path = proj / "linear.json"
        feature = json.loads(path.read_text()).get("feature") if path.exists() else None
        sha = fc_core.sh(["git", "rev-parse", "HEAD"], repo, check=False).stdout.strip()
        fc_ledger.record(feature, fc_costevent.verify_events(proj, fc_tasks.load_tasks(proj), ok, final["cost"]["total"], "", sha))
    for issue in list(dict.fromkeys(i for i, _ in fc_ledger.UNSAVED)):
        fc_ledger.add_row(issue, [])
    lost = fc_ledger.unsaved()
    if lost:
        ok = final["passed"] = False
        fc_core.say("verify FAIL  evidence not saved on Linear: " + ", ".join(r["event_id"] for r in lost))
        fc_core.atomic_write(results, json.dumps(final, indent=2))
    (proj / "results" / "resume.json").unlink(missing_ok=True)
    fc_core.say((f"review {'PASS' if ok else 'FAIL'} (review and acceptance only; the full verify runs the checks and e2e)" if review_only else
                 f"smoke {'PASS' if ok else 'FAIL'} ({', '.join('no ' + x for x in left_out)})" if left_out else f"verify {'PASS' if ok else 'FAIL'}")
                + f"  wall {fc_core.fmt(final['seconds'])}  rounds={len(rounds)}")
    fc_history.record(proj, repo, "verify-end", rounds=len(rounds), seconds=final["seconds"], cost=final["cost"]["total"], outcome="pass" if ok else "fail")
    if ok and not left_out:
        fc_linear.linear_reconcile(proj, verified=True)
    fc_core.say(fc_core.LEDGER.line())
    if not ok and not left_out and not lost:  # a smoke run is a sign of life only; its findings never reach the reflect screen
        print("\nUnresolved after the fix budget. Escalate to the team lead with the confirmed findings above.")
        verify_failed(proj, rounds, findings)
    if ok and (space := fc_herdr.herdr_project_space(proj)):  # a failed run keeps its tab to read, like a missed task's pane
        fc_herdr.herdr_close_tabs(space, fc_herdr.tab_label(proj, "verify"))
    sys.exit(0 if ok else 1)
