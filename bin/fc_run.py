import argparse, json, os, signal, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import fc_core, fc_config, fc_tasks, fc_blockers, fc_linear, fc_ledger, fc_claude, fc_models, fc_proc, fc_herdr, fc_harness, fc_pi, fc_worktree, fc_layers, fc_stack, fc_change, fc_crew, fc_briefs, fc_implement, fc_init, fc_verify, fc_ship, fc_prove, fc_providers, fc_reroute, fc_route, fc_costevent, fc_fast, fc_regress, fc_unproven, fc_tierpin, fc_history, fc_credit, fc_load, fc_update


TRIAGE_BAR = ("Flag the layer only for: a change outside its listed files; hardening, flags, helpers, or abstractions the brief "
              "does not ask for; code far more complex than the brief needs; a UI state not placed or styled the way the brief says; "
              "anything aimed at the test rather than the user. Everything else is clean; style nits are clean.")

def own_diff_base(forked, below, depth):
    """The commit a task's own diff starts at: of the trunk fork point `forked` and the layers `below` it, the one furthest above the trunk (`depth(c)` commits; the first on a tie)."""
    return max([forked, *below], key=depth)

def triage(repo, proj, ids):
    """One fast-model look per passed task, all at once: `clean`, or `flag: <reason quoting the code>`. It only points the
    team lead at layers to read in full; it never rejects. Stored as `triage` in the task's result file."""
    env = fc_models.fast_env()
    if not env.get("TEXT_MODEL_API_KEY"):
        return {}
    metas = {m["id"]: m for m in fc_tasks.load_tasks(proj)}
    trunk = fc_core.current_branch(repo)
    layers = list(fc_worktree.task_branches(repo, proj))
    def one(tid):
        m, branch = metas.get(tid) or {}, fc_worktree.task_branch(repo, proj, tid)
        try:
            # A stacked task's own change starts at the nearest layer below it, not at the trunk under the whole chain.
            below = [b for b in layers if b != branch
                     and fc_core.sh(["git", "merge-base", "--is-ancestor", b, branch], repo, check=False).returncode == 0]
            base = own_diff_base(fc_core.sh(["git", "merge-base", trunk, branch], repo).stdout.strip(), below,
                                 lambda c: int(fc_core.sh(["git", "rev-list", "--count", f"{trunk}..{c}"], repo).stdout))
            diff = fc_core.sh(["git", "diff", base, branch, "--", *(m.get("files") or [])], repo, check=False).stdout
            shape = '{"verdict": "clean" | "flag", "reason": str}'
            v = fc_models.fast_object(env, "You triage one finished coding task for a senior reviewer. " + TRIAGE_BAR +
                          f" Return JSON {shape}; a flag reason is one line quoting the code.",
                          f"TASK BRIEF:\n{(m.get('body') or '')[:6000]}\n\nFILES IT MAY CHANGE: {', '.join(m.get('files') or [])}\n\n"
                          f"DIFF:\n{diff[:40000]}", shape, "verdict", max_tokens=300, timeout=30)
            verdict, reason = ("flag" if v.get("verdict") == "flag" else "clean"), str(v.get("reason") or "").strip()[:300]
        except Exception as e:  # noqa: BLE001 - triage is advisory
            verdict, reason = "unknown", f"triage failed: {str(e)[:150]}"
        f = proj / "results" / f"{tid}.json"
        if f.exists():
            r = json.loads(f.read_text()); r["triage"] = {"verdict": verdict, "reason": reason}
            fc_core.atomic_write(f, json.dumps(r, indent=2))
        return tid, verdict, reason
    with ThreadPoolExecutor(max_workers=8) as ex:
        out = list(ex.map(one, ids))
    for tid, verdict, reason in out:
        fc_core.say(f"{tid} triage  {verdict}" + (f": {reason}" if verdict != "clean" and reason else ""))
    return {tid: (v, r) for tid, v, r in out}

def cmd_triage(a):
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    ids = [x.strip() for x in a.only.split(",")] if a.only else sorted(
        f.stem for f in (proj / "results").glob("[0-9]*.json") if f.stem.isdigit() and json.loads(f.read_text()).get("status") == "passed")
    if not triage(repo, proj, ids):
        sys.exit("no fast-model key (TEXT_MODEL_API_KEY) or no passed tasks: nothing triaged")

