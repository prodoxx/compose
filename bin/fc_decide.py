"""The project's open questions, on one page (`compose decisions`), and the one way a decision is recorded (`compose decide`):
with the product owner's words attached, or as an unconfirmed recommendation while away or yolo is on."""
import json, re, sys, time
import fc_core, fc_tasks, fc_linear, fc_linear_cmd, fc_plan, fc_herdr, fc_change, fc_ask


# ---------- the map's two decision sections ----------
OPEN, DECIDED = "Not yet specified", "Decisions so far"
UNCONFIRMED = "({mode} recommendation, unconfirmed, {date})"  # the tag on a line the engineering manager decided for the product owner
CONFIRMED = '(product owner, {date}; said: "{quote}")'       # the tag on a line the product owner decided, their words attached
ANSWER = re.compile(rf"^\s*({fc_ask.CARD_ID}|D\d+)(?:\s*[:=.]\s*(.+?))?\s*$")  # a bare `D7` or `Q3` confirms a recorded line; the old `Q1: A` answers a card
TAG = re.compile(r"\s*\([^()]*\)\s*$")  # the tag a decision line ends with
APPROVAL = re.compile(r"^(?:the )?(?:design|stories|tech doc|PRD)\b[^.]{0,60}?\b(?:is |are )?approved\.?$", re.I)  # a go line the skill writes: refused while questions are open
KEEP = re.compile(r"^(?:keep\b|[A-Z]+\d+ stands\b)", re.I)  # a change-of-course answer that leaves the replaced line standing
NEW_FORM = '`compose decide --project {slug} --new "<decision>" --quote "<their words>" --closes <id>`'

def map_issue(proj, a):
    """The map whose questions are asked: --issue, else the project's feature."""
    if getattr(a, "issue", None):
        return a.issue
    meta = json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}
    if not meta.get("feature"):
        sys.exit(f"{proj.name} has no feature issue in linear.json; pass --issue <id>")
    return meta["feature"]

def read_map(issue):
    """(Linear id, identifier, description) of the map."""
    got = fc_linear.linear_gql("query($id: String!) { issue(id: $id) { id identifier description } }", {"id": issue})["issue"]
    if not got:
        sys.exit(f"no issue {issue} on Linear")
    return got["id"], got["identifier"], got.get("description") or ""

def heading_line(body, name):
    """The map's heading line for the section, as written (`## Not yet specified`), or the plain `## <name>` when the map has none yet."""
    m = re.search(rf"^(#+[ \t]*{re.escape(name)}[^\n]*)$", body, re.M)
    return m.group(1) if m else f"## {name}"

def asked_at(proj):
    """question id -> when it was first asked, from results/asked.json, whatever the hop and issue."""
    path = proj / "results" / "asked.json"
    asked = json.loads(path.read_text()) if path.exists() else {}
    out = {}
    for key, rec in asked.items():
        out.setdefault(key.rsplit("/", 1)[-1], rec["at"])
    return out

def decided_line(body, cid):
    """The live Decisions so far line the id heads, or None."""
    return next((l for l in fc_change.decision_lines(body) if re.match(rf"[\s*_`(\[]*{re.escape(cid)}(?!\w)", l)), None)


def open_questions(body):
    """The map's open questions, by id (fc_ask.questions_in on its Not yet specified)."""
    return fc_ask.questions_in(fc_change.map_section(body, OPEN))

def open_left(ident, body):
    """What every successful decide prints: the open questions left on the map, ids and text, or that none is."""
    qs = open_questions(body)
    return "\n".join([f"open questions on {ident} ({len(qs)}):"] + [f"- {cid}: {q['question']}" for cid, q in qs.items()]) if qs else f"no open questions on {ident}"


# ---------- `compose decisions`: the page the engineering manager reads the open questions from ----------
def question_text(cid, q, asked):
    """One open question as the page shows it: id, question, when it was asked, the recommendation, and why it matters."""
    out = [f"{cid}: {q['question']}" + (f"  (asked {asked[cid]})" if cid in asked else "  (not asked yet)"),
           f"   Recommendation: {q['recommendation'] or '(none)'}"]
    return "\n".join(out + ([f"   Why: {q['why']}"] if q["why"] else []))

