import json, os, re, shlex, shutil, subprocess, sys, time, uuid
from pathlib import Path
import fc_core, fc_pause, fc_config, fc_providers, fc_readguard, fc_tasks, fc_linear, fc_claude, fc_models, fc_proc, fc_load, fc_harness, fc_herdr, fc_worktree, fc_layers, fc_libapi, fc_green, fc_gate, fc_fast, fc_format, fc_started, fc_figma, fc_trailpack

def implementer(cwd, model, prompt, session=None, task=None, timeout=None):
    """Route a turn to the harness named by the model id: `pi:<provider/model>` runs pi (as a named herdr agent when the run
    is inside herdr and `task` names the project's space), anything else runs OpenCode."""
    fake = os.environ.get("COMPOSE_FAKE_402")  # test hook: behave as if the provider refused for billing (optionally only for prompts matching)
    if fake and os.environ.get("COMPOSE_FAKE_402_MATCH", "") in prompt:
        raise fc_models.PaymentRequired(fake, "Payment required to access this resource (simulated)")
    prompt = fc_core.scrub(prompt)
    if fc_harness.harness_of(model):
        p = fc_proc.run(fc_harness.once_command(model, cwd, prompt), cwd=cwd, env=fc_claude.headless_env(), timeout=timeout or 900)
        return fc_harness.once_result(model, p)
    if fc_claude.is_claude(model):
        return fc_claude.claude_once(cwd, prompt, model=fc_claude.claude_id(model))
    if model.startswith("pi:"):
        if fc_herdr.herdr_env() and task and task.get("space"):
            return herdr_pi(cwd, model[3:], prompt, session, task, timeout)
        return fc_models.pi_turn(cwd, model[3:], prompt, session, timeout)
    return fc_models.opencode(cwd, model, prompt, session)

def herdr_pi(cwd, model, prompt, session, task, timeout=None):
    """One turn of an interactive pi running as a named herdr agent in its own pane of the space's `implementers` tab.
    Same contract as pi_turn(): (session, reply_text, tool_calls, usage). The session dict names the agent, its pane and tab,
    and the pi session file the turn's text and usage are read from. The team lead steers the same agent with
    `herdr agent prompt <name> ...` between the CLI's own prompts."""
    timeout = timeout or fc_models.TURN_TIMEOUT
    meta, proj = task["meta"], task["proj"]
    tid, name = meta["id"], fc_herdr.agent_name(proj.name, meta["id"])
    title = f"{tid} {meta.get('title', '')}".strip()[:60]
    if session is None:
        old = fc_herdr.herdr("agent", "get", name)
        if old:  # a previous run's agent for this task: its pane goes, the name is reused
            fc_herdr.herdr("pane", "close", (old.get("agent") or old).get("pane_id"))
        guard = fc_readguard.guard_file(cwd)
        env = {**({"OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"]} if "OPENROUTER_API_KEY" in os.environ else {}),
               "GRAFT_NO_GITIGNORE": "1", "GRAFT_NO_IGNORE": "1", "COMPOSE_CONFIG": fc_core.scratch_config(),
               "PI_CODING_AGENT_DIR": fc_models.pi_agent_dir(cwd), **fc_providers.pi_env(model)}
        if guard.exists():
            env["COMPOSE_READGUARD"] = fc_readguard.command(guard)
        pane = fc_herdr.herdr_implementer_pane(task["space"], cwd, env, fc_herdr.tab_label(proj, fc_herdr.IMPLEMENTERS_TAB))
        fc_herdr.herdr("pane", "rename", pane, title)
        sdir = Path(cwd).parent / (Path(cwd).name + ".pi-sessions")
        sdir.mkdir(exist_ok=True)
        args = ["--tui-mode", "regular", "--no-extensions", *(["-e", str(fc_herdr.PI_HERDR_EXT)] if fc_herdr.PI_HERDR_EXT.exists() else []),
                *(["-e", str(fc_readguard.PI_EXT)] if guard.exists() else []), *fc_providers.pi_args(model),
                "--no-skills", "--no-prompt-templates", "--thinking", "low", "--session-id", str(uuid.uuid4()), "--session-dir", str(sdir), "--model", model]
        started = fc_herdr.herdr_agent_start(name, "--kind", "pi", "--pane", pane, "--timeout", "60000", "--", *args)
        pids = fc_herdr.agent_pids(pane)
        if pids:
            fc_started.record(min(pids), "implementer", str(proj), waiter=os.getpid(), cwd=str(cwd))
        sfile = ((started.get("agent") or {}).get("agent_session") or {}).get("value")
        session = {"name": name, "pane": pane, "sfile": sfile, "seen": 0}
        fc_herdr.herdr("pane", "report-metadata", pane, "--source", "compose", "--title", title, "--token", f"model={model.split('/')[-1].split('@')[0]}")
    fc_proc.HOOKS[name] = lambda: (fc_herdr.herdr("agent", "send-keys", session["name"], "esc"), fc_herdr.herdr("pane", "close", session["pane"]))
    t0 = time.time()
    for attempt in range(2):
        result, err = fc_herdr.herdr_error("agent", "prompt", session["name"], prompt, "--wait", "--timeout", str(timeout * 1000), timeout=timeout + 30)
        if err == "agent_blocked" and attempt == 0:  # pi is showing a dialog from the last turn; dismiss it and resend
            fc_herdr.herdr("agent", "send-keys", session["name"], "esc")
            time.sleep(1)
            continue
        break
    if fc_proc.task_paused(meta["id"]):  # the pause hook's esc and pane close ended the wait, with an error or without
        raise fc_proc.Paused
    def read_turn():  # what pi wrote to its session file since the last turn, priced and added to the ledger
        if not session.get("sfile"):
            session["sfile"] = (((fc_herdr.herdr("agent", "get", session["name"]) or {}).get("agent") or {}).get("agent_session") or {}).get("value")
        session["seen"], text, tools, usage, errors = fc_models.parse_pi_session(session["sfile"] or "", session["seen"])
        usage["cost"] = fc_providers.cost(model, usage)
        fc_core.LEDGER.add("openrouter" if "openrouter" in model else model.split("/")[0], usage["tokens_in"], usage["tokens_out"], usage["cost"])
        return text, tools, usage, errors
    if err == "agent_prompt_stalled":  # herdr saw no work within its 5 s, which does not mean pi dropped the prompt
        err = fc_herdr.herdr_stalled(session, prompt, t0 + timeout)
    if err == "timeout" or err == "agent_prompt_stalled":
        fc_herdr.herdr("agent", "send-keys", session["name"], "esc")
        fc_herdr.herdr("pane", "close", session["pane"])
        timed_out = subprocess.TimeoutExpired("herdr agent prompt", timeout)
        _, timed_out.tools, timed_out.usage, _ = read_turn()  # the tokens a cut turn spent were billed all the same
        raise timed_out
    if err:
        raise RuntimeError(f"herdr agent prompt {session['name']}: {err}")
    text, tools, usage, errors = read_turn()
    busy = fc_models.provider_busy(errors, not text and not tools and not usage["tokens_in"])
    if not text and not tools and not usage["tokens_in"] and not busy:  # a 429 or a 503 refuses before any token, and passes
        raise RuntimeError(f"provider refused the request: {'; '.join(errors) or 'empty turn with no usage'}")
    if busy:
        raise fc_models.RateLimited(usage, busy)
    return session, text, tools, usage