RUN_PARALLEL = 3  # tasks at once when neither --parallel nor the config's `parallel` says

def require_committed(repo, proj, stack=True, force=False):
    """The preflight of a run (fc_init.start_checks), every problem at once before anything is cloned. Every task clone starts
    from the project branch's last commit, so its tests must be committed: it stops on anything dirty, naming it (the agent wiring
    a session hook rewrites and the tools' own directories are left out: fc_worktree.dirty_paths), on a key its service rejects,
    and with `stack` on a gh that is not logged in, and on a Mac on battery unless `force`; then it prints the startup checklist
    (power, docker, the database targets, sleep). A loaded machine or a crash-looping container only warns."""
    fc_init.start_checks(repo, stack, hint="commit the tests first so worktrees include them", force=force, proj=proj)

QUEUE = "run-queue"  # results/run-queue/<id>.json: a task `compose run --only` added to the live run
QUEUE_POLL = 2  # seconds the live run waits for a task to finish before it looks at the queue again

def joins_live_run(a):
    """Whether this `compose run --only` adds its tasks to a run that is live on the project, instead of starting a second one.
    A live run that does not say it reads the queue started before joining existed: nothing would ever read the tasks, so this stops."""
    if getattr(a, "cmd", None) != "run" or not a.only:
        return False
    m = fc_tasks.run_marker(fc_core.project_dir(a))
    live = bool(m and m["pid"] != os.getpid() and fc_tasks.pid_alive(m["pid"]) and not m.get("closed"))
    if live and not m.get("reads_queue"):
        sys.exit(f"STOP  the live run (pid {m['pid']}) predates `run --only` joining and never reads the queue; nothing was queued; wait for it to end or stop it, then run again")
    return live

def still_open(proj, tid, pid):
    """Whether the live run (pid) still has the task pending or running. A run that ended, or whose marker lists no open tasks, has none open."""
    m = fc_tasks.run_marker(proj) or {}
    return m.get("pid") == pid and not m.get("closed") and fc_tasks.pid_alive(pid) and tid in m.get("open", ())

def queue_tasks(proj, tasks):
    """Hand the prepared tasks to the live run, which starts each one as soon as its `after` tasks allow.
    A task the live run still has pending or running is waited for; one it ended without passing is handed over again; one that passed there is kept."""
    live = fc_tasks.run_marker(proj)
    pid = live["pid"]
    queue = proj / "results" / QUEUE
    queue.mkdir(exist_ok=True)
    stop = "STOP  the run ended before it could take the task; start it again: compose run --project " + proj.name + " --only "
    new = []
    for m in tasks:
        tid, now = m["id"], fc_tasks.run_marker(proj) or live
        if tid in {f.stem for f in queue.glob("*.json")} or ("open" not in now and tid in now["tasks"]):  # a run that lists no open tasks cannot say it ended the task
            fc_core.say(f"{tid} keep    already in the live run (pid {pid})")
            continue
        if still_open(proj, tid, pid):
            fc_core.say(f"{tid} waits   for the live run (pid {pid}) to finish it; it runs again if it does not pass")
            while still_open(proj, tid, pid):
                time.sleep(QUEUE_POLL)
        if tid in (fc_tasks.run_marker(proj) or now)["tasks"] and (why := fc_tasks.task_finished(proj, m)):
            fc_core.say(f"{tid} keep    {why} in the live run (pid {pid})")
        else:
            new.append(m)
    if new and (fc_tasks.run_marker(proj) or {}).get("closed"):
        sys.exit(stop + ",".join(m["id"] for m in new))
    preflight({m["model"] for m in new} | {fc_models.fallback_for(m["model"]) for m in new})  # here, where a wrong model stops only this command
    for m in new:
        fc_core.atomic_write(queue / f"{m['id']}.json", json.dumps(m))
        fc_core.say(f"{m['id']} queued  added to the live run (pid {pid}); it starts once its `after` tasks have passed")
    if new and (fc_tasks.run_marker(proj) or {}).get("closed"):  # the run ended meanwhile: nothing will read the queue
        for m in new:
            (queue / f"{m['id']}.json").unlink(missing_ok=True)
        sys.exit(stop + ",".join(m["id"] for m in new))

