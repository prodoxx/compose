"""compose shrink: a small JPEG copy of an image, so an LLM that reads it pays fewer tokens."""
import argparse
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
    raise NotImplementedError("shrink")


def cmd_shrink(a: argparse.Namespace) -> None:
    """Print one line per image `a` names: its pixels and bytes before and after."""
    raise NotImplementedError("cmd_shrink")
