"""The read guard: decides one implementer tool call against the task's allowed files. pi (pi_readguard.ts), OpenCode
(opencode_readguard.js) and Claude (a PreToolUse hook) run it as a script, `python3 -I fc_readguard.py <guard file>`, with the
call on stdin. Stdlib only, no fc_ module."""
import json, os, re, shlex, sys
from pathlib import Path

PI_EXT = Path(__file__).resolve().parent / "pi_readguard.ts"
OC_PLUGIN = Path(__file__).resolve().parent / "opencode_readguard.js"


def guard_file(wt):
    """`<wt parent>/<wt name>.guard.json`, beside the task clone (never inside it, so `git add -A` never commits it)."""
    wt = Path(wt)
    return wt.parent / (wt.name + ".guard.json")


def pack_paths(pack):
    """The paths a trail pack cites on its `path:L<a>-L<b>` lines, in order, each once."""
    out = []
    for p in re.findall(r"(\S+?):L\d+-L\d+", pack):
        if p not in out:
            out.append(p)
    return out


LOCK_MANAGERS = {"bun.lock": "bun", "bun.lockb": "bun", "package-lock.json": "npm", "npm-shrinkwrap.json": "npm", "pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn"}


def installers(wt, files):
    """The package managers a task may run, as the guard's `installers`: none unless its `files` name a package.json or a lockfile (a task that
    adds a dependency edits one and must install it, which the sandbox of an implementer cannot do for itself). The managers are the ones whose
    lockfile `files` names or that sits at the clone's root or beside a listed package.json, else npm."""
    names = {os.path.basename(f) for f in files}
    if not names & ({"package.json"} | set(LOCK_MANAGERS)):
        return []
    dirs = {"", *(os.path.dirname(f) for f in files if os.path.basename(f) == "package.json")}
    found = {LOCK_MANAGERS[n] for n in names if n in LOCK_MANAGERS}
    found |= {m for d in dirs for n, m in LOCK_MANAGERS.items() if os.path.exists(os.path.join(wt, d, n))}
    return sorted(found) or ["npm"]


def write_guard(wt, files, pack, extra=(), installs=()):
    """Write {"root", "allow", "installers", "log"} to guard_file(wt) and return that path. `allow` holds the absolute paths of `files`, of
    pack_paths(pack), and of `extra` (absolute paths the prompt itself tells the model to read); `installers` the package managers whose
    plain `install` and `add` the model may run (see `installers`); `log` is `<wt parent>/<wt name>.refused`."""
    root = os.path.realpath(wt)
    allow = sorted({os.path.realpath(os.path.join(root, f)) for f in [*files, *pack_paths(pack), *extra]})
    path = guard_file(wt)
    path.write_text(json.dumps({"root": root, "allow": allow, "installers": sorted(installs), "log": str(path.parent / (Path(wt).name + ".refused"))}, indent=2))
    return path


def command(guard):
    """The shell line that runs this file as the guard for `guard`: `python3 -I <this file> <guard>`."""
    return "python3 -I {} {}".format(shlex.quote(os.path.realpath(__file__)), shlex.quote(str(guard)))


AWK_WORDS = {"length", "print", "printf", "NR", "NF", "if", "else", "END", "BEGIN", "FILENAME"}  # a name of one or two letters is a variable


def awk_program_ok(prog):
    """Whether an awk program only measures lines: no pipe, system, getline, close, or other function, and no redirect (a `>` outside
    parentheses inside an action writes a file; in a pattern, or in an `if (...)`, it compares)."""
    bare, paren, brace = re.sub(r'"[^"\\]*"', '""', prog), 0, 0
    if not re.fullmatch(r'[\w\s$(){};,<>=!+\-*/%".]*', bare):
        return False
    if any(w not in AWK_WORDS and len(w) > 2 for w in re.findall(r"[A-Za-z_]\w*", bare)):
        return False
    for c in bare:
        paren += (c == "(") - (c == ")")
        brace += (c == "{") - (c == "}")
        if c == ">" and brace > 0 and paren <= 0:
            return False
    return paren == 0 and brace == 0


def words_of(cmd):
    """The words of one shell line, operators (`;`, `&&`, `|`, `>`, `(`) as words of their own; None for a substitution, a newline, or a
    quote that never closes."""
    if "`" in cmd or "$(" in cmd or "${" in cmd or "\n" in cmd:
        return None
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return None

def plain(files, root, allow):
    """Whether `files` is a non-empty list of plain paths (no glob, no `~`, no `$`, not an option) that all are in `allow`."""
    return bool(files) and all(not re.search(r"[*?\[\]{}~$]|^-", f) and os.path.realpath(os.path.join(root, f)) in allow for f in files)

