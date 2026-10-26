"""Night mode's prep: with nothing ready, a one-shot agent researches the next tickets that need planning and keeps a Linear document of questions
for the product owner's next planning session. It never shapes or plans anything and writes nothing to a ticket."""
from __future__ import annotations
import hashlib, json, os, re, subprocess, time
from pathlib import Path

import fc_claude, fc_config, fc_core, fc_delivery, fc_factorystate, fc_factoryturn, fc_nightstate, fc_plan, fc_prd, fc_readiness

PREP_TITLE = "Night prep: questions for the next planning session"
REFRESH_ON = ("dispatched", "verified", "held", "changed", "answered")
REFRESH_SECONDS = 1800
INTRO = "Kept current overnight by night prep. These are researched questions with a recommendation marked as a guess; nothing here is a plan or a decision."
GONE = ("done", "canceled", "cancelled", "in progress", "in review")
MAX_QUESTIONS = 5
WORKER_SECONDS = 1800 + 300  # a worker's timeout plus a margin: a pid older than this is another process's
RETRY_SECONDS = 1800
ERRORS = (RuntimeError, ValueError, TypeError, KeyError, OSError, SystemExit, subprocess.SubprocessError)
ASK = ("You prepare the next planning session for this ticket; you never plan: do not write stories, mark anything ready, or answer your own questions. "
       "Read the code it touches, say what is built and what is still open, and write at most 5 questions the product owner must answer, "
       "each with your recommendation marked as a guess. When an old section is given, rewrite, merge, or drop its questions against what is true now; "
       "set drop when none still applies. Reply with only JSON: "
       '{"code": [str], "built": [str], "open": [str], "questions": [{"question": str, "recommendation": str, "why": str}], "drop": bool}.')
PRD_TITLE = "product requirements"  # the PRD's section of the prep document is "## PRD: product requirements"
PRD_ASK = ("You prepare the product owner's PRD session; the project has no PRD yet. You never plan: do not write the PRD or stories, "
           "mark anything ready, or answer your own questions. Read the seed below (the project, its documents and tickets, what is built) and the code, "
           "say what is built and what is still open, and write at most 5 questions about the product (its problem, who it is for, goals, non-goals, "
           "anti-goals) the product owner must answer, each with your recommendation marked as a guess. When an old section is given, rewrite, merge, "
           "or drop its questions against what is true now; set drop when none still applies. Reply with only JSON: "
           '{"code": [str], "built": [str], "open": [str], "questions": [{"question": str, "recommendation": str, "why": str}], "drop": bool}.')

def prep_print(ticket_text: str, feature_text: str) -> str:
    return hashlib.sha256(f"{ticket_text}\0{feature_text}".encode()).hexdigest()[:16]

def _q(text, n=None) -> str:
    return " ".join(str(text).replace('"', "'").split())[:n]

def _line(text) -> str:
    """Model text as one line that cannot open a heading."""
    return " ".join(str(text).split()).lstrip("# ")

def outcomes_path(repo) -> Path:
    return fc_core.repo_link(repo).with_name("night-prep.jsonl")

def _outcome(repo, ident, status) -> None:
    path = outcomes_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as out:
        out.write(json.dumps({"ticket": ident, "status": status, "at": time.time()}) + "\n")

def _last_outcome(repo, ident, at):
    """The status of the ticket's latest outcome recorded at or after `at`, or None."""
    try:
        lines = outcomes_path(repo).read_text().splitlines()
    except FileNotFoundError:
        return None
    last = None
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("ticket") == ident and isinstance(r.get("at"), (int, float)) and r["at"] >= at:
            last = r.get("status")
    return last

def prep_status(repo, ident, rec, now) -> str:
    """How the record's prep stands: running, prepared, dropped, cut (the night ended first), failed, or gave-up (failed on its retry).
    A worker that died without an outcome failed. Read from the outcome file, so it holds before the loop has settled anything."""
    if _running(rec, now):
        return "running"
    status = _last_outcome(repo, ident, rec.get("at") or 0)
    if status not in ("prepared", "dropped", "cut"):
        status = "failed"
    return "gave-up" if status == "failed" and (rec.get("attempts") or 0) >= 1 else status