def cmd_run(a):
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    only = set(a.only.split(",")) if a.only else None
    why = None if getattr(a, "unproven", False) else fc_prove.unproven(repo, proj, fc_prove.verify_config(a)[2])
    if why:  # an e2e case that fails for the environment fails every verify, and no implementer can fix it
        sys.exit(f"run stopped before anything was cloned: {why}. Run `compose verify --project {proj.name} --prove` and fix every case it "
                 "names first; `--unproven` runs anyway.")
    require_committed(repo, proj, not a.no_stack, getattr(a, "force", False))
    fc_linear.refresh_tasks(proj)  # a task edited on Linear since the last sync reaches its implementer
    tasks = [m for m in fc_tasks.runnable(fc_tasks.load_tasks(proj, required=True)) if not only or m["id"] in only]
    if not tasks:
        sys.exit("no tasks on Linear under this feature carry a compose block" + (f" with ids {a.only}" if only else ""))
    redo = bool(getattr(a, "redo", False))
    a.parallel = getattr(a, "parallel", None) or fc_config.PARALLEL or RUN_PARALLEL
    kept = {m["id"]: fc_tasks.task_finished(proj, m, redo) for m in tasks}
    for tid, why in kept.items():
        if why:
            fc_core.say(f"{tid} keep    {why}" + ("; pass --redo to rebuild it" if why.startswith("its result") else ""))
    tasks = [m for m in tasks if not kept[m["id"]]]
    if not tasks:
        return fc_core.say("nothing to run: every task is finished")
    if redo:
        os.environ["COMPOSE_REDO"] = "1"
    # Test-writing teammates write on their own branches; their tests must be on the project branch before any implementer starts.
    pending = [m["id"] for m in tasks if fc_briefs.test_open(proj, m["id"])]
    if pending:
        busy = [t for t in pending if (fc_crew.crew_reports(proj, fc_herdr.agent_name(proj.name, f"tests-{t}")) or [{}])[-1].get("state") != "done"]
        if busy:
            sys.exit(f"test-writing teammates still writing: {', '.join('tests-' + t for t in busy)}; wait for their done reports, then run again")
        fc_briefs.collect_tests(proj, ",".join(pending))
    unwritten = [m["id"] for m in tasks if fc_briefs.test_open(proj, m["id"]) or m["id"] in fc_briefs.waiting_writers(proj)]
    if unwritten:  # the collect starts the writers that waited for another writer of the same test file
        sys.exit(f"test-writing teammates still to write: {', '.join('tests-' + t for t in unwritten)}; wait for their done reports, then run again")
    missing = [f"{m['id']} {p}" for m in tasks for p in m.get("protected") or [] if not (repo / p).exists()]
    if missing:  # checked here, not in spec-check: the cut is checked before its tests are written
        sys.exit(f"protected tests not on the project branch: {', '.join(missing)}; write them (compose testcrews) or fix `protected:`, then run again")
    if redo:
        for m in tasks:
            fc_ledger.run_id(proj, m["id"], fresh=True)
    routes, held = fc_route.route_tasks(repo, proj, tasks)
    held.update(fc_fast.run_holds(repo, proj, tasks, routes))
    for m in tasks:
        if m["id"] in held:
            m.setdefault("waits_on", []).append(f"the route ({held[m['id']]})")
    run_tasks(repo, proj, tasks, a)