def measure(words, root, allow):
    """Whether `words` are one `wc` (flags among -l -L -c -m -w) or `awk '<measuring program>'` over plain paths that all are in `allow`:
    how long a line of the task's own file is, which a model cannot count by eye."""
    if len(words) < 2 or words[0] not in ("wc", "awk"):
        return False
    if words[0] == "wc":
        flags = [w for w in words[1:] if w.startswith("-")]
        files = words[1 + len(flags):]
        if not all(re.fullmatch(r"-[lLcmw]+", f) for f in flags) or words[1:1 + len(flags)] != flags:
            return False
    else:
        if not awk_program_ok(words[1]):
            return False
        files = words[2:]
    return plain(files, root, allow)

def count_len(words):
    """How many leading `words` are a line count of `head` or `tail` (`-20`, `-n20`, or `-n 20`): 0 when they do not start with one."""
    if words[:1] == ["-n"] and len(words) > 1 and words[1].isdigit():
        return 2
    return 1 if words and re.fullmatch(r"-\d+|-n\d+", words[0]) else 0

def viewer(words, root, allow):
    """Whether `words` only show part of the task's own files, which the Read tool shows anyway: `cat [-n]`, `head`/`tail` with a line
    count, `sed -n '<a>,<b>p'`, or `grep` with display flags and one pattern. Never an option that names another file or writes."""
    if len(words) < 2:
        return False
    name, rest = words[0], words[1:]
    if name == "cat":
        rest = rest[1:] if rest[0] == "-n" else rest
        return plain(rest, root, allow)
    if name in ("head", "tail"):
        return plain(rest[count_len(rest):], root, allow)
    if name == "sed":
        return len(rest) > 2 and rest[0] == "-n" and re.fullmatch(r"(\d+|\$)(,(\d+|\$))?p", rest[1]) is not None and plain(rest[2:], root, allow)
    if name != "grep":
        return False
    found, i = [], 0  # the pattern and the files, with display flags anywhere among them
    while i < len(rest):
        if re.fullmatch(r"-[ABCm]", rest[i]) and i + 1 < len(rest) and rest[i + 1].isdigit():
            i += 1
        elif rest[i].startswith("-") and not re.fullmatch(r"-[ABCm]\d+|-[nicwFEHhvx]+", rest[i]):
            return False
        elif not rest[i].startswith("-"):
            found.append(rest[i])
        i += 1
    return len(found) > 1 and plain(found[1:], root, allow)

def view(cmd, root, allow):
    """Whether bash line `cmd` only measures or shows the task's own files: `measure` or `viewer`, led by `cd <the clone> &&` and ended
    by `| head` or `| tail` with a line count. No other pipe, redirect, second command, substitution, glob or file."""
    words = words_of(cmd)
    if not words:
        return False
    if words[0] == "cd" and len(words) > 3 and words[2] == "&&" and os.path.realpath(os.path.join(root, words[1])) == root:
        words = words[3:]
    if "|" in words:
        i = words.index("|")
        words, cut = words[:i], words[i + 1:]
        if not cut or cut[0] not in ("head", "tail") or len(cut) - 1 != count_len(cut[1:]):
            return False
    if not words or any(re.fullmatch(r"[;&|<>()]+", w) for w in words):
        return False
    return measure(words, root, allow) or viewer(words, root, allow)


PACKAGE_MANAGERS = ("bun", "bunx", "npm", "npx", "pnpm", "yarn")
INSTALL_FLAGS = {"-d", "-D", "--dev", "--save-dev", "-E", "--exact", "--save-exact", "--optional", "-O", "--save-optional", "--peer", "--ignore-scripts"}
PACKAGE_RE = re.compile(r"(@[A-Za-z0-9._~-]+/)?[A-Za-z0-9._~-]+(@[A-Za-z0-9._~^<>=*|+-]+)?")


def cut_words(words):
    """`words` without a trailing `| head <n>` or `| tail <n>`; None for any other pipe."""
    if "|" not in words:
        return words
    i = words.index("|")
    cut = words[i + 1:]
    return words[:i] if cut and cut[0] in ("head", "tail") and len(cut) - 1 == count_len(cut[1:]) else None


