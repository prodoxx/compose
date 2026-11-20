"""Where the sections of an epic, feature or story sit: what a product manager, designer or tester reads comes first, then a rule
and the planning and decision log that compose and its agents work from. Pure: a body in, a body out."""
import re

import fc_plan

LEVELS = {"epic", "feature", "story"}  # the tickets a person outside the build reads; a task's brief is for its implementer
# The note under the log's heading: who wrote what follows, who it is for, and that nobody edits it by hand.
DISCLAIMER = ("Everything a product manager, designer or tester needs is above this line. Below it is the planning and decision record "
              "that the product owner and compose wrote while planning this work. Compose and its agents read it and keep it up to date "
              "through compose's own commands, so please do not edit it by hand.")
READER = ("Intent", "Goals", "Non-goals", "Anti-goals", "Acceptance criteria")  # written for a reader who was not in the planning
WORKING = ("Constraints", "Decided by the product owner", "My calls", "Left to the builder", "Decisions so far", "Decisions",
           "Not yet specified", "Open questions", "Notes", "Plan", "Tech doc", "Evidence")  # what planning wrote and the build reads


def _named(names):
    return re.compile(rf"##[ \t]+(?:{'|'.join(map(re.escape, names))})[ \t]*(?::|\(.*\))?[ \t]*$", re.I)

IS_READER, IS_WORKING = _named(READER), _named(WORKING)


def _text(lines):
    return "\n".join(lines).strip("\n")


def _own_notes(chunk):
    """What a person wrote under the log's heading: its paragraphs other than the note compose keeps there."""
    paragraphs = re.split(r"\n[ \t]*\n", _text(chunk[1:]))
    return [p for p in paragraphs if p.strip() and not fc_plan.same_words(p, DISCLAIMER)]


def laid_out(body, labels=None):
    """The body with its sections on their side of the line: a working section (WORKING) found above goes below, a reader's section
    (READER) found below goes up, and any other section stays where it is. The rule, the log's heading and its note appear with the
    first working section. `labels` are the ticket's: only an epic, feature or story is laid out, and None says the caller knows it
    is one. A task, which carries a compose block, is never touched, and neither is a body Linear already stores this way."""
    body = body or ""
    lines = body.split("\n")
    if labels is not None and not LEVELS & {str(l).lower() for l in labels}:
        return body
    if fc_plan.compose_block_start(lines) is not None:
        return body
    marks = [i for i, n in fc_plan.headings(lines) if n <= 2]
    log = fc_plan.log_start(lines)
    head = lines[:min([*marks, len(lines)] if log is None else [*marks, log])]
    chunks = [lines[a:b] for a, b in zip(marks, [*marks[1:], len(lines)])]
    at = next((n for n, c in enumerate(chunks) if c[0].strip() == fc_plan.LOG_HEADING), None)
    above, below = (chunks, []) if at is None else (chunks[:at], chunks[at + 1:])
    if at is not None and above and log is not None and log < marks[at]:  # the rule above the log's heading is the log's, not the section's before it
        above[-1] = above[-1][:len(above[-1]) - (marks[at] - log)]
    top = [c for c in above if not IS_WORKING.match(c[0].strip())] + [c for c in below if IS_READER.match(c[0].strip())]
    bottom = [c for c in below if not IS_READER.match(c[0].strip())] + [c for c in above if IS_WORKING.match(c[0].strip())]
    own = _own_notes(chunks[at]) if at is not None else []
    parts = [_text(head), *map(_text, top)]
    if bottom or own:
        parts += [("---" if any(parts) else ""), fc_plan.LOG_HEADING, DISCLAIMER, *own, *map(_text, bottom)]
    new = "\n\n".join(p for p in parts if p) + "\n"
    return body if fc_plan.same_markdown(new, body) else new


def without_empty_log(body):
    """The body without its planning and decision log when the log holds nothing but compose's note."""
    lines = (body or "").split("\n")
    log = fc_plan.log_start(lines)
    at = next((i for i in range(log or 0, len(lines)) if lines[i].strip() == fc_plan.LOG_HEADING), None)
    if log is None or at is None or any(n <= 2 for i, n in fc_plan.headings(lines) if i > at) or _own_notes(lines[at:]):
        return body or ""
    return "\n".join(lines[:log]).rstrip("\n") + "\n"