def preflight(models):
    """One tiny turn per model a run can call, before any clone: a wrong model id or preset slug stops the run here with the
    provider's message, instead of failing every task (and, for a fallback, only on the first miss). A rate limit proves the
    model resolved, so it passes."""
    import shutil
    def probe(model):
        if fc_harness.harness_of(model):
            ok, why = fc_models.model_available(model)
            return None if ok else f"{model}: {why}"
        root = Path(tempfile.mkdtemp())
        (root / "p").mkdir()
        try:
            fc_implement.implementer(root / "p", model, "Reply with the single word OK. Do not use tools.")
            return None
        except fc_models.RateLimited:
            return None
        except Exception as e:  # noqa: BLE001
            return f"{model}: {str(e)[-400:]}"
        finally:
            shutil.rmtree(root, ignore_errors=True)
    models = sorted(m for m in models if m and not fc_claude.is_claude(m))  # Claude Code is the subscription; nothing to resolve
    for name in sorted({p[0] for p in map(fc_config.provider_of, models) if p}):
        ok, why = fc_providers.reach(name)
        if not ok:
            sys.exit("preflight stopped; nothing was cloned or run. " + why)
    # pi takes a provider's key from its own auth.json before the workspace key compose hands it, so such an entry bills elsewhere.
    shadowing = [p for p in fc_pi.pi_auth_overrides(fc_pi.PI_AUTH) if any(m.startswith(f"pi:{p}/") for m in models)]
    if shadowing:
        sys.exit(f"preflight stopped; nothing was cloned or run. {fc_pi.PI_AUTH} holds a key for {' and '.join(shadowing)}, which pi "
                 f"uses instead of the key of workspace {fc_config.WORKSPACE!r}: remove {'that entry' if len(shadowing) == 1 else 'those entries'} from it")
    fc_core.say(f"preflight  {', '.join(models)}")
    with ThreadPoolExecutor(max_workers=max(1, len(models))) as ex:
        bad = [b for b in ex.map(probe, models) if b]
    if bad:
        sys.exit("preflight failed; nothing was cloned or run. Fix the model id or preset in "
                 f"{fc_config.CONFIG_PATH} (`models`) or the task's `model:`:\n  " + "\n  ".join(bad))

def claude_capped(ready, running_models, free, metas):
    """The ready task ids to start now: at most `free`, and no more Claude Code tasks than `claude_parallel` allows beside the ones
    running (`running_models`), whatever `parallel` says. A task past the cap waits for a running one to end, the others go ahead."""
    live, out = sum(fc_claude.is_claude(m) for m in running_models), []
    for p in ready:
        if len(out) >= free:
            break
        if fc_claude.is_claude(metas[p]["model"]):
            if live >= fc_config.CLAUDE_PARALLEL:
                continue
            live += 1
        out.append(p)
    return out

