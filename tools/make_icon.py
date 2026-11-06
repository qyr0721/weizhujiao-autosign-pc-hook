#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成软件图标 icon.ico（多尺寸）与 docs/img/logo.png。

画法：2048 超采样 → LANCZOS 缩小，保证 16px 下对勾依旧清晰。
构图：圆角方块 + 绿→青对角渐变 + 白色圆头对勾 + 细白描边（增加层次）。

ICO 编码（关键）：16-128px 用未压缩 BMP 条目（BGRA + AND 掩码），
256px 用 PNG 条目。PIL 默认把所有尺寸都存成 PNG 条目，部分 Windows
图标提取器对小尺寸 PNG 条目会显示空白/默认图标，所以这里手工打包。
"""
import os
import struct
from io import BytesIO

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 2048          # 画布边长
RADIUS = 0.22     # 圆角占比
BMP_SIZES = [16, 24, 32, 48, 64, 128]
PNG_SIZE = 256


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


def make_base():
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

    # 白色圆头对勾（端点画圆补圆角，保证接头平滑）
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
    return base


def bmp_entry(im, size):
    """未压缩 BMP 条目：BITMAPINFOHEADER + BGRA（自底向上）+ AND 掩码。"""
    frame = im.resize((size, size), Image.LANCZOS).convert("RGBA")
    raw = frame.tobytes()
    stride = size * 4
    xor = bytearray()
    for y in range(size - 1, -1, -1):          # BMP 自底向上
        row = raw[y * stride:(y + 1) * stride]
        for x in range(size):
            r, g, b, a = row[x * 4:x * 4 + 4]
            xor += bytes((b, g, r, a))         # BGRA
    mask_stride = ((size + 31) // 32) * 4
    and_mask = bytes(mask_stride * size)       # 全 0：透明度交给 alpha
    bih = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                      len(xor) + len(and_mask), 0, 0, 0, 0)
    return bih + bytes(xor) + and_mask


def png_entry(im, size):
    buf = BytesIO()
    im.resize((size, size), Image.LANCZOS).save(buf, format="PNG")
    return buf.getvalue()


def save_ico(path, im):
    entries = [(s, bmp_entry(im, s)) for s in BMP_SIZES]
    entries.append((PNG_SIZE, png_entry(im, PNG_SIZE)))

    count = len(entries)
    offset = 6 + 16 * count
    head = struct.pack("<HHH", 0, 1, count)
    dir_bytes = b""
    blobs = b""
    for s, data in entries:
        dim = 0 if s >= 256 else s            # 256 在条目里记 0
        dir_bytes += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                 len(data), offset)
        offset += len(data)
        blobs += data
    with open(path, "wb") as f:
        f.write(head + dir_bytes + blobs)


def main():
    base = make_base()
    os.makedirs(os.path.join(HERE, "docs", "img"), exist_ok=True)

    base.resize((512, 512), Image.LANCZOS).save(
        os.path.join(HERE, "docs", "img", "logo.png"))

    save_ico(os.path.join(HERE, "icon.ico"), base)

    print("icon.ico  ->", os.path.getsize(os.path.join(HERE, "icon.ico")), "bytes")
    print("logo.png  ->", os.path.getsize(os.path.join(HERE, "docs", "img", "logo.png")), "bytes")


if __name__ == "__main__":
    main()