def decisions_page(proj, ident, body, asked):
    """The project's open questions with their recommendations and when each was asked, then the decisions still to confirm."""
    qs = open_questions(body)
    unconfirmed = [l for l in fc_change.decision_lines(body) if "unconfirmed" in l]
    out = [f"{proj.name}: {len(qs)} open question(s) on {ident}" + (f", {len(unconfirmed)} decision(s) to confirm" if unconfirmed else "")]
    out += [x for cid, q in qs.items() for x in ("", question_text(cid, q, asked))] or ["", "no open questions: Not yet specified is empty"]
    if unconfirmed:
        out += ["", f"Decisions to confirm ({len(unconfirmed)}):"] + [f"- {l}" for l in unconfirmed]
    if qs:
        out += ["", f"record each answer with {NEW_FORM.format(slug=proj.name)}, `--drop <id>` for a question the answers made moot, "
                    "or `--accept-all` for every recommendation"]
    if unconfirmed:
        out += [f"confirm a decision with `compose decide --project {proj.name} --quote \"<their words>\" <id>`"]
    return "\n".join(out)

def cmd_decisions(a):
    """Print the project's open questions with their recommendations and when each was asked, then the decisions still to confirm."""
    proj = fc_core.project_dir(a)
    issue = map_issue(proj, a)
    _, ident, body = read_map(issue)
    print(decisions_page(proj, ident, body, asked_at(proj)))


# ---------- `compose decide`: a decision lands on the map only with the product owner's words, or tagged unconfirmed under away/yolo ----------
def parse_answers(words):
    """Bare ids to confirm, or the old `Q1: A` card answers -> [(id, answer)], the answer None for a bare id; a word that is neither is refused."""
    out = []
    for w in words:
        m = ANSWER.match(w)
        if not m:
            sys.exit(f'not recorded: {w!r} is not an answer; confirm a recorded line with its bare id ("D7"), and record an answer with --new "<decision>" --closes <id>')
        out.append((m.group(1), m.group(2)))
    return out

def answer_text(cid, q, answer):
    """The old answer's text: a letter becomes `A. <option>` (plus anything after it); other words stay as given."""
    m = re.match(r"^([A-Da-d])\b[.)]?\s*(.*)$", answer)
    if m and m.group(1).upper() in q["options"]:
        letter = m.group(1).upper()
        rider = m.group(2).strip(" ,;:-")
        return f"{letter}. {q['options'][letter][0]}" + (f" ({rider})" if rider else "")
    if m and not m.group(2).strip():
        sys.exit(f"not recorded: {cid} has no option {m.group(1).upper()}; it has {', '.join(sorted(q['options'])) or 'none'}")
    return answer

def close_questions(body, ids):
    """The map with those questions' lines (a whole old card block) gone from Not yet specified; an emptied section keeps its heading."""
    qs = open_questions(body)
    gone = {n for cid in ids for n in range(*qs[cid]["span"])}
    lines = fc_change.map_section(body, OPEN).splitlines()
    kept = re.sub(r"\n{3,}", "\n\n", "\n".join(l for i, l in enumerate(lines) if i not in gone)).strip()
    return fc_plan.replace_section(body, heading_line(body, OPEN), kept or heading_line(body, OPEN))

def superseded(live, q):
    """The live Decisions so far line an old change card's `Supersedes:` names, when the map still has it."""
    want = " ".join(q["supersedes"].split()).strip("~ ")
    return next((l for l in live if want and (want in l or " ".join(l.split()).startswith(want))), None)

def record(body, answers, tag, change=None):
    """The map with each bare id's unconfirmed line rewritten under the tag (a confirmed one refuses: a change goes through
    `compose change`), and each old `Q1: A` answer moved from Not yet specified to a Decisions so far line `<id>: <text> <tag>`.
    Under a change of course an old card's `Supersedes:` line is struck through, `~~<old>~~ (<change>)`, unless the answer keeps it."""
    qs = open_questions(body)
    dec = fc_change.map_section(body, DECIDED).splitlines()  # every line of the section, struck ones included
    live = lambda: {m.group(1): i for i, m in enumerate(map(fc_change.LIST_ITEM.match, dec)) if m and "~~" not in m.group(1)}
    out, closed = [], []
    for cid, answer in answers:
        old = decided_line(body, cid)
        if old and "unconfirmed" not in old:
            sys.exit(f"not recorded: {cid} is already decided ({old}); a change of a decision goes through `compose change start`")
        if cid not in qs or answer is None:
            if not old and cid in qs:
                sys.exit(f'not recorded: {cid} is an open question; record its answer with --new "<decision>" --closes {cid}')
            if not old:
                sys.exit(f"not recorded: no question {cid} under Not yet specified and no line {cid} to confirm; the open questions are {', '.join(qs) or 'none'}")
            kept = TAG.sub("", re.sub(rf"^[\s*_`(\[]*{re.escape(cid)}[\s*_`)\]]*[:=.]?\s*", "", old))
            line = f"{cid}: {answer if answer is not None else kept} {tag}"
            dec[live()[old]] = f"- {line}"
        else:
            text = answer_text(cid, qs[cid], answer)
            line = f"{cid}: {text} {tag}"
            if old:
                dec[live()[old]] = f"- {line}"
            else:
                dec.append(f"- {line}")
            if change and (gone := superseded(live(), qs[cid])) and not KEEP.match(text.split(". ", 1)[-1]):
                dec[live()[gone]] = f"- ~~{gone}~~ ({change})"
            closed.append(cid)
        out.append(line)
    body = fc_plan.replace_section(body, heading_line(body, DECIDED), "\n".join(dec))
    return (close_questions(body, closed) if closed else body), out

