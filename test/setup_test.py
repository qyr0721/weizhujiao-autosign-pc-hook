#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
setup_test.py — 环境安装器的纯函数与安全边界（不需要联网）

  python test/setup_test.py

测什么
  · 解压时的 zip slip 防护（压缩包里带 ../ 的条目必须被丢弃）
  · 去掉顶层目录（GitHub / Node 的 zip 都是这样的结构）
  · 子进程输出解码（cmd 默认 GBK，npm 有时 UTF-8）
  · 体积格式化、步骤标记
  · 下载失败不会被当成成功（install_node 的多源回退）
"""
import io
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import setup as S  # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s   %s" % (name, extra))


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)


def main():
    print("=== 体积与解码 ===")
    ok(S.human(512) == "512 B", "字节")
    ok(S.human(2048) == "2 KB", "KB")
    ok(S.human(3 << 20) == "3.0 MB", "MB")
    ok(S.decode("微信测试".encode("utf-8")) == "微信测试", "UTF-8 输出")
    ok(S.decode("微信测试".encode("gbk")) == "微信测试", "GBK 输出（cmd 默认）")
    ok(S.decode(b"\xff\xfe\x00bad") is not None, "非法字节不抛异常")

    print("\n=== 解压：去顶层目录 ===")
    tmp = tempfile.mkdtemp(prefix="wzsetup_")
    try:
        zp = os.path.join(tmp, "a.zip")
        make_zip(zp, {
            "repo-main/package.json": '{"name":"x"}',
            "repo-main/src/index.ts": "code",
            "repo-main/node_modules/tsx/dist/cli.mjs": "x",
        })
        dest = os.path.join(tmp, "out")
        S.extract_zip(zp, dest)
        ok(os.path.isfile(os.path.join(dest, "package.json")), "顶层目录已剥掉")
        ok(os.path.isfile(os.path.join(dest, "src", "index.ts")), "子目录结构保留")
        ok(not os.path.exists(os.path.join(dest, "repo-main")), "不再有 repo-main 目录")

        print("\n=== 解压：zip slip 防护 ===")
        zp2 = os.path.join(tmp, "evil.zip")
        make_zip(zp2, {
            "root/ok.txt": "ok",
            "root/../../evil.txt": "pwned",
            "root/sub/../../../evil2.txt": "pwned",
        })
        dest2 = os.path.join(tmp, "out2")
        S.extract_zip(zp2, dest2)
        ok(os.path.isfile(os.path.join(dest2, "ok.txt")), "正常条目照常解出")
        escaped = [p for p in (
            os.path.join(tmp, "evil.txt"),
            os.path.join(os.path.dirname(tmp), "evil.txt"),
            os.path.join(dest2, "..", "evil.txt"),
        ) if os.path.exists(p)]
        ok(not escaped, "越界条目被丢弃", str(escaped))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== 步骤标记 ===")
    s = S.Step("k", "标签")
    ok(s.state == "pending", "初始为 pending")
    ok(S.Step("k", "l").mark("ok") == "✓", "ok 标记")
    ok(S.Step("k", "l").mark("fail") == "✗", "fail 标记")
    ok(S.Step("k", "l").mark("skip") == "·", "skip 标记")

    print("\n=== 下载失败要能被识别 ===")
    # 两个镜像都不可用时，install_node 必须返回 False 且状态为 fail
    orig = S.NODE_MIRRORS[:]
    S.NODE_MIRRORS[:] = ["http://127.0.0.1:9/v{ver}/node.zip"]
    tmp2 = tempfile.mkdtemp(prefix="wzsetup2_")
    try:
        class FakeLog:
            short = "x"
            ok = False

            def write(self, *a, **k):
                pass

        class FakeTerm:
            plain = True
            DIM = GREEN = RED = CYAN = BOLD = ""

            def c(self, t, color=None):
                return t

        st = S.Setup(FakeTerm(), FakeLog(), tmp2)
        st.by["node"].state = "pending"
        got = st.install_node()
        ok(got is False, "全部镜像失败时返回 False")
        ok(st.by["node"].state == "fail", "步骤状态为 fail", st.by["node"].state)
        ok(not os.path.exists(os.path.join(tmp2, "runtime", "node", "node.exe")),
           "失败后不留下残缺的 node.exe")
    finally:
        S.NODE_MIRRORS[:] = orig
        shutil.rmtree(tmp2, ignore_errors=True)

    print("\n%d 通过，%d 失败" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
