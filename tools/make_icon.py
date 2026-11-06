#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成软件图标 icon.ico（多尺寸）与 docs/img/logo.png。

画法：2048 超采样 → LANCZOS 缩小，保证 16px 下对勾依旧清晰。
构图：圆角方块 + 绿→青对角渐变 + 白色圆头对勾 + 细白描边（增加层次）。
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根
S = 2048          # 画布边长
RADIUS = 0.22     # 圆角占比


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(size, c1, c2):
    """对角线性渐变（左上 c1 → 右下 c2）。小图画完再放大，避免百万级逐像素循环。"""
    n = 256
    img = Image.new("RGB", (n, n))
    px = img.load()
    for y in range(n):
        for x in range(n):
            t = (x + y) / (2 * (n - 1))
            px[x, y] = lerp(c1, c2, t)
    return img.resize((size, size), Image.BILINEAR)


def rounded_mask(size, radius):
    m = Image.new("L", (size * 2, size * 2), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size * 2 - 1, size * 2 - 1],
                        radius=radius * 2, fill=255)
    return m.resize((size, size), Image.LANCZOS)


def main():
    top = (0x3E, 0xE9, 0xB0)      # 薄荷绿
    bottom = (0x0E, 0x9F, 0xDB)   # 晴空蓝
    base = gradient(S, top, bottom)

    # 内描边：白色 12% 的一圈，让方块更精致
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dr = ImageDraw.Draw(ring)
    inset = int(S * 0.045)
    dr.rounded_rectangle([inset, inset, S - inset, S - inset],
                         radius=int(S * (RADIUS - 0.02)), outline=(255, 255, 255, 32),
                         width=int(S * 0.012))
    base = Image.alpha_composite(base.convert("RGBA"), ring)

    # 白色圆头对勾（4 倍线宽的圆点补圆角，保证接头平滑）
    check = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dc = ImageDraw.Draw(check)
    w = int(S * 0.082)
    pts = [(S * 0.27, S * 0.515), (S * 0.435, S * 0.685), (S * 0.745, S * 0.335)]
    dc.line(pts, fill=(255, 255, 255, 255), width=w, joint="curve")
    for p in pts:
        dc.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2],
                   fill=(255, 255, 255, 255))
    base = Image.alpha_composite(base, check)

    # 应用圆角
    base.putalpha(rounded_mask(S, int(S * RADIUS)))

    os.makedirs(os.path.join(HERE, "docs", "img"), exist_ok=True)
    logo = base.resize((512, 512), Image.LANCZOS)
    logo.save(os.path.join(HERE, "docs", "img", "logo.png"))

    ico = base.resize((256, 256), Image.LANCZOS)
    ico.save(os.path.join(HERE, "icon.ico"), format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                    (128, 128), (256, 256)])

    print("icon.ico  ->", os.path.getsize(os.path.join(HERE, "icon.ico")), "bytes")
    print("logo.png  ->", os.path.getsize(os.path.join(HERE, "docs", "img", "logo.png")), "bytes")


if __name__ == "__main__":
    main()
