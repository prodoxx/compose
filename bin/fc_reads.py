"""A task's `reads:`: the files its implementer may read besides its own, because the task consumes them (a module whose names or
types it calls) or ports data from them (a file as of an older commit). The implementer's read guard allows only the task's own files
and the context pack, and the brief cannot carry such facts: it is held to 2500 characters, and a section the CLI rewrites (`## Evidence`)
never reaches the implementer. An entry is `<path>` (a file of the clone, at the task's base, so one an `after:` task made is there) or
`<rev>:<path>` (the file as `git show <rev>:<path>` prints it, written into the clone as `.reads/<rev>/<path>`, kept out of git)."""
import os, re, subprocess
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


PACKAGE_FILES = ("package.json", "pyproject.toml", "composer.json", "go.mod", "Cargo.toml", "Gemfile")  # what says which packages the code may import
BIG = 20000  # bytes: an example longer than this is the last one picked


def package_files(wt, files):
    """The package files the implementer may read whatever its task touches, as sorted absolute paths: the clone root's own, and for each
    of `files` those of the nearest directory above it that holds one. They say what is installed, so a test never imports what is not."""
    wt, found = Path(wt).resolve(), set()
    for d in [wt] + [wt / f for f in files]:
        while d != wt and wt in d.parents and not any((d / n).is_file() for n in PACKAGE_FILES):
            d = d.parent
        found |= {str(d / n) for n in PACKAGE_FILES if (d / n).is_file()}
    return sorted(found)


def kind(name):
    """What kind of file `name` is by its name alone, None for a plain source file: a compound ending (`.test.mjs`, `.stories.tsx`), a
    `test_` start or `_test` end (Python, Go), or a `Test`, `Tests` or `Spec` end (PHP, Java)."""
    stem, dot, rest = name.lstrip(".").partition(".")
    if "." in rest:
        return "." + rest
    if stem.startswith("test_") or stem.endswith("_test"):
        return ("test_*" if stem.startswith("test_") else "*_test") + dot + rest
    return "*Test" + dot + rest if re.search(r"[a-z0-9](Tests?|Spec)$", stem) else None


def examples(wt, files):
    """{file: absolute path of an existing file to write it like} for each of `files` the clone does not hold yet (the task creates it) and
    that has a kind: a file of the same kind in the same directory, the one whose name starts most like it, then the shortest. A model
    that sees no test of the repo guesses the setup (the environment line, the helper imports, JSX or not); one existing test shows it."""
    wt, out = Path(wt).resolve(), {}
    for f in files:
        new = wt / f
        if new.exists() or not kind(new.name) or not new.parent.is_dir():
            continue
        near = lambda c: len(os.path.commonprefix([c.name, new.name]))
        same = [c for c in new.parent.iterdir() if c.is_file() and kind(c.name) == kind(new.name)]
        if same:
            out[f] = str(min(same, key=lambda c: (c.stat().st_size > BIG, -near(c), c.stat().st_size, c.name)))
    return out


def grounding(wt, files, may_install):
    """(paths, note): the package files and examples of a task whose files are `files`, and the prompt lines that name them ("" for none)."""
    wt = Path(wt).resolve()
    packages, like = package_files(wt, files), examples(wt, files)
    note = ""
    if packages:
        note += (f"The repo's package files, read-only: {', '.join(packages)}. Import only what they declare: "
                 + ("a package the task names that they lack is not there yet, so add it with the package manager command below.\n" if may_install else
                    "a package they lack is not installed, so never import it; if the task cannot be done without it, end with a CONTRADICTION line that names it.\n"))
    if like:
        note += ("New files of a kind the repo already has, each with one existing file to write it like (the same setup lines, imports, file "
                 "ending and helpers), read-only: " + "; ".join(f"{wt / f} like {e}" for f, e in like.items()) + ".\n")
    return packages + sorted(set(like.values())), note