def replace_decision(body, old_id, note):
    """The map with the live Decisions so far line `old_id` heads struck through, `~~<old text and tag>~~ (<note>)`."""
    old = decided_line(body, old_id)
    dec = fc_change.map_section(body, DECIDED).splitlines()
    for i, m in enumerate(map(fc_change.LIST_ITEM.match, dec)):
        if m and m.group(1) == old:
            dec[i] = f"- ~~{old}~~ ({note})"
    return fc_plan.replace_section(body, heading_line(body, DECIDED), "\n".join(dec))

def record_new(body, items, tag):
    """The map with one `- D<n>: <text> (closes <ids>) <tag>` line appended under Decisions so far per (text, closed ids), and the closed
    questions gone from Not yet specified; <n> runs on from the highest D id the section holds, struck lines included. Returns (body, the lines written)."""
    dec = fc_change.map_section(body, DECIDED).splitlines()
    top = max((int(n) for n in re.findall(r"^\s*[-*]\s+(?:~~)?D(\d+)\b", "\n".join(dec), re.M)), default=0)  # Linear saves `- ` items as `* `
    lines = [f"D{top + i}: {' '.join(t.split())}" + (f" (closes {', '.join(ids)})" if ids else "") + f" {tag}" for i, (t, ids) in enumerate(items, 1)]
    dec = [l for l in dec if l.strip()] + [f"- {l}" for l in lines]
    body = fc_plan.replace_section(body, heading_line(body, DECIDED), "\n".join(dec))
    return close_questions(body, [i for _, ids in items for i in ids]), lines

def check_replaces(body, ident, replaces, change, quote):
    """Refuse a --replaces that names no live line, that comes after stories exist outside a change of course, or that rewrites a
    confirmed line without the product owner's words."""
    old = decided_line(body, replaces)
    if not old:
        sys.exit(f"not recorded: {replaces} is not a live decision on {ident}; --replaces names a line that is not struck through")
    if not change and [c for c in fc_linear.linear_children(ident) if ((c.get("state") or {}).get("name") or "").lower() not in fc_tasks.CANCELED]:
        sys.exit(f"not recorded: {ident} already has stories or tasks, so {replaces} changes through a change of course: compose change start")
    if not quote and "unconfirmed" not in old:
        sys.exit(f'not recorded: {replaces} is confirmed; replacing it needs the product owner\'s words: add --quote "<their message, verbatim>"')