def _issue(repo, ident) -> dict:
    got = json.loads(fc_factoryturn.compose_cli(["linear", "get", ident, "--json", "--repo", str(repo)]))
    if not isinstance(got, dict):
        raise ValueError(f"no issue {ident}")
    return got

def _texts(repo, issue) -> tuple:
    """(the ticket's title and description, its feature's description): what a change to either would reopen."""
    parent = (issue.get("parent") or {}).get("identifier")
    feature = _issue(repo, parent).get("description") or "" if parent else ""
    return f"{issue.get('title') or ''}\n{issue.get('description') or ''}", feature

def candidates(repo, offer_id, limit) -> list:
    """The next tickets that need planning, from the offer on in delivery order: a feature without stories, or a story not specced."""
    plan = fc_delivery.product_plan(repo)
    if not plan or limit <= 0:
        return []
    entries = fc_plan.order_entries(plan["tracks"], plan["features"])
    start = next((i for i, e in enumerate(entries) if e["id"] == offer_id), 0)
    out = []
    for e in entries[start:]:
        if len(out) >= limit:
            break
        if e["state"].lower() not in fc_factoryturn.OPEN:
            continue
        if e["kind"] == "feature":
            out.append({"id": e["id"], "title": e["title"], "layer": "stories", "feature": e["feature"]})
            continue
        try:
            got = fc_readiness.story_readiness(repo, e["id"], write=False)
        except ERRORS:
            continue
        if not got.get("error") and (got.get("specced") is False or got.get("needs_shaping")):
            out.append({"id": e["id"], "title": e["title"], "layer": "story", "feature": e["feature"]})
    return out

def render(ticket, found: dict, model, at) -> str:
    def items(head, key):
        return [head] + [f"- {x}" for x in found.get(key) or []] + [""]
    lines = [f"## {ticket['id']}: {ticket['title']}", "",
             f"Layer: {ticket['layer']}. Prepared {at} by night prep ({model}): a likely opening, not a script.", "",
             *items("Code involved:", "code"), *items("What is built:", "built"), *items("What is still open:", "open"), "Questions:"]
    for n, q in enumerate(found.get("questions") or [], 1):
        lines += [f"{n}. {q['question']}", f"   Recommendation (a guess): {q['recommendation']} - {q['why']}"]
    return "\n".join(lines) + "\n"

def find_doc(repo) -> dict:
    """The prep document as {id, content, url}, found only: id is None when it is missing."""
    docs = json.loads(fc_factoryturn.compose_cli(["linear", "doc", "list", "--json", "--repo", str(repo)]))
    if not isinstance(docs, list):
        raise ValueError("doc list --json did not print a list")
    for d in docs:
        if isinstance(d, dict) and (d.get("title") or "").strip() == PREP_TITLE and d.get("id"):
            return {"id": d["id"], "content": _body(fc_factoryturn.compose_cli(["linear", "doc", "get", d["id"], "--repo", str(repo)])), "url": d.get("url") or ""}
    return {"id": None, "content": "", "url": ""}

def _body(shown) -> str:
    """A doc get's body: after the title line, before the section hashes."""
    out = []
    for line in shown.split("\n")[1:]:
        if line.startswith("--- section hashes"):
            break
        out.append(line)
    return "\n".join(out).rstrip("\n")

def prep_doc(repo) -> dict:
    """The prep document, created when missing."""
    doc = find_doc(repo)
    if doc["id"]:
        return doc
    url = fc_factoryturn.compose_cli(["linear", "doc", "create", "--title", PREP_TITLE, "--repo", str(repo)], stdin=INTRO + "\n").strip()
    doc = find_doc(repo)
    if not doc["id"]:
        raise RuntimeError("the prep document was created but is not listed")
    return {**doc, "url": doc["url"] or url}

def _heading(body, ident, title) -> str:
    return next((l for l in body.split("\n") if l.startswith(f"## {ident}:")), f"## {ident}: {title}")

def _old_section(body, ident) -> str:
    lines = body.split("\n")
    heading = next((l for l in lines if l.startswith(f"## {ident}:")), None)
    span = fc_plan.section_span(lines, heading) if heading else None
    return "\n".join(lines[span[0]:span[1]]).strip() if span else ""

