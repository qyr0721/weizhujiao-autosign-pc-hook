#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_bundle.py — 确认打包产物里装的是当前源码，不是旧缓存

PyInstaller 把模块压进 PYZ（zlib 压缩），直接 grep exe 找不到字符串。
这里把 PYZ 解开，取回代码对象，检查新增的函数名是否真的在里面。

用法
  python tools/verify_bundle.py dist/wxsign-portable.exe
  python tools/verify_bundle.py dist/wxsign/wxsign.exe
"""
import os
import sys

import PyInstaller.archive.readers as R


def co_names_deep(co, out):
    """递归收集代码对象里出现的所有名字（含嵌套函数/类）。"""
    out.update(co.co_names)
    for c in co.co_consts:
        if hasattr(c, "co_names"):
            co_names_deep(c, out)


def find_pyz(ca):
    for name, ent in ca.toc.items():
        typ = ent[-1]
        if typ in (b"z", "z", b"Z", "Z"):
            return name
    return None


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/verify_bundle.py <exe>")
        return 2
    exe = sys.argv[1]
    if not os.path.isfile(exe):
        print("找不到 %s" % exe)
        return 2

    ca = R.CArchiveReader(exe)
    pyz_name = find_pyz(ca)
    if not pyz_name:
        print("没找到 PYZ 条目：%s" % exe)
        return 2

    # 用官方的 open_embedded_archive 打开内嵌 PYZ，别自己拼 BytesIO
    za = ca.open_embedded_archive(pyz_name)

    # 该有的符号：保留历史回归项，加上本次「版本自适应」新增的关键入口。
    # 命中这些才能证明 exe 里是新代码，而不是上一次的缓存产物。
    # 注意入口脚本 wizard.py 被 PyInstaller 放在 CArchive 顶层（type 's'），
    # 不是 PYZ 条目，这里只验被 import 的模块，别把入口名列进来。
    want = {
        "get_openid": "oid_state_async",
        "run": "_startup_offsets",
        "offsets": "recover_if_needed",
        "channel_keeper": "refresh_args",
        "installer": "write_config",
        # 内置偏移库是新模块：漏打包时启动会退回纯扫描，症状和「扫描不出」一样，
        # 光看运行日志分不清，所以必须在产物里验到它。
        "known_offsets": "validate",
    }
    bad = 0
    for mod, sym in sorted(want.items()):
        try:
            co = za.extract(mod)
        except Exception as exc:  # noqa: BLE001
            print("  FAIL  %s 取不出：%s" % (mod, exc))
            bad += 1
            continue
        names = set()
        co_names_deep(co, names)
        got = sym in names
        print("  %-5s %-10s 内含 %s" % ("PASS" if got else "FAIL", mod, sym))
        if not got:
            bad += 1

    # 反证：确认我们看的确实是打包产物里的模块，而不是本地源文件
    print("\n  PYZ 条目: %s" % pyz_name)
    print("  %s" % ("全部命中 —— exe 里是新代码"
                    if bad == 0 else "有 %d 项缺失 —— exe 里还是旧代码" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
