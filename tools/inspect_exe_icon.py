#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""排查 exe 内嵌图标：解析 PE 资源，导出 RT_GROUP_ICON 目录与各 RT_ICON 图像。

用法
  python tools/inspect_exe_icon.py dist/wxsign-portable.exe [输出目录]
"""
import os
import struct
import sys


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def parse(data):
    pe = data.find(b"PE\0\0")
    if pe < 0:
        raise SystemExit("不是 PE 文件")
    nsec = u16(data, pe + 6)
    optsz = u16(data, pe + 20)
    opt = pe + 24
    magic = u16(data, opt)
    dd = opt + (112 if magic == 0x20B else 96)
    rsrc_rva = u32(data, dd + 2 * 8)
    sec = opt + optsz
    sections = []
    rsrc_off = None
    for i in range(nsec):
        s = sec + i * 40
        vsz, va, rsz, roff = u32(data, s + 8), u32(data, s + 12), u32(data, s + 16), u32(data, s + 20)
        sections.append((va, max(vsz, rsz), roff))
        if va <= rsrc_rva < va + max(vsz, rsz):
            rsrc_off = roff + (rsrc_rva - va)
    if rsrc_off is None:
        raise SystemExit("没有资源段")

    def walk(off, path):
        out = []
        for i in range(u16(data, off + 12) + u16(data, off + 14)):
            e = off + 16 + i * 8
            name, doff = u32(data, e), u32(data, e + 4)
            if doff & 0x80000000:
                out += walk(rsrc_off + (doff & 0x7FFFFFFF), path + [name & 0xFFFF])
            else:
                out.append((path + [name & 0xFFFF], rsrc_off + doff))
        return out

    def rva2off(rva):
        for va, sz, roff in sections:
            if va <= rva < va + sz:
                return roff + (rva - va)
        return None

    leaves = walk(rsrc_off, [])
    group = [l for l in leaves if l[0][0] == 14]
    icons = {l[0][1]: l for l in leaves if l[0][0] == 3}
    return data, group, icons, rva2off


def main():
    path = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else "."
    data = open(path, "rb").read()
    data, group, icons, rva2off = parse(data)

    print("文件:", path, "大小:", len(data))
    print("RT_GROUP_ICON 数:", len(group), " RT_ICON 数:", len(icons))
    if not group:
        print("!! 没有任何 RT_GROUP_ICON —— 资源管理器就会显示默认图标")
        return 1

    # 解析 GRPICONDIR：结构与 ICONDIR 相同，最后是 GRPICONDIRENTRY 数组
    off = group[0][1]
    rva, size = u32(data, off), u32(data, off + 4)
    gd = data[rva2off(rva):rva2off(rva) + size]
    reserved, gtype, count = struct.unpack_from("<HHH", gd, 0)
    print("GRPICONDIR: reserved=%d type=%d count=%d" % (reserved, gtype, count))

    ok = True
    entries = []
    for i in range(count):
        # ICONDIR 头是 6 字节（reserved/type/count），条目从第 6 字节开始
        e = 6 + i * 14
        # GRPICONDIRENTRY: bWidth, bHeight, bColorCount, bReserved,
        #                 wPlanes, wBitCount, dwBytesInRes, nID
        w, h, cc, rs, planes, bpp, sz, nid = struct.unpack_from("<BBBBHHIH", gd, e)
        entries.append((w or 256, h or 256, bpp, sz, nid))
        exists = nid in icons
        if not exists:
            ok = False
        print("  条目%d: %3dx%-3d bpp=%-2d size=%-7d -> RT_ICON#%-4d %s"
              % (i, w or 256, h or 256, bpp, sz, nid, "存在" if exists else "!! 缺失"))

    if not ok:
        print("!! 有条目指向不存在的 RT_ICON，图标无法显示")
    else:
        print("所有条目都能找到对应的 RT_ICON 图像")

    # 导出各尺寸图像为 PNG，肉眼确认图案
    try:
        from PIL import Image
    except ImportError:
        print("(无 PIL，跳过导出)")
        return 0 if ok else 1

    os.makedirs(outdir, exist_ok=True)
    for w, h, bpp, sz, nid in entries:
        if nid not in icons:
            continue
        ioff = icons[nid][1]
        rva, size = u32(data, ioff), u32(data, ioff + 4)
        blob = data[rva2off(rva):rva2off(rva) + size]
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            im = Image.open(__import__("io").BytesIO(blob))
        else:
            # BMP 条目：BITMAPINFOHEADER + 上下颠倒的 BGRA + AND 掩码
            biw, bih = struct.unpack_from("<ii", blob, 4)
            planes2, bpp2 = struct.unpack_from("<HH", blob, 12)
            compression = struct.unpack_from("<I", blob, 16)[0]
            # BITMAPINFOHEADER 没有 offBits 字段：像素数据紧跟头部（biSize=40）
            off_bits = struct.unpack_from("<I", blob, 0)[0]
            w2, h2 = biw, abs(bih) // 2
            if compression != 0:
                print("  跳过 %dx%d（压缩格式 %d）" % (w2, h2, compression))
                continue
            stride = ((w2 * bpp2 + 31) // 32) * 4
            px = blob[off_bits:off_bits + stride * h2]
            im = Image.new("RGBA", (w2, h2))
            for y in range(h2):
                row = px[y * stride:(y + 1) * stride]
                for x in range(w2):
                    if bpp2 == 32:
                        b, g, r, a = row[x * 4:x * 4 + 4]
                    else:                      # 24bpp / 8bpp：按调色板近似取灰度
                        if bpp2 == 24:
                            b, g, r = row[x * 3:x * 3 + 3]
                        else:
                            v = row[x] if x < len(row) else 0
                            b = g = r = v
                        a = 255
                    im.putpixel((x, h2 - 1 - y), (r, g, b, a))
        fp = os.path.join(outdir, "icon_%dx%d.png" % (w, h))
        im.save(fp)
        print("  导出", fp)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
