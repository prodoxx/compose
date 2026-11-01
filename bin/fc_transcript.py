import json, re
from pathlib import Path

# Reads a Claude Code session transcript (a JSONL file, one record per line) and shapes it: how large the context is, where it was
# compacted, what the product owner typed, how many exchanges happened since, and the message list the judge's snapshot is built from.
# Reading and shaping only - nothing here judges, sends, or touches a terminal.

USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
MACHINE_PREFIXES = ("<local-command", "<task-notification", "<system-reminder", "This session is being continued")


def transcript_path(cwd, session):
    """Where Claude Code keeps a session's transcript: one folder per working directory, named by the path with every non-alphanumeric turned to a dash."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{session}.jsonl"


def records(path):
    """One dict per parseable line, in file order; a file that cannot be read gives []."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _message(row):
    m = row.get("message")
    return m if isinstance(m, dict) else {}


def _usage_total(row):
    """The summed input tokens of an assistant record's usage, or None when the record has none."""
    usage = _message(row).get("usage")
    if not isinstance(usage, dict):
        return None
    return sum(v for v in (usage.get(k) for k in USAGE_KEYS) if isinstance(v, int) and not isinstance(v, bool))


def _main_assistant(row):
    return row.get("type") == "assistant" and not row.get("isSidechain")


def context_tokens(rows):
    """The context size at the last main-thread assistant record that reports usage, or None."""
    for row in reversed(rows):
        if _main_assistant(row):
            total = _usage_total(row)
            if total is not None:
                return total
    return None


def compactions(rows):
    out = []
    for i, row in enumerate(rows):
        if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            meta = row.get("compactMetadata") if isinstance(row.get("compactMetadata"), dict) else {}
            out.append({"at": row.get("timestamp"), "trigger": meta.get("trigger"), "pre": meta.get("preTokens"),
                        "post": meta.get("postTokens"), "duration_ms": meta.get("durationMs"), "index": i})
    return out


def _blocks(content):
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(content):
    """The text of a message body: the string itself, or its text blocks joined by newlines."""
    if isinstance(content, str):
        return content
    return "\n".join(b["text"] for b in _blocks(content) if b.get("type") == "text" and isinstance(b.get("text"), str))


def _has_text(content):
    return isinstance(content, str) or any(b.get("type") == "text" for b in _blocks(content))


def is_owner_prompt(row):
    """True for what the product owner typed: not a hook or meta record, not the compaction summary, not a tool result or notification."""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isCompactSummary"):
        return False
    content = _message(row).get("content")
    if not _has_text(content):
        return False
    origin = row.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    if "origin" in row:
        return False
    return not _text(content).lstrip().startswith(MACHINE_PREFIXES)


def owner_prompts(rows, after=None):
    """The owner's prompts as {at, text}, only those stamped after `after` (ISO strings compare as text)."""
    out = []
    for row in rows:
        if not is_owner_prompt(row):
            continue
        at = row.get("timestamp")
        if after is not None and not (isinstance(at, str) and at > after):
            continue
        out.append({"at": at, "text": _text(_message(row).get("content"))})
    return out


def _is_exchange_start(row):
    """A user record that opens an exchange: not a sidechain, meta or summary record, and not only tool results."""
    if row.get("type") != "user" or row.get("isSidechain") or row.get("isMeta") or row.get("isCompactSummary"):
        return False
    content = _message(row).get("content")
    return isinstance(content, str) or any(b.get("type") != "tool_result" for b in _blocks(content))


def exchanges_since_compaction(rows):
    """After the last compact boundary (or from the start), how many user records were answered by an assistant record."""
    marks = compactions(rows)
    start = marks[-1]["index"] + 1 if marks else 0
    count, waiting = 0, False
    for row in rows[start:]:
        if _is_exchange_start(row):
            waiting = True
        elif _main_assistant(row) and waiting:
            count += 1
            waiting = False
    return count


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _text(content)
    return None


def messages(rows):
    """The snapshot's message list: user and assistant turns with the tools the assistant used and what each returned.
    Assistant records that share a message.id are one response and merge; a tool result attaches to its tool use by id."""
    out, by_id, uses = [], {}, {}
    for row in rows:
        if row.get("isSidechain") or row.get("isMeta"):
            continue
        kind, content = row.get("type"), _message(row).get("content")
        if kind == "assistant":
            mid = _message(row).get("id")
            msg = by_id.get(mid) if mid is not None else None
            if msg is None:
                msg = {"role": "assistant", "text": "", "toolUses": []}
                out.append(msg)
                if mid is not None:
                    by_id[mid] = msg
            text = _text(content)
            if text:
                msg["text"] = f"{msg['text']}\n{text}" if msg["text"] else text
            for b in _blocks(content):
                if b.get("type") == "tool_use":
                    use = {"tool": b.get("name"), "input": b.get("input") if isinstance(b.get("input"), dict) else {}, "text": None, "isError": False}
                    msg["toolUses"].append(use)
                    if b.get("id") is not None:
                        uses[b["id"]] = use
        elif kind == "user":
            for b in _blocks(content):
                if b.get("type") == "tool_result" and b.get("tool_use_id") in uses:
                    uses[b["tool_use_id"]].update(text=_result_text(b.get("content")), isError=bool(b.get("is_error")))
            if _has_text(content) and _text(content):
                out.append({"role": "user", "text": _text(content), "toolUses": []})
    return out


def daily_input(rows):
    """ISO date to the input tokens summed over every main-thread assistant record stamped that day."""
    out = {}
    for row in rows:
        if not _main_assistant(row):
            continue
        total, at = _usage_total(row), row.get("timestamp")
        if total is None or not isinstance(at, str) or len(at) < 10:
            continue
        out[at[:10]] = out.get(at[:10], 0) + total
    return out
