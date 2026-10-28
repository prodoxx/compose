"""What a failed check leaves behind: the last lines of its output for the verify log, and a copy of the files its tool wrote
(Playwright's test-results, reports) that the next round's run would otherwise wipe."""
import shutil
from pathlib import Path

TAIL_LINES = 20


def tail(full, lines=TAIL_LINES):
    """The last `lines` non-empty lines of a check's output, indented under its `check` line."""
    rows = [r.rstrip() for r in full.splitlines() if r.strip()][-lines:]
    return "".join(f"\n           | {r}" for r in rows)


def keep_artifacts(repo, paths, log):
    """Copy each existing path of verify.json `check_artifacts` (relative to the repo) into a folder beside the check's saved
    output, `<log without .log>-artifacts/`. Returns that folder, or None when nothing was there to keep."""
    dest = Path(log).with_name(Path(log).stem + "-artifacts")
    for rel in paths:
        src = (Path(repo) / rel).resolve()
        if not src.is_relative_to(Path(repo).resolve()) or not src.exists():
            continue
        target = dest / Path(rel).name
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    return str(dest) if dest.exists() else None
