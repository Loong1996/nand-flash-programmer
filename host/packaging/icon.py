#!/usr/bin/env python3
"""Draw the nsprog app icon (same design as the web UI's app icon).

    python host/packaging/icon.py OUTDIR   # -> nsprog.png (1024), nsprog.icns, nsprog.ico

Needs Pillow (``pip install pillow``); Pillow writes .icns and .ico on every OS.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def draw(size: int = 1024) -> Image.Image:
    s = size
    # diagonal gradient #5AC8FA -> #5856D6 -> #AF52DE
    grad = Image.new("RGB", (s, s))
    stops = [(0.0, (0x5A, 0xC8, 0xFA)), (0.7, (0x58, 0x56, 0xD6)), (1.0, (0xAF, 0x52, 0xDE))]
    px = grad.load()
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2 * (s - 1))
            for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
                if t <= t1:
                    k = (t - t0) / (t1 - t0)
                    px[x, y] = tuple(int(a + (b - a) * k) for a, b in zip(c0, c1))
                    break
    # macOS icon grid: 824 px rounded square centred in 1024
    inset, radius = int(s * 0.0977), int(s * 0.18)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((inset, inset, s - inset, s - inset), radius, fill=255)
    icon = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((inset, inset + s // 60, s - inset, s - inset + s // 60),
                                             radius, fill=(40, 30, 120, 110))
    icon.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(s // 50)))
    body = grad.convert("RGBA")
    body.putalpha(mask)
    icon.alpha_composite(body)
    # chip glyph
    d = ImageDraw.Draw(icon)
    w = max(2, s // 26)
    g0, g1 = int(s * 0.34), int(s * 0.66)
    d.rounded_rectangle((g0, g0, g1, g1), radius=s // 22, outline="white", width=w)
    for k in (0.43, 0.57):
        p = int(s * k)
        for a, b in ((int(s * 0.25), g0), (g1, int(s * 0.75))):
            d.line((p, a, p, b), fill="white", width=w)
            d.line((a, p, b, p), fill="white", width=w)
    # top highlight
    fade = Image.new("L", (s, s), 0)
    fd = ImageDraw.Draw(fade)
    for y in range(inset, s // 2):                  # white glaze fading out towards the middle
        fd.line((0, y, s, y), fill=int(56 * (1 - (y - inset) / (s // 2 - inset)) ** 1.6))
    hl = Image.new("RGBA", (s, s), (255, 255, 255, 0))
    hl.putalpha(Image.composite(fade, Image.new("L", (s, s), 0), mask))
    icon.alpha_composite(hl)
    return icon


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    img = draw(1024)
    img.save(out / "nsprog.png")
    img.save(out / "nsprog.icns")
    img.save(out / "nsprog.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    print("icons written to %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
