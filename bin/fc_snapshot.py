import json, re
from collections import namedtuple

# The compact-adviser snapshot builder (packages/claude-mod/lib/snapshot.ts), ported as pure functions: a message list in, the bounded
# text-only state the judge reads out - the owner's asks, the recent tail, the prior summary, saved artifact names and coverage markers.
# Keep it in step with snapshot.ts: every budget, pattern and field name here is the TypeScript's, so the judge sees what the mod's judge sees.
# Helpers answer a tuple where the TypeScript answers {text, truncated} or {text, redacted}.

# `messages()` answers at most this many in the mod; a full answer means older ones exist.
MESSAGE_LIMIT = 4096
# Recent messages considered for the snapshot, including assistant tool results.
RECENT_TAIL_MESSAGES = 64
# Per-tool-result byte cap inside the recent tail; long results are middle-truncated.
TOOL_RESULT_BUDGET = 512
SUMMARY_PREFIX = "This session is being continued from a previous conversation"
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash"}  # Claude Code's shell tool, whose `command` input may write files itself
DESCRIPTION = "Lossy structured summary of older context (request, files, errors, pending tasks, current work, next step) plus a few recent messages. Exact tool output and long file contents are compressed away. Other compaction hooks may differ."

# JavaScript's \s and String.trim() set, so the patterns and trims below split text where the TypeScript does.
SPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_WS = "".join(re.escape(c) for c in SPACE)
_SPACE_RE = re.compile("[" + _WS + "]")
_NOT_END = r"[^\n\r\u2028\u2029]"  # JavaScript's `.`

SENSITIVE_PATH = re.compile(r"(?:^|[\\/])(?:\.env(?:\.[^\\/]*)?|auth\.json|id_(?:rsa|ed25519)|[^\\/]*\.(?:pem|key))\Z", re.I | re.A)
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|\Z)")
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{15,}|Bearer[" + _WS + r"]+[^" + _WS + r"]+)", re.I | re.A)
_ASSIGNED = re.compile(r"\b([A-Z_]*(?:API_KEY|TOKEN|SECRET|PASSWORD))[" + _WS + r"]*[=:][" + _WS + r"]*[\"']?[^" + _WS + "\"',}]+", re.A)
_KEY_JSON = re.compile(r'("(?:[^"\\]*\.)?typesafeApiKey")[' + _WS + r']*:[' + _WS + r']*"(?:\\' + _NOT_END + r'|[^"\\])*"', re.A)
_KEY_ASSIGNED = re.compile(r"\b(typesafeApiKey)[" + _WS + r"]*[=:][" + _WS + r"]*[\"']?[^" + _WS + "\"',}]+", re.A)


# ---- shell command parsing: which files one command line writes ----

ShellWord = namedtuple("ShellWord", "text quoted")  # quoted: some part came from quotes or an escape, so spaces are literal characters
Token = namedtuple("Token", "kind word text io")  # kind "word" carries word; kind "op" carries text and io (digits attached before it: an fd)

SHELL_OPERATORS = ("((", ";;&", ";;", ";&", "||", "&&", "|&", "<<-", "<<<", "<<", "<>", ">&", "<&", ">|", ">>", "<", ">", "|", "&", ";", "(", ")")
SHELL_CONTROL_OPS = {"((", ";;&", ";;", ";&", "||", "&&", "|&", "|", "&", ";", "(", ")"}
SHELL_PREFIX_WORDS = {"sudo", "command", "exec", "nohup", "time", "env", "nice", "stdbuf", "xargs"}  # may stand before a command name without hiding it
SHELL_DEV_PATHS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin"}
SHELL_COND_INTRO_WORDS = {"if", "then", "else", "elif", "while", "until", "do", "done", "fi", "{", "}", "!"}  # `[[` after them still opens a conditional
# Reserved words that may stand directly before a command name. `if`, `while` and `until` stay out as a deliberate conservative miss.
SHELL_CMD_INTRO_WORDS = {"then", "do", "else", "elif", "!", "{"}
_UNEXPANDED = re.compile(r"""[$`*?{}\[\]()<>;&'"|\\~]""")  # the shell would have expanded these; the text names no single file
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _operator_at(line, at):
    for op in SHELL_OPERATORS:
        if line.startswith(op, at):
            return op
    return None