def write_section(repo, ident, title, text) -> str:
    """Replace this ticket's section of the prep document, or drop it (heading too) when text is None. Returns the document's url.
    The document is read back after the write; when another writer's save has undone ours, it is redone on the fresh body (two tries in all)."""
    url = ""
    for _ in range(2):
        if text is None:
            doc = find_doc(repo)
            if not doc["id"] or not any(l.startswith(f"## {ident}:") for l in doc["content"].split("\n")):
                return url
        else:
            doc = prep_doc(repo)
        body = fc_plan.replace_section(doc["content"], _heading(doc["content"], ident, title), text or "")
        shown = fc_factoryturn.compose_cli(["linear", "doc", "update", doc["id"], "--description", "--repo", str(repo)], stdin=body)
        url = shown.strip().removeprefix("updated ").strip() or doc["url"]
        now = find_doc(repo)["content"]
        if _old_section(now, ident) == (text or "").strip():
            return url
    raise RuntimeError(f"the prep document did not keep the section of {ident}")

def _running(rec, now) -> bool:
    """Whether the record's worker still runs: a pid older than the worker's timeout is another process's."""
    pid = rec.get("pid")
    return bool(pid) and now - (rec.get("at") or 0) <= WORKER_SECONDS and _alive(pid)

def _alive(pid) -> bool:
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:  # our own finished child is a zombie until reaped
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True

def parse_reply(reply):
    """The last JSON object in the reply that has a questions list or says drop."""
    objs, dec = [], json.JSONDecoder()
    for m in re.finditer(r"\{", reply):
        try:
            v = dec.raw_decode(reply[m.start():])[0]
        except ValueError:
            continue
        if isinstance(v, dict) and (isinstance(v.get("questions"), list) or v.get("drop")):
            objs.append(v)
    return objs[-1] if objs else None

def _strings(value) -> list:
    return [_line(x) for x in value if _line(x)] if isinstance(value, list) else []

def _clean(got) -> dict:
    qs = [{"question": _line(q.get("question") or ""), "recommendation": _line(q.get("recommendation") or ""), "why": _line(q.get("why") or "")}
          for q in got.get("questions") or [] if isinstance(q, dict)]
    return {"code": _strings(got.get("code")), "built": _strings(got.get("built")), "open": _strings(got.get("open")),
            "questions": [q for q in qs if q["question"]][:MAX_QUESTIONS], "drop": bool(got.get("drop"))}

def prep_prompt(issue, feature, plan_doc, notes, old) -> str:
    return (f"{ASK}\n\nTicket {issue.get('identifier')}: {issue.get('title')}\n{(issue.get('description') or '')[:6000]}\n\n"
            f"Its feature:\n{feature[:6000]}\n\nThe product plan:\n{plan_doc[:6000]}\n\nNight notes:\n{notes[-3000:]}\n\n"
            + (f"Old section to rewrite, merge, or drop:\n{old}\n" if old else "No old section.\n"))

def prd_prompt(seed, notes, old) -> str:
    return (f"{PRD_ASK}\n\nThe seed (compose prd seed):\n{seed[:14000]}\n\nNight notes:\n{notes[-3000:]}\n\n"
            + (f"Old section to rewrite, merge, or drop:\n{old}\n" if old else "No old section.\n"))

def _subject(repo, ident) -> tuple:
    """(title, layer, prompt) of what the worker prepares: a ticket, or the PRD while the project has none."""
    content = find_doc(repo)["content"]
    if ident == fc_prd.PRD_ID:
        seed = fc_factoryturn.compose_cli(["prd", "seed", "--repo", str(repo)])
        return PRD_TITLE, "prd", prd_prompt(seed, fc_nightstate.notes_tail(repo), _old_section(content, ident))
    issue = _issue(repo, ident)
    title = _line(issue.get("title") or "") or ident
    feature = _texts(repo, issue)[1]
    try:
        plan = fc_delivery.product_plan(repo)
    except (RuntimeError, SystemExit):
        plan = None
    entries = fc_plan.order_entries(plan["tracks"], plan["features"]) if plan else []
    layer = "stories" if any(e["id"] == ident and e["kind"] == "feature" for e in entries) else "story"
    return title, layer, prep_prompt(issue, feature, ((plan or {}).get("doc") or {}).get("content") or "", fc_nightstate.notes_tail(repo), _old_section(content, ident))