def install(cmd, root, managers):
    """Whether bash line `cmd` is a plain `<manager> install|i|add [flags] [package[@version]...]` of one of `managers`, alone or after
    `cd <a directory of the clone> &&`, and ended at most by `| head` or `| tail` with a line count. No other flag (a registry, a global or
    another directory), package spec (a URL, a path), pipe, redirect, second command, substitution or glob."""
    words = words_of(cmd)
    if not words:
        return False
    if words[0] == "cd" and len(words) > 3 and words[2] == "&&":
        there = os.path.realpath(os.path.join(root, words[1]))
        if there != root and not there.startswith(root + os.sep):
            return False
        words = words[3:]
    words = cut_words(words)
    if not words or len(words) < 2 or words[0] not in managers or words[1] not in ("install", "i", "add"):
        return False
    flags = [w for w in words[2:] if w.startswith("-")]
    specs = [w for w in words[2:] if not w.startswith("-")]
    return all(f in INSTALL_FLAGS for f in flags) and all(PACKAGE_RE.fullmatch(x) and ".." not in x for x in specs) and (words[1] != "add" or bool(specs))


def installs_why(cmd, managers):
    """The reply to a package manager command the guard refused, so the model stops trying variants; None for any other command."""
    words = words_of(cmd) or cmd.split()
    words = words[3:] if words[:1] == ["cd"] and words[2:3] == ["&&"] else words
    if not words or words[0].split("/")[-1] not in PACKAGE_MANAGERS:
        return None
    if not managers:
        return ("Blocked: this task may not install or run anything: its files: name no package.json or lockfile, and a package manager is never "
                "allowed for it. Do not try another form; if the brief needs a dependency, end your reply with a CONTRADICTION line saying so.")
    return ("Blocked: the only package manager commands allowed here are `{m} install` and `{m} add <package>[@version]...` (flags -d, -E, --optional, "
            "--peer, --ignore-scripts), each alone or after `cd <directory of this clone> &&`, with at most `| head -N` or `| tail -N`. "
            "No other form works: not `{m} --version`, a full path to {m}, `export PATH=...`, a second command, a registry flag, or `npm view`.").format(m=managers[0])


def refusal(tool, args, guard):
    """None when the call is allowed, else the reply text for the model. `guard` is the loaded guard dict."""
    why = ("Blocked: you may read only the files the context pack names (or a directory directly holding one), and in bash only "
           "`trail ask \"<question>\"` or `trail callers <symbol>`, plus `wc -L`, `awk '{print length}'`, `cat`, `head`, `tail`, `sed -n 'a,bp'` or `grep` on those files. "
           "For anything else, run trail ask \"<question>\".")
    tool = (tool or "").lower()
    root, allow = guard["root"], set(guard["allow"])
    names = sorted(os.path.relpath(a, root) if a.startswith(root + os.sep) else a for a in allow)
    why += (" The files you may read: " + ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "") + ". " if names else " ") + \
        "A file or an older commit you need that is not among them is a fact the brief lacks: do not guess, end your reply with `CONTRADICTION: brief lacks <the file or fact>`."
    if guard.get("installers"):
        why += " This task may also run `{} install` and `{} add <package>`, each alone.".format(guard["installers"][0], guard["installers"][0])
    if tool == "bash":
        cmd = (args.get("command") or "").strip()
        if (cmd in ("trail ask", "trail callers") or cmd.startswith(("trail ask ", "trail callers "))) \
                and not any(x in cmd for x in (";", "&", "|", "`", "$(", "<", ">", "\n")):
            return None
        if view(cmd, root, allow) or install(cmd, root, guard.get("installers") or []):
            return None
        return installs_why(cmd, guard.get("installers") or []) or why
    p = args.get("file_path") or args.get("path")
    if tool == "read":
        if p and os.path.realpath(os.path.join(root, p)) in allow:
            return None
        return why
    if tool in ("grep", "glob", "find", "ls", "list"):
        r = os.path.realpath(os.path.join(root, p)) if p else root  # no path means the root
        if r in allow or any(os.path.dirname(a) == r for a in allow):
            return None
        return why
    return None


def refused(wt):
    """How many calls the guard of the clone `wt` has refused: the lines in its log, 0 when there is none."""
    log = Path(json.loads(guard_file(wt).read_text())["log"])
    return len(log.read_text().splitlines()) if log.exists() else 0


def cli():
    """Read one event on stdin (a Claude PreToolUse event or {"tool", "args"}), log a refusal, print the reply."""
    try:
        guard = json.loads(Path(sys.argv[1]).read_text())
        ev = json.load(sys.stdin)
        claude = "tool_name" in ev
        tool = ev.get("tool_name") if claude else ev.get("tool")
        if not tool:
            raise ValueError("event names no tool")
        args = (ev.get("tool_input") if claude else ev.get("args")) or {}
        why = refusal(tool, args, guard)
        if not why:
            return
        with open(guard["log"], "a") as f:
            f.write(json.dumps({"tool": tool, "args": args}) + "\n")
        if claude:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": why}}))
        else:
            print(why)
    except Exception as e:
        print("fc_readguard: {}".format(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    cli()
