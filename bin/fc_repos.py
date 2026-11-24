import json, re
import urllib.parse
from collections import namedtuple
from pathlib import Path
import fc_config, fc_core, fc_plan, fc_tasks

# A project can hold code in several repos. Each is named `<owner>/<name>` from its origin URL, and the project's content lists them
# in a `## Repos` section, one bullet per repo: `- <owner>/<name>: <one line on what lives there>`. Nothing here asks Linear or reads
# a path map: it turns a URL into a name and a text into a list, so the commands that do ask build on one definition of both. The path
# map, below them, is the one place this module touches the machine: where each repo is checked out, per workspace.

def name_from_url(url):
    """`<owner>/<name>` from a git remote URL, the same for its ssh, scp-like, https and `.git` forms; a subgroup keeps its whole path and
    case is kept. None when the URL has no owner: a local path (a Windows one too, `C:/code/api`), a `file://` URL, or a host with one segment."""
    url = (url or "").strip()
    if re.match(r"[A-Za-z]:[\\/]", url):
        return None
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        path = "" if parts.scheme == "file" else parts.path
    else:
        head, sep, path = url.partition(":")  # scp-like `git@host:owner/name.git`; a local path has no `host:` before its first slash
        if not sep or "/" in head:
            return None
    path = path.strip("/")
    path = path[:-4] if path.endswith(".git") else path
    segments = [s for s in path.split("/") if s]
    return "/".join(segments) if len(segments) >= 2 else None

def origin_url(repo):
    """The repo's `origin` URL, "" when it has none or is in no git checkout at all: the one place this module asks git."""
    if fc_core.checkout(repo) is None:
        return ""
    p = fc_core.sh(["git", "remote", "get-url", "origin"], repo, check=False)
    return p.stdout.strip() if p.returncode == 0 else ""

def repo_name(repo):
    """The repo's name: `"repo"` in its Linear link when set (a fork or a renamed origin), else the one its origin URL gives."""
    return fc_core.recorded_repo(repo) or name_from_url(origin_url(repo))

def same(a, b):
    """Whether two repo names are one repo: they compare without regard to case, though the list keeps the case it first saw."""
    return a.casefold() == b.casefold()

def section(lines):
    """(start, end) of the `Repos` section, a heading of that text at any level to the next heading of its level or above (a hand edit may
    write `### Repos`, close it as `## Repos ##` or indent it by up to three spaces, and an editor may end every line with `\\r`), outside
    fenced code; None when the content has none."""
    flat = [l.rstrip("\r") for l in lines]
    flat = [l.lstrip(" ") if len(l) - len(l.lstrip(" ")) <= 3 else l for l in flat]
    found = list(fc_plan.headings(flat))
    for n, (i, level) in enumerate(found):
        if re.sub(r"(^|\s)#+$", "", flat[i][level:].strip()).strip().casefold() == "repos":
            return i, next((j for j, lv in found[n + 1:] if lv <= level), len(lines))
    return None

BULLET_LINK = re.compile(r"\[([^\]]*)\]\(<?([^()<>\s]*)>?\)")

def plain(name):
    return "/" in name and not any(c.isspace() for c in name)

URL_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")

def url_name(token):
    """The name a token of a hand-written bullet gives when it is unmistakably a URL: it has a scheme, starts `git@`, or starts with a
    known host (`github.com/acme/api`, `www.` allowed); None for anything else, which is a name as written: `foo.bar/group/api` is a
    GitLab group's name, not a host path."""
    token = token.strip("<>")
    head = token.split("/", 1)[0].casefold()
    if "://" in token or token.startswith("git@"):
        return name_from_url(token)
    return name_from_url("https://" + token) if "/" in token and head.removeprefix("www.") in URL_HOSTS else None

def bullet(line):
    """(name, about) of a list line `- owner/name: about` (or `*` or `+`, the name in backticks, the colon optional), None for any other
    line: a name counts only with a `/` and no space. A hand-written URL (`- https://github.com/acme/api`, `- github.com/acme/api`) or a
    link (`- [acme/api](https://github.com/acme/api)`) reads as the name it gives (name_from_url, else the link's text). Read as Linear
    serves it back: its `\\_` and other markdown escapes undone (fc_plan.LINEAR_ESCAPE), so a name with `_` is the one that was written."""
    line = line.rstrip("\r")
    if line[:2] not in ("- ", "* ", "+ "):
        return None
    rest = fc_plan.LINEAR_ESCAPE.sub(r"\1", line[2:]).strip()
    token = rest.split(None, 1)[0] if rest else ""
    if m := BULLET_LINK.match(rest):
        text = m.group(1).strip("` ")
        name, about = url_name(text) or (text if plain(text) else None) or url_name(m.group(2)) or "", rest[m.end():]
    elif found := url_name(token.rstrip(":")):
        name, about = found, rest[len(token):]
    else:
        name, _, about = rest.partition(":")
        name = name.strip("` ")
    about = about.strip()
    return (name, about[1:].strip() if about.startswith(":") else about) if plain(name) else None

