"""
Build assets/icon.ico (multi-size, 16..256) from one of two sources:

  1. assets/icon_source.png  — if present, use this (your hand-made deli icon).
                                Should be a square PNG, ideally 1024x1024.
  2. otherwise, generate a placeholder programmatically.

    py -3.13 make_icon.py
"""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent / "assets"
SOURCE_PNG = ASSETS / "icon_source.png"
OUT = ASSETS / "icon.ico"
PNG_OUT = ASSETS / "icon.png"

BG = (18, 18, 18, 255)
RING = (60, 60, 60, 255)
ACCENT = (74, 144, 226, 255)         # cyan-blue, matches transport-bar accent
ACCENT_BRIGHT = (110, 170, 240, 255)
PERF = (230, 230, 230, 255)


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Outer disc with thin ring.
    pad = max(2, size // 32)
    ring_w = max(1, size // 64)
    d.ellipse([(pad, pad), (size - pad, size - pad)], fill=BG, outline=RING, width=ring_w)

    # Film perforations on the left and right edges (only show on 32px+).
    if size >= 32:
        perf_w = max(2, size // 18)
        perf_h = max(2, size // 14)
        gap = max(2, size // 14)
        x_left = size // 12
        x_right = size - size // 12 - perf_w
        # Five perforations stacked on each side.
        cy = size // 2
        for i in range(-2, 3):
            y = cy + i * (perf_h + gap) - perf_h // 2
            d.rounded_rectangle(
                [(x_left, y), (x_left + perf_w, y + perf_h)],
                radius=max(1, size // 64),
                fill=PERF,
            )
            d.rounded_rectangle(
                [(x_right, y), (x_right + perf_w, y + perf_h)],
                radius=max(1, size // 64),
                fill=PERF,
            )

    # Play triangle, slightly off-centre right for visual balance.
    cx, cy = size // 2, size // 2
    tri_h = size * 5 // 12
    tri_w = int(tri_h * 0.95)
    apex_x = cx + tri_w // 3
    base_x = cx - tri_w * 2 // 3
    points = [
        (base_x, cy - tri_h // 2),
        (base_x, cy + tri_h // 2),
        (apex_x, cy),
    ]
    d.polygon(points, fill=ACCENT)
    # Highlight on the leading edge.
    if size >= 48:
        d.line([points[0], points[2]], fill=ACCENT_BRIGHT, width=max(1, size // 96))

    return img


def _from_source_png(path: Path) -> Image.Image:
    """Load a custom PNG, square-crop, preserve original transparency."""
    img = Image.open(path).convert("RGBA")
    w, h = img.size
    if w != h:
        s = min(w, h)
        left = (w - s) // 2
        top = (h - s) // 2
        img = img.crop((left, top, left + s, top + s))
    return img


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    sizes = [256, 128, 96, 64, 48, 32, 24, 16]

    if SOURCE_PNG.is_file():
        print(f"Using custom source: {SOURCE_PNG}")
        base = _from_source_png(SOURCE_PNG).resize((256, 256), Image.LANCZOS)
    else:
        print("No custom source found at assets/icon_source.png — generating placeholder.")
        base = render(256)

    base.save(OUT, format="ICO", sizes=[(s, s) for s in sizes])
    base.save(PNG_OUT, format="PNG")
    print(f"Wrote {OUT}  ({', '.join(f'{s}x{s}' for s in sizes)})")
    print(f"Wrote {PNG_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
