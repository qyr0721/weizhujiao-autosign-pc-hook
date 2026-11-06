#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build.py — 构建 wxsign 发布产物

用法
  python build.py portable       # dist/wxsign-portable.exe  单文件图形界面（默认入口）
  python build.py integrated     # dist/wxsign-integrated.zip（本地自行构建，不发布）
  python build.py all            # 两个都做

入口为 app.py（本地 Web 图形界面，--windowed，不弹控制台）；
TUTORIAL.md 与 docs/img 通过 --add-data 打进包，界面里点「使用教程」即可离线查看。
源码运行 `python wizard.py` 仍可回到原来的终端面板。

集成版复用本机已部署的组件，可用参数指定（不指定时自动探测）：
  --node-dir   完整 Node 便携目录（含 node.exe / npm.cmd / node_modules）
  --wmpf-dir   WMPFDebugger 目录（需已 npm install，含 tsx 与 frida）

产物只写入 dist/ 与 build/，两者都已在 .gitignore 中。
签到/取号/偏移模块由 PyInstaller 的静态分析自动收进 PYZ，
websockets 有动态导入，用 --collect-submodules 显式收全。
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")

PYI_BASE = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean", "--windowed",
    "--distpath", DIST, "--workpath", BUILD,
    "--log-level", "WARN",
    "--collect-submodules", "websockets",
    "--exclude-module", "tkinter",
    "--exclude-module", "pytest",
    "--exclude-module", "PyInstaller",
    # 界面里点「使用教程」要能离线打开，所以把教程与配图一起打进包
    "--add-data", os.path.join(HERE, "TUTORIAL.md") + os.pathsep + ".",
    "--add-data", (os.path.join(HERE, "docs", "img") + os.pathsep
                   + os.path.join("docs", "img")),
]

# 集成版不需要把这些塞进 zip（体积 / 缓存 / 日志 / 上游仓库元数据）
SKIP_DIRS = {"__pycache__", ".git", ".github", ".vscode", ".idea", "logs", "test"}
SKIP_FILES = {"proxy.log", "run.log", "get_openid.log", "openid.txt",
              "openid.stale", "config.json", "state.json", "a8key_scan.json"}
SKIP_SUFFIX = (".log", ".pyc", ".pyo", ".tmp", ".part", ".zip")


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def pyi(extra, entry="app.py"):
    cmd = PYI_BASE + extra + [os.path.join(HERE, entry)]
    log("PyInstaller: %s" % " ".join(cmd[3:]))
    return subprocess.run(cmd, cwd=HERE).returncode


def build_portable():
    log("构建便携版（单文件）…")
    rc = pyi(["--onefile", "--name", "wxsign-portable"])
    if rc != 0:
        return None
    exe = os.path.join(DIST, "wxsign-portable.exe")
    if not os.path.isfile(exe):
        log("没有生成 %s" % exe)
        return None
    log("便携版完成：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1e6))
    return exe


def find_node_dir():
    cand = os.path.join(HERE, "runtime", "node")
    if os.path.isfile(os.path.join(cand, "node.exe")):
        return cand
    import uikit as U
    node = U.resolve_node(HERE)
    if node and os.path.isfile(node):
        return os.path.dirname(node)
    return None


def find_wmpf_dir():
    cand = os.path.join(HERE, "WMPFDebugger")
    if os.path.isfile(os.path.join(cand, "src", "index.ts")):
        return cand
    import uikit as U
    return U.resolve_wmpf_dir(HERE)


def skip(rel):
    parts = rel.split(os.sep)
    if any(p in SKIP_DIRS for p in parts):
        return True
    name = parts[-1]
    if name in SKIP_FILES or name.endswith(SKIP_SUFFIX):
        return True
    # 上游仓库的说明文档不进发布包，README 由本仓库提供
    if name.lower() in ("readme.md", "readme.zh.md", "faq.zh.md",
                        "adaptation.md", "extension.md", "changelog.md"):
        return True
    return False


def copy_tree(src, dst, label):
    log("复制 %s：%s" % (label, src))
    count = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel_dir = os.path.relpath(root, src)
        for name in files:
            rel = os.path.normpath(os.path.join(rel_dir, name)) if rel_dir != "." else name
            if skip(rel):
                continue
            out = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(os.path.join(root, name), out)
            count += 1
    log("  %s：%d 个文件" % (label, count))


def build_integrated(node_dir=None, wmpf_dir=None):
    log("构建集成版（解压即用）…")
    rc = pyi(["--onedir", "--name", "wxsign"])
    if rc != 0:
        return None
    root = os.path.join(DIST, "wxsign")
    exe = os.path.join(root, "wxsign.exe")
    if not os.path.isfile(exe):
        log("没有生成 %s" % exe)
        return None

    node_dir = node_dir or find_node_dir()
    wmpf_dir = wmpf_dir or find_wmpf_dir()
    if not node_dir or not os.path.isfile(os.path.join(node_dir, "node.exe")):
        log("跳过内置 Node：找不到完整 Node 便携目录（用 --node-dir 指定）")
    else:
        copy_tree(node_dir, os.path.join(root, "runtime", "node"), "Node 运行时")
    if not wmpf_dir or not os.path.isfile(os.path.join(wmpf_dir, "src", "index.ts")):
        log("跳过内置 WMPFDebugger：找不到目录（用 --wmpf-dir 指定）")
    else:
        copy_tree(wmpf_dir, os.path.join(root, "WMPFDebugger"), "WMPFDebugger")

    # signer/config.json 由程序首次运行生成，这里先把目录建好
    os.makedirs(os.path.join(root, "signer"), exist_ok=True)
    with open(os.path.join(root, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(
            "微助教自动签到（集成版）\n\n"
            "1. 整个目录解压到任意位置，运行 wxsign.exe 即可，全程无需联网。\n"
            "2. 微信版本变化后程序会自动探测 hook 偏移，无需降级微信。\n"
            "3. 首次使用仍需在微信里手动授权一次（见 README）。\n"
            "4. GPS 签到请编辑 signer/config.json 里的 lat / lon，改完重启程序。\n"
            "5. 调试通道要求先启动本程序，再从「最近使用」打开小程序。\n"
        )

    out = os.path.join(DIST, "wxsign-integrated.zip")
    log("打包 %s …" % out)
    if os.path.isfile(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                p = os.path.join(base, name)
                rel = os.path.relpath(p, root)
                if skip(rel):
                    continue
                z.write(p, os.path.join("wxsign", rel))
    log("集成版完成：%s（%.1f MB）" % (out, os.path.getsize(out) / 1e6))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default="all",
                    choices=["portable", "integrated", "all"])
    ap.add_argument("--node-dir", default="")
    ap.add_argument("--wmpf-dir", default="")
    args = ap.parse_args()

    os.makedirs(DIST, exist_ok=True)
    made = []
    if args.target in ("portable", "all"):
        p = build_portable()
        if p:
            made.append(p)
        else:
            log("便携版构建失败")
            return 1
    if args.target in ("integrated", "all"):
        z = build_integrated(args.node_dir or None, args.wmpf_dir or None)
        if z:
            made.append(z)
        else:
            log("集成版构建失败")
            return 1
    log("产物：\n  " + "\n  ".join(made))
    return 0


if __name__ == "__main__":
    sys.exit(main())
