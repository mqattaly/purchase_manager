"""ساخت آیکون‌های بهینه لیستیا از روی فایل اصلی لوگو.

استفاده:
    pip install pillow
    python tools/make_icons.py            # از static/logo.png می‌خواند
    python tools/make_icons.py my-logo.png

خروجی‌ها در پوشه static ساخته می‌شوند:
    logo-192.png, logo-512.png, apple-touch-icon.png,
    favicon-32.png, favicon-16.png, favicon.ico
"""

import os
import sys

from PIL import Image

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

TARGETS = [
    ("logo-192.png", 192),
    ("logo-512.png", 512),
    ("apple-touch-icon.png", 180),
    ("favicon-32.png", 32),
    ("favicon-16.png", 16),
]


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else os.path.join(STATIC_DIR, "logo.png")
    if not os.path.exists(source):
        print(f"فایل لوگو پیدا نشد: {source}")
        return 1

    image = Image.open(source).convert("RGBA")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))

    for name, size in TARGETS:
        out = os.path.join(STATIC_DIR, name)
        image.resize((size, size), Image.LANCZOS).save(out, "PNG", optimize=True)
        print(f"✓ {name} ({size}×{size}) — {os.path.getsize(out) // 1024} KB")

    ico_path = os.path.join(STATIC_DIR, "favicon.ico")
    image.resize((64, 64), Image.LANCZOS).save(
        ico_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)]
    )
    print(f"✓ favicon.ico — {os.path.getsize(ico_path) // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
