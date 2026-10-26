"""Night mode's one-time frontier answerer: it settles a team lead's question on an approved ticket while the product owner sleeps."""
from __future__ import annotations
import json, os, re, subprocess, time
from pathlib import Path

import fc_ask, fc_claude, fc_config, fc_core, fc_crew, fc_delivery, fc_factorystate, fc_factoryturn, fc_herdr, fc_models, fc_nightstate, fc_tasks, fc_yolo

ANSWERERS = 2
MORNING = ("shape ", "deep-dive ", "stories ")
ISSUE_RE = re.compile(r"\b([A-Z][A-Z0-9]*-\d+)\b")
NEVER_WORDS = re.compile("|".join((
    r"force[- ]?push|push\s+--force|--force\b|--delete-unmerged|--abandon",
    r"(?:delete|drop|wipe|truncate)\s+(?:the\s+)?(?:branch|database|table|data|bucket)",
    r"(?:add|create|rotate|new|generate|paste|use)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+)?(?:api[- ]?key|secret key|access token|credential)",
    r"(?:switch|change)\s+(?:the\s+)?(?:model|provider)",
    r"(?:buy|purchase|pay for|subscribe to|upgrade (?:the )?plan)",
    r"billing account|payment halt|toolsmith")), re.I)
ASK = ("Answer this question the team lead raised while building an approved ticket, as the product owner would from what is written here; "
       "read the code you need. If answering needs anything in the never-set above, or needs the product owner to plan something new, "
       'set never_set and answer nothing. Reply with only JSON: {"never_set": bool, "why_wait": str, "answer": str, "reasoning": str, '
       '"answers": {"<question id>": "<the decision, as a sentence>"}}; answers holds one entry per question id the team lead named, '
       "each recorded as a decision that closes that question.")

def answers_path(repo) -> Path:
    return fc_core.repo_link(repo).with_name("night-answers.jsonl")

def _records(repo) -> list:
    try:
        lines = answers_path(repo).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out

def _feature(proj) -> str:
    try:
        return str(json.loads((proj / "linear.json").read_text()).get("feature") or "")
    except (OSError, ValueError, AttributeError):
        return ""

def _issue(message, proj) -> str:
    """The issue a message names: only ids with the team prefix of the project's feature count (SHA-256 is not one); else the feature."""
    feature = _feature(proj)
    prefix = feature.rsplit("-", 1)[0] if "-" in feature else ""
    m = re.search(rf"\b({re.escape(prefix)}-\d+)\b", message) if prefix else ISSUE_RE.search(message)
    return m.group(1) if m else feature

def _questions(slug, proj) -> list[dict]:
    """Every needs-user report of the project's orchestrator; `nth` tells apart reports made in the same second."""
    try:
        reports = fc_crew.crew_reports(proj, fc_herdr.agent_name(slug, "orchestrator"))
    except (ValueError, OSError):  # a torn or unreadable status file
        return []
    out, seen = [], {}
    for r in reports:
        if not isinstance(r, dict) or r.get("state") != "needs-user" or not isinstance(r.get("at"), str):
            continue
        n = seen[r["at"]] = seen.get(r["at"], -1) + 1
        msg = str(r.get("message") or "")
        out.append({"key": f"{slug}@{r['at']}" + (f"#{n}" if n else ""), "project": slug, "at": r["at"], "nth": n, "message": msg, "issue": _issue(msg, proj)})
    return out

def new_questions(repo, night) -> list[dict]:
    """The orchestrators' needs-user reports since the night began that nobody has been asked or told about yet. A repeat of a
    question still in flight or already sent to the morning is left out; a key with a record is never asked again."""
    recorded = {r.get("key") for r in _records(repo)}
    held = {f"{a['project']}|{a['message']}" for a in night["asked"].values() if not a.get("done")} | set(night.get("morning_msgs") or [])
    out = []
    for slug, (_wt, proj) in fc_core.all_projects(repo).items():
        if (fc_tasks.yolo_state(proj) or {}).get("mode") != "night":
            continue
        for q in _questions(slug, proj):
            try:
                late = fc_core.report_time(q["at"]) < night["since"]
            except (ValueError, TypeError):
                continue
            same = f"{slug}|{q['message']}"
            if late or q["key"] in recorded or q["key"] in night["asked"] or q["key"] in night["morning"] or same in held:
                continue
            held.add(same)
            out.append(q)
    return out

def kind(message) -> str:
    return "morning" if str(message).strip().lower().startswith(MORNING) else "answer"

