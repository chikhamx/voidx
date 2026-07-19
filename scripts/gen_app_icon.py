#!/usr/bin/env python3
"""生成 voidx 应用图标：深色圆角方底 + 蓝色渐变火花（Kimi 风格）。

用法: ./python.py scripts/gen_app_icon.py
输出: desktop/tauri/icons/icon.png (1024) 与 icon.ico (多尺寸)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).resolve().parent.parent / "desktop" / "tauri" / "icons"
MASTER = 1024

BG_TOP = (30, 31, 36)
BG_BOTTOM = (16, 17, 20)
GLOW = (46, 107, 255)
SPARK_TOP = (138, 175, 255)
SPARK_BOTTOM = (46, 107, 255)


def vertical_gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        img.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return img.resize((size, size))


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size * 4, size * 4), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size * 4 - 1, size * 4 - 1], radius=radius * 4, fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def sparkle_points(cx: float, cy: float, r: float, ctrl: float = 0.16, steps: int = 32) -> list[tuple[float, float]]:
    tips = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
    c = ctrl * r * math.sqrt(2) / 2
    ctrls = [(cx + c, cy - c), (cx + c, cy + c), (cx - c, cy + c), (cx - c, cy - c)]
    pts: list[tuple[float, float]] = []
    for i in range(4):
        p0, p1, p2 = tips[i], ctrls[i], tips[(i + 1) % 4]
        for s in range(steps):
            t = s / steps
            x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t**2 * p2[0]
            y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t**2 * p2[1]
            pts.append((x, y))
    return pts


def sparkle_mask(size: int, shapes: list[tuple[float, float, float]]) -> Image.Image:
    mask = Image.new("L", (size * 4, size * 4), 0)
    d = ImageDraw.Draw(mask)
    for cx, cy, r in shapes:
        d.polygon(sparkle_points(cx * 4, cy * 4, r * 4), fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def build_master(size: int = MASTER) -> Image.Image:
    radius = round(size * 0.225)

    base = vertical_gradient(size, BG_TOP, BG_BOTTOM).convert("RGBA")
    base.putalpha(rounded_mask(size, radius))

    # 蓝色径向辉光，位于主火花后方偏上
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gr = size * 0.42
    gcx, gcy = size * 0.5, size * 0.47
    gd.ellipse([gcx - gr, gcy - gr, gcx + gr, gcy + gr], fill=(*GLOW, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.12))
    base = Image.alpha_composite(base, Image.composite(glow, Image.new("RGBA", (size, size), (0, 0, 0, 0)), rounded_mask(size, radius)))

    # 火花：主火花 + 右上小火花
    shapes = [
        (size * 0.5, size * 0.52, size * 0.30),
        (size * 0.745, size * 0.255, size * 0.085),
    ]
    sparks = vertical_gradient(size, SPARK_TOP, SPARK_BOTTOM).convert("RGBA")
    sparks.putalpha(sparkle_mask(size, shapes))
    base = Image.alpha_composite(base, sparks)

    # 顶部细高光，增强立体感
    highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    hd.rounded_rectangle([size * 0.02, size * 0.015, size * 0.98, size * 0.5], radius=radius, fill=(255, 255, 255, 14))
    highlight = highlight.filter(ImageFilter.GaussianBlur(size * 0.02))
    base = Image.alpha_composite(base, Image.composite(highlight, Image.new("RGBA", (size, size), (0, 0, 0, 0)), rounded_mask(size, radius)))

    return base


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    master = build_master()
    png_path = OUT_DIR / "icon.png"
    master.save(png_path, "PNG")

    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(OUT_DIR / "icon.ico", format="ICO", sizes=ico_sizes, append_images=[])

    print(f"written: {png_path} ({master.size[0]}x{master.size[1]})")
    print(f"written: {OUT_DIR / 'icon.ico'} sizes={[s[0] for s in ico_sizes]}")


if __name__ == "__main__":
    main()
