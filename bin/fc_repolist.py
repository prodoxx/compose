"""The `## Repos` list of a Linear project, kept by compose itself: every command that reads the list (`compose linear project`, spec-check,
the planner's brief) lists this repo when it is absent (and the first time, while this repo is the project's only one, names it on the open
tasks), and `compose linear repos` prints, adds and drops by hand.
It sits above fc_linear_cmd, which reads the project and waits for a write, and holds the `compose linear` entry point so that
fc_linear_cmd's own verbs never call back up into it."""
import argparse, json, re, subprocess, sys
from pathlib import Path

import fc_config, fc_core, fc_linear, fc_linear_cmd, fc_repos, fc_tasks

CONTENT = "query($id: String!) { project(id: $id) { content } }"
PROJECT_UPDATE = "mutation($id: String!, $in: ProjectUpdateInput!) { projectUpdate(id: $id, input: $in) { success } }"
OPEN_TASKS = ('query($p: ID!, $after: String) { issues(first: 100, after: $after, filter: {project: {id: {eq: $p}}, state: {type: {nin: ["completed", "canceled"]}}}) '
              "{ pageInfo { hasNextPage endCursor } nodes { id identifier description } } }")
ISSUE_UPDATE = "mutation($id: String!, $in: IssueUpdateInput!) { issueUpdate(id: $id, input: $in) { success } }"
NOT_HERE = " (not on this machine: run compose linear project in its checkout)"
ABOUT_CHARS = 120

def about(repo):
    """One line on what lives in the repo, for its bullet: its GitHub description, else the first paragraph of its README that is not a
    heading, a badge row, an image or HTML, cut to ABOUT_CHARS on a word; "" with neither, which lists the repo by name alone."""
    try:
        p = fc_core.sh(["gh", "repo", "view", "--json", "description", "-q", ".description"], repo, check=False, timeout=20)
        text = " ".join(p.stdout.split()) if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        text = ""
    if text:
        return text
    try:
        readme = (Path(repo) / "README.md").read_text(errors="replace")
    except OSError:
        return ""
    para = next((b for b in re.split(r"\n\s*\n", readme) if b.strip() and not b.lstrip().startswith(("#", "[!", "![", "<"))), "")
    text = " ".join(para.split())
    if len(text) <= ABOUT_CHARS:
        return text
    cut = text[:ABOUT_CHARS + 1].rsplit(" ", 1)[0]  # the words that fit; a first word longer than the line is cut where the line ends
    return cut if " " in text[:ABOUT_CHARS + 1] else text[:ABOUT_CHARS]

def content_of(pid):
    return fc_linear.linear_gql(CONTENT, {"id": pid})["project"]["content"]

def write_content(p, old, new):
    """Write the project's content and wait until Linear serves it back, so the next read (the list printed, the next writer) is the new one."""
    if not fc_linear.linear_gql(PROJECT_UPDATE, {"id": p["id"], "in": {"content": new}})["projectUpdate"]["success"]:
        raise RuntimeError(f"Linear did not update the content of {p['name']}")
    fc_linear_cmd.await_write(lambda: content_of(p["id"]), old, new)

def write_task(issue, new):
    if not fc_linear.linear_gql(ISSUE_UPDATE, {"id": issue["id"], "in": {"description": new}})["issueUpdate"]["success"]:
        raise RuntimeError(f"Linear did not update {issue['identifier']}")
    fc_linear_cmd.await_write(lambda: fc_linear.linear_gql("query($id: String!) { issue(id: $id) { description } }", {"id": issue["id"]})["issue"]["description"],
                              issue["description"], new)

def listed_as(content, name):
    """How the project's list spells `name` (the case first seen), None when it does not list it."""
    return next((n for n, _ in fc_repos.parse_list(content) if fc_repos.same(n, name)), None)

def open_tasks(p):
    """Every open issue of the project, {id, identifier, description}, every page read."""
    tasks, after = [], None
    while True:
        page = fc_linear.linear_gql(OPEN_TASKS, {"p": p["id"], "after": after})["issues"]
        tasks += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return tasks

