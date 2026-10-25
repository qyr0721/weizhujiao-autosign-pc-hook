#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_remote_zip.py — 不下整个 zip，只取其中一个条目来验证

集成版 zip 有 111 MB，而我们要看的只是里面的 wxsign.exe（约 3 MB）。
zip 的本地文件头在条目数据之前，所以可以从头顺序解析，拿到目标条目后就
不再下载——用 HTTP Range 只取开头几 MB 即可。

用法
  python tools/verify_remote_zip.py <zip_url> [entry_name] [out_path]
"""
import io
import struct
import sys
import urllib.request
import zlib

PREFIX = 6 << 20          # 只取开头 6 MB，足够覆盖前面的条目
DEFAULT_ENTRY = "wxsign/wxsign.exe"


def get(url, headers=None):
    r = urllib.request.Request(url, headers=headers or {})
    r.add_header("User-Agent", "verify-remote-zip")
    with urllib.request.urlopen(r, timeout=120) as resp:
        return resp.read()


def find_entry(buf, want):
    """从缓冲区头部顺序解析本地文件头，返回 (name, 数据起始, csize, usize, method)。"""
    pos = 0
    while pos + 30 <= len(buf):
        sig, = struct.unpack_from("<I", buf, pos)
        if sig != 0x04034B50:
            break
        (_ver, flags, method, _mt, _md, _crc, csize, usize,
         nlen, elen) = struct.unpack_from("<HHHHHIIIHH", buf, pos + 4)
        name = buf[pos + 30:pos + 30 + nlen].decode("utf-8", "replace")
        data = pos + 30 + nlen + elen
        if name == want:
            return name, data, csize, usize, method, flags
        if csize == 0 and (flags & 0x08):
            break                      # 长度在数据描述符里，顺序解析无法跳过
        pos = data + csize
    return None


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    want = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_ENTRY
    out = sys.argv[3] if len(sys.argv) > 3 else "dist/.remote_exe"
    if not url:
        print("用法: python tools/verify_remote_zip.py <zip_url> [entry] [out]")
        return 2

    print("取前 %d 字节：%s" % (PREFIX, url))
    buf = get(url, {"Range": "bytes=0-%d" % (PREFIX - 1)})
    got = find_entry(buf, want)
    if not got:
        print("在前缀里没找到 %s（可能条目顺序靠后，需加大 PREFIX）" % want)
        return 2
    _name, data, csize, usize, method, flags = got
    print("命中 %s  csize=%s usize=%s method=%s" % (want, csize, usize, method))
    if data + csize > len(buf):
        print("数据不完整，需加大 PREFIX")
        return 2

    raw = buf[data:data + csize]
    if method == 0:
        blob = raw
    elif method == 8:
        blob = zlib.decompress(raw, -15)
    else:
        print("不支持的压缩方法 %s" % method)
        return 2

    with open(out, "wb") as f:
        f.write(blob)
    print("已解出 %s → %s（%s bytes）" % (want, out, format(len(blob), ",")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
