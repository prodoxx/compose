import json, os, sys, time
from pathlib import Path
import fc_cost, fc_core, fc_tasks, fc_worktree, fc_decide, fc_watch


# ---------- `away` and `yolo`: the product owner is away; the engineering manager answers with the recommendation and logs it ----------
YOLO_NEVER = ("destructive, irreversible, and security-sensitive actions are never pre-authorizable whatever the words say: "
              "anything that costs money beyond the configured models (a model or provider switch, keys), the payment halt, "
              "merging a toolsmith fix into compose itself, force-push, `clean --delete-unmerged`, `clean --delete-remote`, `clean --abandon`, `crew stop --force`, "
              "attended prompts (Frame0 Allow, sign-ins), and anything the product owner's CLAUDE.md forbids. These still stop and wait.")
YOLO_TAG = fc_decide.UNCONFIRMED  # what the map's Decisions so far shows after a decision taken for the product owner
def digest_sections(repo, proj, y):
    """What the product owner reads on return, rendered only from disk: their words, the decisions, what waits, what failed, what is done, cost."""
    since = fc_core.report_time(y["since"])
    snap = fc_watch.watch_snapshot(repo, proj)
    changed = lambda tid: (f := proj / "results" / f"{tid}.json").exists() and f.stat().st_mtime >= since
    tasks = {tid: t for tid, t in snap["tasks"].items() if changed(tid)}
    waiting = [f"{n}: {r[-1]['state']}  {r[-1]['message']}" for n, r in snap["crew"].items()
               if r and r[-1]["state"] in ("needs-user", "blocked") and fc_core.report_time(r[-1]["at"]) >= since]
    if snap["halt"]:
        waiting.append(f"payment halt at {snap['halt'][1]} ({snap['halt'][2]} task(s) left): top up, then `compose resume`")
    verify = snap["verify"] if snap["verify"] and snap["verify"][0] >= since else None
    if verify and not verify[1]:
        waiting.append("verify FAIL: read results/verify.json")
    failed = [f"{tid} {t['title']}  {t['state']}" for tid, t in tasks.items() if t["state"] == "failed" or t["state"].startswith(("rejected_", "exhausted"))]
    done = [f"{tid} {t['title']}  {t['state']}" for tid, t in tasks.items() if t["state"] in ("passed", "already_green", "merged")]
    if verify and verify[1]:
        done.append("verify PASS")
    layers = proj / "layers.json"
    if layers.exists() and layers.stat().st_mtime >= since:
        done.append("stack submitted: " + ", ".join(json.loads(layers.read_text())))
    elif stack := fc_worktree.stack_order(repo, proj):
        done.append(f"stack ok locally, not pushed: {' <- '.join(stack)}")
    t = fc_cost.total(fc_cost.rows(proj))
    task_cost = sum(v["cost"] for v in snap["tasks"].values())
    sections = [("Your instructions", [y.get("words") or "(no words)", push_line(y, fc_tasks.push_stance(proj))]),
                ("Decided for you", [f"{d['ts']}  {d['text']}" for d in y.get("decisions", [])]),
                ("To confirm", [f"the {len(y['decisions'])} decision(s) above are recommendations, not your answers: the map's Decisions so far shows them as "
                                f"{YOLO_TAG.format(mode=y.get('mode', 'yolo'), date='<date>')}. Ask once: accept all, or name the ones to change; the engineering manager then "
                                f"records each one with `compose decide --project {proj.name} \"<id>\" --quote \"<their words>\"`, "
                                f"and work cut on one is redone if you change it"] if y.get("decisions") else []),
                ("Waiting on you", waiting), ("Tried and failed", failed), ("Done", done),
                ("Cost", [f"{t['input']}in  {t['cache_created']}cc  {t['cache_read']}cr  {t['output']}out  ({t['teammates']} teammates)  tasks ${task_cost:.4f}"])]
    return sections

def push_line(y, stance):
    """The `push:` line of the instructions: the posture's push, or the project's standing submit_after_verify when that is what pushes."""
    if stance == "auto":
        return "push: yes, a passed verify submits the stack (verify.json submit_after_verify); --update and land still need your go"
    return f"push: {'yes' if y.get('push') else 'no'}"

def yolo_digest(repo, proj, y, verb="off"):
    """The digest rendered: a headline, then each section's items."""
    out = [f"{y['mode']} {verb} for {proj.name}: on since {y['since']}"]
    for head, items in digest_sections(repo, proj, y):
        out += ["", head] + [f"- {i}" for i in items or ["none"]]
    return "\n".join(out)