def named(issue):
    """The repo the issue's compose block names (its first block, as `compose tasks` reads it), "" for none."""
    return fc_tasks.block_meta((fc_tasks.task_blocks(issue["description"]) or [""])[0]).get("repo", "")

def name_open_tasks(repo, p, name):
    """Write `repo: <name>` into the compose block of each open task of the project that names no repo, and say which, by Linear id in the
    order written, also when a write fails midway (the line comes before the error), so a task that belongs to another repo can be moved.
    Every page is read before any write, so a write never moves an issue between the pages still to read. When an open task already names
    another repo, the project has one whatever its list reads (a section renamed or reformatted by hand reads as none): no task is written."""
    tasks = open_tasks(p)
    others = {}
    for r in filter(None, map(named, tasks)):
        if not fc_repos.same(r, name):
            others.setdefault(r.casefold(), r)
    if others:
        print(f"repos: wrote no repo: on open tasks, because some already name another repo ({', '.join(sorted(others.values(), key=str.casefold))})", flush=True)
        return
    written = []
    try:
        for issue in tasks:
            new = fc_repos.with_task_repo(issue["description"], name)
            if new is not None:
                write_task(issue, new)
                written.append(issue["identifier"])
    finally:
        if written:  # a cached `get` would serve the body from before the write, also when a later write failed
            fc_linear_cmd.drop_get_cache(argparse.Namespace(repo=repo))
            print(f"repos: wrote repo: {name} on {len(written)} open task(s): {', '.join(written)}", flush=True)

def ensure(repo):
    """Read the Linear project's list, add this repo when it is absent (a first run, a hand edit, or the write race below dropped it), and
    record where this repo is on this machine; return the fc_repos.Known it ended with, and print one line for each thing it wrote.
    Every run reads the list once, so a caller has it without asking again, and a repo the list lost comes back on the next command.
    The link file's mark, `"listed": "<the project's URL>"`, says only that the open tasks were seen to from this checkout for that project:
    while it is missing and the list holds this repo alone, each open task that names no repo is named this one (name_open_tasks). The mark
    is written last, so a run that died earlier names the rest, and a second run makes no write and reads no open issue.
    The list is read again after it is written, and the tasks are named only from that read: two repos listing themselves at once, or a
    hand edit between the read and the write, can drop a bullet, and neither run may then believe it is the project's only repo."""
    path = fc_core.repo_link(repo)
    if not path.exists():  # what fc_linear_cmd.project_link says, before the name: a fresh local repo has neither, and needs the link first
        sys.exit(f"no .compose/linear.json in {path.parent.parent}")
    link = fc_core.read_link(repo)
    project = link.get("project")
    if not project:
        sys.exit(f'{path} has no "project": add the Linear project URL')
    name = fc_repos.repo_name(repo)
    if not name:
        sys.exit(f'compose cannot name this repo: it has no origin remote. Ask the user for its <owner>/<name> and add "repo": "<owner>/<name>" to {fc_core.repo_link(repo)}')
    p = fc_linear_cmd.linear_project(repo)
    content = content_of(p["id"])
    fc_repos.record_path(listed_as(content, name) or name, repo)  # on every run: a pruned or lost path map, or another workspace, is fixed here
    if listed_as(content, name) is None:
        new = fc_repos.with_repo(content, name, about(repo))
        write_content(p, content, new)
        content = content_of(p["id"])
        if listed_as(content, name) is None:
            raise RuntimeError(f"another write to the content of {p['name']} replaced its list before {name} showed in it: "
                               "run compose linear project again")
        print(f"repos: added {name} to {p['name']}", flush=True)
    listed = fc_repos.parse_list(content)
    name = listed_as(content, name) or name
    if link.get("listed") != project and len(listed) == 1:
        name_open_tasks(repo, p, name)
    if (link.get("repo"), link.get("listed")) != (name, project):
        fc_core.atomic_write(path, json.dumps({**json.loads(path.read_text()), "repo": name, "listed": project}, indent=2) + "\n")
    return found(name, listed)