def task_base(repo, proj, meta):
    """The branch a task builds on. A task whose `after:` names unmerged task branches stacks on top of them (they must form one
    chain, so each PR shows only its own layer); otherwise it builds on HEAD like any independent task. An already_green dependency
    has no layer: its code is its own `after:` tasks' code, so those stand in for it."""
    after_of = lambda m: [x.strip() for x in str(m.get("after") or "").split(",") if x.strip()]
    after, seen = after_of(meta), set()
    head = fc_core.sh(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    unmerged = []
    while after:
        dep = after.pop(0)
        if dep in seen:
            continue
        seen.add(dep)
        b = fc_worktree.task_branch(repo, proj, dep)
        if fc_core.sh(["git", "rev-parse", "--verify", "-q", b], repo, check=False).returncode != 0:
            res = proj / "results" / f"{dep}.json"
            if res.exists() and json.loads(res.read_text()).get("status") == "already_green":
                after += [d for m in fc_tasks.load_tasks(proj) if m["id"] == dep for d in after_of(m)]
            continue  # no branch: the dependency was merged (or never ran, which `run` refuses earlier)
        if fc_core.sh(["git", "merge-base", "--is-ancestor", b, head], repo, check=False).returncode != 0:
            unmerged.append(b)
    if not unmerged:
        return "HEAD", head
    # The project is one stack: linearize through the last dependency only; a layer above may sit on one being rebuilt now
    trunk = fc_core.current_branch(repo)
    with fc_worktree.STACK_LOCK:  # read the base before another task's linearize rewrites it
        chain = fc_layers.linearize(repo, proj, trunk, fc_worktree.stack_worktree(repo, proj, trunk), through=unmerged)
        missing = [b for b in unmerged if b not in chain]
        if missing:
            raise RuntimeError(f"after: {', '.join(missing)} could not be chained: {fc_layers.STACK_STOP or 'rebase conflict'}")
        # Base on the highest dependency, not the chain top: layers ordered after this task must stay above it in history.
        base = max(unmerged, key=chain.index)
        return base, fc_core.sh(["git", "rev-parse", base], repo).stdout.strip()

READER_TIMEOUT = 120  # seconds the test reader may take before the raw tail goes back alone

def read_failure(wt, meta, output, log_path):
    """(brief, usage): the test reader's short fix brief for a failed test run, for the implementer's next attempt. The reader
    (models.test_reader, Claude Haiku by default) runs headless in the task's clone with read-only tools, so it can point at the
    file:line but never edit or run anything. ("", None) when no Claude reader is set, or it fails or times out: the raw tail
    always follows, so this only ever adds."""
    if not fc_config.TEST_READER or not (fc_claude.is_claude(fc_config.TEST_READER) or fc_harness.harness_of(fc_config.TEST_READER)):
        return "", None
    prompt = (f"A test run failed in this repository.\nTest command: {meta['test']}\nFull output: {log_path}\nOutput (tail):\n{output[-6000:]}\n\n"
              f"Files the fix may change: {', '.join(meta['files']) or 'any'}. Open the test file and those files with Read when the output alone does not "
              "show where the problem is; you cannot edit files or run commands. "
              "Reply with at most 8 short lines and nothing else: each failing case, what it expected versus what it got, and the file:line "
              "to look at. No advice beyond that, no code.")
    try:
        if fc_harness.harness_of(fc_config.TEST_READER):
            p = fc_proc.run(fc_harness.once_command(fc_config.TEST_READER, wt, prompt, read_only=True), cwd=wt, env=fc_claude.headless_env(), timeout=READER_TIMEOUT)
            _, reply, _, usage = fc_harness.once_result(fc_config.TEST_READER, p)
        else:
            _, reply, _, usage = fc_claude.claude_once(wt, prompt, timeout=READER_TIMEOUT, model=fc_claude.claude_id(fc_config.TEST_READER), tools="Read,Grep,Glob")
    except Exception:  # noqa: BLE001 - a timeout or a refused call leaves the raw tail
        return "", None
    return reply.strip()[:1500], usage

HANG_HINT = ("The test did not finish within {limit}s, so it hangs: most likely a deadlock (for example a query run while "
             "another query's rows are still open on a single connection) or something waiting forever. Fix the hang.")

def timed(cmd, cwd, limit, env=None, stdin=subprocess.DEVNULL, check=True):
    """Run a shell command with a hard time limit, whatever the language or test runner. On a hang the whole process
    group goes (a test binary must not outlive its shell) and the result is exit 124 with a deadlock hint. The output
    comes back scrubbed of secrets: it goes on to models and results/. A change to the user's ~/.compose meanwhile is flagged.
    The command runs under one of the machine's slots (fc_proc.slot; only a whole suite takes a suite slot, so any other command never waits on one)
    and its group is in fc_proc.RUNNING, so a stop ends it. A `check` (every caller but `compose timed`) gets COMPOSE_CHECK=1,
    which lets compose's own `python3 -m tests.run` run the whole suite with no files named."""
    quick = not fc_proc.suite(cmd)
    if not quick and "COMPOSE_TEST_SLOT" not in os.environ:  # a suite waits at the load gate before it takes its slot
        fc_load.wait(fc_core.say)
    with fc_proc.slot(fc_core.say, quick) as held:
        env = {**(os.environ if env is None else env), "COMPOSE_TEST_SLOT": held} | ({"COMPOSE_CHECK": "1"} if check else {})
        before = fc_core.home_state()
        p = subprocess.Popen(cmd, cwd=cwd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             stdin=stdin, start_new_session=True, env=env)
        fc_proc.RUNNING.add(p.pid)
        try:
            fc_started.record(p.pid, "timed", None, waiter=os.getpid(), until=time.time() + limit, cwd=str(cwd))
            out, err = p.communicate(timeout=limit)
            return subprocess.CompletedProcess(cmd, p.returncode, fc_core.scrub(out), fc_core.scrub(err))
        except subprocess.TimeoutExpired:
            fc_proc.kill_group(p.pid, p)
            out, err = (fc_core.scrub(x or "") for x in p.communicate())
            return subprocess.CompletedProcess(cmd, 124, out[-2000:], (err[-1000:] + "\n" + HANG_HINT.format(limit=limit)).strip())
        finally:
            fc_proc.RUNNING.discard(p.pid)
            fc_started.forget(p.pid)
            fc_core.flag_home(before, f"`{cmd}`", sid=p.pid)  # the command runs in a session of its own

def run_test(meta, wt):
    """A task's test command under the time limit (a task's block can set `test_timeout:`)."""
    return timed(meta["test"], wt, int(meta.get("test_timeout") or fc_models.TEST_TIMEOUT), env=fc_core.clean_env())

def cmd_timed(argv):
    """`compose timed <seconds> -- <command>`: what teammates run tests and commands through, so a hang fails fast.
    One argument is a shell line; several are the command's own arguments, quoted so sh passes each through unchanged.
    The command gets the caller's env as it is, keys included: no config is applied here, so every key in it is the caller's
    own, not one the CLI exported. Only COMPOSE_CONFIG is a scratch one, and the output is still scrubbed.
    The caller's stdin goes on to the command (`< file` reaches it), unless it is a terminal: a test must not wait on the keyboard."""
    if len(argv) < 3 or argv[1] != "--":
        sys.exit("usage: compose timed <seconds> -- <command>")
    cmd = argv[2] if len(argv) == 3 else shlex.join(argv[2:])
    r = timed(cmd, os.getcwd(), int(argv[0]), env={**os.environ, "COMPOSE_CONFIG": fc_core.scratch_config()},
              stdin=subprocess.DEVNULL if sys.stdin is None or sys.stdin.isatty() else sys.stdin, check=False)
    sys.stdout.write(r.stdout); sys.stderr.write(r.stderr if r.stderr.endswith("\n") or not r.stderr else r.stderr + "\n")
    sys.exit(r.returncode)

def task_checks(cfg):
    """The checks `run` makes after each task: verify.json `task_checks` when it has them, else `checks`, which verify and the stack's
    layers run on the whole change."""
    return (cfg["task_checks"] if "task_checks" in cfg else cfg.get("checks")) or []

def run_task(repo, proj, task, attempts, model=None, reset_files=False, tag=""):
    meta = dict(task)  # the fallback switches meta["model"]; the shared task list stays as synced
    body = meta.pop("body")
    delete = meta.get("delete") or []
    if model:
        meta["model"] = model
    tid, branch = meta["id"], fc_worktree.task_branch(repo, proj, meta["id"]) + tag
    wt = proj / "worktrees" / (tid + tag)
    # A separate clone, not a git worktree: opencode links worktrees to the main checkout as "project copies" and
    # resolves edits of existing files there. A clone has its own .git, so nothing can leak. Shared objects keep it cheap.
    fc_pause.clear_clone(proj, tid, wt)  # a clone a failed pause snapshot kept is moved aside, never deleted
    fc_core.sh(["git", "worktree", "prune"], repo, check=False)
    why = not tag and fc_tasks.task_finished(proj, meta, redo=bool(os.environ.get("COMPOSE_REDO")))
    if why:
        # A passed layer may already have a PR, and a hand-done one carries a teammate's commit. Never replace either silently;
        # `--redo` says the user wants it rebuilt.
        fc_core.say(f"{tid} keep    {why}; pass --redo to rebuild it")
        return json.loads((proj / "results" / f"{tid}.json").read_text())
    fc_worktree.replace_branch(repo, proj, branch)  # a re-run (or --redo) replaces the previous attempt
    fc_core.sh(["git", "clone", "--quiet", "--shared", "--no-hardlinks", str(repo), str(wt)], repo)
    base_name, base = task_base(repo, proj, meta)
    fc_core.sh(["git", "checkout", "-q", "-b", branch, base], wt)  # a stacked task starts from its lower layer's tip
    fc_worktree.ready_checkout(repo, wt, borrow=not fc_worktree.names_lockfile(meta), install=fc_core.project_config(proj).get("install"))
    for name in ("opencode.jsonc", "opencode.json"):  # untracked local config the implementer needs
        if (repo / name).exists() and not (wt / name).exists():
            shutil.copy(repo / name, wt / name)
    if reset_files:
        # Benchmark mode: remove the implementation so the frozen test is red again and the model starts from scratch.
        for f in meta["files"]:
            if (wt / f).exists():
                (wt / f).unlink()
        fc_core.sh(["git", "commit", "-qam", fc_core.typed("reset implementation", "deps", fc_core.commit_convention(proj)) if fc_core.commit_convention(proj) else "bench: reset implementation"], wt, check=False)
    before = fc_core.digests(wt, meta["protected"])
    # Red first: a test that already passes on HEAD means the task is done or the test is vacuous. Do not spend an implementer on it.
    fast = not (tag or reset_files) and fc_fast.workflow(proj, meta) == "fix-then-regression-test"  # a bench run is never routed: it leaves the real result and Linear alone
    red = run_test(meta, wt)
    if fast and red.returncode:
        return fc_fast.broken_base(proj, meta, red, base_name, branch, wt)
    if red.returncode == 0 and not fast and not delete:  # a deleting task has its change to make whatever the test says
        fc_core.say(f"{tid} skip    test already passes on {base_name}; fix the test or drop the task")
        result = {"id": tid, "title": meta.get("title"), "status": "already_green", "model": meta["model"], "attempts": 0,
                  "seconds": 0.0, "cost": 0.0, "branch": branch, "worktree": str(wt), "diffstat": "", "log": []}
        (proj / "results").mkdir(exist_ok=True)
        fc_core.atomic_write((proj / "results" / f"{tid}{tag}.json"), json.dumps(result, indent=2))
        return result
    if delete:  # the task's own deletions, applied in the clone before any turn
        fc_core.sh(["git", "rm", "-r", "-q", "--"] + delete, wt)
    res = proj / "results" / f"{tid}{tag}.json"  # a bench run keeps its own file: the real task's result (and its paused record) stays
    last = res.read_text() if res.exists() else None  # the pre-run result, before progress("running") replaces it
    kept = [] if tag or reset_files else fc_pause.carry_over(repo, wt, meta, json.loads(last) if last else {})
    checks, limit = task_checks(fc_core.project_config(proj)), int(meta.get("test_timeout") or fc_models.TEST_TIMEOUT)
    def checked(where):  # the project's checks run a long time: each one says what it runs, and where, before it starts
        def run(cmd, tree, secs, env=None):
            sha = fc_green.tree_of(tree)
            if line := fc_green.taken(proj, sha, cmd):  # green on these very files already: not run twice
                fc_core.say(f"{tid} checks  {line}")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            fc_core.say(f"{tid} checks  `{cmd}` {where}, up to {secs}s")
            r = timed(cmd, tree, secs, env=env)
            fc_green.record(proj, sha, cmd, r.returncode)
            if r.returncode == 124:
                fc_core.say(f"{tid} checks  `{cmd}` {where} timed out after {secs}s")
            return r
        return run
    learned = {}  # base-tree runs of the checks whose ids settle needed, shared by this task's attempts
    goal = re.search(r"## Goal\n(.*?)(?:\n## |\Z)", body, re.S)
    words = "\n".join([goal.group(1) if goal else body] + [(wt / f).read_text() for f in meta["protected"] if (wt / f).exists()])
    section = fc_libapi.section(wt, meta["files"], words)
    pack = fc_trailpack.trail_pack(wt, f"{meta.get('title', '')}: {(goal.group(1) if goal else body)[:400]}", repo=repo, cap=12000 - len(section))
    fc_readguard.write_guard(wt, meta["files"] + meta["protected"] + (nodes := fc_figma.code_nodes(proj, wt, meta)), pack)
    context = (f"CONTEXT PACK from the repo's trail graph (exact file:line, with source). Use it instead of exploring. "
               f"If you need more, run `trail ask \"<question>\"` or `trail callers <symbol>`; do not open unrelated files.\n{pack}\n\n"
               if pack else "")
    if section:
        context += section + "\n\n"
    root = str(wt.resolve())
    abs_files = ", ".join(str(wt.resolve() / f) for f in meta["files"]) or "files under the root as needed"
    abs_protected = ", ".join(str(wt.resolve() / f) for f in meta["protected"]) or "none"
    prompt = (fc_pause.carry_note(kept) + f"You are implementing one small task in this repository.\n\n"
              f"REPOSITORY ROOT FOR THIS TASK: {root}\n"
              f"Every file you read, create, or edit must be under that root. Project docs may mention other absolute paths "
              f"(for example the original checkout this task was cloned from); those are read-only references, never edit them.\n\n"
              f"{context}{body}\n\n"
              f"Files you may edit: {abs_files}.\n"
              f"Files you must NOT edit: {abs_protected}.\n" + (f"Figma nodes this task builds, as Framelink JSON (read, never edit): {', '.join(nodes)}.\n" if nodes else "") +
              f"Done means this command exits 0: {meta['test']}\n"
              "Do not run it, or any other test or build: compose runs the test after your turn ends and sends you what failed. "
              "Read the test file, edit, and end your turn.\n"
              "The test contains cases beyond the examples listed here, so implement the described behavior, not the examples. "
              "Read only the test file and the files named above; do not explore other docs or directories. "
              "When the test pins a line width, measure with `wc -L <file>` (longest line) or `awk '{print length}' <file>` instead of counting. "
              "Make the smallest, simplest change that passes: no defensive checks, flags, "
              "hardening, helpers, or comments the task does not ask for. Do not explain, just edit.\n"
              f"Hard limits: the whole change must stay under {fc_config.MAX_DIFF_LINES} changed lines (added plus deleted), you must not "
              "create any document file (.md, .txt, .rst, docs/), and you may change ONLY the files listed above, even if other "
              "tests in the repo fail: other tasks own them. A change that breaks any of these is rejected.\n"
              "If a fact this brief states is wrong (a function, file, or API it names is missing or behaves differently), or a fact you "
              "need is not in it (which file, which signature, what a value looks like), do not work around it or guess: end your reply "
              "with one line `CONTRADICTION: <what the brief says> / <what the code shows, file:line>` (`brief lacks <fact>` before the slash "
              "for a missing one) and stop.")
    log, session, started, status, cost, reference_cost = [], None, time.time(), "failed", 0.0, 0.0
    original_prompt = prompt
    # The attempts spent: each numbered turn once (a turn and the test of the edits it left are one), never a provider's refusal.
    spent = lambda: len({e["attempt"] for e in log if "attempt" in e and not e.get("rate_limited") and "payment_required" not in e and "slept" not in e})
    def progress(state):  # a parked `watch` and the status board read this while the task runs
        (proj / "results").mkdir(exist_ok=True)
        fc_core.atomic_write(res, json.dumps({"id": tid, "title": meta.get("title"), "status": state, "model": meta["model"],
            "attempts": spent(), "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)), "seconds": round(time.time() - started, 1),
            "cost": round(cost, 6), "branch": branch, "base": base_name, "worktree": str(wt), "log": log, **fc_pause.carried(last)}, indent=2))
    progress("running")
    # A hold placed while the run cloned, based, and installed lands here, before any turn. `running` is on disk before the
    # holds are read, and `hold` writes the hold before it reads `running`: either this sees the hold, or `hold` says it runs.
    hold = None if tag else fc_tasks.held(proj).get(tid)
    if hold:
        if last is None:
            res.unlink(missing_ok=True)
        else:
            fc_core.atomic_write(res, last)  # the task never started: its last run's result stands
        shutil.rmtree(wt, ignore_errors=True)
        fc_core.say(f"{tid} held    before its first turn: {hold.get('reason', '')}")
        return {"id": tid, "title": meta.get("title"), "status": "held", "attempts": 0, "seconds": 0.0, "cost": 0.0, "branch": "-", "log": []}
    fc_core.say(f"{tid} start   {meta.get('title', '')}  model={meta['model']}")
    if not reset_files and not tag:  # bench runs leave Linear alone
        fc_linear.linear_event(proj, meta, "dispatched")
    rate_waits = 0
    attempt = 0
    fails = 0  # failed attempts on the current model
    fall = lambda: None if reset_files else fc_models.fallback_for(meta["model"])  # bench measures the named model alone
    def switch(why, next_prompt):  # hand the task to the fallback model in a fresh session (a session is bound to its model)
        nonlocal session, prompt, fails
        model = fall()
        fc_core.say(f"{tid} fallback {meta['model']} {why}; switching to {model}")
        log[-1]["fallback"] = {"from": meta["model"], "to": model, "why": why}  # on the attempt that triggered it
        meta["model"], session, prompt, fails = model, None, next_prompt, 0
    def failed(why, next_prompt):  # one more failed attempt: the fallback takes over after models.fallback_after of them
        nonlocal fails
        fails += 1
        if fall() and fails >= fc_config.FALLBACK_AFTER:
            switch(why, next_prompt)
            return True
        return False
    def edited():  # a turn cut short may leave edits: they are tested before the next session reads the brief against them
        return bool(meta["files"]) and bool(fc_core.sh(["git", "status", "--porcelain", "--", *meta["files"]], wt, check=False).stdout.strip())
    only_delete = bool(delete) and not meta["files"]  # the deletion is the whole task: no model turn, one test run decides
    while attempt < attempts:
        if fc_proc.STOP.is_set():
            raise fc_proc.Stopped
        if fc_proc.task_paused(tid):
            status = "paused"
            break
        attempt += 1
        t0, clk = time.time(), fc_proc.clock()
        ref_before = fc_readguard.refused(wt)
        cut = None  # the number of a turn cut short whose edits the test and verdict below judge
        try:
            if only_delete:
                session, reply, tools, usage = None, "", 0, {"cost": 0.0}
            else:
                fc_proc.TASK.id = tid  # a hold pauses the turn's commands, not the checks and tests below
                try:
                    session, reply, tools, usage = implementer(wt, meta["model"], prompt, session, task={"proj": proj, "meta": meta, "space": fc_herdr.HERDR_SPACES.get(proj.name)},
                                                               timeout=fc_models.TURN_TIMEOUT)
                finally:
                    fc_proc.TASK.id = None
                if fc_proc.task_paused(tid):  # a harness that swallowed the cut: the turn's edits are kept, never tested or committed
                    raise fc_proc.Paused
        except fc_proc.Paused:
            status = "paused"
            break
        except fc_models.RateLimited as e:
            cost += e.usage["cost"]
            reference_cost += e.usage.get("reference_cost") or 0
            if fall():
                log.append({"attempt": attempt, "rate_limited": True, "seconds": round(time.time() - t0, 1), "usage": e.usage})
                switch(e.why, original_prompt)
                attempt -= 1
            else:
                rate_waits += 1
                if rate_waits > 4:
                    log.append({"attempt": attempt, "rate_limited": True, "error": f"{e.why}; giving up after 4 waits"})
                    break
                wait = 20 * rate_waits
                fc_core.say(f"{tid} {e.why}; fresh session in {wait}s (wait {rate_waits}/4)")
                log.append({"attempt": attempt, "rate_limited": True, "seconds": round(time.time() - t0, 1), "usage": e.usage})
                session = None  # the compacted session is dead; restart from the original prompt
                prompt = original_prompt
                attempt -= 1
                time.sleep(wait)
            if not edited():
                continue
            cut = attempt + 1
        except subprocess.TimeoutExpired as e:
            if fc_proc.STOP.is_set():
                raise fc_proc.Stopped
            # A hung implementer (tool call never returning) costs one attempt, not the task: fresh session, same prompt.
            # A turn the machine slept in is no hang: it costs no attempt and no step toward the fallback (the awake clock timed it out).
            nap = fc_proc.slept(clk)
            fc_core.say(f"{tid} attempt {attempt} timeout  implementer turn did not finish within {fc_core.fmt(time.time() - t0)}"
                        + (f", and {fc_proc.slept_text(nap)}: it does not count" if nap else "") + "; fresh session")
            cut_usage = getattr(e, "usage", {"cost": 0.0, "tokens_in": 0, "tokens_out": 0})  # what the cut turn cost is counted here, as for a rate limit
            cost += cut_usage["cost"]
            reference_cost += cut_usage.get("reference_cost") or 0
            log.append({"attempt": attempt, "seconds": round(time.time() - t0, 1), "error": "implementer timed out", "tool_calls": getattr(e, "tools", 0), "usage": cut_usage,
                        **({"slept": round(nap)} if nap else {})})
            session, prompt, cut = None, original_prompt, attempt
            if nap:
                attempt -= 1
            else:
                failed("timed out", original_prompt)
            if not edited():
                continue
        except fc_models.PaymentRequired as e:
            fc_core.say(f"{tid} HALT    payment required at {e.provider}: {e.message[:120]}")
            log.append({"attempt": attempt, "payment_required": {"provider": e.provider, "message": e.message}})
            status = "payment_required"
            break
        except Exception as e:  # noqa: BLE001
            if fc_proc.STOP.is_set():
                raise fc_proc.Stopped
            fc_core.say(f"{tid} error   {str(e)[-300:]}")
            log.append({"attempt": attempt, "error": str(e)[-2000:]})
            if fall():  # a fresh session on the same model until the fallback takes over
                if not failed("errored", original_prompt):
                    session, prompt = None, original_prompt
                if not edited():
                    continue
                cut = attempt
            else:
                break
        if cut:
            reply, tools, usage = "", 0, {"cost": 0.0, "tokens_in": 0, "tokens_out": 0}
        n = cut or attempt
        cost += usage["cost"]
        reference_cost += usage.get("reference_cost") or 0
        found = None if cut else contradiction(reply)
        if found:  # the brief states something the code disproves: more attempts would only work around it
            log.append({"attempt": attempt, "seconds": round(time.time() - t0, 1), "tool_calls": tools, "usage": usage,
                        "contradiction": found, "reply": reply[-1500:]})
            fc_core.say(f"{tid} attempt {attempt} contradiction  {found[:160]}")
            status = "contradiction"
            break
        sha = fc_green.tree_of(wt)
        if line := fc_green.taken(proj, sha, meta["test"]):  # the task's own test, green on these very files already
            fc_core.say(f"{tid} test    {line}")
            test = subprocess.CompletedProcess(meta["test"], 0, "", "")
        else:
            test = run_test(meta, wt)
            fc_green.record(proj, sha, meta["test"], test.returncode)
        fc_core.sh(["git", "add", "-A"], wt, check=False)
        changed = fc_core.sh(["git", "diff", "--cached", "--name-only", base], wt, check=False).stdout.split()
        cmds = fc_gate.after_checks(checks, fc_tasks.load_tasks(proj), changed, meta["test"])
        now = fc_gate.check_state(checked("after the task"), cmds, wt, limit) if cmds and test.returncode == 0 else {}  # the task's own test is not the only one that must hold
        # What passed on the base is asked only for a check that fails after the task: a suite that passes needs no base run.
        ahead = fc_gate.baseline_state(checked("on its base"), cmds, wt, (base, tuple(delete)), limit) if any(c["exit"] for c in now.values()) else {}
        broke, red_on_base = fc_gate.regressions(ahead, now, fc_gate.touches(wt, base, changed)) if now else ([], [])
        if red_on_base:  # unknown is never base-red: the base tree says which of these it failed too
            more, cannot = fc_gate.settle(checked("on its base"), wt, base, tuple(delete), limit, red_on_base, now, learned)
            broke, red_on_base = broke + more, [t for t in red_on_base if t not in more]
        else:
            cannot = []
        tampered = [f for f, d in fc_core.digests(wt, meta["protected"]).items() if d != before[f]]
        lines, new_docs, stray = fc_gate.branch_budget(wt, base, allowed=meta["files"] if meta["files"] or delete else None, delete=delete)
        entry = {"attempt": n, **({"cut_short": True} if cut else {}), "seconds": round(time.time() - t0, 1), "tool_calls": tools, "usage": usage,
                 "test_exit": test.returncode, "tampered": tampered, **({"broke": broke} if broke else {}), **({"base unknown": cannot} if cannot else {}), **({"red on base (baseline unknown)": red_on_base} if red_on_base else {}), "lines": lines, "new_docs": new_docs, "stray": stray,
                 "refused": fc_readguard.refused(wt) - ref_before,
                 "reply": reply[-1500:], "test_output": (test.stdout + test.stderr)[-3000:]}
        log.append(entry)
        progress("running")
        if isinstance(session, dict):
            fc_herdr.herdr_label(session, f"attempt {attempt}/{attempts}", cost)
        over = lines > fc_config.MAX_DIFF_LINES
        verdict = "pass" if test.returncode == 0 and not over and not new_docs and not stray and not broke and not cannot else "fail"
        output, reader = test.stdout + test.stderr, None
        if test.returncode != 0 and not tampered:  # the reader's brief, written before the line so its cost is on it
            (proj / "results").mkdir(parents=True, exist_ok=True)
            log_path = proj / "results" / f"{tid}.test.log"
            log_path.write_text(output)
            entry["summary"], reader = read_failure(wt, meta, output, log_path)
            if reader:
                entry["reader"] = {"model": fc_config.TEST_READER, "usage": reader}
                cost += reader["cost"]
                reference_cost += reader.get("reference_cost") or 0
                progress("running")
        fc_core.say(f"{tid} attempt {n} {verdict}  {fc_core.fmt(entry['seconds'])}  tools={tools}  lines={lines}  refused={entry['refused']}  ${usage['cost']:.4f}"
            + (f"  reader ${reader['cost']:.4f}" + (f" (list ${reader['reference_cost']:.4f})" if reader.get("reference_cost") else "") if reader else "")
            + (f"  OVER {fc_config.MAX_DIFF_LINES}" if over else "") + (f"  new docs: {', '.join(new_docs)}" if new_docs else "")
            + (f"  outside files: {', '.join(stray)}" if stray else "") + (f"  broke: {', '.join(broke)[:160]}" if broke else "")
            + (f"  cannot tell if red on base: {'; '.join(cannot)[:300]}" if cannot else ""))
        if tampered:
            status = "rejected_tampered"
            break
        if cannot:  # another attempt cannot change what the base run did
            status = "rejected_regression"
            break
        if verdict == "pass":
            status = "passed"
            break
        if only_delete:  # no turn can fix anything: the one attempt decides
            break
        problems = []
        if test.returncode != 0:
            summary = entry["summary"]
            problems.append(f"The test still fails.\nCommand: {meta['test']}\nExit code: {test.returncode}\n"
                            + (f"What failed:\n{summary}\n" if summary else "") + f"Output (tail):\n{output[-1000:]}"
                            + f"\nFull output: {log_path.resolve()}; read it before you change anything.")
        if over:
            problems.append(f"Your change is {lines} lines (added plus deleted); the hard limit is {fc_config.MAX_DIFF_LINES}. Shrink it: revert "
                            "unrelated edits, drop new files that are not named in the task, remove long comments and duplication.")
        if new_docs:
            problems.append("Delete these document files; implementers never add documents: " + ", ".join(new_docs))
        if stray:
            problems.append("Revert every change to these files, they are outside this task (another task owns them; the test for "
                            "this task must pass with only the named files changed): " + ", ".join(stray)
                            + f"\nFiles you may change: {', '.join(meta['files'])}")
        if broke:
            problems.append(fc_gate.advice(broke))
        status = ("rejected_size" if over else "rejected_docs" if new_docs else "rejected_scope" if stray else "rejected_regression" if broke else status) if test.returncode == 0 else status
        prompt = "\n\n".join(problems) + "\nFix it without editing protected files."
        fresh = original_prompt + "\n\nA previous attempt left edits in the files and did not pass:\n\n" + prompt
        if cut:  # the cut-short turn already counted; its session is gone, so the next one starts from the task and what it left
            prompt = fresh
        elif not failed("missed", fresh) and session is None:  # nothing to resume (Claude Code turns are one-shot): start from the task too
            prompt = fresh
    # Isolation guard: an implementer must never touch the main checkout. If it did (seen with opencode despite --dir),
    # revert those files and record it; the task's own result lives in its worktree branch.
    # Only this task's own files are reverted: the team lead may be editing other files in the main checkout meanwhile.
    dirty = fc_worktree.dirty_paths(repo)
    mine = [f for f in dirty if f in meta["files"] or f in meta["protected"]]
    others = [f for f in dirty if f not in mine]
    if mine:
        fc_core.sh(["git", "checkout", "--"] + [f for f in mine if (repo / f).exists() and fc_core.sh(["git", "ls-files", "--error-unmatch", f], repo, check=False).returncode == 0], repo, check=False)
        for f in mine:
            if fc_core.sh(["git", "ls-files", "--error-unmatch", f], repo, check=False).returncode != 0 and (repo / f).exists():
                (repo / f).unlink()
        fc_core.say(f"{tid} WARNING implementer wrote into the main checkout; reverted its task files: {', '.join(mine)}")
        log.append({"leak_reverted": mine})
    if others:
        fc_core.say(f"{tid} note: main checkout has other uncommitted changes (left alone): {', '.join(others)[:160]}")
    if fc_pause.ends_paused(status, tid):  # a hold during the last test or checks that did not pass pauses too
        return fc_pause.paused_result(repo, proj, meta, wt, base, base_name, log, spent(), cost, started, fc_tasks.held(proj).get(tid, {}).get("reason", ""))
    if status == "passed":
        fc_core.sh(["git", "add", "-A"], wt)
        changed = [f for f in fc_core.sh(["git", "diff", "--cached", "--name-only", base], wt, check=False).stdout.split() if f in meta["files"]]
        kept = {f: (wt / f).read_bytes() for f in changed if (wt / f).is_file()}
        formatted = fc_format.format_files(wt, list(kept), base)
        if formatted and run_test(meta, wt).returncode != 0:
            for f, b in kept.items():
                (wt / f).write_bytes(b)
            fc_core.say(f"{tid} note    formatting broke the test; committed unformatted: {', '.join(formatted)}")
        elif formatted:
            fc_core.say(f"{tid} format  {', '.join(formatted)}")
        fc_core.sh(["git", "add", "-A"], wt)
        fc_core.sh(["git", "commit", "-qm", fc_layers.commit_message(meta, fc_core.commit_convention(proj))], wt, check=False)
        fc_core.sh(["git", "fetch", "-q", str(wt), f"{branch}:{branch}"], repo)  # the task branch now exists in the main repo: a layer for review, never merged here
        if fc_worktree.names_lockfile(meta):
            fc_worktree.adopt_installs(repo, wt)
    if fast and status == "passed":
        status = "unproven"
    diff = fc_core.sh(["git", "diff", base, "--stat"], wt, check=False).stdout
    total = round(time.time() - started, 1)
    fc_core.say(f"{tid} {status:10} total {fc_core.fmt(total)}  attempts={spent()}  ${cost:.4f}")
    if isinstance(session, dict):
        fc_herdr.herdr_label(session, status, cost, state="idle")
    if status in ("passed", "unproven"):  # its branch is the record; a missed task's pane stays for the team lead to read and steer
        if isinstance(session, dict):
            fc_herdr.herdr("pane", "close", session["pane"])
        else:  # a fallback turn took over: the pi pane of the earlier attempts is still open
            fc_herdr.herdr_close_task(proj, tid)
    result = {"id": tid, "title": meta.get("title"), "status": status, "model": meta["model"], "attempts": spent(),
              "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)), "seconds": total, "cost": round(cost, 6),
              "reference_cost": round(reference_cost, 6), "branch": branch, "base": base_name, "worktree": str(wt), "diffstat": diff, "log": log}
    (proj / "results").mkdir(exist_ok=True)
    fc_core.atomic_write(res, json.dumps(result, indent=2))
    if not reset_files and not tag:
        fc_linear.linear_event(proj, meta, status if status in ("passed", "unproven") else "failed", result)
    return result

CONTRADICTION_RE = re.compile(r"^[ \t>*_`-]*CONTRADICTION[*_`]*:[*_]*[ \t]*(.+?)[`*_ \t]*$", re.M)  # markdown around it too

def says_none(text):
    """True when a contradiction field only reports there is none: empty, or starting with none, n/a or no contradiction (`none - the brief and the code agreed.`)."""
    return re.fullmatch(r"(?:none|n/a)(?:\s*[-\u2013\u2014.:;,!].*)?|no contradictions?\b.*|", text.strip(), re.I) is not None

def contradiction(reply):
    """The implementer's last `CONTRADICTION:` line when it found its brief wrong; the prompt's own template echoed back, or a line saying there is none, is not one."""
    found = [x.strip() for x in CONTRADICTION_RE.findall(reply or "") if "<what the brief says>" not in x and not says_none(x)]
    return found[-1] if found else None
