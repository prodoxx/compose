"""A stack worktree's `.compose`: the project worktree's, entry by entry, with each project's e2e folder copied in."""
import os, shutil
from pathlib import Path


def mirror(tools, link):
    """Make `link` a real folder of links to each entry of `tools`, and each project folder in it a real folder of links too,
    except its `e2e`, which is copied fresh. Node resolves a spec's real path, so an e2e spec reached through a link imports
    `@playwright/test` and `../../../app` from the project worktree, not from the checkout verify runs in (#428).
    A link from an older layout (the whole `.compose`) is replaced; a real file of the checkout's own is never touched."""
    tools, link = Path(tools), Path(link)
    if link.is_symlink():
        link.unlink()
    if link.exists() and link.samefile(tools):  # the checkout is the project worktree itself: nothing to mirror, never copy onto itself
        return
    link.mkdir(exist_ok=True)
    for src in sorted(tools.iterdir()):
        dst = link / src.name
        if src.is_dir() and (src / "e2e").is_dir():
            if dst.is_symlink():
                dst.unlink()
            dst.mkdir(exist_ok=True)
            for sub in sorted(src.iterdir()):
                if sub.name == "e2e":
                    _copy(sub, dst / "e2e")
                else:
                    _link(sub, dst / sub.name)
            _prune(dst)
        else:
            _link(src, dst)
    _prune(link)


def _link(src, dst):
    if dst.is_symlink() and os.readlink(dst) != str(src):
        dst.unlink()
    if not dst.exists() and not dst.is_symlink():
        dst.symlink_to(src)


def _copy(src, dst):
    if dst.is_symlink():
        dst.unlink()
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)


def _prune(folder):
    """Drop the links whose entry is gone from the project worktree."""
    for p in folder.iterdir():
        if p.is_symlink() and not p.exists():
            p.unlink()