def parse_list(content):
    """The `## Repos` list of a project's content as [(name, about)], [] when it has no such section."""
    lines = (content or "").split("\n")
    span = section(lines)
    return [b for b in map(bullet, lines[span[0] + 1:span[1]]) if b] if span else []

def with_repo(content, name, about):
    """The content with `name` listed: after the last bullet of its `## Repos` section, or in a section added at the end when there is none.
    Every line it does not add stays as it was, and a repo already listed (in any case) leaves the content untouched."""
    new = f"- {name}: {' '.join(about.split())}" if about.strip() else f"- {name}"
    lines = (content or "").split("\n")
    span = section(lines)
    if span is None:
        eol = "\r\n" if "\r\n" in (content or "") else "\n"
        body = (content or "").rstrip("\r\n")
        return (body + eol * 2 if body else "") + f"## Repos{eol}{new}{eol}"
    listed = [(i, b[0]) for i in range(span[0] + 1, span[1]) if (b := bullet(lines[i]))]
    if any(same(n, name) for _, n in listed):
        return content
    after = listed[-1][0] if listed else span[0]
    lines.insert(after + 1, new + ("\r" if lines[after].endswith("\r") else ""))
    return "\n".join(lines)

def without_repo(content, name):
    """The content without the bullet that lists `name` (in any case); every other line stays as it was."""
    lines = (content or "").split("\n")
    span = section(lines)
    if span is None:
        return content or ""
    keep = [l for i, l in enumerate(lines) if not (span[0] < i < span[1] and (b := bullet(l)) and same(b[0], name))]
    return "\n".join(keep)

def with_task_repo(description, name):
    """The task's description with `repo: <name>` on the line after `id:` in its compose block, the first one, which is the block
    `compose tasks` reads; None when that block already names a repo, has no `id:` line, or there is no block: nothing to write then.
    A key counts the way the block is read, its spaces stripped."""
    m = fc_tasks.TASK_BLOCK.search(description or "")
    if not m:
        return None
    meta = fc_tasks.block_meta(m.group(1))
    if "repo" in meta or "id" not in meta:
        return None
    lines = m.group(1).split("\n")
    at = next(i for i, l in enumerate(lines) if ":" in l and l.split(":", 1)[0].strip() == "id")  # the line to write after
    indent = lines[at][:len(lines[at]) - len(lines[at].lstrip())]
    block = "\n".join([*lines[:at + 1], f"{indent}repo: {name}", *lines[at + 1:]])
    return description[:m.start(1)] + block + description[m.end(1):]

Known = namedtuple("Known", "own listed paths")  # this repo's name, the project's [(name, about)] list, {name: path} of the listed repos on this machine

# ---------- where each repo is on this machine: `{"<workspace>": {"<owner>/<name>": "<main checkout>"}}` ----------

def PATHS_FILE():
    """The path map's file, beside the config file; a function so a test that points the config elsewhere takes the map with it."""
    return fc_config.CONFIG_PATH.parent / "repos.json"

def read_all():
    """Every workspace's map as written; {} for a file that is missing, unreadable or not an object of objects of strings."""
    try:
        data = json.loads(PATHS_FILE().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {w: {n: p for n, p in m.items() if isinstance(p, str)} for w, m in data.items() if isinstance(m, dict)}

def write_all(data):
    PATHS_FILE().parent.mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(PATHS_FILE(), json.dumps(data, indent=2) + "\n")

def paths():
    """The active workspace's map, name to the path it was recorded with; {} when it has none."""
    return read_all().get(fc_config.WORKSPACE, {})

def record_path(name, repo):
    """Remember that `name` is checked out at the main checkout of `repo`. An entry of that name in another case keeps its key, the name
    as first seen, and takes the new path."""
    data = read_all()
    old = data.get(fc_config.WORKSPACE, {})
    key = next((n for n in old if same(n, name)), name)
    mine = {n: p for n, p in old.items() if not same(n, name)}
    mine[key] = str(fc_core.main_checkout(repo))
    write_all({**data, fc_config.WORKSPACE: mine})

def path_of(name):
    """Where `name` is checked out on this machine, None when it is unknown or its folder is gone."""
    found = next((Path(p) for n, p in paths().items() if same(n, name)), None)
    return found if found and found.is_dir() else None

def prune_paths():
    """Drop the active workspace's entries whose folder is gone and return their names; the file is untouched when none is."""
    data = read_all()
    mine = data.get(fc_config.WORKSPACE, {})
    gone = [n for n, p in mine.items() if not Path(p).is_dir()]
    if gone:
        write_all({**data, fc_config.WORKSPACE: {n: p for n, p in mine.items() if n not in gone}})
    return gone
