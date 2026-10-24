#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wizard.py — 一键引导式启动器（打包成 exe 的入口）

它不是"点一下就跑"，而是在需要的时刻把该做的事说清楚：
  ① 环境自检（微信在不在、WMPF 版本对不对、WMPFDebugger 装没装、Node 有没有）
  ② 自动起调试代理
  ③ 提示你在微信里点哪几个地方（小程序面板 / 微助教签到页）
  ④ 通道一通就自动取号 + 开始签到

冻结（exe）后：签到核心以线程方式进程内运行，不依赖外部 python；
未冻结（源码）：同样可用，`python wizard.py`。
"""
import argparse
import asyncio
import ctypes
import ctypes.wintypes as wintypes
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    HERE = os.path.dirname(sys.executable)

import channel_keeper as ck          # noqa: E402
import get_openid as g               # noqa: E402
import run as runmod                 # noqa: E402
import signcore                      # noqa: E402

KNOWN_VERSION = 25799        # hook_a8key.js 内置 RVA 对应的 WMPF 版本
WMPF_ROOT = os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat",
                         "xplugin", "plugins", "RadiumWMPF")


def ts():
    return time.strftime("%H:%M:%S")


def _fix_console():
    """Windows 控制台默认 GBK，中文会乱码；统一切到 UTF-8。"""
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


_fix_console()


def say(msg=""):
    print(msg, flush=True)


def log(msg):
    print("[%s] %s" % (ts(), msg), flush=True)


def ok(msg):
    print("  [OK] %s" % msg, flush=True)


def warn(msg):
    print("  [!]  %s" % msg, flush=True)


def bad(msg):
    print("  [X]  %s" % msg, flush=True)


# ---------------------------------------------------------------- 环境自检
def find_wmpf_versions():
    if not os.path.isdir(WMPF_ROOT):
        return []
    out = []
    for name in os.listdir(WMPF_ROOT):
        p = os.path.join(WMPF_ROOT, name, "extracted", "runtime", "flue.dll")
        if name.isdigit() and os.path.isfile(p):
            out.append((int(name), p))
    return sorted(out)


def wechat_browser_alive():
    """用 Toolhelp 快照数 WeChatAppEx.exe（不依赖 psutil）"""
    TH32CS_SNAPPROCESS = 0x2

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return 0
    n = 0
    e = PE32()
    e.dwSize = ctypes.sizeof(PE32)
    try:
        if k32.Process32First(snap, ctypes.byref(e)):
            while True:
                if e.szExeFile.decode("latin1").lower() == "wechatappex.exe":
                    n += 1
                if not k32.Process32Next(snap, ctypes.byref(e)):
                    break
    finally:
        k32.CloseHandle(snap)
    return n


def resolve_wmpf_dir():
    env = os.environ.get("WZ_WMPF_DIR")
    if env and os.path.isdir(env):
        return env
    for cand in (os.path.join(HERE, "WMPFDebugger"),
                 os.path.join(os.path.dirname(HERE), "WMPFDebugger"),
                 ck.DEFAULT_WMPF):
        if cand and os.path.isdir(os.path.join(cand, "node_modules", "tsx")):
            return cand
    return None


def resolve_node():
    env = os.environ.get("WZ_NODE")
    if env and os.path.isfile(env):
        return env
    import shutil
    p = shutil.which("node")
    if p:
        return p
    return ck.DEFAULT_NODE if os.path.isfile(ck.DEFAULT_NODE) else None


def preflight():
    say("")
    say("【1/4】环境自检")
    problems = []

    n = wechat_browser_alive()
    if n:
        ok("微信内置浏览器进程在跑（WeChatAppEx × %d）" % n)
    else:
        bad("没找到 WeChatAppEx 进程 —— 请先启动并登录微信 PC 版")
        problems.append("wechat")

    vers = find_wmpf_versions()
    if not vers:
        bad("没找到 WMPF 运行时（%s）" % WMPF_ROOT)
        problems.append("wmpf")
    else:
        cur = vers[-1][0]
        if cur == KNOWN_VERSION:
            ok("WMPF 运行时 %d（与内置 RVA 表匹配）" % cur)
        else:
            warn("WMPF 运行时 %d，而本项目 RVA 表基于 %d" % (cur, KNOWN_VERSION))
            warn("  代理请加 --auto-detect；hook_a8key.js 需用 tools/pe_scan.py 重扫")
        if len(vers) > 1:
            say("      检测到多个版本：%s" % ", ".join(str(v) for v, _ in vers))

    d = resolve_wmpf_dir()
    if d and os.path.isdir(os.path.join(d, "node_modules", "tsx")):
        ok("WMPFDebugger：%s" % d)
    else:
        bad("没找到 WMPFDebugger（需含 node_modules/tsx）")
        say("      git clone https://github.com/evi0s/WMPFDebugger")
        say("      cd WMPFDebugger && yarn        # frida 原生绑定需能下载")
        say("      或用环境变量指定：set WZ_WMPF_DIR=D:\\tools\\WMPFDebugger")
        problems.append("wmpfdebugger")

    node = resolve_node()
    if node:
        ok("Node：%s" % node)
    else:
        bad("没找到 node.exe（装 Node 22+，或 set WZ_NODE=...）")
        problems.append("node")

    return problems, d, node


# ---------------------------------------------------------------- 引导文本
GUIDE = """
【3/4】现在请在微信里做两件事（只做一次）

  A. 给调试通道"点火"——必须做
     ① 微信左侧竖排图标里点「小程序」（绿色方块图标）
     ② 在面板里点任意一个用过的小程序；若列表为空，点面板里的「搜索」，
        输入任一小程序名（如"腾讯文档"）并打开
     ③ 小程序窗口出现后**不要关**，可以最小化或缩到后台
        （这条 9421 连接是小程序的生命线，关代理它才会退出）

  B. 打开签到页——建议做（不做也能取号，做了更稳、更快）
     ① 微信左侧点「通讯录」→ 公众号 → 找到「微助教」
     ② 进会话后点底部菜单里的「签到」
     ③ 会跳到一个 H5 页面（地址含 student/sign?openid=...），留着不用管

  之后本程序会自动：读号 → 每 5 秒轮询 → 有签到立刻提交。
  （没有课时会降到 30 秒一次探活，一有课立刻恢复 5 秒——不看时钟，只看服务端）