def cmd_yolo(a):
    """Enter yolo (words pre-answer questions verbatim; --push permits the stack's submit), log a decision taken, clear the words, or come back (`off`)."""
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    path = proj / "results" / "yolo.json"
    y = fc_tasks.yolo_state(proj)
    words = list(a.words or [])
    P = a.cmd
    if (words[:1] in (["off"], ["digest"], ["decided"]) or a.clear_words) and not y:
        print(f"{P} is not on for {proj.name}; `compose {P} --project {proj.name} [--push] [words]` turns it on")
        sys.exit(1)
    if (words[:1] in (["off"], ["digest"], ["decided"]) or a.clear_words) and y["mode"] != P:
        word = "--clear-words" if a.clear_words else words[0]
        fix = "compose factory day" if y["mode"] == "night" else f"compose {y['mode']} {word} --project {proj.name}"  # night is the factory's: day ends it
        print(f"{y['mode']} is on for {proj.name}, not {P}; run `{fix}`")
        sys.exit(1)
    if a.clear_words:
        if words or a.push:
            sys.exit(f"--clear-words changes only the words; run it alone: `compose {P} --project {proj.name} --clear-words`")
        y["words"] = ""
        fc_core.atomic_write(path, json.dumps(y, indent=2))
        print(f"words cleared for {proj.name}; kept: since {y['since']}, push: {'yes' if y.get('push') else 'no'}, {len(y['decisions'])} decision(s)")
        return
    if words == ["digest"]:
        print(yolo_digest(repo, proj, y, "digest"))
        return
    if words == ["off"]:
        print(yolo_digest(repo, proj, y))
        archive = path.with_name(f"yolo-{y['since'].replace(' ', 'T').replace(':', '')}.json")
        os.replace(path, archive)
        print(f"\nresults/yolo.json archived as {archive.name}")
        return
    if words[:1] == ["decided"]:
        if len(words) < 2:
            sys.exit(f'{P} decided needs "<question>: <answer taken>"')
        y["decisions"].append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": " ".join(words[1:])})
        fc_core.atomic_write(path, json.dumps(y, indent=2))
        print(f"logged for the product owner's return: {' '.join(words[1:])}")
        print(f"an answer to an open question goes on the map with `compose decide --project {proj.name} --new \"<decision>\" --closes <id>` (no --quote under {P}): "
              f"it writes the line \"D<n>: <decision> (closes <id>) {YOLO_TAG.format(mode=P, date=time.strftime('%Y-%m-%d'))}\" and logs it here, never as the product owner's")
        return
    # A shell variable holding a whole command reaches here as one word (zsh never splits an unquoted one): refuse it, never store it.
    if any(w.startswith(("--repo", "--project")) for w in words) or " decided " in f" {' '.join(words)} ":
        sys.exit(f"these words look like a compose command passed as one word, so nothing changed: {' '.join(words)!r}\n"
                 f"write the command out and quote only the words:\n"
                 f"  compose {P} --project {proj.name} [--push] \"<words>\"\n"
                 f"  compose {P} decided --project {proj.name} \"<question>: <answer taken>\"\n"
                 f"words that themselves say ` decided ` are taken for a mislaid `{P} decided`: reword them")
    y = y or {"since": time.strftime("%Y-%m-%d %H:%M:%S"), "decisions": []}
    switch = "mode" in y and y["mode"] != P
    y.update(mode=P, push=bool(a.push) or (switch and y.get("push", False)), words=" ".join(words) if words else y.get("words", ""),
             by={"role": os.environ.get("COMPOSE_ROLE") or "manager", "session": fc_core.session_id()})  # watch and status say who: fc_tasks.yolo_by
    path.parent.mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(path, json.dumps(y, indent=2))
    print(f"{P} on for {proj.name} since {y['since']}: the engineering manager answers with the recommendation and logs each answer")
    print(f"your words: {y['words'] or '(none)'}")
    if y["push"]:
        print("push: yes, the verified stack is submitted")
    elif fc_tasks.submit_after_verify(proj):
        print(push_line(y, "auto"))
    else:
        print("push: no, the stack waits for you")
    print(YOLO_NEVER)
    print(f"back: `compose {P} off --project {proj.name}` prints what happened")
