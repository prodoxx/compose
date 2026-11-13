"""The installs a checkout runs on: which git-ignored dependency directories are copied and which are made.
Imports only fc_core. fc_worktree.ready_checkout decides which of them each checkout gets."""
import shutil, sys
from pathlib import Path
import fc_core

DEV_SERVERS = ("vite", "vitest", "storybook", "@storybook")  # in a node_modules: a dev server that serves files to a browser

def serves_files(install):
    """True for a node_modules whose project runs a dev server (Vite, Vitest browser mode, Storybook). Vite resolves a symlink to
    its real path and refuses to serve a file outside the clone's root (server.fs.allow), so a dependency or setup file reached
    through a borrowed link fails in the browser with `Failed to fetch dynamically imported module`. Such an install is copied."""
    return install.name == "node_modules" and any((install / n).exists() for n in DEV_SERVERS)

def copy_install(src, dst):
    """A copy-on-write copy of the install `src` at `dst` (clonefile on APFS, reflink on Linux, a plain copy elsewhere), so the
    clone holds real files and costs no disk where the filesystem can share blocks. A copy made from a source that has changed
    since (its mtime is the stamp inside the copy) is made again, which a link never needs."""
    stamp = dst / ".compose-copied"
    if dst.is_dir() and not dst.is_symlink() and stamp.exists() and stamp.read_text() == str(src.stat().st_mtime_ns):
        return
    if dst.is_symlink():
        dst.unlink()
    shutil.rmtree(dst, ignore_errors=True)
    fast = ["cp", "-cR"] if sys.platform == "darwin" else ["cp", "-a", "--reflink=auto"]
    if fc_core.sh([*fast, str(src), str(dst)], ".", check=False).returncode:
        shutil.rmtree(dst, ignore_errors=True)
        fc_core.sh(["cp", "-R", str(src), str(dst)], ".")
    stamp.write_text(str(src.stat().st_mtime_ns))

def is_venv(path):
    """Whether `path` is a Python virtual environment: it holds a pyvenv.cfg."""
    return (Path(path) / "pyvenv.cfg").is_file()

def uv_venvs(repo, wt, deps):
    """The `.venv`s among `deps` (the main checkout's git-ignored installs) that a uv project keeps: a venv at `repo`, and a uv.lock beside
    it in `wt`. Such a venv is never shared between checkouts (see sync_venv); none when uv is not installed."""
    return [d for d in deps if Path(d).name == ".venv" and is_venv(repo / d) and (wt / Path(d).parent / "uv.lock").is_file() and shutil.which("uv")]

def sync_venv(wt, d):
    """Give the checkout `wt` its own venv at `d`, made from its own uv.lock (`uv sync --frozen`: packages come from uv's cache, so it is
    quick). A venv is not relocatable and not shareable: its scripts and activate name the venv's own path, and its editable install of the
    project names the checkout it was made in. A link to another checkout's venv ran that checkout's Python, and the first `uv run` in a
    borrower rewrote the editable install to name itself, so every other checkout imported the wrong code. A link from an earlier borrow goes first."""
    venv = wt / d
    if venv.is_symlink():
        venv.unlink()
    r = fc_core.sh(["uv", "sync", "--frozen", "--quiet"], venv.parent, check=False)
    if r.returncode:
        fc_core.say(f"warning: uv sync --frozen failed in {venv.parent}; `uv run` there makes the venv: {(r.stderr or r.stdout).strip()[-300:]}")
