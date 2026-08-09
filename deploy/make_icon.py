"""Draw the Obsidion icon and write assets/obsidion.ico.

The desktop shortcut wore Python's own logo, which made a wallet look like a
stray script. This produces the icon it wears instead: a faceted obsidian
shard in the violet the HUD and explorer already use (`--accent: #9b6dff`), on
a transparent background so it sits cleanly on any wallpaper.

**Why draw it in code rather than ship a hand-made file.** The project's only
runtime dependencies are `flask` and `ecdsa`. Adding an imaging library so a
downloader can have an icon would be a poor trade, and CI would have to install
it too. A Windows `.ico` is a small directory header followed by one PNG per
size, and PNG encoding is `zlib` plus a handful of `struct` calls — both
standard library. So this needs nothing installed, and because the drawing is
deterministic the committed file can be checked against it byte for byte
(`tests/test_icon.py`), which is what stops the art and the shipped icon
drifting apart.

Run it only when the artwork changes:

    python deploy/make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ICON_PATH = REPO_ROOT / "assets" / "obsidion.ico"

#: Sizes Windows actually asks for: 16 and 32 in the taskbar and small-icon
#: views, 48 for medium, 256 for large icons and the file dialog preview. The
#: rest fill the gaps so Windows never has to rescale one of ours badly.
SIZES = (16, 24, 32, 48, 64, 128, 256)

#: Supersampling factor. Drawing at 4x and averaging down is the whole of the
#: antialiasing: no dependency, and edges that would otherwise be a staircase.
SUPERSAMPLE = 4

# The palette, taken from the accent already in use across both front-ends so
# the icon and the apps read as one thing.
VIOLET_LIGHT = (198, 163, 255)   # lit facets, top-left
VIOLET = (155, 109, 255)         # --accent, the body of the stone
VIOLET_DEEP = (104, 62, 190)     # facets turned away from the light
VIOLET_SHADOW = (61, 33, 120)    # the underside
HIGHLIGHT = (232, 216, 255)      # the one bright edge that says "glass"

Point = tuple[float, float]
Colour = tuple[int, int, int]


# --------------------------------------------------------------------------
# The shape
# --------------------------------------------------------------------------


def gem_facets(detailed: bool) -> list[tuple[list[Point], Colour]]:
    """The gem as flat-filled polygons, in unit coordinates (0..1).

    Facets are listed back to front. Coordinates are fractions of the canvas so
    one description serves every size.

    `detailed=False` returns the simplified form used at 16px, where the facet
    seams are thinner than a pixel: drawn there they turn into grey mush and
    the shape stops reading. A silhouette plus a single highlight survives the
    size, which is the point of having it at all.
    """
    # A cut stone: table (flat top), crown facets, and a pavilion tapering to
    # a point. Slightly taller than wide so it reads as a shard, not a rhombus.
    top_left = (0.30, 0.30)
    top_right = (0.70, 0.30)
    left = (0.12, 0.42)
    right = (0.88, 0.42)
    bottom = (0.50, 0.92)
    peak_left = (0.36, 0.12)
    peak_right = (0.64, 0.12)

    if not detailed:
        return [
            # Whole silhouette in one fill, then one lit edge for depth.
            (
                [peak_left, peak_right, right, bottom, left],
                VIOLET,
            ),
            (
                [peak_left, (0.50, 0.16), (0.50, 0.92), left],
                VIOLET_LIGHT,
            ),
        ]

    return [
        # Pavilion (the lower half), split so one side catches the light.
        ([left, bottom, (0.50, 0.42)], VIOLET_DEEP),
        ([(0.50, 0.42), bottom, right], VIOLET_SHADOW),
        # Crown: the angled band between the table and the girdle.
        ([peak_left, top_left, left], VIOLET_LIGHT),
        ([peak_right, right, top_right], VIOLET_DEEP),
        ([top_left, (0.50, 0.42), left], VIOLET),
        ([top_right, right, (0.50, 0.42)], VIOLET_DEEP),
        ([top_left, top_right, (0.50, 0.42)], VIOLET),
        # Table: the flat top, brightest surface.
        ([peak_left, peak_right, top_right, top_left], VIOLET_LIGHT),
        # A single specular edge down the left crown. One highlight reads as
        # polished stone; more than one reads as noise.
        ([peak_left, (0.42, 0.14), (0.335, 0.30), top_left], HIGHLIGHT),
    ]


def _fill_polygon(
    pixels: list[list[tuple[int, int, int, int]]],
    polygon: list[Point],
    colour: Colour,
    canvas: int,
) -> None:
    """Scanline-fill one polygon, opaque, onto the supersampled canvas.

    Even-odd rule, sampling at each row's centre. Facets share edges exactly,
    so painting back to front leaves no seams — and because every fill is
    opaque, overlap is decided by order rather than by blending.
    """
    points = [(x * canvas, y * canvas) for x, y in polygon]
    count = len(points)

    ys = [y for _, y in points]
    y_start = max(0, int(min(ys)))
    y_end = min(canvas - 1, int(max(ys)) + 1)

    for row in range(y_start, y_end + 1):
        centre = row + 0.5
        crossings: list[float] = []
        for i in range(count):
            x1, y1 = points[i]
            x2, y2 = points[(i + 1) % count]
            if (y1 <= centre < y2) or (y2 <= centre < y1):
                # Where this edge crosses the row's centre line.
                crossings.append(x1 + (centre - y1) / (y2 - y1) * (x2 - x1))
        crossings.sort()
        for i in range(0, len(crossings) - 1, 2):
            left = max(0, int(crossings[i] + 0.5))
            right = min(canvas - 1, int(crossings[i + 1] - 0.5))
            for column in range(left, right + 1):
                pixels[row][column] = (*colour, 255)


def render(size: int) -> bytes:
    """Draw the gem at `size` and return raw RGBA rows.

    Renders at SUPERSAMPLE times the target and box-downsamples, averaging
    alpha along with colour so the edge fades out instead of stepping.
    """
    canvas = size * SUPERSAMPLE
    pixels = [[(0, 0, 0, 0)] * canvas for _ in range(canvas)]

    for polygon, colour in gem_facets(detailed=size >= 24):
        _fill_polygon(pixels, polygon, colour, canvas)

    out = bytearray()
    factor = SUPERSAMPLE
    weight = factor * factor
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(factor):
                row = pixels[y * factor + dy]
                for dx in range(factor):
                    pr, pg, pb, pa = row[x * factor + dx]
                    # Weight colour by coverage, or transparent pixels would
                    # drag the edge toward black rather than toward nothing.
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a:
                out += bytes((r // a, g // a, b // a, a // weight))
            else:
                out += b"\x00\x00\x00\x00"
    return bytes(out)


# --------------------------------------------------------------------------
# PNG and ICO containers, by hand
# --------------------------------------------------------------------------


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_png(size: int, rgba: bytes) -> bytes:
    """A minimal 8-bit RGBA PNG: signature, IHDR, IDAT, IEND.

    Every row is prefixed with filter type 0 (none). Filtering would compress
    better, but an icon is a few kilobytes either way and unfiltered rows are
    obviously correct.
    """
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw += rgba[y * stride : (y + 1) * stride]

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        # Fixed compression level, so the same art always gives the same bytes.
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def build_ico(sizes=SIZES) -> bytes:
    """Assemble the .ico: ICONDIR, one ICONDIRENTRY each, then the PNGs.

    Entries carry PNG payloads rather than the older BMP form, which Windows
    has understood since Vista and which keeps 256px from bloating the file.
    A 256px image is recorded as 0 in the single-byte width and height fields,
    as the format requires.
    """
    images = [encode_png(size, render(size)) for size in sizes]

    # 6-byte header, then a 16-byte entry per image; payloads follow.
    offset = 6 + 16 * len(images)
    directory = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    for size, png in zip(sizes, images):
        directory += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0,          # palette size; 0 for truecolour
            0,          # reserved
            1,          # colour planes
            32,         # bits per pixel
            len(png),
            offset,
        )
        offset += len(png)

    return bytes(directory) + b"".join(images)


def favicon_data_uri(size: int = 32) -> str:
    """The same artwork as a `data:` URI, for the HUD and explorer tabs.

    Inlined rather than served: no route, no extra request, and nothing to get
    out of step with the icon on the desktop.
    """
    import base64

    png = encode_png(size, render(size))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def main() -> None:
    ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = build_ico()
    ICON_PATH.write_bytes(data)
    print(f"wrote {ICON_PATH} ({len(data):,} bytes, sizes {', '.join(map(str, SIZES))})")


if __name__ == "__main__":
    main()