def _tokenize_line(line, carry=None):
    """Shell-lex one line into words and operators; quoted text stays literal. A quote the line never closes comes back as `carry`, fed back with the next line."""
    tokens = carry["tokens"] if carry else []
    st = {"word": "", "quoted": False, "has": False, "open": None}

    def flush():
        if st["has"]:
            tokens.append(Token("word", ShellWord(st["word"], st["quoted"]), None, None))
        st["word"], st["quoted"], st["has"] = "", False, False

    def read_quoted(quote, start):
        j = start
        while j < len(line):
            if quote == '"' and line[j] == "\\" and j + 1 < len(line):
                st["word"] += line[j + 1]
                j += 2
                continue
            if line[j] == quote:
                return j + 1
            st["word"] += line[j]
            j += 1
        st["open"] = quote
        return j

    i = 0
    if carry:
        st["word"], st["quoted"], st["has"] = carry["word"] + "\n", True, True
        i = read_quoted(carry["quote"], 0)
    while i < len(line):
        ch = line[i]
        if ch in ("'", '"'):
            i = read_quoted(ch, i + 1)
            st["quoted"] = st["has"] = True
            continue
        if ch == "\\" and i + 1 < len(line):
            st["word"] += line[i + 1]
            st["quoted"] = st["has"] = True
            i += 2
            continue
        if ch in SPACE:
            flush()
            i += 1
            continue
        if ch == "#" and not st["has"]:
            break
        op = _operator_at(line, i)
        if op:
            # Digits attached directly to a redirection are its fd, not a word.
            io = st["word"] if st["has"] and op not in SHELL_CONTROL_OPS and re.fullmatch("[0-9]+", st["word"]) else None
            if io is not None:
                st["has"] = False
            flush()
            tokens.append(Token("op", None, op, io))
            i += len(op)
            continue
        st["word"] += ch
        st["has"] = True
        i += 1
    if st["open"]:
        return tokens, {"quote": st["open"], "word": st["word"], "tokens": tokens}
    flush()
    return tokens, None


def _add_written_path(word, paths):
    """A word becomes a written path only when it confidently names one real file."""
    path = word.text
    if not path or path in (".", "..") or path.startswith("-"):
        return
    if path in SHELL_DEV_PATHS or path.startswith("/dev/fd/"):
        return
    if _UNEXPANDED.search(path):
        return
    if not word.quoted and _SPACE_RE.search(path):
        return
    paths.append(path)


def _tee_targets(args, paths):
    for arg in args:
        if arg.text.startswith("-"):
            continue
        _add_written_path(arg, paths)


def _sed_targets(args, paths):
    in_place = script_given = suffix_ambiguous = bare_in_place = False
    i = 0
    while i < len(args):
        text = args[i].text
        after_bare = bare_in_place
        bare_in_place = False
        if after_bare and (text == "" or not text.startswith("-")):
            suffix_ambiguous = text != ""
            i += 1
            continue
        if text == "--":
            i += 1
            break
        if not text.startswith("-") or text == "-":
            break
        if not text.startswith("--"):
            consumes_next = False
            for k in range(1, len(text)):
                letter, rest = text[k], text[k + 1:]
                if letter in ("e", "f"):
                    script_given = True
                    consumes_next = rest == ""
                    break
                if letter == "i":
                    in_place = True
                    bare_in_place = rest == ""
                    break
                if letter == "l":
                    consumes_next = rest == ""
                    break
            i += 2 if consumes_next and i + 1 < len(args) else 1
            continue
        if text == "--in-place" or text.startswith("--in-place="):
            in_place = True
        elif text in ("--expression", "--file"):
            script_given = True
            if i + 1 < len(args):
                i += 1
        elif text.startswith("--expression=") or text.startswith("--file="):
            script_given = True
        elif text == "--line-length":
            if i + 1 < len(args):
                i += 1
        i += 1
    if not in_place:
        return
    # A non-empty word after a bare -i is a BSD suffix or a GNU script; without -e/-f, undecidable.
    if suffix_ambiguous and not script_given:
        return
    operands = [a for a in args[i:] if a.text != ""]
    # Without -e/-f the first operand is the sed script; with them, all are files.
    for f in (operands if script_given else operands[1:]):
        _add_written_path(f, paths)