def run_prep(repo, ident) -> dict:
    """The worker: research one ticket, or the PRD, and write its section of the prep document. A failure is an event, never a crash."""
    model, effort = fc_config.NIGHT_MODEL
    since = fc_nightstate.night_since(repo)
    try:
        title, layer, prompt = _subject(repo, ident)
        reply = fc_claude.claude_once(repo, prompt, timeout=1800, model=fc_claude.claude_id(model), tools="Read,Grep,Glob", effort=effort, keep=8000)[1]
        got = parse_reply(reply)
        if got is None:
            fc_factorystate.add_event(repo, f'event factory=prep-failed ticket={ident} why="{_q(reply.strip(), 120)}"')
            _outcome(repo, ident, "failed")
            return {"ticket": ident, "status": "failed"}
        found = _clean(got)
        if not fc_nightstate.still_on(repo, since):  # nothing is written once the night it was started under has ended
            fc_factorystate.add_event(repo, f'event factory=prep-failed ticket={ident} why="the night ended before the prep"')
            _outcome(repo, ident, "cut")  # not the worker's fault: the next night prepares it again
            return {"ticket": ident, "status": "cut"}
        if found["drop"] or not found["questions"]:
            url = write_section(repo, ident, title, None)
            _outcome(repo, ident, "dropped")
            fc_factorystate.add_event(repo, f"event factory=prep-dropped ticket={ident}  (no question is left for the product owner{': ' + _q(url) if url else ''})")
            return {"ticket": ident, "status": "dropped"}
        text = render({"id": ident, "title": title, "layer": layer}, found, fc_claude.claude_id(model), time.strftime("%Y-%m-%d %H:%M"))
        url = write_section(repo, ident, title, text)
        _outcome(repo, ident, "prepared")
        fc_factorystate.add_event(repo, f"event factory=prepared ticket={ident} questions={len(found['questions'])}  (ready for the next planning session: {_q(url)})")
        return {"ticket": ident, "status": "prepared", "questions": len(found["questions"])}
    except ERRORS as e:
        fc_factorystate.add_event(repo, f'event factory=prep-failed ticket={ident} why="{_q(str(e) or type(e).__name__, 120)}"')
        try:
            _outcome(repo, ident, "failed")
        except OSError:
            pass  # with no outcome the loop counts the worker as failed all the same
        return {"ticket": ident, "status": "failed"}

def refresh(repo, state, lines, now, out) -> None:
    """Keep the document true: drop the sections of tickets that moved on or became ready, and queue the ones whose text changed."""
    kinds = {m for line in lines for m in re.findall(r"factory=([\w-]+)", line)}
    prep = state.setdefault("prep", {})
    _settle(repo, prep, now)
    if not (kinds & set(REFRESH_ON)) and now - (state.get("prep_checked") or 0) < REFRESH_SECONDS:
        return
    state["prep_checked"] = now
    found = prd_found(state)
    for ident, rec in list(prep.items()):
        if _running(rec, now):
            continue
        try:
            if ident == fc_prd.PRD_ID:
                _refresh_prd(repo, prep, rec, found)
                continue
            issue = _issue(repo, ident)
            gone = ((issue.get("state") or {}).get("name") or "").lower() in GONE
            if not gone and rec.get("layer", "story") == "story":
                try:
                    gone = bool(fc_readiness.story_readiness(repo, ident, write=False).get("ready"))
                except ERRORS:
                    pass  # a judge error leaves the section as it is; the text check below still runs
            if gone:
                write_section(repo, ident, _line(issue.get("title") or "") or ident, None)
                del prep[ident]
            elif rec.get("print") is not None and prep_print(*_texts(repo, issue)) != rec["print"]:
                rec.update(print=None, gave_up=False, attempts=0)  # a changed ticket deserves a fresh try
        except ERRORS:
            continue
    try:  # a worker's late write can bring back a section refresh dropped: nothing in state names it, so it goes
        for ident in dict.fromkeys(re.findall(r"^## (PRD|[A-Z][A-Z0-9]*-\d+):", find_doc(repo)["content"], re.M)):
            if ident not in prep and (ident != fc_prd.PRD_ID or found is True):  # the PRD's section stays while the PRD is missing
                write_section(repo, ident, ident, None)
    except ERRORS:
        pass

