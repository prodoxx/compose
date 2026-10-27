import json, re, sys, time, urllib.error, urllib.request
import fc_core, fc_config, fc_linear, fc_change


# ---------- one ask per question set: the link must answer, and a question is asked once ----------
SESSION_URL = re.compile(r"https?://[^\s'\"<>()]*?/session/[0-9A-Za-z]+")  # the form `lavish-axi <file>` prints as `url:`
ASK_FALLBACK = "You can also answer the visual questions in text, in this same message."
ASK_HEAD = re.compile(r"^(?:reminder:\s*)?(?:change (C\d+):\s*)?([A-Z][A-Z0-9]*-\d+):")

def url_problem(url):
    """Why a page link does not answer (empty when it does): the check behind "one working link, every time"."""
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=5) as r:  # a proxy never reaches a local page
            return "" if r.status < 400 else f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        e.close()
        return f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        return str(e)[:100]

CARD_ID = r"(?:[A-Za-z]{1,3}-?)?\d+(?:-\d+)?"  # `Q1`, `REC-19`, or `C1-8` (question 8 of change C1)
ID_PARTS = re.compile(r"((?:[A-Za-z]{1,3}-?)?)(\d+)(?:-(\d+))?")

def question_ids(message):
    """The question ids a `<n> questions <ids>` line names (the old `<n> cards <ids>` too): `Q1-Q3, W5` is Q1, Q2, Q3, W5; `C1-8` is one id;
    `C1-2..C1-4` is C1-2, C1-3, C1-4. A range is `Q1-Q3`, `Q1..Q3`, or `Q1 to Q3`. Empty when the line names none."""
    m = re.search(r"\b\d+\s+(?:questions?|cards?)\s+([^;]+)", message, re.I)
    ids = []
    for tok in re.split(r"[,&\s]+|\band\b", re.sub(r"\s*(?:\.\.|\bto\b|\s-\s)\s*", "~", m.group(1))) if m else []:
        tok = re.sub(r"^([A-Za-z]{1,3}-?)(\d+)-\1(\d+)$", r"\1\2~\3", tok)  # `Q1-Q3`
        lo, _, hi = tok.partition("~")
        a = ID_PARTS.fullmatch(lo)
        b = ID_PARTS.fullmatch(hi.removeprefix(a[1])) if a and hi else None  # the end may leave out the prefix: `Q1 to 3`
        if a and b and a[3] is None and b[3] is None:
            first, last, name = int(a[2]), int(b[2]), lambda n: f"{a[1]}{n}"
        elif a and b and a[3] and b[3] and a[2] == b[2]:  # `C1-2..C1-4` runs within one change
            first, last, name = int(a[3]), int(b[3]), lambda n: f"{a[1]}{a[2]}-{n}"
        else:
            first, last = 0, -1
        if 0 <= last - first < 100:
            ids += [name(n) for n in range(first, last + 1)]
        elif a and not hi:
            ids.append(tok)
    return list(dict.fromkeys(ids))

def map_bodies(issues):
    """issue -> description, for the issues Linear answers (best effort: no key or Linear down means none known)."""
    out = {}
    for issue in dict.fromkeys(issues if fc_config.LINEAR_KEY else []):
        try:
            out[issue] = (fc_linear.linear_gql("query($id: String!) { issue(id: $id) { description } }", {"id": issue})["issue"] or {}).get("description") or ""
        except Exception:  # noqa: BLE001  a Linear outage must not stop an ask
            continue
    return out

CLOSES = re.compile(r"\(closes ([^()]*)\)")

def closed_ids(line):
    """The question ids a decision line closed, from its `(closes Q3, Q4)` note."""
    return [i for m in CLOSES.finditer(line) for i in re.split(r"[,\s]+", m.group(1)) if i]

def open_ids_in(bodies):
    """Every question id open under Not yet specified on those maps."""
    return {cid for desc in bodies.values() for cid in questions_in(fc_change.map_section(desc, "Not yet specified"))}

def decided_ids(bodies, ids):
    """Which of `ids` a live line of those maps' Decisions so far already settles: an old line it heads, or a line whose `(closes ...)` names it."""
    out = set()
    for desc in bodies.values():
        lines = fc_change.decision_lines(desc)
        out |= {i for i in ids if any(re.match(rf"[\s*_`(\[]*{re.escape(i)}(?!\w)", l) or i in closed_ids(l) for l in lines)}
    return out


