"""Cut a rendered design into images a Jev request accepts (280 to 1568 pixels a side), for compose's figma judge.

`python -m jev_ultrafast.tiles <src> <out_dir> <stem> <tiles_max>` prints the tile file names, top first, as one JSON list.
A frame wider than 1568 is shrunk to that width; a taller one is cut top to bottom into at most `tiles_max` tiles of at most
1568 each, shrunk further only when that is not enough, so its text stays readable. Transparency and a side under 280 are filled white.

The frame gets a white margin of MARGIN pixels on every side: Figma renders a node exactly to its bounds, so its text can touch the
edge of the image, which reads as text cut off. A cut between tiles goes through the quietest row (the fewest pixels that differ from the
row's main colour: a gap between table rows, a divider) near where an even cut would fall, so a row of the design is not sliced in two.
"""
import json
import sys
from pathlib import Path

from PIL import Image

EDGE_MIN, EDGE_MAX = 280, 1568
BACKGROUND = (255, 255, 255)
MARGIN = 24


def ink(img, y):
    """How many pixels of row `y` differ from the row's most common colour: 0 for a row of plain background or a full-width line."""
    colors = img.crop((0, y, img.width, y + 1)).getcolors(img.width)
    return img.width - max(count for count, _ in colors)


def cuts(img, n):
    """The n - 1 rows of the framed image (`img` plus its margin) where the tiles meet: near an even split, on the quietest row within a
    fifth of a tile, and never making a tile taller than EDGE_MAX. `img` is the design without its margin."""
    framed_h = img.height + 2 * MARGIN
    out, prev = [], 0
    for k in range(1, n):
        ideal = k * framed_h // n
        window = (framed_h // n) // 5
        hi = min(ideal + window, prev + EDGE_MAX)
        lo = max(ideal - window, framed_h - (n - k) * EDGE_MAX, prev + 1)
        rows = [y for y in range(max(lo, MARGIN + 1), min(hi, MARGIN + img.height - 1) + 1)]
        cut = min(rows, key=lambda y: (ink(img, y - MARGIN), abs(y - ideal))) if rows else max(prev + 1, min(ideal, prev + EDGE_MAX))
        out.append(cut)
        prev = cut
    return out


def tiles(src, out_dir, stem, tiles_max):
    with Image.open(src) as opened:
        img = Image.new("RGBA", opened.size, BACKGROUND + (255,))
        img.alpha_composite(opened.convert("RGBA"))
    img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, (EDGE_MAX - 2 * MARGIN) / w, (tiles_max * EDGE_MAX - 2 * MARGIN) / h)
    if scale < 1:
        w, h = max(1, int(w * scale)), max(1, int(h * scale))  # rounded down: the framed image must not need one tile more than tiles_max
        img = img.resize((w, h), Image.LANCZOS)
    framed = Image.new("RGB", (w + 2 * MARGIN, h + 2 * MARGIN), BACKGROUND)
    framed.paste(img, (MARGIN, MARGIN))
    n = -(-framed.height // EDGE_MAX)
    edges = [0, *cuts(img, n), framed.height]
    names = []
    for k in range(n):
        tile = framed.crop((0, edges[k], framed.width, edges[k + 1]))
        if min(tile.size) < EDGE_MIN:
            padded = Image.new("RGB", (max(tile.width, EDGE_MIN), max(tile.height, EDGE_MIN)), BACKGROUND)
            padded.paste(tile, (0, 0))
            tile = padded
        name = f"{stem}.png" if n == 1 else f"{stem}.{k + 1}.png"
        tile.save(Path(out_dir) / name)
        names.append(name)
    return names


if __name__ == "__main__":
    src, out_dir, stem, tiles_max = sys.argv[1:5]
    print(json.dumps(tiles(src, out_dir, stem, int(tiles_max))))
