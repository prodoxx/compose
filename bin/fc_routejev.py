"""Jev's route for one task: a workflow and a depth on closed choices, one retry with more context, the fallback of each
answer that stays unsure, and the policy floor Jev never lowers."""

import fc_models


def rule_workflow(state):
    """fix-then-regression-test for a Bug-labelled task, else test-first when the task's test is protected; None (Jev is asked)
    when a pin decides or neither applies."""
    if state.get("pin"):
        return None
    if (state.get("bug") or {}).get("label"):
        return "fix-then-regression-test"
    return "test-first" if state.get("test") else None


def route_questions(state, plain):
    """The choice questions for one task: workflow (unless pinned or decided by its protected test), depth, and one per
    policy without globs."""
    workflows = {"TEST_FIRST": "Write the failing test first, then make it pass.",
                 "IMPLEMENT_THEN_TEST": "Implement the task, then cover it with a test.",
                 "FIX_THEN_REGRESSION_TEST": "Fix the bug and add a test that fails without the fix."}
    depths = {"FAST": "Low regression risk and low implementation uncertainty: only docs or prose change, or a one-line or "
                      "one-argument edit, or a new file nothing else uses yet; the task text says exactly what to write.",
              "STANDARD": "Some regression risk or some uncertainty: new behavior in an existing code module, up to about three "
                          "code files; the task's own tests would catch a mistake.",
              "DEEP": "High regression risk or high implementation uncertainty: many modules or users rely on what changes (a "
                      "shared interface, a command line, a data format, process or file handling), or it spans more than three "
                      "code files, or it is unclear how to do it."}
    applies = {"APPLIES": "The task's own change can delete or overwrite something the user made.",
               "DOES_NOT_APPLY": "The task cannot delete or overwrite what the user made. Files that a tool or the task itself "
                                 "creates and removes (clones, checkouts, scratch, caches, locks, logs, results) are not the "
                                 "user's work, even when the task deletes them."}
    if state.get("check"):
        workflows["CHECK_ONLY"] = ("Small work whose result shows by running it: state the expected behavior, do the work, "
                                   "run the same check again; no written test and no suite")
    questions = {}
    if not state.get("pin") and not rule_workflow(state):
        questions["workflow"] = {"type": "choice", "criteria": workflows, "instructions": {"task": state.get("task", "")}}
    if state.get("pin") != "check-only" or state.get("recorded"):
        questions["depth"] = {"type": "choice", "criteria": depths, "instructions": {"task": state.get("task", "")}}
    for i, policy in enumerate(plain):
        if policy.get("globs"):
            continue
        questions[f"policy{i}"] = {"type": "choice", "criteria": applies,
                                   "instructions": {"policy": policy["name"], "statement": policy["statement"], "task": state.get("task", ""),
                                                    "files": [f["path"] for f in state.get("files", [])], "criteria": state.get("criteria", [])}}
    return questions


def decide(env, cfg, state, matched, plain, more):
    """Ask Jev, retry once with {**state, "dependencies": more()} while an answer is unsure, let each answer that stays
    unsure fall back on its own (workflow to test-first, depth to STANDARD), apply the floor. Returns {"workflow", "depth",
    "decided_by", "confidence", "answers", "retry", "fallback", "fell_back", "floor", "checks"}."""
    questions = route_questions(state, plain)
    threshold = cfg.get("route_threshold", 0.6)
    pin = state.get("pin")
    rule = rule_workflow(state)
    asked = [k for k in ("workflow", "depth") if k in questions]

    def weak(a):
        return not a or (a.get("confidence") or 0) < threshold

    def low(answers):
        return any(weak(answers.get(k)) for k in questions)

    reply, retry, why = None, None, None
    try:
        reply = fc_models.jev_ask(env, cfg, state, questions, "Jev route") if questions else {"answers": {}}
        if questions and low(reply.get("answers", {})):
            retry = more()
            reply = fc_models.jev_ask(env, cfg, {**state, "dependencies": retry}, questions, "Jev route retry")
    except fc_models.PaymentRequired:
        raise
    except Exception as e:  # noqa: BLE001
        why = f"Jev unavailable: {e}"
    answers = (reply or {}).get("answers", {})
    confs = [a["confidence"] for a in answers.values() if a.get("confidence") is not None]
    fell = asked if why else [k for k in asked if weak(answers.get(k))]
    workflow = pin or rule or ("test-first" if "workflow" in fell else answers["workflow"]["choice"].lower().replace("_", "-"))
    depth = "STANDARD" if "depth" not in asked or "depth" in fell else answers["depth"]["choice"]
    recorded = state.get("recorded")
    if workflow == "check-only" and not recorded:
        depth, asked, fell = "FAST", [k for k in asked if k != "depth"], [k for k in fell if k != "depth"]
    if why:
        decided_by = "fallback"
    elif not questions:
        decided_by = None
    else:
        decided_by = "jev" if retry is None else "jev after retry"
        if fell:
            decided_by = "fallback" if len(fell) == len(asked) else decided_by + " + fallback"
    floor = [p["name"] for p in matched]
    checks = [c for p in matched for c in p["checks"]]
    if reply is None:
        checks += [c for p in plain for c in p["checks"]]
    else:
        for i, p in enumerate(plain):
            a = answers.get(f"policy{i}")
            if not (a and a.get("choice") == "DOES_NOT_APPLY" and (a.get("confidence") or 0) >= threshold):
                floor.append(p["name"])
                checks += p["checks"]
    checks = list(dict.fromkeys(checks))
    if floor:
        depth = "DEEP"
        if workflow == "check-only" and not recorded:
            workflow, decided_by, pin, rule = "test-first", "floor", None, None
    label = "pin" if pin else "rule" if rule else None
    by = label if decided_by is None else f"{label} + {decided_by}" if label else decided_by
    return {"workflow": workflow, "depth": depth, "decided_by": by,
            "confidence": min(confs) if confs else None, "answers": answers, "retry": retry,
            "fallback": why or (f"confidence below {threshold}" if fell else None), "fell_back": fell,
            "floor": floor, "checks": checks}