def prd_found(state):
    """The factory's last PRD lookup: True, False, or None when it has none (night prep prepares the PRD only on False)."""
    return (state.get("prd") or {}).get("found") if isinstance(state.get("prd"), dict) else None

def _refresh_prd(repo, prep, rec, found) -> None:
    """The PRD's prep: dropped with its section once the PRD exists, queued again when the project's tickets changed."""
    if found is True:
        write_section(repo, fc_prd.PRD_ID, PRD_TITLE, None)
        del prep[fc_prd.PRD_ID]
    elif rec.get("print") is not None and fc_prd.ticket_print(repo) != rec["print"]:
        rec.update(print=None, gave_up=False, attempts=0)

def _settle(repo, prep, now) -> None:
    """Act on how each finished worker ended, on every tick: a cut prep is queued again at once, a failed one is tried once more
    half an hour after it began, and one that failed its retry is given up until its ticket changes."""
    for ident, rec in prep.items():
        if not rec.get("pid") or rec.get("print") is None:
            continue
        try:
            status = prep_status(repo, ident, rec, now)
        except OSError:
            continue  # an unreadable outcome file settles nothing: the next tick reads it again
        if status == "cut":
            rec["print"] = None
        elif status == "failed" and now - (rec.get("at") or 0) >= RETRY_SECONDS:
            rec["print"], rec["attempts"] = None, 1
        elif status == "gave-up":
            rec["gave_up"] = True

def prep_idle(repo, state, offer, now, out) -> None:
    """A night turn with nothing to start: refresh the document, then start one prep worker on the next unprepared ticket."""
    state.setdefault("said", {})
    refresh(repo, state, [], now, out)
    prep = state.setdefault("prep", {})
    night = state.get("night") or {}
    if any(_running(r, now) for r in prep.values()) or night.get("wait") or night.get("ended") or night.get("draining") not in (None, "budget"):
        return
    try:
        if prd_found(state) is False:  # the PRD first: every plan starts from it
            rec = prep.get(fc_prd.PRD_ID)
            if not (rec and (rec.get("print") is not None or rec.get("gave_up"))):
                fingerprint = fc_prd.ticket_print(repo)
                pid = fc_nightstate.spawn_once(repo, ["factory", "prep", "--repo", str(repo), "--ticket", fc_prd.PRD_ID], "night-prep.log")
                prep[fc_prd.PRD_ID] = {"pid": pid, "print": fingerprint, "at": now, "layer": "prd", "attempts": (rec or {}).get("attempts", 0)}
                return
        for c in candidates(repo, offer["id"], fc_config.FACTORY["prep_ahead"]) if offer else []:
            rec = prep.get(c["id"])
            if rec and (rec.get("print") is not None or rec.get("gave_up")):
                continue
            try:
                fingerprint = prep_print(*_texts(repo, _issue(repo, c["id"])))
            except ERRORS:
                continue
            pid = fc_nightstate.spawn_once(repo, ["factory", "prep", "--repo", str(repo), "--ticket", c["id"]], "night-prep.log")
            prep[c["id"]] = {"pid": pid, "print": fingerprint, "at": now, "layer": c["layer"], "attempts": (rec or {}).get("attempts", 0)}
            return
    except ERRORS as e:
        fc_factorystate.say_once(state, "prep-error", f'event factory=prep-failed why="{_q(str(e) or type(e).__name__, 120)}"  (night prep skips this turn)', out)

def prep_prd(repo, state, now, out) -> None:
    """A night turn that waits on the PRD with no delivery plan: prepare the PRD's questions."""
    prep_idle(repo, state, None, now, out)

fc_factoryturn.IDLE["night"] = prep_idle
fc_factoryturn.PRD_IDLE["night"] = prep_prd