def _match_writers(words, paths):
    start = 0
    while start < len(words):
        text = words[start].text
        if text in SHELL_PREFIX_WORDS or text in SHELL_CMD_INTRO_WORDS or _ASSIGNMENT.match(text):
            start += 1
            continue
        break
    first = words[start].text if start < len(words) else None
    if first == "tee":
        _tee_targets(words[start + 1:], paths)
    elif first == "sed":
        _sed_targets(words[start + 1:], paths)


def _collect_written_paths(tokens, paths, heredocs):
    segment = []
    cond = arith = suppress = False
    arith_depth = 0
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.kind == "word":
            text = token.word.text
            if not cond and text == "[[" and (not segment or all(w.text in SHELL_COND_INTRO_WORDS for w in segment)):
                cond = suppress = True
            elif cond and text == "]]":
                cond = False
            segment.append(token.word)
            i += 1
            continue
        if token.text in SHELL_CONTROL_OPS:
            if token.text == "((" and not arith:
                arith = True
            elif token.text == "(" and arith:
                arith_depth += 1
            elif token.text == ")" and arith:
                if arith_depth > 0:
                    arith_depth -= 1
                else:
                    arith = False
            if not suppress:
                _match_writers(segment, paths)
            segment = []
            suppress = cond or arith
            i += 1
            continue
        target = tokens[i + 1] if i + 1 < len(tokens) else None
        target_word = target.word if target is not None and target.kind == "word" else None
        if token.text in ("<<", "<<-"):
            if target_word:
                if target_word.text:
                    heredocs.append({"delimiter": target_word.text, "dashed": token.text == "<<-"})
                i += 1
            i += 1
            continue
        if not cond and not arith and token.text in (">", ">>", ">|") and (token.io is None or token.io == "1") and target_word:
            _add_written_path(target_word, paths)
        if target_word:
            i += 1
        i += 1
    if not suppress:
        _match_writers(segment, paths)


def shell_written_paths(command):
    """The files one shell command line writes through output redirection (>, >> and >|), tee, or in-place sed. The command text is data - nothing is executed or expanded.
    Parsing is conservative: heredoc bodies never yield a path, > and < inside [[ ]] and (( )) are comparisons, an expanded or globbed word names no file, and any construct the parser cannot read with confidence yields nothing."""
    paths, heredocs, carry = [], [], None
    for line in command.split("\n"):
        if heredocs:
            candidate = line.lstrip("\t") if heredocs[0]["dashed"] else line
            if candidate == heredocs[0]["delimiter"]:
                heredocs.pop(0)
            continue
        tokens, carry = _tokenize_line(line, carry)
        if carry:
            continue
        _collect_written_paths(tokens, paths, heredocs)
    return paths


# ---- clipping and redaction ----

def _encode(text):
    """UTF-8 bytes the way TextEncoder makes them: a lone surrogate becomes U+FFFD."""
    return re.sub("[\ud800-\udfff]", "\ufffd", text).encode("utf-8")


def _bytes(text):
    return len(_encode(text))


