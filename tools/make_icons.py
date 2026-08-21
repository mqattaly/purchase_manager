"""Build optimized Listia icons from the square source logo.

Usage:
    pip install pillow
    python tools/make_icons.py [optional-source.png]
"""

import sys
from pathlib import Path

from PIL import Image

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
TARGETS = (
    ("logo-192.png", 192),
    ("logo-512.png", 512),
    ("apple-touch-icon.png", 180),
    ("favicon.png", 256),
    ("favicon-32.png", 32),
    ("favicon-16.png", 16),
)


def optimized_png(image, size, output):
    resized = image.resize((size, size), Image.Resampling.LANCZOS)
    # A palette is materially smaller for app icons and remains visually
    # indistinguishable at these sizes. FASTOCTREE preserves RGBA transparency.
    quantized = resized.quantize(
        colors=256,
        method=Image.Quantize.FASTOCTREE,
        dither=Image.Dither.FLOYDSTEINBERG,
    )
    quantized.save(output, "PNG", optimize=True)


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else STATIC_DIR / "logo.png"
    if not source.exists():
        print(f"فایل لوگو پیدا نشد: {source}")
        return 1

    image = Image.open(source).convert("RGBA")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))

    for name, size in TARGETS:
        output = STATIC_DIR / name
        optimized_png(image, size, output)
        print(f"✓ {name} ({size}×{size}) — {output.stat().st_size // 1024} KB")

    icon_path = STATIC_DIR / "favicon.ico"
    image.resize((64, 64), Image.Resampling.LANCZOS).save(
        icon_path,
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
    )
    print(f"✓ favicon.ico — {icon_path.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