def run_tasks(repo, proj, tasks, a):
    for m in tasks: m["model"] = fc_tierpin.start_model(proj, m)  # each implementer starts on its tier
    if joins_live_run(a):
        return queue_tasks(proj, tasks)
    fc_update.refuse_while_verifying(proj, repo)  # the run links the stack in the CLI worktree a verify stands on
    fc_proc.keep_awake()  # a run lasts hours; a sleeping Mac stops every agent and fails every check that waits on a timeout
    if why := fc_credit.check(repo, tasks, getattr(a, "parallel", None) or RUN_PARALLEL, getattr(a, "force", False)):
        sys.exit("preflight stopped; nothing was cloned or run. " + why)
    models = {m["model"] for m in tasks}
    preflight(models | {fc_models.fallback_for(m) for m in models})
    a.trunk = a.trunk or fc_core.current_branch(repo)  # the branch the user started on is the stack's trunk; it is never switched
    released, still = fc_blockers.released(repo, proj, tasks, a.trunk)  # said first: which outside blockers git lets go, and why the rest hold
    for b, proof in released.items():
        fc_core.say(f"unblocked  {b}  {proof}")
    for b, why in still.items():
        fc_core.say(f"waits      {b}  {why}")
    if not a.no_stack and fc_worktree.stack_order(repo, proj):
        # Earlier layers must contain everything committed on trunk since they were built (new tests, new task files), or a
        # task stacked on them starts without its own test. Rebase the existing chain onto the current trunk tip first.
        # `--no-stack` leaves the chain alone: a task with `after:` still rebases what it stacks on, the rest never wait on it.
        # A task this run rebuilds is replaced first (run_task would, later): its old commits conflicting with the trunk must not stop the run.
        for m in tasks:
            if not fc_tasks.task_finished(proj, m, redo=bool(os.environ.get("COMPOSE_REDO"))):
                fc_worktree.replace_branch(repo, proj, fc_worktree.task_branch(repo, proj, m["id"]))
        fc_layers.linearize(repo, proj, a.trunk, fc_worktree.stack_worktree(repo, proj, a.trunk))
        if fc_layers.STACK_STOP:
            sys.exit(f"stack  STOP  {fc_layers.STACK_STOP}")
    fc_herdr.herdr_space(proj)
    t0 = time.time()
    (proj / "results").mkdir(exist_ok=True)
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    live = {"open": [m["id"] for m in tasks]}  # what is still pending or running: `compose run --only` waits on these
    def write_marker(closed=False):  # closed: the queue below takes no more tasks
        fc_core.atomic_write((proj / "results" / "run.json"), json.dumps({"pid": os.getpid(), "started_at": started_at, "closed": closed, "reads_queue": True, "open": live["open"],
            "tasks": [m["id"] for m in tasks], "args": {"attempts": a.attempts, "parallel": a.parallel, "trunk": a.trunk, "no_stack": a.no_stack, "redo": bool(os.environ.get("COMPOSE_REDO"))}}, indent=2))
    write_marker()
    fc_history.record(proj, repo, "run-start", tasks=len(tasks), parallel=a.parallel, attempts=a.attempts)
    fc_core.say(f"run start project={proj.name} tasks={len(tasks)} parallel={a.parallel} attempts={a.attempts}")
    # Dependency order: a task runs once every `after:` task in this run has passed (it then stacks on that branch) or was
    # merged or skipped earlier. Independent tasks run in parallel; a chain runs layer by layer.
    metas = {m["id"]: m for m in tasks}
    ids = set(metas)
    deps = {p: [d for d in (x.strip() for x in str(m.get("after") or "").split(",")) if d in ids] for p, m in metas.items()}
    results, pending, failed_ids, halt, reflecting, capped = [], dict(deps), set(), None, [], set()
    unproven = set()
    waits = fc_tasks.waiting(tasks, released)  # an outside blocker git does not place in the trunk holds its task like a hold
    from concurrent.futures import wait, FIRST_COMPLETED
    queue = proj / "results" / QUEUE
    with ThreadPoolExecutor(max_workers=a.parallel) as ex:
        running, proving = {}, set()  # proving: the futures that are a fast fix's regression proof round, not a task's turns
        def adopt():
            """The tasks `compose run --only` queued on this run join it, and start once their `after` tasks have passed. Whether any did."""
            joined = False
            for f in sorted(queue.glob("*.json")):
                m = json.loads(f.read_text())
                f.unlink()
                if m["id"] in pending or m["id"] in running.values() or any(r["id"] == m["id"] and r["status"] == "passed" for r in results):
                    continue  # still in the run, or passed in it
                results[:] = [r for r in results if r["id"] != m["id"]]  # one the run ended earlier: this is its retry
                tasks[:] = [x for x in tasks if x["id"] != m["id"]]
                failed_ids.discard(m["id"]), unproven.discard(m["id"]), capped.discard(m["id"])
                settled = ids - set(pending) - set(running.values())  # passed or ended already: a passed one holds nothing back
                tasks.append(m)
                metas[m["id"]] = m
                ids.add(m["id"])
                deps[m["id"]] = [d for d in (x.strip() for x in str(m.get("after") or "").split(",")) if d in ids]
                pending[m["id"]] = [d for d in deps[m["id"]] if d not in settled or d in failed_ids or d in unproven]
                waits.update(fc_tasks.waiting([m], released))
                write_marker()
                fc_core.say(f"{m['id']} joined  added to the run while it goes on")
                joined = True
            return joined
        held_before = False
        while adopt() or pending or running:  # a task queued as the last one ends still starts
            if live["open"] != sorted(set(pending) | set(running.values())):
                live["open"] = sorted(set(pending) | set(running.values()))
                write_marker()
            held = fc_tasks.held(proj)  # one read per pass: a release landing mid-pass cannot split it
            hold = {**waits, **held}  # re-read every pass: a hold or a release lands while the run goes on
            # a hold on a task that runs: pause it, keep its work. Not a regression writer's proof round (no turn to cut) nor one already ended.
            for p in [p for f, p in running.items() if p in held and f not in proving and not f.done() and not fc_proc.task_paused(p)]:
                fc_proc.pause_task(p, fc_herdr.agent_name(proj.name, p))
                fc_core.say(f"{p} paused  {held[p].get('reason', '')}")
            st = None if fc_load.off(getattr(a, "force", False)) else fc_load.update()  # the machine-wide load hold, re-read every pass
            ready = [] if halt else [p for p, d in pending.items() if not d and p not in running.values() and p not in hold]
            free = 0 if st and st["held"] else a.parallel - len(running)  # held: no slot opens; running tasks go on
            if st and st["held"] and not held_before:
                lim = fc_load.limits()
                fc_core.say(f"load {st['avg']:.1f} on {lim['cores']} cores: holding new task starts until it falls below {lim['low']:.1f}")
            held_before = bool(st and st["held"])
            if st and st["held"] and ready and not running:
                time.sleep(QUEUE_POLL)  # held with tasks ready and nothing running: go round again, do not end the run
                continue
            starts = claude_capped(ready, [metas[q]["model"] for q in running.values()], free, metas)  # only into free slots: a task queued inside the pool is out of a hold's reach
            for p in ready:
                if free > len(starts) and p not in starts and p not in capped:
                    capped.add(p)
                    fc_core.say(f"{p} waits   {fc_config.CLAUDE_PARALLEL} Claude tasks already run (claude_parallel in {fc_config.CONFIG_PATH})")
            for p in starts:
                del pending[p]
                prior = None if getattr(a, "redo", False) else fc_unproven.restartable(proj, metas[p]["id"])
                if prior:
                    fc_core.say(f"{p} regress  fix kept on {prior['branch']}; restarting its regression test writer")
                    proof = ex.submit(fc_regress.regress, repo, proj, metas[p], prior, a.attempts)
                    running[proof] = p
                    proving.add(proof)
                else:
                    running[ex.submit(fc_implement.run_task, repo, proj, metas[p], a.attempts)] = p
            if halt and not running:
                break  # a payment halt: leave the rest pending, do not mark them blocked
            if not running:
                stuck = fc_tasks.held_behind(pending, hold, failed_ids)
                behind = {p for p in pending if set(pending[p]) & unproven}
                while more := {p for p in pending if set(pending[p]) & behind} - behind:
                    behind |= more
                for p in pending:
                    if p in stuck or p in behind:  # left pending for the run after the re-plan, not blocked
                        fc_core.say(f"{p} held    " + (hold[p]["reason"] if p in hold else "after unproven " + ",".join(sorted(set(pending[p]) & unproven)) if set(pending[p]) & unproven else "after held " + ",".join(sorted(set(pending[p]) & (stuck | behind)))))
                        continue
                    results.append({"id": metas[p]["id"], "title": metas[p].get("title"), "status": "blocked_by_" + ",".join(deps[p]),
                                    "attempts": 0, "seconds": 0.0, "cost": 0.0, "branch": "-"})  # it waits on something that failed
                break
            try:
                finished, _ = wait(list(running), timeout=QUEUE_POLL, return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                fc_proc.stop()
                fc_tasks.mark_interrupted(proj)
                fc_history.record(proj, repo, "run-end", tasks=len(results), outcome="interrupted", seconds=round(time.time() - t0, 1))
                print(f"\n[{fc_core.now()}] run stopped by Ctrl-C; `compose resume` continues it", file=sys.stderr)
                os._exit(130)
            for f in finished:
                p = running.pop(f)
                proving.discard(f)
                try:
                    r = f.result()
                except RuntimeError as e:  # a chain that no longer rebases: the task never started; one line with the commands, and the run goes on
                    fc_core.say(f"{p} STOP  {e}")
                    r = {"id": metas[p]["id"], "title": metas[p].get("title"), "status": "unchained", "attempts": 0, "seconds": 0.0, "cost": 0.0,
                         "branch": "-", "log": [{"stop": str(e)}]}
                    fc_core.atomic_write((proj / "results" / f"{p}.json"), json.dumps(r, indent=2))
                fc_proc.resume_task(p)
                if r["status"] in ("held", "paused"):  # held before its first turn, or paused mid-turn with its work kept: pending again, the next pass reads the hold
                    pending[p] = []
                    continue
                if "proof" in r:  # a proof round's result replaces the task's row
                    results[:] = [r if x["id"] == r["id"] else x for x in results]
                    if r["status"] == "passed":
                        unproven.discard(metas[p]["id"])
                        for q in pending:
                            pending[q] = [d for d in pending[q] if d != metas[p]["id"]]
                    continue
                results.append(r)
                fc_history.record(proj, repo, "task", task=r["id"], model=metas[p].get("model"), attempts=r["attempts"], seconds=round(r["seconds"], 1), cost=r["cost"], outcome=r["status"])
                events =fc_fast.run_events(proj, repo, metas[p], r)
                cost = fc_costevent.run_event(proj, metas[p], r, events[-1]) if events else None
                fc_ledger.record_task(repo, proj, metas[p], events + ([cost] if cost else []))
                if r["status"] in fc_change.REFLECT_ON:  # the screen runs beside the run; dispatch never waits on it
                    reflecting.append(threading.Thread(target=reflect_task, args=(proj, r)))
                    reflecting[-1].start()
                tid = metas[p]["id"]
                if r["status"] == "payment_required":
                    pay = next(x["payment_required"] for x in r["log"] if "payment_required" in x)
                    halt = halt or fc_models.PaymentRequired(pay["provider"], pay["message"])
                if r["status"] == "unproven":
                    unproven.add(tid)
                elif r["status"] != "passed":
                    failed_ids.add(tid)
                for q in pending:
                    pending[q] = [d for d in pending[q] if d != tid or d in failed_ids or d in unproven]
                if fc_regress.fast_fix(proj, metas[p], r):
                    proof = ex.submit(fc_regress.regress, repo, proj, metas[p], r, a.attempts)
                    running[proof] = p
                    proving.add(proof)
    live["open"] = []
    write_marker(closed=True)  # then a task queued in the instant before is told, never lost
    for f in queue.glob("*.json"):
        fc_core.say(f"{f.stem} not run  queued as the run ended; start it again: compose run --project {proj.name} --only {f.stem}")
        f.unlink()
    for t in reflecting:  # every screen is on disk before the run reports its end
        t.join()
    if any(m.get("linear") for m in tasks):
        fc_linear.linear_reconcile(proj)
    results.sort(key=lambda r: r["id"])
    total = time.time() - t0
    print()
    print(f"{'task':6} {'status':18} {'attempts':8} {'time':>9} {'cost':>9}  branch")
    for r in results:
        print(f"{r['id']:6} {r['status']:18} {r['attempts']:<8} {fc_core.fmt(r['seconds']):>9} {'$' + format(r['cost'], '.4f'):>9}  {r['branch']}")
    passed = sum(r["status"] == "passed" for r in results)
    halt = halt or fc_reroute.after_run(repo, proj, results, tasks, a.trunk)
    if passed:
        triage(repo, proj, [r["id"] for r in results if r["status"] == "passed"])
    fc_core.say(f"run done   {passed}/{len(results)} passed  wall {fc_core.fmt(total)}  sum of task time {fc_core.fmt(sum(r['seconds'] for r in results))}")
    fc_core.say(fc_core.LEDGER.line())
    fc_history.record(proj, repo, "run-end", tasks=len(results), passed=passed, seconds=round(total, 1), cost=fc_core.LEDGER.snapshot()["total"],
                      providers=fc_history.providers(fc_core.LEDGER.snapshot()), outcome="halted" if halt else "passed" if passed == len(results) else "failed")
    if passed and not a.no_stack:
        fc_stack.register_stack(repo, proj, a.trunk)
    if halt:
        done_ids = {r["id"] for r in results if r["status"] in (*fc_tasks.FINISHED, "already_green")}
        remaining = sorted(metas[p]["id"] for p in metas if metas[p]["id"] not in done_ids)
        fc_models.payment_halt(repo, proj, "run", remaining, {"attempts": a.attempts, "parallel": a.parallel, "trunk": a.trunk, "no_stack": a.no_stack, "redo": bool(os.environ.get("COMPOSE_REDO"))}, halt)
    (proj / "results" / "resume.json").unlink(missing_ok=True)
    (proj / "results" / "run.json").unlink(missing_ok=True)
    fc_core.atomic_write((proj / "results" / "run-cost.json"), json.dumps(fc_core.LEDGER.snapshot(), indent=2))
    sys.exit(0 if all(r["status"] in fc_tasks.FINISHED for r in results) else 1)


def reflect_task(proj, r):
    """A task ended without passing: screen the settled lines against what its last attempt showed. It runs beside the run,
    so nothing here may stop it."""
    try:
        last = (r.get("log") or [{}])[-1]
        head = f"task {r['id']} {r.get('title') or ''}".strip()
        if r["status"] == "contradiction":
            found = last.get("contradiction", "")
            fc_change.reflect(proj, f"task:{r['id']}", f"{head}: the implementer found its brief wrong: {found}",
                    against=[found.split(" / ")[0].strip()])
            return
        why = (last.get("error") or (f"changed files outside its task: {', '.join(last['stray'])}" if last.get("stray") else "")
               or last.get("summary") or (last.get("test_output") or "").strip()[-1500:] or "no detail")
        fc_change.reflect(proj, f"task:{r['id']}", f"{head} ended {r['status']} after {r['attempts']} attempt(s): {why}")
    except Exception as e:  # noqa: BLE001
        fc_core.say(f"{r['id']} reflect failed ({str(e)[:200]}); the run goes on")


def since_of(stamp):
    """Epoch seconds of a `%Y-%m-%d %H:%M:%S` local time stamp as the run and halt files write it, None when it is not one."""
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None

def cmd_resume(a):
    """Continue a run or verify that stopped on PAYMENT REQUIRED, from the saved state in results/resume.json."""
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    path = proj / "results" / "resume.json"
    if not path.exists():
        m = fc_tasks.run_marker(proj)
        if not m or fc_tasks.pid_alive(m.get("pid")):
            sys.exit("nothing to resume: no payment halt and no interrupted run recorded for this project" + (" (a run is live)" if m else ""))
        redo = bool(m["args"].get("redo"))
        done = {t["id"] for t in fc_tasks.load_tasks(proj, required=True) if fc_tasks.task_finished(proj, t, redo, since_of(m["started_at"]))}
        state = {"command": "run", "remaining": [t for t in m["tasks"] if t not in done], "args": m["args"], "halted_at": m["started_at"], "provider": "interrupted run"}
        (proj / "results" / "run.json").unlink(missing_ok=True)
    else:
        state = json.loads(path.read_text())
    fc_core.say(f"resume {state['command']}  halted {state['halted_at']} at {state['provider']}")
    ns = argparse.Namespace(repo=str(repo), project=proj.name, force=getattr(a, "force", False), **state["args"])
    if state["command"] == "run":
        require_committed(repo, proj, not getattr(ns, "no_stack", False), getattr(a, "force", False))
        redo, since = bool(state["args"].get("redo")), since_of(state["halted_at"])
        if redo:  # the tasks that had not started still read passed from before the redo; only a result newer than the stop is new
            os.environ["COMPOSE_REDO"] = "1"
        tasks = [m for m in fc_tasks.load_tasks(proj, required=True) if m["id"] in set(state["remaining"]) and not fc_tasks.task_finished(proj, m, redo, since)]
        if not tasks:
            path.unlink(missing_ok=True)
            sys.exit("nothing left to run; cleared the halt")
        ns.force = getattr(a, "force", False)
        ns.parallel = getattr(a, "parallel", None) or fc_config.PARALLEL or getattr(ns, "parallel", None) or RUN_PARALLEL  # flag, config, the halted run's
        fc_core.say(f"resume {len(tasks)} task(s): {', '.join(m['id'] for m in tasks)}")
        run_tasks(repo, proj, tasks, ns)
    else:
        ns.fix, ns.skip_e2e, ns.skip_review = state["args"]["fix"], state["args"]["skip_e2e"], state["args"]["skip_review"]
        fc_ship.cmd_verify(ns)