# ---------- an open question: one line under Not yet specified, asked open, with the recommendation an away answer takes ----------
QUESTION_FORMAT = "`- Q1: <question> - Recommendation: <recommendation> - <why it matters>`"
CARD_HEAD = re.compile(rf"^\s*(?:[-*]\s+|\d+[.)]\s+)?[*_`]*({CARD_ID})[*_`]*[.:)][*_`]*\s*(.*)")  # `- Q3: question`, old `Q1. question`, `- **Q1**: question`
MENU = re.compile(r"(?:^|[\s(:;,])\(?A[).]\s+\S.*[\s;,]\(?B[).]\s+\S")  # `A) x; B) y` in a question: a menu, where an open question belongs
INLINE_RECOMMENDATION = re.compile(r"\s+-\s+[*_`]*recommend(?:ation|ed)?[*_`]*\s*:[*_`]*\s*", re.I)
REPLACES = re.compile(rf"\((?:replaces|supersedes)\s+({CARD_ID})\)", re.I)  # a change-of-course question names the decision it would replace
# an old card block's lines: `A. <option> - affects: <what>`, `Why: ...`, `Recommendation: <letter>`, `Supersedes: ...`
OPTION = re.compile(r"^\s*(?:[-*]\s+)?[*_`]*([A-D])[*_`]*[.):][*_`]*\s+(.*\S)")
AFFECTS = re.compile(r"\baffects?\b[*_`]*\s*[:\-]?\s*(\S.*)", re.I)
WHY = re.compile(r"^\s*(?:[-*]\s+)?[*_`]*why\b[^:]*:\s*(\S.*)", re.I)
RECOMMENDATION = re.compile(r"^\s*(?:[-*]\s+)?[*_`]*recommend(?:ation|ed)?[*_`]*\s*[:\-]\s*[*_`]*([A-D])\b(.*)", re.I)
SUPERSEDES = re.compile(r"^\s*(?:[-*]\s+)?[*_`]*(?:supersedes|replaces)[*_`]*\s*:\s*(\S.*)", re.I)

def old_card(q, lines):
    """Fill an old card block's question from its lines: why, options, the recommended option, and the line a change card supersedes."""
    letter = ""
    for l in lines:
        if o := OPTION.match(l):
            text, aff = o.group(2), AFFECTS.search(o.group(2))
            q["options"][o.group(1)] = (text[:aff.start()].rstrip(" -:;,(") if aff else text, aff.group(1).strip(" )") if aff else "")
        elif r := RECOMMENDATION.match(l):
            letter = r.group(1).upper()
        elif w := WHY.match(l):
            q["why"] = w.group(1).strip()
        elif sup := SUPERSEDES.match(l):
            q["supersedes"] = sup.group(1).strip()
    q["recommendation"] = f"{letter}. {q['options'][letter][0]}" if letter in q["options"] else letter

def unwrapped(text):
    """`text` without the markdown marks left dangling at its ends (`**` or a backtick the recommendation regex cut from its pair);
    a pair that closes inside the text, like inline code ending the line, stays whole."""
    t = text.strip()
    while t and t[-1] in "*_`" and t.count(t[-1]) % 2:
        t = t[:-1].rstrip()
    while t and t[0] in "*_`" and t.count(t[0]) % 2:
        t = t[1:].lstrip()
    return t

def questions_in(section):
    """The open questions written in a map's Not yet specified, by id: question, recommendation, why, replaces (the decision a
    change-of-course question would replace), options and supersedes (an old card's letters and `Supersedes:` line), and span, the
    (start, end) of its lines in the section: its head and every line after it up to the next question head or the section's end.
    A question is one line, `- Q3: <question> - Recommendation: <recommendation> - <why it matters>`; an old card block (`Q1. <question>`
    / `Why:` / `A. <option> - affects: ...` / `Recommendation: <letter>`) still reads as one, its recommendation the option it names."""
    lines = section.splitlines()
    heads = [(i, m) for i, m in ((i, CARD_HEAD.match(l)) for i, l in enumerate(lines)) if m and not OPTION.match(lines[i])]
    out = {}
    for n, (i, m) in enumerate(heads):
        text, end = m.group(2).strip(), heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        q = {"question": text, "recommendation": "", "why": "", "replaces": "", "options": {}, "supersedes": "", "span": (i, end)}
        if r := INLINE_RECOMMENDATION.search(text):
            q["question"] = text[:r.start()].strip()
            rec, dash, why = text[r.end():].rpartition(" - ")  # the why follows the last dash: a recommendation may hold one
            q["recommendation"], q["why"] = unwrapped(rec if dash else why), why.strip() if dash else ""
        else:
            old_card(q, lines[i + 1:end])
        if p := REPLACES.search(q["question"]):
            q["replaces"] = p.group(1)
        out.setdefault(m.group(1), q)
    return out

