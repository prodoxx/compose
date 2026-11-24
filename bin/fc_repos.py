import json
import urllib.parse
import fc_core, fc_plan

# A project can hold code in several repos. Each is named `<owner>/<name>` from its origin URL, and the project's content lists them
# in a `## Repos` section, one bullet per repo: `- <owner>/<name>: <one line on what lives there>`. Nothing here asks Linear or reads
# a path map: it turns a URL into a name and a text into a list, so the commands that do ask build on one definition of both.

def name_from_url(url):
    """`<owner>/<name>` from a git remote URL, the same for its ssh, scp-like, https and `.git` forms; a subgroup keeps its whole path and
    case is kept. None when the URL has no owner: a local path, a `file://` URL, or a host with one segment."""
    url = (url or "").strip()
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
    """The repo's `origin` URL, "" when it has none: the one place this module asks git."""
    p = fc_core.sh(["git", "remote", "get-url", "origin"], repo, check=False)
    return p.stdout.strip() if p.returncode == 0 else ""

def repo_name(repo):
    """The repo's name: `"repo"` in its Linear link when set (a fork or a renamed origin), else the one its origin URL gives."""
    try:
        link = json.loads(fc_core.repo_link(repo).read_text())
    except (OSError, ValueError):
        link = None
    recorded = link.get("repo") if isinstance(link, dict) else None
    return recorded if isinstance(recorded, str) and recorded else name_from_url(origin_url(repo))

def same(a, b):
    """Whether two repo names are one repo: they compare without regard to case, though the list keeps the case it first saw."""
    return a.casefold() == b.casefold()

def section(lines):
    """(start, end) of the `Repos` section, a heading of that text at any level to the next heading of its level or above (a hand edit may
    write `### Repos`), outside fenced code; None when the content has none."""
    found = list(fc_plan.headings(lines))
    for n, (i, level) in enumerate(found):
        if lines[i][level:].strip().casefold() == "repos":
            return i, next((j for j, lv in found[n + 1:] if lv <= level), len(lines))
    return None

def bullet(line):
    """(name, about) of a list line `- owner/name: about` (or `*`, the name in backticks, the colon optional), None for any other line:
    a name counts only with a `/` and no space."""
    if line[:2] not in ("- ", "* "):
        return None
    name, _, about = line[2:].partition(":")
    name = name.strip("` ")
    return (name, about.strip()) if "/" in name and not any(c.isspace() for c in name) else None

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
        body = (content or "").rstrip("\n")
        return (body + "\n\n" if body else "") + f"## Repos\n{new}\n"
    listed = [(i, b[0]) for i in range(span[0] + 1, span[1]) if (b := bullet(lines[i]))]
    if any(same(n, name) for _, n in listed):
        return content
    lines.insert((listed[-1][0] if listed else span[0]) + 1, new)
    return "\n".join(lines)

def without_repo(content, name):
    """The content without the bullet that lists `name` (in any case); every other line stays as it was."""
    lines = (content or "").split("\n")
    span = section(lines)
    if span is None:
        return content or ""
    keep = [l for i, l in enumerate(lines) if not (span[0] < i < span[1] and (b := bullet(l)) and same(b[0], name))]
    return "\n".join(keep)