def _utf16_len(text):
    """String.length: UTF-16 code units, so a character outside the BMP counts twice."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def clip(text, limit):
    """(text cut to at most `limit` UTF-8 bytes, whether it was cut)."""
    raw = _encode(text)
    if len(raw) <= limit:
        return text, False
    cut = raw[:max(0, limit - 3)].decode("utf-8", "replace")
    # A multi-byte character split at the cut decodes as U+FFFD; drop it.
    return (cut[:-1] if cut.endswith("\ufffd") else cut), True


def _truncated_marker(omitted):
    return f"...[truncated {omitted} bytes]..."


def clip_middle(text, limit):
    """Keep a head and tail slice so one long tool dump cannot hide its start or end."""
    raw = _encode(text)
    if len(raw) <= limit:
        return text, False
    if limit <= 0:
        return "", True
    omitted, head, tail = len(raw), 0, 0
    for _ in range(5):
        marker_bytes = _bytes(_truncated_marker(omitted))
        if marker_bytes >= limit:
            return clip(text, limit)
        keep = limit - marker_bytes
        head = -(-keep // 2)
        tail = keep // 2
        omitted = max(0, len(raw) - head - tail)
    out = raw[:head] + _encode(_truncated_marker(omitted)) + (raw[len(raw) - tail:] if tail else b"")
    return out.decode("utf-8", "replace").replace("\ufffd", ""), True


def _is_owned_secret_field(key):
    return key == "typesafeApiKey" or key.endswith(".typesafeApiKey")


def _redact_owned_secret_fields(value):
    hit = [False]

    def walk(node):
        if isinstance(node, list):
            return [walk(n) for n in node]
        if isinstance(node, dict):
            out = {}
            for key, child in node.items():
                if _is_owned_secret_field(key) and child != "" and child is not None:
                    out[key] = "[REDACTED]"
                    hit[0] = True
                else:
                    out[key] = walk(child)
            return out
        return node
    return walk(value), hit[0]


def _reject_constant(name):
    raise ValueError(name)  # JSON.parse refuses NaN and Infinity


def _js_number(text):
    """A JSON number as JavaScript prints it back: 1.0 is 1."""
    value = float(text)
    return int(value) if value.is_integer() and abs(value) < 1e21 else value


def redact_owned_settings(text):
    """Strip the product's saved-key fields from JSON text; keep non-secret settings. -> (text, redacted)"""
    if "typesafeApiKey" not in text:
        return text, False
    try:
        walked, hit = _redact_owned_secret_fields(json.loads(text, parse_float=_js_number, parse_constant=_reject_constant))
        if hit:
            return json.dumps(walked, separators=(",", ":"), ensure_ascii=False), True
    except ValueError:
        pass  # clipped or non-JSON tool output still goes through the field patterns below
    clean = _KEY_JSON.sub(lambda m: m.group(1) + ':"[REDACTED]"', text)
    clean = _KEY_ASSIGNED.sub(lambda m: m.group(1) + "=[REDACTED]", clean)
    return clean, clean != text


def scrub_known_secrets(text, secrets):
    """text with every known secret value as [REDACTED]. -> (text, redacted)"""
    clean, redacted = text, False
    for secret in secrets:
        value = secret.strip(SPACE) if isinstance(secret, str) else ""
        if not value or value not in clean:
            continue
        clean = clean.replace(value, "[REDACTED]")
        redacted = True
    return clean, redacted


def redact(text):
    """text with private keys, API-key-looking tokens, bearer tokens and secret assignments as [REDACTED]. -> (text, redacted)"""
    fields, fields_hit = redact_owned_settings(text)
    clean = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", fields)
    clean = _TOKEN.sub("[REDACTED]", clean)
    clean = _ASSIGNED.sub(lambda m: m.group(1) + "=[REDACTED]", clean)
    return clean, fields_hit or clean != fields


def _sanitize(text, secrets):
    cleaned, cleaned_hit = redact(text)
    scrubbed, scrubbed_hit = scrub_known_secrets(cleaned, secrets)
    return scrubbed, cleaned_hit or scrubbed_hit


# ---- the snapshot ----

def _input(use):
    value = use.get("input")
    return value if isinstance(value, dict) else {}


def _tool_path(use):
    inp = _input(use)
    path = next((inp[k] for k in ("file_path", "notebook_path", "path") if inp.get(k) is not None), None)  # `??`: the first one set, even if not a string
    return path if isinstance(path, str) else None