def format_problems(bodies, ids):
    """Why the asked questions cannot go out as written on the maps: one missing from every Not yet specified, one with no recommendation,
    or one whose question lists lettered options (the product owner is asked open, brainstorming style; options sit in the recommendation).
    Nothing when no map was read (Linear down): the format is checked where it can be."""
    if not bodies:
        return []
    found = {}
    for desc in bodies.values():
        for cid, q in questions_in(fc_change.map_section(desc, "Not yet specified")).items():
            found.setdefault(cid, q)
    return [f"{i} is not written under Not yet specified on {', '.join(bodies)}" if i not in found else
            f"{i} has no recommendation" if not found[i]["recommendation"] else
            f"{i} lists lettered options: ask it open and put the options in its recommendation"
            for i in ids if i not in found or not found[i]["recommendation"] or MENU.search(found[i]["question"])]

class Refused(Exception):
    """A report that must not go out, but is no error of the reporter's command: it is told why, and the attempt is recorded."""

def ask_gate(proj, message, hop=""):
    """What `report needs-user` sends: the message (a verified link and the text fallback added, repeats marked). Exits with the reason
    when a page link is missing, doubled, or does not answer; raises Refused when a question is decided or asked already, or is not on the map as an open question with a recommendation. A question
    is asked once per `hop` (whom the reporter reports to): a teammate's ask to its team lead does not use up the team lead's ask to the engineering manager."""
    urls = list(dict.fromkeys(SESSION_URL.findall(message)))
    if re.search(r"open in Lavish", message, re.I) and not urls:
        sys.exit("not reported: a visual page needs its link in the message. Run `npx -y lavish-axi <file>` and put the `url:` it prints in the line.")
    if len({u.rsplit("/session/", 1)[1] for u in urls}) < len(urls):
        sys.exit(f"not reported: the page is given in {len(urls)} forms ({', '.join(urls)}). Give the one `url:` lavish-axi printed.")
    for u in urls:
        if why := url_problem(u):
            sys.exit(f"not reported: {u} does not answer ({why}). Reopen the page with `npx -y lavish-axi <file>`, use the `url:` it prints, and report again.")
    if urls and ASK_FALLBACK not in message:
        message += f" {ASK_FALLBACK}"
    ids = question_ids(message)
    if not ids:
        return message
    reminder = message.lower().startswith("reminder:")
    head = ASK_HEAD.match(message)
    scope = (f"{hop}@" if hop else "") + (f"{head[1] or ''}/{head[2]}" if head else "?")
    feature = (json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}).get("feature")
    bodies = map_bodies([x for x in (head[2] if head else None, feature) if x])
    decided = decided_ids(bodies, ids)
    if reused := [i for i in ids if i in decided and i in open_ids_in(bodies)]:
        raise Refused(f"not reported: renumber: {', '.join(reused)} was already used (a Decisions so far line closed it). Give the open "
                      "question a new id on the map, never reuse an id, then report again.")
    open_ids = [i for i in ids if i not in decided]
    if not open_ids:
        raise Refused(f"not reported: {', '.join(ids)} already in the map's Decisions so far. Build on the decisions; nothing is open.")
    if problems := format_problems(bodies, open_ids):
        raise Refused(f"not reported: the questions are not open questions on the map with a recommendation: {'; '.join(problems)}. Write each one "
                      f"under Not yet specified as {QUESTION_FORMAT}, then report again; the engineering manager reads them with `compose decisions`.")
    path = proj / "results" / "asked.json"
    asked = json.loads(path.read_text()) if path.exists() else {}
    key = lambda i: f"{scope}/{i}"
    old = [i for i in open_ids if key(i) in asked]
    if old and len(old) == len(open_ids):
        first = asked[key(old[0])]
        if not reminder or any(asked[key(i)].get("reminded") for i in old):
            raise Refused(f"not reported: {', '.join(old)} already asked at {first['at']} and not answered yet. Wait for the answer; do not ask again."
                          + (" (the one reminder is spent)" if reminder else " If the window closed, one `reminder: ` line may follow."))
        for i in old:
            asked[key(i)]["reminded"] = True
    for i in open_ids:
        asked.setdefault(key(i), {"at": time.strftime("%Y-%m-%d %H:%M:%S")})
    fc_core.atomic_write(path, json.dumps(asked, indent=1))
    return message