def cmd_decide(a):
    """Record the product owner's answers on the map, their words attached (`--quote`), and tell the team lead. Each answer is a
    `--new` decision line, `--closes` naming the open questions it settles; `--drop` closes a question the answers made moot. Without
    --quote it refuses, unless away or yolo is on: then the line lands tagged unconfirmed and is logged for the digest. An approval
    is refused while questions are open. With --change <id>, the lines carry the change's id and --replaces strikes the old line with it."""
    proj = fc_core.project_dir(a)
    quote = " ".join((a.quote or "").split())
    posture = fc_tasks.yolo_state(proj)
    if not quote and not posture:
        sys.exit('not recorded: a decision is recorded only with the product owner\'s words: add --quote "<their message, verbatim>". '
                 "While away or yolo is on, `compose decide` without --quote records the recommendation as unconfirmed.")
    news = [t for t in (getattr(a, "new", None) or []) if t.strip()]
    closes, drops = list(getattr(a, "closes", None) or []), list(getattr(a, "drop", None) or [])
    replaces, change = getattr(a, "replaces", None), getattr(a, "change", None)
    if replaces and (len(news) != 1 or not re.fullmatch(fc_ask.CARD_ID, replaces)):
        sys.exit("not recorded: --replaces <id> goes with exactly one --new \"<decision>\" that takes its place")
    if closes and len(news) != 1:
        sys.exit("not recorded: --closes goes with exactly one --new \"<decision>\", the answer that settles those questions")
    if not a.answers and not a.accept_all and not news and not drops:
        sys.exit('decide needs --new "<decision>" (with --closes <id> for the question it answers), --drop <id>, --accept-all, or a bare id to confirm')
    answers = parse_answers(a.answers or [])
    named = closes + drops + [cid for cid, _ in answers]
    if twice := list(dict.fromkeys(i for i in named if named.count(i) > 1)):
        sys.exit(f"not recorded: {', '.join(twice)} {'is' if len(twice) == 1 else 'are'} named more than once across --closes, --drop and the "
                 "answers; record each question once")
    if not quote and any(ans is None for _, ans in answers):
        sys.exit('not recorded: confirming a recorded decision needs the product owner\'s words: add --quote "<their message, verbatim>"')
    lid, ident, body = read_map(map_issue(proj, a))
    qs = open_questions(body)
    if bad := [i for i in closes + drops if i not in qs]:
        sys.exit(f"not recorded: {', '.join(bad)} {'is' if len(bad) == 1 else 'are'} not an open question on {ident}; the open ones are {', '.join(qs) or 'none'}")
    if replaces:
        check_replaces(body, ident, replaces, change, quote)
    answered = set(closes + drops) | {cid for cid, ans in answers if ans is not None}
    accepted = [cid for cid in qs if cid not in answered] if a.accept_all else []  # answers given win
    if missing := [cid for cid in accepted if not qs[cid]["recommendation"]]:
        sys.exit(f"not recorded: --accept-all needs a recommendation on every open question; {', '.join(missing)} carry none")
    if (left := [cid for cid in qs if cid not in answered and cid not in accepted]) and any(APPROVAL.search(t) for t in news):
        sys.exit(f"not recorded: {ident} still has open questions: {', '.join(left)}. Ask them first and record each answer with "
                 f"--new \"<decision>\" --quote \"<their words>\" --closes <id> (or --drop <id> for one the answers made moot), then the approval.")
    date = time.strftime("%Y-%m-%d")
    tag = CONFIRMED.format(date=date, quote=quote.replace('"', "'")) if quote else UNCONFIRMED.format(mode=posture["mode"], date=date)
    if change:
        tag = f"({change}; " + tag[1:]
    new_body, lines = record(body, answers, tag, change) if answers else (body, [])
    items = ([(t, closes if n == 0 else []) for n, t in enumerate(news)]
             + [(f"no longer applies: {fc_ask.REPLACES.sub('', qs[i]['question']).strip()}", [i]) for i in drops]
             + [(qs[cid]["recommendation"], [cid]) for cid in accepted])
    new_body, added = record_new(new_body, items, tag) if items else (new_body, [])
    lines += added
    if replaces:
        new_body = replace_decision(new_body, replaces, change or f"replaced by {added[0].split(':')[0]}")
    for cid in accepted:  # an accepted change-of-course question strikes what it replaces, as --replaces with its change does
        if (rid := qs[cid]["replaces"]) and decided_line(new_body, rid) and not KEEP.match(qs[cid]["recommendation"].split(". ", 1)[-1]):
            by = next(l.split(":")[0] for l in added if f"(closes {cid})" in l)
            new_body = replace_decision(new_body, rid, change or (cid.split("-")[0] if re.fullmatch(r"C\d+-\d+", cid) else f"replaced by {by}"))
    fc_linear.linear_gql("mutation($id: String!, $in: IssueUpdateInput!) { issueUpdate(id: $id, input: $in) { success } }", {"id": lid, "in": {"description": new_body}})
    fc_linear_cmd.await_write(lambda: fc_linear.linear_gql("query($id: String!) { issue(id: $id) { description } }", {"id": lid})["issue"]["description"], body, new_body)
    if posture and not quote:
        posture["decisions"] += [{"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": l} for l in lines]
        fc_core.atomic_write(proj / "results" / "yolo.json", json.dumps(posture, indent=2))
    for l in lines:
        print(f"recorded on {ident}: {l}")
    if any(ans is not None for _, ans in answers):
        print("note: the new form is " + NEW_FORM.format(slug=proj.name))
    print(open_left(ident, new_body))
    lead = fc_herdr.agent_name(proj.name, "orchestrator")
    note = (f"recorded on {ident}: {', '.join(l.split(':')[0] for l in lines)}; read Decisions so far. While shaping, wait for `shaped <id>`; "
            "a `stories <id> approved` line is the planner's go (tell the planner, or the change architect, if live)")
    if fc_herdr.herdr_env() and fc_herdr.herdr("agent", "prompt", lead, note) is not None:
        print(f"told {lead}")
    else:
        print(f"tell the team lead: herdr agent prompt {lead} \"{note}\"")
