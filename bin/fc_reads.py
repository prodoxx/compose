"""A task's `reads:`: the files its implementer may read besides its own, because the task consumes them (a module whose names or
types it calls) or ports data from them (a file as of an older commit). The implementer's read guard allows only the task's own files
and the context pack, and the brief cannot carry such facts: it is held to 2500 characters, and a section the CLI rewrites (`## Evidence`)
never reaches the implementer. An entry is `<path>` (a file of the clone, at the task's base, so one an `after:` task made is there) or
`<rev>:<path>` (the file as `git show <rev>:<path>` prints it, written into the clone as `.reads/<rev>/<path>`, kept out of git)."""
import re, subprocess
from pathlib import Path
import fc_worktree

DIR = ".reads"


def split(entry):
    """(rev, path) for `<rev>:<path>`, else (None, entry)."""
    rev, colon, path = entry.partition(":")
    return (rev, path) if colon and path and re.fullmatch(r"[0-9A-Za-z][\w./~^@{}-]*", rev) else (None, entry)


def invalid(entry):
    """Why `entry` cannot be a read, or None: it must name a file inside the clone, so no absolute path, no `..`, no glob."""
    path = split(entry)[1]
    if not path or Path(path).is_absolute() or ".." in Path(path).parts or re.search(r"[*?\[\]{}~$]", path):
        return "a path inside the repo, with no `..`, glob or absolute path"
    return None


def resolve(wt, reads):
    """(paths, missing): the absolute paths the implementer may read for the task's `reads:`, and a line for each entry that cannot be read.
    A `<rev>:<path>` entry is written into `<wt>/.reads/<rev>/<path>` first, a revision the clone does not hold or a path it lacks being missing."""
    wt, paths, missing, copied = Path(wt).resolve(), [], [], False
    for entry in reads:
        rev, path = split(entry)
        if invalid(entry):
            missing.append(f"{entry}: {invalid(entry)}")
        elif rev is None:
            if (wt / path).is_file():
                paths.append(str(wt / path))
            else:
                missing.append(f"{entry}: no such file in the clone")
        else:
            shown = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=wt, capture_output=True)
            if shown.returncode:
                missing.append(f"{entry}: git cannot show it ({shown.stderr.decode(errors='replace').strip()[-120:]})")
                continue
            dest = wt / DIR / re.sub(r"[^\w-]", "_", rev) / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(shown.stdout)
            paths.append(str(dest))
            copied = True
    if copied:
        fc_worktree.exclude_paths(wt, [f"/{DIR}/"])
    return paths, missing