def answer_prompt(q, chain: list[tuple[str, str]], plan_doc: str, notes: str) -> str:
    tickets = "\n\n".join(f"{ident}:\n{text}" for ident, text in chain) or "(none)"
    return (f"Question from the team lead on {q['issue']}:\n{q['message']}\n\nThe ticket and its parents:\n{tickets}\n\n"
            f"The product delivery plan:\n{plan_doc or '(none)'}\n\nThe night's notes:\n{notes or '(none)'}\n\n"
            f"Never pre-authorizable: {fc_yolo.YOLO_NEVER}\n\n{ASK}")

def comment_body(q, rec, model, date) -> str:
    return (f"Answered while you were away, by the night answerer ({model}, {date}).\n\nQuestion: {q['message']}\nAnswer: {rec['answer']}\n"
            f"Why: {rec['reasoning']}\n\nTo confirm: this is the answerer's call, not yours; overruling it in the morning is an ordinary plan change.")

def _q(text, n=None) -> str:
    """A value for an event line: no double quotes, optionally cut to n characters."""
    return str(text).replace('"', "'")[:n]

def _failed(slug, message, why=None) -> str:
    if why:
        return (f'event factory=answer-failed project={slug} question="{_q(message)}" why="{_q(why, 120)}"  '
                "(the answer was not fully recorded: it waits for the product owner)")
    return (f'event factory=answer-failed project={slug} question="{_q(message)}"  '
            "(the answerer ended without an answer: it waits for the product owner)")

def _chain(repo, issue) -> list[tuple[str, str]]:
    chain, seen = [], set()
    while issue and issue not in seen and len(chain) < 4:
        seen.add(issue)
        try:
            data = json.loads(fc_factoryturn.compose_cli(["linear", "get", issue, "--json", "--repo", str(repo)]))
        except (RuntimeError, ValueError):
            break
        if not isinstance(data, dict):
            break
        chain.append((issue, f"{data.get('title') or ''}\n{data.get('description') or ''}".strip()))
        issue = (data.get("parent") or {}).get("identifier") if isinstance(data.get("parent"), dict) else None
    return chain

def parse_reply(reply) -> dict | None:
    """The answerer's verdict: any object in the reply that says never_set wins; else the last object with both answer and never_set."""
    objs, dec = [], json.JSONDecoder()
    for m in re.finditer(r"\{", reply):
        try:
            v = dec.raw_decode(reply[m.start():])[0]
        except ValueError:
            continue
        if isinstance(v, dict) and "never_set" in v:
            objs.append(v)
    stop = next((o for o in objs if o["never_set"]), None)
    return stop or next((o for o in reversed(objs) if "answer" in o), None)

def never_hits(*texts) -> list[str]:
    return sorted({m.group(0).lower() for t in texts for m in NEVER_WORDS.finditer(t)})

