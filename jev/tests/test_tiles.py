"""`python -m jev_ultrafast.tiles`: a rendered design cut into images a Jev request accepts, nothing of it lost."""

import json
import subprocess
import sys

import pytest
from PIL import Image

from jev_ultrafast.tiles import EDGE_MAX, EDGE_MIN, MARGIN, tiles


def render(tmp_path, w, h, mode="RGB", color=(0, 0, 0)):
    src = tmp_path / f"{w}x{h}.png"
    Image.new(mode, (w, h), color).save(src)
    return src


@pytest.mark.parametrize("size, want", [
    ((100, 40), [(280, 280)]),
    ((800, 600), [(800 + 2 * MARGIN, 600 + 2 * MARGIN)]),
    ((4000, 500), [(1568, 280)]),
    ((1440, 4000), [(1440 + 2 * MARGIN, 1349), (1440 + 2 * MARGIN, 1349), (1440 + 2 * MARGIN, 1350)]),
    ((1000, 20000), [(359, 1567), (359, 1568), (359, 1568), (359, 1568)]),
])
def test_every_tile_is_inside_jevs_limits(tmp_path, size, want):
    names = tiles(render(tmp_path, *size), tmp_path, "1-2", 4)
    got = [Image.open(tmp_path / n).size for n in names]
    assert got == want
    assert all(EDGE_MIN <= side <= EDGE_MAX for s in got for side in s)


def test_tiles_run_top_to_bottom_and_keep_the_first_and_last_row(tmp_path):
    img = Image.new("RGB", (400, 4000), (0, 0, 0))
    for x in range(400):
        img.putpixel((x, 0), (255, 0, 0))
        img.putpixel((x, 3999), (0, 0, 255))
    img.save(tmp_path / "src.png")
    names = tiles(tmp_path / "src.png", tmp_path, "9-1", 4)
    assert names == ["9-1.1.png", "9-1.2.png", "9-1.3.png"]
    first, last = Image.open(tmp_path / names[0]), Image.open(tmp_path / names[-1])
    assert first.getpixel((10 + MARGIN, MARGIN)) == (255, 0, 0)
    assert last.getpixel((10 + MARGIN, last.height - 1 - MARGIN)) == (0, 0, 255)


def test_the_frame_has_a_white_margin_so_its_edge_text_is_not_flush_with_the_image(tmp_path):
    """Figma renders a node to its exact bounds: text on the bottom row looked cut off to the judge."""
    Image.new("RGB", (900, 861), (0, 0, 0)).save(tmp_path / "src.png")
    (name,) = tiles(tmp_path / "src.png", tmp_path, "237", 4)
    tile = Image.open(tmp_path / name)
    assert tile.size == (900 + 2 * MARGIN, 861 + 2 * MARGIN)
    for x, y in ((0, 0), (tile.width - 1, tile.height - 1), (tile.width - 1, 400), (0, 400), (450, 0), (450, tile.height - 1)):
        assert tile.getpixel((x, y)) == (255, 255, 255)
    assert tile.getpixel((MARGIN, MARGIN)) == (0, 0, 0) and tile.getpixel((tile.width - 1 - MARGIN, tile.height - 1 - MARGIN)) == (0, 0, 0)


def table(tmp_path, bars):
    """A 600 x 3000 sheet of white with a black bar over each row range in `bars`: a table row is a bar, a gap between rows is white."""
    img = Image.new("RGB", (600, 3000), (255, 255, 255))
    for top, bottom in bars:
        img.paste((0, 0, 0), (100, top, 500, bottom))
    img.save(tmp_path / "sheet.png")
    return tiles(tmp_path / "sheet.png", tmp_path, "236", 4)


def test_a_cut_falls_in_a_gap_between_rows_never_through_a_row(tmp_path):
    """236 was cut at k * h // n, through the middle of a row: the same swatch ended image 1 and began image 2."""
    names = table(tmp_path, [(1480, 1540)])  # the even cut of the two tiles lands in this bar
    assert len(names) == 2
    first, second = (Image.open(tmp_path / n) for n in names)
    assert first.height + second.height == 3000 + 2 * MARGIN
    for edge_row in (first.getpixel((300, first.height - 1)), second.getpixel((300, 0))):
        assert edge_row == (255, 255, 255)
    assert first.height - MARGIN < 1480 or first.height - MARGIN >= 1540


def test_a_cut_never_makes_a_tile_taller_than_jev_takes(tmp_path):
    names = table(tmp_path, [(y, y + 40) for y in range(0, 3000, 55)])  # a bar every 55 rows: no gap wider than 15, many near the even cut
    heights = [Image.open(tmp_path / n).height for n in names]
    assert all(EDGE_MIN <= x <= EDGE_MAX for x in heights) and sum(heights) == 3000 + 2 * MARGIN


def test_transparency_and_padding_are_white_never_black(tmp_path):
    names = tiles(render(tmp_path, 100, 40, "RGBA", (0, 0, 0, 0)), tmp_path, "t", 4)
    tile = Image.open(tmp_path / names[0])
    assert tile.mode == "RGB"
    assert tile.getpixel((5, 5)) == tile.getpixel((200, 200)) == (255, 255, 255)


def test_the_command_prints_the_tile_names(tmp_path):
    src = render(tmp_path, 600, 2000)
    out = subprocess.run([sys.executable, "-m", "jev_ultrafast.tiles", str(src), str(tmp_path), "3-4", "4"],
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == ["3-4.1.png", "3-4.2.png"]