def unread(repo):
    """Why the project's list cannot be read from this repo, None when it can: it takes a Linear key and a Linear project in the repo's link."""
    if not fc_config.LINEAR_KEY:
        return "no Linear key"
    return None if fc_core.read_link(repo).get("project") else "no Linear project linked"

def found(own, listed):
    """The fc_repos.Known of this repo's name `own` and the project's list `listed`: with where each listed repo is on this machine."""
    return fc_repos.Known(own, listed, {n: str(at) for n, _ in listed if (at := fc_repos.path_of(n))})

def known(repo, p=None):
    """This repo's name, the project's list as Linear serves it, and where each listed repo is checked out on this machine, with nothing
    added: what `repos` prints. `p`, the project when the caller has read it already."""
    return found(fc_repos.repo_name(repo), fc_repos.parse_list(content_of((p or fc_linear_cmd.linear_project(repo))["id"])))

def list_lines(k):
    """The list `k` (fc_repos.Known, which holds a repo) as `project` and `repos` print it, this repo marked, and each listed repo with no
    checkout here told where to run."""
    def mark(n):
        if k.own and fc_repos.same(n, k.own):
            return " (this repo)"
        return "" if n in k.paths else NOT_HERE
    return ["repos:", *(f"  {n}: {a}{mark(n)}" if a else f"  {n}{mark(n)}" for n, a in k.listed)]

def verb_repos(a):
    if a.add and a.drop:
        sys.exit("repos takes one of --add, --drop")
    if a.about and not a.add:
        sys.exit("--about goes with --add")
    name = a.add or a.drop
    if name and (len(name.split("/")) < 2 or not all(name.split("/")) or any(c.isspace() for c in name)):
        sys.exit(f"--{'add' if a.add else 'drop'} takes <owner>/<name>, such as acme/api, not {name!r}")
    if a.drop and (own := fc_repos.repo_name(a.repo)) and fc_repos.same(a.drop, own):
        sys.exit(f'this repo is linked to the project: to drop it, remove "project" from {fc_core.repo_link(a.repo)}, or run the drop from another checkout')
    p = fc_linear_cmd.linear_project(a.repo)
    if not name:
        k = known(a.repo, p)
        print("\n".join(list_lines(k)) if k.listed else f"repos: none listed in {p['name']}")
        return
    content = content_of(p["id"])
    there = listed_as(content, name)
    if a.add and there:
        print(f"repos: {there} is already listed in {p['name']}")
    elif a.drop and not there:
        print(f"repos: {name} is not listed in {p['name']}")
    else:
        write_content(p, content, fc_repos.with_repo(content, name, a.about or "") if a.add else fc_repos.without_repo(content, name))
        print(f"repos: added {name} to {p['name']}" if a.add else f"repos: dropped {there} from {p['name']}")
        if a.drop and (still := [i["identifier"] for i in open_tasks(p) if (r := named(i)) and fc_repos.same(r, name)]):
            print(f"repos: {len(still)} open task(s) still name {there}: {', '.join(still)}")  # dropped all the same: spec-check fails each

def wrapped(action, fn, *args):
    """`fn(*args)`, with a Linear or file error turned into the exit `compose linear` gives for one."""
    try:
        return fn(*args)
    except (RuntimeError, OSError) as e:
        sys.exit(f"linear {action}: {e}")

def cmd_linear(a):
    """`compose linear`: `repos` here, `project` (when it only reads) with the repo listed first and the list printed after (not after
    `--body`, whose section hashes end it), and every other verb as fc_linear_cmd has it."""
    if a.action != "repos" and any(getattr(a, f, None) for f in ("add", "about", "drop")):
        sys.exit("--add, --about and --drop belong to compose linear repos")
    if not fc_config.LINEAR_KEY or a.action not in ("project", "repos") or (a.action == "project" and fc_linear_cmd.writes_body(a, "project")):
        return fc_linear_cmd.cmd_linear(a)
    if a.action == "repos":
        return wrapped("repos", verb_repos, a)
    k = wrapped("project", ensure, a.repo)
    fc_linear_cmd.cmd_linear(a)
    if not a.body:  # --body ends with its section hashes, which a section write reads back
        print("\n".join(list_lines(k)))
