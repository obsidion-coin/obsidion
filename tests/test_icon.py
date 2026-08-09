"""Tests for the desktop icon and the generator that draws it.

An icon fails silently: Windows shows a blank rectangle and says nothing about
why. So these check the things that produce that blank rectangle — a directory
that disagrees with its payloads, a size Windows wants and cannot find — rather
than trusting that a file exists.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))

from make_icon import (  # noqa: E402
    ICON_PATH,
    SIZES,
    build_ico,
    encode_png,
    render,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _entries(data: bytes) -> list[dict]:
    """Parse the ICONDIR and its entries the way a shell would."""
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0, "the first field of an ICO must be zero"
    assert kind == 1, "type 1 is an icon; 2 would be a cursor"

    out = []
    for i in range(count):
        width, height, palette, _res, planes, bpp, length, offset = struct.unpack(
            "<BBBBHHII", data[6 + 16 * i : 6 + 16 * (i + 1)]
        )
        out.append(
            {
                # 0 means 256 in this format; the field is a single byte.
                "width": width or 256,
                "height": height or 256,
                "palette": palette,
                "planes": planes,
                "bpp": bpp,
                "payload": data[offset : offset + length],
                "length": length,
                "offset": offset,
            }
        )
    return out


def test_the_committed_icon_exists_and_parses():
    assert ICON_PATH.exists(), (
        f"{ICON_PATH} is missing - regenerate with: python deploy/make_icon.py"
    )
    entries = _entries(ICON_PATH.read_bytes())
    assert len(entries) == len(SIZES)


def test_every_size_windows_asks_for_is_present():
    """16 and 32 are the taskbar and small-icon views; 256 is large icons and
    the file dialog. A missing size makes Windows rescale another badly."""
    entries = _entries(ICON_PATH.read_bytes())
    present = {e["width"] for e in entries}

    assert present == set(SIZES)
    for required in (16, 32, 48, 256):
        assert required in present, f"{required}px is missing"


def test_each_entry_holds_a_png_of_the_size_it_advertises():
    """A directory that lies about its payload is the classic blank icon.

    The entry says "48x48 here"; if the PNG inside actually says something
    else, the shell renders nothing and reports no error.
    """
    for entry in _entries(ICON_PATH.read_bytes()):
        payload = entry["payload"]
        assert payload[:8] == PNG_SIGNATURE, (
            f"{entry['width']}px entry is not a PNG"
        )
        assert len(payload) == entry["length"], "declared length is wrong"

        # IHDR is the first chunk: 8-byte signature, 4-byte length, 4-byte type.
        assert payload[12:16] == b"IHDR"
        width, height = struct.unpack(">II", payload[16:24])
        assert (width, height) == (entry["width"], entry["height"]), (
            f"directory says {entry['width']}x{entry['height']} but the PNG "
            f"says {width}x{height}"
        )
        assert entry["bpp"] == 32, "the artwork has an alpha channel"
        assert entry["palette"] == 0, "truecolour images declare no palette"


def test_entry_offsets_stay_inside_the_file():
    data = ICON_PATH.read_bytes()
    for entry in _entries(data):
        assert entry["offset"] + entry["length"] <= len(data), (
            "an entry points past the end of the file"
        )


def test_the_committed_icon_still_matches_the_generator():
    """The art and the shipped file must not drift apart.

    The icon is committed so no downloader has to run anything, which means the
    file could quietly stop matching the code that draws it. Regenerating in
    memory and comparing is what keeps them honest - and it only works because
    the render is deterministic (fixed compression level, no randomness).
    """
    assert ICON_PATH.read_bytes() == build_ico(), (
        "assets/obsidion.ico is stale - regenerate it with: "
        "python deploy/make_icon.py"
    )


def test_rendering_is_deterministic():
    """Two runs must produce identical bytes, or the test above is a coin toss."""
    assert build_ico() == build_ico()
    assert render(32) == render(32)


def test_the_artwork_is_transparent_outside_the_gem_and_violet_inside():
    """Corners must be fully transparent so the icon sits on any wallpaper, and
    the centre must actually be the accent colour rather than an empty canvas."""
    size = 64
    rgba = render(size)

    def pixel(x: int, y: int) -> tuple[int, int, int, int]:
        i = (y * size + x) * 4
        return tuple(rgba[i : i + 4])

    for corner in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
        assert pixel(*corner)[3] == 0, f"{corner} should be transparent"

    # Middle of the stone: opaque, and violet (blue and red dominant, and
    # clearly not grey).
    r, g, b, a = pixel(size // 2, size // 2)
    assert a == 255, "the centre of the gem must be opaque"
    assert b > g and r > g, f"expected a violet, got rgb({r},{g},{b})"


def test_a_tiny_icon_drops_detail_it_cannot_render():
    """At 16px the facet seams are thinner than a pixel; drawn there they turn
    into mush. The simplified form keeps the silhouette instead."""
    from make_icon import gem_facets

    assert len(gem_facets(detailed=False)) < len(gem_facets(detailed=True))
    # Still recognisably a shape, not an empty canvas.
    assert any(byte for byte in render(16)), "the 16px icon rendered blank"


def test_a_png_round_trips_through_the_encoder():
    """Guard the hand-rolled encoder: zlib must give the pixels back exactly."""
    import zlib

    size = 8
    rgba = render(size)
    png = encode_png(size, rgba)

    # Walk the chunks to find IDAT rather than assuming a byte offset.
    offset, idat = 8, b""
    while offset < len(png):
        length = struct.unpack(">I", png[offset : offset + 4])[0]
        kind = png[offset + 4 : offset + 8]
        if kind == b"IDAT":
            idat += png[offset + 8 : offset + 8 + length]
        offset += 12 + length

    raw = zlib.decompress(idat)
    stride = size * 4
    for y in range(size):
        start = y * (stride + 1)
        assert raw[start] == 0, "each row is written with filter type 0"
        assert raw[start + 1 : start + 1 + stride] == rgba[y * stride : (y + 1) * stride]


def test_the_shortcut_installer_points_at_the_icon_and_survives_it_missing():
    """The installer must prefer the gem but never fail without it - a missing
    icon file must not cost somebody their shortcut."""
    script = (
        Path(__file__).resolve().parent.parent / "deploy" / "install-hud-shortcut.ps1"
    ).read_text(encoding="utf-8")

    assert "assets\\obsidion.ico" in script
    assert "Test-Path $icon" in script
    # The fallback to Python's own icon is still there.
    assert '"$python,0"' in script
    # Same ASCII discipline as the other PowerShell scripts: a UTF-8 dash is
    # read as a smart quote under PS 5.1 and breaks the parse.
    assert not [c for c in script if ord(c) > 127], "install script must be ASCII"


def test_both_front_ends_serve_the_icon_at_the_path_browsers_ask_for():
    """Browsers request /favicon.ico unprompted, so serving it there means the
    tab gets the gem with no markup and no second copy of the artwork."""
    from explorer.app import create_app as explorer_app
    from hud.app import create_app as hud_app

    for build in (hud_app, explorer_app):
        client = build(1, "token").test_client()
        response = client.get("/favicon.ico")

        assert response.status_code == 200
        assert response.data[:4] == b"\x00\x00\x01\x00", "not an ICO"
        assert response.data == ICON_PATH.read_bytes()