"""


# ---------------------------------------------------------------- 签到线程
class Signer:
    """进程内跑签到核心；401 时置位事件让主循环续号，续完再自动拉起。"""

    def __init__(self, openid_file, cfg, log_fn):
        self.openid_file = openid_file
        self.cfg = cfg
        self.log = log_fn
        self.stale = threading.Event()
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        bad_oid = None
        while not self.stop.is_set():
            code = signcore.sign_loop(self.cfg, self.openid_file,
                                      stale_event=self.stale,
                                      log=lambda m: log("  [签到] %s" % m),
                                      stop=self.stop)
            if self.stop.is_set():
                return
            if code == 2:
                bad_oid = signcore.read_openid(self.openid_file)
                log("  [签到] 号失效，等续号…")
            else:
                log("  [签到] 退出 code=%s，5s 后重试" % code)
                time.sleep(5)
            # 等号变化（最多 90s），避免拿着旧号空转
            deadline = time.time() + 90
            while not self.stop.is_set() and time.time() < deadline:
                cur = signcore.read_openid(self.openid_file)
                if cur and cur != bad_oid:
                    break
                time.sleep(2)

    def take_stale(self):
        if self.stale.is_set():
            self.stale.clear()
            return True
        return False

    def halt(self):
        self.stop.set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只做环境自检")
    ap.add_argument("--no-sign", dest="with_sign", action="store_false")
    ap.add_argument("--dry-run", action="store_true", help="签到侧只打印不提交")
    ap.add_argument("--url", default=g.DEFAULT_URL)
    ap.add_argument("--out", default=os.path.join(HERE, "openid.txt"))
    ap.add_argument("--config", default=os.path.join(HERE, "signer", "config.json"))
    ap.set_defaults(with_sign=True)
    args = ap.parse_args()

    say("=" * 60)
    say(" 微助教自动签到 · 引导式启动器")
    say("=" * 60)

    problems, wmpf_dir, node = preflight()
    if args.check:
        say("")
        say("自检结束：%s" % ("全部就绪" if not problems else "有 %d 项待处理" % len(problems)))
        return 0 if not problems else 1

    if "wechat" in problems or "wmpfdebugger" in problems or "node" in problems:
        say("")
        say("上面标 ✗ 的项目必须先解决，否则起不来。解决后重跑本程序。")
        return 1
    if not wmpf_dir or not node:
        return 1

    say("")
    say("【2/4】启动调试代理（自动）")
    ns = argparse.Namespace(
        url=args.url, out=args.out, poll=10.0, cooldown=300.0, observe=18.0,
        check=False, once=False, with_sign=False,
        wmpf_dir=wmpf_dir, node=node,
    )

    say(GUIDE)

    signer = None
    if args.with_sign:
        cfg = signcore.load_cfg(args.config)
        signer = Signer(args.out, cfg, log)
        signer.start()
        say("【4/4】签到侧已启动（5 秒轮询，命中即提交）")
    else:
        say("【4/4】只取号模式（--no-sign）")

    say("")
    say("等待小程序连上 9421 …… 连上后本程序会自动接手，无需再操作。")
    say("按 Ctrl+C 退出（不会关闭代理，小程序不受影响）。")
    say("")

    try:
        asyncio.run(runmod.run(ns, log,
                               force_check=(signer.take_stale if signer else None)))
    except KeyboardInterrupt:
        log("已停止。代理仍在运行；小程序不受影响。")
    finally:
        if signer:
            signer.halt()
    return 0


if __name__ == "__main__":
    sys.exit(main())