def run_answer(repo, slug, at, nth=0) -> dict:
    """The worker: answer one question, record it on the issue and in the lead, and return the record. Nothing is written once the night it
    was started under has ended."""
    since = fc_nightstate.night_since(repo)
    wt, proj = fc_core.all_projects(repo)[slug]
    q = next((x for x in _questions(slug, proj) if x["at"] == at and x["nth"] == nth), None)
    if q is None:
        raise RuntimeError(f"no report of {slug} at {at} (#{nth})")
    model, effort = fc_config.NIGHT_MODEL
    rec = {"key": q["key"], "project": slug, "issue": q["issue"], "question": q["message"], "answer": "", "reasoning": "", "answers": {},
           "never_set": False, "why_wait": "", "model": model, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "error": "", "commented": False, "acted": False}
    try:
        try:
            plan = fc_delivery.product_plan(repo)
            doc = ((plan or {}).get("doc") or {}).get("content") or ""
        except (RuntimeError, SystemExit):
            doc = ""
        prompt = answer_prompt(q, _chain(repo, q["issue"]), doc, fc_nightstate.notes_tail(repo))
        reply = fc_claude.claude_once(wt, prompt, timeout=1800, model=fc_claude.claude_id(model), tools="Read,Grep,Glob", effort=effort, keep=6000)[1]
        got = parse_reply(reply)
        if got is None:
            raise ValueError("no verdict with answer and never_set in the reply")
        rec.update(never_set=bool(got.get("never_set")), why_wait=str(got.get("why_wait") or ""), answer=str(got.get("answer") or ""),
                   reasoning=str(got.get("reasoning") or ""))
        answers = got.get("answers") if isinstance(got.get("answers"), dict) else {}
        rec["answers"] = {str(k): str(v).strip() for k, v in answers.items() if str(v).strip()
                          and re.fullmatch(fc_ask.CARD_ID, str(k)) and not re.fullmatch(r"D\d+", str(k)) and re.search(rf"(?<![\w-]){re.escape(str(k))}(?![\w-])", q["message"])}
        hits = never_hits(q["message"], rec["answer"], rec["reasoning"], *rec["answers"].values())
        if hits and not rec["never_set"]:
            rec.update(never_set=True, why_wait=f"mentions {', '.join(hits)}")
        if not rec["never_set"] and not rec["answers"] and not rec["answer"].strip():
            raise ValueError("the reply has no answer and no decision for a question of this report")
        if rec["never_set"]:
            rec.update(answer="", answers={})
        else:
            if not fc_nightstate.still_on(repo, since):
                raise RuntimeError("the night ended before the answer")
            fc_factoryturn.compose_cli(["linear", "comment", q["issue"], "--repo", str(repo)], stdin=comment_body(q, rec, model, time.strftime("%Y-%m-%d")))
            rec["commented"] = True
            if rec["answers"]:
                for qid, text in rec["answers"].items():  # one decide per question (--closes takes one --new), on the issue the question heads
                    fc_factoryturn.compose_cli(["decide", "--repo", str(repo), "--project", slug, "--new", text, "--closes", qid, "--issue", q["issue"]])
            elif not fc_factoryturn.prompt_lead(fc_herdr.agent_name(slug, "orchestrator"), f"night: {rec['answer']}"):
                raise RuntimeError("the team lead did not take the answer")
            rec["acted"] = True
    except (RuntimeError, ValueError, TypeError, OSError, subprocess.SubprocessError) as e:
        rec["error"] = str(e)[:300] or type(e).__name__
    _append(repo, rec)
    if rec["error"]:
        if rec["commented"]:
            fc_nightstate.add_note(repo, f"{slug} {q['issue']}: the answer is on the issue but was not fully acted on ({rec['error'][:120]})")
        fc_factorystate.add_event(repo, _failed(slug, q["message"], rec["error"]))
    elif rec["never_set"]:
        fc_nightstate.add_note(repo, f"{slug} {q['issue']}: never-set, waits for the product owner ({rec['why_wait']})")
        fc_factorystate.add_event(repo, f'event factory=waiting project={slug} question="{_q(q["message"])}" why="{_q(rec["why_wait"])}"  '
                                        "(never-set: it waits for the product owner; send one PushNotification naming it and the recommendation)")
    else:
        fc_nightstate.add_note(repo, f"{slug} {q['issue']}: answered {rec['answer'][:200]}")
        fc_factorystate.add_event(repo, f'event factory=answered project={slug} issue={q["issue"]} answer="{_q(rec["answer"], 120)}"  '
                                        "(the night answerer settled it; it is listed under To confirm in the morning)")
    return rec

def _append(repo, rec) -> None:
    path = answers_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as out:
        out.write(json.dumps(rec) + "\n")

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

def answer_step(repo, state, now) -> list[str]:
    night, lines = state["night"], []
    done = {r.get("key") for r in _records(repo)}
    for key, a in night["asked"].items():
        if not a.get("done") and not _alive(a["pid"]):
            a["done"] = True
            if key not in done:
                lines.append(_failed(a["project"], a["message"]))
    running = sum(1 for a in night["asked"].values() if not a.get("done"))
    held = night.setdefault("morning_msgs", [])
    answered = {(r.get("project"), r.get("question")) for r in _records(repo)}
    for q in new_questions(repo, night):
        mine = f"{q['project']}|{q['message']}"
        if kind(q["message"]) == "morning":
            night["morning"].append(q["key"])
            held.append(mine)
            lines.append(f'event factory=for-morning project={q["project"]} question="{_q(q["message"])}"  '
                         "(planning waits for the product owner: it is listed under Ready to plan)")
        elif (q["project"], q["message"]) in answered:
            night["morning"].append(q["key"])
            held.append(mine)
            lines.append(f'event factory=for-morning project={q["project"]} question="{_q(q["message"])}"  '
                         "(asked again after the night answer: it waits for the product owner)")
        elif running < ANSWERERS and not night.get("wait"):  # during a usage wait the question stays unasked until the reset
            args = ["factory", "answer", "--repo", str(repo), "--project", q["project"], "--at", q["at"], "--nth", str(q["nth"])]
            pid = fc_nightstate.spawn_once(repo, args, "night-answer.log")
            night["asked"][q["key"]] = {"pid": pid, "at": now, "project": q["project"], "issue": q["issue"], "message": q["message"]}
            running += 1
    return lines
