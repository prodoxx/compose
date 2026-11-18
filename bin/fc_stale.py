"""Lines on a ticket that stopped being true and that nobody came back to: the placeholder a ticket was created with, a readiness
check's question on a story readiness no longer judges, a tech doc written before a decision that is now live on its issue.
`spec-check` fails a story for the first two, readiness holds a story for the third, and `compose decide` says to reread the tech
doc as it records the decision that may have outdated it."""
import re
import fc_change, fc_linear, fc_tasks

READINESS_TAG = re.compile(r"\(readiness check, \d{4}-\d{2}-\d{2}\)")  # ends a question a readiness check wrote under Open questions
NOT_SHAPED = re.compile(r"(?i)^([ \t]*)Not shaped yet\b[.:]?[ \t]*")  # how a ticket made for a gap opens (sections/0k.md)
DEEP_DIVE_OWED = re.compile(r"(?i)^([ \t]*)Needs a deep dive\b[.:]?[ \t]*")
STAMP = re.compile(r"\((?:[^();]+; )?(?:product owner|[a-z ]+, unconfirmed), (\d{4}-\d{2}-\d{2})(?:; said: |\))")  # the tag `compose decide` ends a line with
ABOUT_DOC = re.compile(r"(?i)\btech doc\b.*\b(?:stands|approved)\b")


def _starts(description, pattern):
    return any(pattern.match(l) for l in (description or "").splitlines())


def shaped_text(description):
    """The description without the placeholder it was created with: `Not shaped yet` goes, and `Needs a deep dive` with it once a tech
    doc is linked under `## Tech doc`. A line that held nothing else goes whole; what else it said stays."""
    linked, out = bool(fc_change.map_section(description, "Tech doc")), []
    for line in (description or "").splitlines(keepends=True):
        new = NOT_SHAPED.sub(r"\1", line)
        if linked:
            new = DEEP_DIVE_OWED.sub(r"\1", new)
        if new == line or new.strip():
            out.append(new)
    return "".join(out)


def stale(issue):
    """What a ticket still says that its own state contradicts, as gaps for spec-check. `issue` is {"id", "description", "status"} and
    "labels" when they are known (a feature read from a sync carries none, and is judged by what it holds)."""
    desc, labels, status = issue.get("description") or "", issue.get("labels"), issue.get("status") or ""
    waits = labels is not None and fc_linear.NEEDS_SHAPING in [l.lower() for l in labels]
    shaped = bool(fc_tasks.parse_criteria(desc) or fc_change.map_section(desc, "Intent") or fc_change.map_section(desc, "Plan"))
    gaps = []
    if _starts(desc, NOT_SHAPED) and shaped and not waits:
        gaps.append('it still says "Not shaped yet" and it is shaped (it has criteria, an Intent or a Plan, and no `needs-shaping` label): remove that line')
    if any(DEEP_DIVE_OWED.match(NOT_SHAPED.sub(r"\1", l)) for l in desc.splitlines()) and fc_change.map_section(desc, "Tech doc") and not waits:
        gaps.append('it still says "Needs a deep dive" and its tech doc is linked under `## Tech doc`: remove that line (the `deep-dive` label stays: it is how the planner and readiness know to read the tech doc)')
    if status.lower() not in ("", "todo", "backlog"):
        for q in fc_linear.open_questions(desc):
            if READINESS_TAG.search(q):
                gaps.append(f'"{q}" is a readiness check\'s question, and readiness judges only a Todo or Backlog story while this one is {status}: '
                            "answer it (`compose decide`) or remove the line")
    return gaps


def _decisions(description):
    """[(id, date, text)] of the live decision lines that carry a `D<n>` id and a `compose decide` tag."""
    out = []
    for line in fc_change.decision_lines(description):
        ident, stamps = re.match(r"(D\d+):", line), STAMP.findall(line)
        if ident and stamps:
            out.append((ident.group(1), stamps[-1], line))
    return out


def tech_doc_behind(ident, description, docs):
    """The gap when `ident`'s tech doc was last written before a decision that is live on the issue, else None. A day is the finest a
    decision line records, so a decision of the day the doc was written counts as known to it. A later line that says the tech doc
    stands, or approves it, moves the mark: decisions up to that day are settled against the doc."""
    doc = next((d for d in docs if d.get("title") == f"{ident}: tech doc"), None)
    if not doc or not doc.get("updatedAt"):
        return None
    lines = _decisions(description)
    mark = max([doc["updatedAt"][:10]] + [date for _, date, text in lines if ABOUT_DOC.search(text)])
    newer = [(i, date) for i, date, text in lines if date > mark and not ABOUT_DOC.search(text)]
    if not newer:
        return None
    ids = ", ".join(i for i, _ in newer)
    return (f"{ident}'s tech doc was last written {doc['updatedAt'][:10]}, before {ids} ({max(d for _, d in newer)}): reread it against {ids}, then update it "
            f"(`compose linear doc update`) or record that it stands (`compose decide --issue {ident} --ruling --new \"the tech doc on {ident} stands after {newer[-1][0]}\"`).")


def reread_note(ident, description, recorded):
    """What `compose decide` says after it records `recorded` (its `D<n>: <decision>` lines) on an issue that links a tech doc: the
    moment the doc may stop being true. None when the issue has no tech doc, or every line is about the doc itself."""
    ids = [l.split(":")[0] for l in recorded if not ABOUT_DOC.search(l)]
    if not ids or not fc_change.map_section(description, "Tech doc"):
        return None
    return (f"{ident} has a tech doc, and {', '.join(ids)} may have outdated it: reread it now, then update it (`compose linear doc update`) or record "
            f"`compose decide --issue {ident} --ruling --new \"the tech doc on {ident} stands after {ids[-1]}\"`; readiness holds its stories until one is done")
