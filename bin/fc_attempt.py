import json, shutil
import fc_core, fc_models, fc_tasks

def branch_name(prefix, tid, tag):
    """A task's branch: the project's task prefix, the id, then the helper's tag (`-bench-x`) when a bench run built it."""
    return prefix + tid + tag

def fast_route(tag, reset_files, workflow):
    """Whether the task takes the fast route: a real run (never a bench run) of a `fix-then-regression-test` task. `workflow` is asked only then."""
    return not (tag or reset_files) and workflow() == "fix-then-regression-test"

def only_delete(delete, files):
    """Whether the deletion is the whole task: no model turn, the one test run decides."""
    return bool(delete) and not files

def scope_of(files, delete):
    """The files a task may change, deletions apart; None (no scope check) for a task that names neither files nor deletions."""
    return files if files or delete else None

def base_state(fast, red_code, delete):
    """What the test's run on the base says: "broken_base" (a fast task red on HEAD), "already_green" (a test-first task that passes already: done or
    vacuous; a deleting task has its change to make whatever the test says), else "go"."""
    return "broken_base" if fast and red_code else "already_green" if red_code == 0 and not fast and not delete else "go"

def final_status(status, fast):
    """A fast task that passed ends `unproven`: its test was never seen red."""
    return "unproven" if fast and status == "passed" else status

def hold_before_turn(proj, tag, meta, res, last, wt):
    """A hold placed while the run cloned, based and installed the task: the last run's result stands (none when there was none), the clone goes and
    no turn runs. Returns the `held` result, or None when nothing holds the task (a bench run is never held)."""
    hold = None if tag else fc_tasks.held(proj).get(meta["id"])
    if not hold:
        return None
    if last is None:
        res.unlink(missing_ok=True)
    else:
        fc_core.atomic_write(res, last)
    shutil.rmtree(wt, ignore_errors=True)
    fc_core.say(f"{meta['id']} held    before its first turn: {hold.get('reason', '')}")
    return {"id": meta["id"], "title": meta.get("title"), "status": "held", "attempts": 0, "seconds": 0.0, "cost": 0.0, "branch": "-", "log": []}

def attempts_spent(log):
    """The attempts spent: each numbered turn once (a turn and the test of the edits it left are one), never a provider's refusal, a payment stop or a turn the machine slept in."""
    return len({e["attempt"] for e in log if "attempt" in e and not e.get("rate_limited") and "payment_required" not in e and "slept" not in e})

def fallback_due(fails, fallback, after):
    """Whether the fallback model takes over: one exists and `after` attempts have failed on the current model."""
    return bool(fallback) and fails >= after

def rate_step(fallback, waits):
    """(action, waits, seconds) for a rate-limited turn: "switch" to the fallback at once, else "wait" 20 s longer each time and "give_up" past 4 waits."""
    if fallback:
        return "switch", waits, 0
    waits += 1
    return ("give_up" if waits > 4 else "wait"), waits, 20 * waits

def timeout_step(stopped, nap):
    """What a turn that timed out costs: "stop" after a stop (no attempt, no turn after it), "refund" when the machine slept in it (no attempt, no step
    toward the fallback), else "miss" (one failed attempt)."""
    return "stop" if stopped else "refund" if nap else "miss"

def error_step(stopped, fallback):
    """What a turn that errored does: "stop" after a stop (no attempt, no fallback), "retry" in a fresh session when a fallback may take over, else "end" the task."""
    return "stop" if stopped else "retry" if fallback else "end"

def turn_limit(meta):
    """Seconds one implementer turn may take: the task's test runs after the turn, so its `test_timeout` has no room in it."""
    return fc_models.TURN_TIMEOUT

def cut_of(err):
    """(tool calls, usage) a turn cut short carries on its error; what the turn cost is counted even then."""
    return getattr(err, "tools", 0), getattr(err, "usage", {"cost": 0.0, "tokens_in": 0, "tokens_out": 0})

def tally(cost, reference, usage):
    """(cost, reference cost) with one more turn's usage added: the real cost stays apart from the list price, and a missing or None one counts as zero."""
    return cost + usage["cost"], reference + (usage.get("reference_cost") or 0)

def verdict_of(code, over, new_docs, stray, broke, cannot):
    """"pass" when the test passed and nothing about the change is over its limits: size, documents, files outside the task, a regression, an unknown base."""
    return "pass" if code == 0 and not over and not new_docs and not stray and not broke and not cannot else "fail"

def settled(verdict, tampered, cannot, only_delete, status):
    """The status a tested attempt ends the task on, or None when another attempt may follow: a tampered protected file, a base run that cannot be known
    or a pass end it, and so does a delete-only task, whose one attempt decides."""
    return "rejected_tampered" if tampered else "rejected_regression" if cannot else "passed" if verdict == "pass" else status if only_delete else None

def miss_status(code, over, new_docs, stray, broke, status):
    """The status of a missed attempt: a test that passed names what else rejected the change, one that failed leaves the status as it was."""
    return ("rejected_size" if over else "rejected_docs" if new_docs else "rejected_scope" if stray else "rejected_regression" if broke else status) if code == 0 else status
