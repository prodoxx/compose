"""compose shrink: a small JPEG copy of an image, so an LLM that reads it pays fewer tokens."""
import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple, Optional


class Size(NamedTuple):
    width: int
    height: int
    bytes: int


class Shrunk(NamedTuple):
    path: Path
    before: Size
    after: Size
    warning: str


def shrink(path: Path, out_dir: Optional[Path] = None, max_edge: int = 1000, quality: int = 72) -> Shrunk:
    """Make a JPEG copy of `path` in `out_dir` (default: the system temp dir under compose-shrink/) and return its sizes."""
    data = path.read_bytes()
    if not shutil.which("sips"):
        size = Size(0, 0, len(data))
        return Shrunk(path, size, size, f"compose shrink: sips not found, using the original {path}")

    def measure(p: Path) -> Size:
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(p)], capture_output=True, text=True).stdout
        px = {k: int(v) for k, v in re.findall(r"(pixelWidth|pixelHeight):\s*(\d+)", out)}
        return Size(px.get("pixelWidth", 0), px.get("pixelHeight", 0), p.stat().st_size)

    before = measure(path)
    out_dir = out_dir if out_dir is not None else Path(tempfile.gettempdir()) / "compose-shrink"
    out_dir.mkdir(parents=True, exist_ok=True)
    copy = out_dir / f"{path.stem}-{hashlib.sha256(data).hexdigest()[:8]}.jpg"
    argv = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
    if max(before.width, before.height) > max_edge:
        argv += ["-Z", str(max_edge)]
    copy.unlink(missing_ok=True)
    done = subprocess.run(argv +[str(path), "--out", str(copy)], capture_output=True, text=True)
    if done.returncode or not copy.exists():
        return Shrunk(path, before, before, f"compose shrink: sips could not shrink {path}, using the original")
    return Shrunk(copy, before, measure(copy), "")


def cmd_shrink(a: argparse.Namespace) -> None:
    """Print one line per image `a` names: its pixels and bytes before and after."""
    for image in a.images:
        r = shrink(Path(image), Path(a.out) if a.out else None, a.max_edge, a.quality)
        if r.warning:
            print(f"{image}  unchanged")
            print(r.warning, file=sys.stderr)
        else:
            print(f"{r.path}  {r.before.width}x{r.before.height} {r.before.bytes} B -> {r.after.width}x{r.after.height} {r.after.bytes} B")