def estimate_conversation_tokens(messages):
    """A local estimate of the conversation's own tokens (about four characters per token)."""
    chars = 0
    for m in messages:
        chars += _utf16_len(m.get("text") or "")
        for use in m.get("toolUses") or []:
            chars += _utf16_len(json.dumps(_input(use), separators=(",", ":"), ensure_ascii=False)) + _utf16_len(use.get("text") or "")
        for result in m.get("toolResults") or []:
            chars += _utf16_len(result.get("text") or "")
    return -(-chars // 4)


def _touch(artifacts, path):
    artifacts.pop(path, None)
    artifacts[path] = True


def snapshot(messages, secrets=()):
    """The bounded judge input for a message list (fc_transcript.messages): {state, conversationTokens, checkpointText, autoCoverage}, the shape of snapshot.ts `Snapshot`."""
    artifacts = {}  # insertion-ordered set: a path written again moves to the end
    redacted = False
    omitted_users = 0
    recent_truncated = False
    user_budget, tail_budget = 8000, 14000
    summary = ""
    users, recent = [], []  # filled newest first, reversed below

    for m in messages:
        for use in m.get("toolUses") or []:
            if use.get("isError"):
                continue
            path = _tool_path(use)
            if path and use.get("tool") in WRITE_TOOLS and not SENSITIVE_PATH.search(path):
                _touch(artifacts, path)
            command = _input(use).get("command") if use.get("tool") in SHELL_TOOLS else None
            for written in shell_written_paths(command) if isinstance(command, str) else []:
                if not SENSITIVE_PATH.search(written):
                    _touch(artifacts, written)

    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        role, text = m.get("role"), m.get("text") or ""
        if role == "user" and text.startswith(SUMMARY_PREFIX):
            if not summary:
                clean, hit = _sanitize(text, secrets)
                summary = clip(clean, 1500)[0]
                redacted = redacted or hit
            continue
        cleaned, hit = _sanitize(text, secrets)
        redacted = redacted or hit
        if role == "user" and cleaned.strip(SPACE):
            part, truncated = clip(cleaned, user_budget)
            if truncated:
                omitted_users += 1
            if part:
                users.append({"role": "user", "text": part})
            user_budget = max(0, user_budget - _bytes(part))
        elif role == "assistant" and i >= len(messages) - RECENT_TAIL_MESSAGES:
            part, truncated = clip(cleaned, min(tail_budget, 8000))
            recent_truncated = recent_truncated or truncated
            tail_budget = max(0, tail_budget - _bytes(part))
            tools = []
            for use in m.get("toolUses") or []:
                path = _tool_path(use)
                if path and SENSITIVE_PATH.search(path):
                    excerpt = "[Sensitive file content excluded]"
                    redacted = True
                else:
                    clean, hit = _sanitize(use.get("text") or "", secrets)
                    redacted = redacted or hit
                    excerpt, truncated = clip_middle(clean, min(tail_budget, TOOL_RESULT_BUDGET))
                    recent_truncated = recent_truncated or truncated
                tail_budget = max(0, tail_budget - _bytes(excerpt))
                tools.append({"tool": use.get("tool"), "error": use.get("isError") is True, "excerpt": excerpt})
            recent.append({"role": "assistant", "text": part, **({"tools": tools} if tools else {})})
    users.reverse()
    recent.reverse()

    last_user = users[-1]["text"] if users else ""
    last_assistant = recent[-1]["text"] if recent else ""
    transcript_limit_reached = len(messages) >= MESSAGE_LIMIT  # the mod learns this from its message API's cap; the transcript reader has no cap, so a long list stands in for it
    saved = []
    for p in list(artifacts)[-8:]:
        clean, hit = _sanitize(p, secrets)
        redacted = redacted or hit
        saved.append(clip(clean, 256)[0])
    state = {
        "userConstraints": users,
        "recent": recent,
        "previousSummary": summary,
        "savedArtifacts": saved,
        "coverage": {
            "omittedUserMessages": omitted_users,
            "olderMessagesOmitted": max(0, len(messages) - RECENT_TAIL_MESSAGES),
            "recentTextTruncated": recent_truncated,
            "hasImages": False,  # a transcript message list carries text only; images cannot be detected here
            "redacted": redacted,
            "transcriptLimitReached": transcript_limit_reached,
        },
        "compaction": {"description": DESCRIPTION},
    }
    return {
        "state": state,
        "conversationTokens": estimate_conversation_tokens(messages),
        "checkpointText": json.dumps([last_user, last_assistant]),
        "autoCoverage": omitted_users == 0 and not recent_truncated and not redacted and not transcript_limit_reached,
    }
