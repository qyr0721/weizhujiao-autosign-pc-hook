#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pe_scan.py — 无 IDA 环境下的 PE 字符串 + 交叉引用扫描

用途：给「让微信自己在后台跳 OAuth」找 hook 点。
      先找 open.weixin.qq.com / oauth2 / waerrpage 这类字符串，
      再找 .text 里 RIP 相对寻址引用它们的指令位置（RVA）。

不依赖任何第三方库：自己解 PE 头，用 mmap + bytes.find + 正则做扫描。

用法：
  python tools/pe_scan.py <dll路径> --kw open.weixin.qq.com oauth2 jump2h5err
  python tools/pe_scan.py <dll路径> --kw ... --json out.json
  python tools/pe_scan.py <dll路径> --rva 0x2d5a240      # 反查某个 RVA 属于哪个函数起点附近

输出：每个字符串一行；每个引用点一行（RVA + 附近字节），可直接喂给 frida 的
      Interceptor.attach(base.add(0xRVA))。
"""
import argparse
import json
import mmap
import re
import struct
import sys


class PE:
    def __init__(self, path):
        self.f = open(path, "rb")
        self.data = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        d = self.data
        if d[:2] != b"MZ":
            raise SystemExit("不是 PE 文件")
        e_lfanew = struct.unpack_from("<I", d, 0x3C)[0]
        if d[e_lfanew:e_lfanew + 4] != b"PE\0\0":
            raise SystemExit("PE 签名缺失")
        coff = e_lfanew + 4
        self.machine, nsec = struct.unpack_from("<HH", d, coff)
        opt_size = struct.unpack_from("<H", d, coff + 16)[0]
        opt = coff + 20
        magic = struct.unpack_from("<H", d, opt)[0]
        if magic == 0x20B:
            self.image_base = struct.unpack_from("<Q", d, opt + 24)[0]
            self.pe32plus = True
        else:
            self.image_base = struct.unpack_from("<I", d, opt + 28)[0]
            self.pe32plus = False
        self.size_of_image = struct.unpack_from("<I", d, opt + 56)[0]

        self.sections = []
        sec = opt + opt_size
        for i in range(nsec):
            off = sec + i * 40
            name = d[off:off + 8].rstrip(b"\0").decode("latin1")
            vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", d, off + 8)
            self.sections.append({
                "name": name, "va": va, "vsize": vsize,
                "rawsize": rawsize, "rawptr": rawptr,
                "chars": struct.unpack_from("<I", d, off + 36)[0],
            })

    def off_to_rva(self, off):
        for s in self.sections:
            if s["rawptr"] <= off < s["rawptr"] + s["rawsize"]:
                return s["va"] + (off - s["rawptr"])
        return None

    def rva_to_off(self, rva):
        for s in self.sections:
            if s["va"] <= rva < s["va"] + max(s["vsize"], s["rawsize"]):
                return s["rawptr"] + (rva - s["va"])
        return None

    def section_of_rva(self, rva):
        for s in self.sections:
            if s["va"] <= rva < s["va"] + max(s["vsize"], s["rawsize"]):
                return s
        return None

    def find_strings(self, keywords, min_len=4):
        """在整文件里找 ASCII 与 UTF-16LE 关键字，返回 [(rva, kind, text)]"""
        out = []
        for kw in keywords:
            for kind, needle in (("ascii", kw.encode("latin1")),
                                 ("utf16", kw.encode("utf-16le"))):
                start = 0
                while True:
                    i = self.data.find(needle, start)
                    if i < 0:
                        break
                    start = i + 1
                    rva = self.off_to_rva(i)
                    if rva is None:
                        continue
                    out.append((rva, kind, self._read_str(i, kind, min_len)))
        # 去重（同一 RVA 可能命中多次）
        seen = set()
        uniq = []
        for rva, kind, text in out:
            if rva in seen:
                continue
            seen.add(rva)
            uniq.append((rva, kind, text))
        return sorted(uniq)

    def _read_str(self, off, kind, min_len):
        if kind == "ascii":
            b = self.data[off:off + 180]
            end = b.find(b"\0")
            return b[:end if end > 0 else 80].decode("latin1", "replace")
        b = self.data[off:off + 360]
        chars = []
        for i in range(0, len(b) - 1, 2):
            ch = b[i:i + 2]
            if ch == b"\0\0":
                break
            chars.append(ch.decode("utf-16le", "replace"))
        return "".join(chars)[:80]

    def xrefs_to(self, targets, text_only=True):
        """在 .text 里找 RIP 相对 lea 引用 targets(VA集合) 的位置，返回 [(xref_rva, target_rva)]"""
        hits = []
        target_set = set(targets)
        pat = re.compile(rb"[\x40-\x4f]?\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]")
        for s in self.sections:
            if text_only and "CODE" not in self._chars_str(s["chars"]):
                continue
            base_va = s["va"]
            blob = self.data[s["rawptr"]:s["rawptr"] + s["rawsize"]]
            for m in pat.finditer(blob):
                g = m.group()
                disp_off = m.start() + len(g)
                if disp_off + 4 > len(blob):
                    continue
                disp = struct.unpack_from("<i", blob, disp_off)[0]
                instr_end_rva = base_va + disp_off + 4
                tgt = instr_end_rva + disp
                if tgt in target_set:
                    hits.append((base_va + m.start(), tgt))
        return hits

    @staticmethod
    def _chars_str(c):
        flags = []
        if c & 0x20000000:
            flags.append("CODE")
        if c & 0x40000000:
            flags.append("INIT")
        if c & 0x80000000:
            flags.append("UNINIT")
        return "|".join(flags)

    def load_pdata(self):
        """.pdata = 异常目录 = RUNTIME_FUNCTION 数组（Begin,End,Unwind 各 4 字节）。
        x64 上每个函数都有条目，这是**精确**拿函数边界的方式，比找 0xCC 对齐靠谱。"""
        if getattr(self, "_funcs", None) is not None:
            return self._funcs
        sec = next((s for s in self.sections if s["name"] == ".pdata"), None)
        if not sec:
            self._funcs = []
            return self._funcs
        self._funcs = []
        d = self.data
        for i in range(0, sec["rawsize"] - 11, 12):
            off = sec["rawptr"] + i
            begin, end, unwind = struct.unpack_from("<III", d, off)
            if begin == 0 and end == 0:
                continue
            if end <= begin:
                continue
            self._funcs.append((begin, end, unwind))
        self._funcs.sort()
        return self._funcs

    def func_at(self, rva):
        """返回 (begin, end, unwind) 或 None"""
        funcs = self.load_pdata()
        if not funcs:
            # 没有 .pdata 时退回块状扫描
            fs = guess_func_start(self, rva)
            return (fs, None, None) if fs else None
        lo, hi = 0, len(funcs) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            b, e, u = funcs[mid]
            if rva < b:
                hi = mid - 1
            elif rva >= e:
                lo = mid + 1
            else:
                return (b, e, u)
        return None


def guess_func_start(pe, rva, back=0x1200):
    """从引用点往前找 0xCC 对齐/padding 后的第一条指令，粗略估计函数起点。"""
    off = pe.rva_to_off(rva)
    if off is None:
        return None
    start = max(off - back, 0)
    blob = pe.data[start:off]
    i = len(blob)
    while i > 4:
        if blob[i - 3:i] == b"\xcc\xcc\xcc" or blob[i - 3:i] == b"\x00\x00\x00":
            nxt = start + i
            while nxt < off and pe.data[nxt] in (0xCC, 0x90, 0x00):
                nxt += 1
            return pe.off_to_rva(nxt)
        i -= 1
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dll")
    ap.add_argument("--kw", nargs="+", default=[
        "open.weixin.qq.com", "oauth2/authorize", "jump2h5err",
        "waerrpage", "connect/oauth2",
    ])
    ap.add_argument("--json", default="")
    ap.add_argument("--rva", default="")
    ap.add_argument("--max", type=int, default=40)
    args = ap.parse_args()

    pe = PE(args.dll)
    print("镜像基址 0x%X  节数 %d  文件 %.1f MB" % (pe.image_base, len(pe.sections), len(pe.data) / 1e6))
    for s in pe.sections:
        print("  %-10s VA=0x%08X size=0x%08X raw=0x%08X %s"
              % (s["name"], s["va"], s["rawsize"], s["rawptr"], PE._chars_str(s["chars"])))

    if args.rva:
        rvas = [int(x, 16) for x in args.rva.split(",")]
        for r in rvas:
            print("RVA 0x%X -> 估计函数起点 0x%X" % (r, guess_func_start(pe, r) or 0))
        return

    print("\n[*] 扫描关键字 ...")
    strs = pe.find_strings(args.kw)
    print("    命中 %d 处" % len(strs))
    for rva, kind, text in strs[:args.max]:
        print("      [%-5s] RVA=0x%08X  %r" % (kind, rva, text[:70]))

    print("\n[*] 扫描 RIP 相对引用 ...")
    targets = [rva for rva, _k, _t in strs]
    xrefs = pe.xrefs_to(targets)
    print("    找到 %d 个引用点" % len(xrefs))

    by_target = {}
    for xr, tgt in xrefs:
        by_target.setdefault(tgt, []).append(xr)

    rows = []
    by_func = {}
    for tgt, xrs in by_target.items():
        text = next((t for r, _k, t in strs if r == tgt), "")
        kind = next((k for r, k, _t in strs if r == tgt), "")
        for xr in sorted(set(xrs))[:6]:
            fn = pe.func_at(xr)
            fb, fe = (fn[0], fn[1]) if fn else (None, None)
            rows.append({"str_rva": tgt, "str": text[:90], "kind": kind,
                         "xref_rva": xr, "func_rva": fb, "func_end": fe})
            if fb is not None:
                by_func.setdefault(fb, set()).add(text[:60])
            print("      str@0x%08X %-30r <- xref@0x%08X  func=0x%X..0x%X"
                  % (tgt, text[:30], xr, fb or 0, fe or 0))

    if by_func:
        print("\n[*] 按函数汇总（这就是 hook 候选表）")
        for fb in sorted(by_func):
            fn = pe.func_at(fb)
            size = (fn[1] - fn[0]) if fn and fn[1] else 0
            print("      0x%08X  size=0x%-5X  refs: %s"
                  % (fb, size, " | ".join(sorted(by_func[fb]))[:100]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"dll": args.dll, "image_base": pe.image_base,
                       "strings": [{"rva": r, "kind": k, "text": t} for r, k, t in strs],
                       "xrefs": rows,
                       "functions": [{"rva": fb,
                                      "size": (pe.func_at(fb)[1] - fb) if pe.func_at(fb) and pe.func_at(fb)[1] else 0,
                                      "refs": sorted(by_func[fb])}
                                     for fb in sorted(by_func)]},
                      f, ensure_ascii=False, indent=2)
        print("\n[=] 已写出 %s（%d 个引用点 / %d 个函数）"
              % (args.json, len(rows), len(by_func)))


if __name__ == "__main__":
    main()
