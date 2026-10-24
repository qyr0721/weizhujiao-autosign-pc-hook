#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wizard.py — wxsign.exe 的源码：面板式启动器

设计原则
  · 面板只显示：程序名 / 作者 / 项目地址 / 微信版本 / 自检 / 当前在做什么 / 最近签到
  · 其余全部写日志（exe 同目录 logs\\wxsign-YYYYMMDD-HHMMSS.log），不污染界面
  · ANSI 定点重绘，只重画变化的行；不支持 ANSI 时退化为纯文本逐行输出

用法
  wxsign.exe               常驻
  wxsign.exe --check       只自检
  wxsign.exe --plain       不要颜色/重绘（重定向到文件时自动降级）
"""
import argparse
import asyncio
import ctypes
import ctypes.wintypes as wintypes
import os
import sys
import threading
import time
import unicodedata

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

APP = "微助教自动签到"
VER = "v1.1"
AUTHOR = "qyr0721"
REPO = "github.com/qyr0721/weizhujiao-autosign-pc-hook"

KNOWN_VERSION = 25799        # hook_a8key.js 内置 RVA 对应的 WMPF 版本
WMPF_ROOT = os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat",
                         "xplugin", "plugins", "RadiumWMPF")


def ts():
    return time.strftime("%H:%M:%S")


# ================================================================ 控制台
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


def _enable_ansi():
    """打开 VT 处理（Win10+）。失败说明这终端不支持 ANSI，改用纯文本。"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        if not k32.SetConsoleMode(h, mode.value | 0x0004):
            return False
        # 顺手把窗口标题改掉
        try:
            k32.SetConsoleTitleW("%s %s" % (APP, VER))
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


_fix_console()


def dwidth(text):
    """终端显示宽度（中文/全角算 2 列）。"""
    w = 0
    for ch in text:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def fit(text, width):
    """按显示宽度截断并右补空格。

    截断标记用 ASCII 的 "..."（宽度确定），不用 "…"（东亚宽度有歧义，
    不同终端按 1 或 2 列渲染，会把边框挤歪）。
    """
    out = []
    used = 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + cw > width:
            # 需要给 "..." 腾出 3 列
            while out and used + 3 > width:
                used -= dwidth(out.pop())
            if used + 3 <= width:
                out.append("...")
                used += 3
            break
        out.append(ch)
        used += cw
    return "".join(out) + " " * max(0, width - used)


class Term:
    """颜色与光标。plain=True 时不输出任何转义。"""

    RESET = "\033[0m"
    DIM = "\033[90m"
    BOLD = "\033[1;97m"
    CYAN = "\033[36m"
    GREEN = "\033[1;32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"

    def __init__(self, plain=False):
        self.plain = plain or not _enable_ansi() or not sys.stdout.isatty()

    def c(self, text, color=None):
        if self.plain or not color:
            return text
        return color + text + self.RESET

    def move(self, row):
        return "" if self.plain else "\033[%d;1H" % row

    def clear_line(self):
        return "" if self.plain else "\033[K"

    def clear_screen(self):
        return "" if self.plain else "\033[2J\033[H"


# ================================================================ 面板
INNER = 58          # 标题框内容宽度
TOTAL = INNER + 2   # │ + 内容 + │
RULE = "─" * (INNER // 2)   # 分隔线：全角横线，宽度确定


class Panel:
    """标题用闭合边框，正文用开放布局。

    为什么不给正文也加右边框：正文里会出现 ✓ · → 这类「东亚宽度歧义」字符，
    不同终端按 1 或 2 列渲染，右边框必然在某些机器上歪一列。标题行只用
    中英文数字（宽度确定），所以框是准的；正文左对齐，怎么渲染都整齐。
    """

    def __init__(self, term):
        self.t = term
        self.lines = []
        self.top = 1

    # ---------- 组装 ----------
    def _box(self, text, color=None):
        body = fit(text, INNER)
        if color:
            body = self.t.c(body, color)
        bar = self.t.c("│", self.t.DIM)
        return bar + body + bar

    def _rule_bar(self, left, right):
        return self.t.c(left + "─" * INNER + right, self.t.DIM)

    def compose(self, info):
        t = self.t
        self._dyn = {"action": info["action"], "sign": info["sign"] or "—"}
        rows = []

        # ---- 标题（唯一带右边框的区域，内容宽度确定）----
        rows.append(self._rule_bar("┌", "┐"))
        rows.append(self._box("  " + APP, t.BOLD))
        rows.append(self._box("  %s   by %s" % (VER, AUTHOR), t.DIM))
        rows.append(self._box("  " + REPO, t.DIM))
        rows.append(self._rule_bar("└", "┘"))
        rows.append("")

        # ---- 环境 ----
        ver = "  微信 %s   ·   WMPF %s" % (info["wechat"], info["wmpf"])
        rows.append(t.c(ver, None if info["wmpf_ok"] else t.YELLOW))
        if info["ok"]:
            rows.append("  自检  " + t.c("正常", t.GREEN))
        else:
            rows.append("  自检  " + t.c("异常", t.RED) + "  " + t.c(info["note"], t.DIM))
        rows.append("")
        rows.append(t.c("  " + RULE, t.DIM))
        rows.append("")

        # ---- 正在做什么 ----
        rows.append(t.c("  │ ", t.DIM) + t.c(info["action"], t.CYAN))
        rows.append("")
        for hint in info["hint"]:
            rows.append(t.c("    " + hint, t.DIM))
        rows.append("")
        rows.append(t.c("  " + RULE, t.DIM))

        # ---- 签到 ----
        sign, kind = info["sign"], info["sign_kind"]
        color = {"ok": t.GREEN, "dup": t.DIM, "fail": t.RED}.get(kind)
        rows.append("  " + t.c("签到", t.DIM) + "  " + t.c(sign or "—", color))
        rows.append(t.c("  " + RULE, t.DIM))
        rows.append("")

        # ---- 日志与退出 ----
        rows.append("  " + t.c("日志", t.DIM) + "  " + t.c(info["logfile"], t.DIM))
        rows.append(t.c("  Ctrl+C 退出，不影响正在运行的小程序", t.DIM))
        return rows

    # ---------- 渲染 ----------
    def render(self, info):
        rows = self.compose(info)
        if self.t.plain:
            self._render_plain(rows)
            return
        out = []
        for i, line in enumerate(rows):
            if i < len(self.lines) and self.lines[i] == line:
                continue
            out.append(self.t.move(self.top + i) + line + self.t.clear_line())
        self.lines = rows
        out.append(self.t.move(self.top + len(rows)))
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _render_plain(self, rows):
        # 无 ANSI：只输出变化的那一行，不重复、不画框
        dyn = getattr(self, "_dyn", {"action": "", "sign": ""})
        prev = getattr(self, "_plain_prev", {"action": None, "sign": None})
        out = []
        if dyn["action"] != prev["action"]:
            out.append("  ▸ %s" % dyn["action"])
        if dyn["sign"] != prev["sign"]:
            out.append("  签到  %s" % dyn["sign"])
        self._plain_prev = dict(dyn)
        if out:
            print("\n".join(out))
            sys.stdout.flush()


def _strip(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == "\033":
            j = s.find("m", i)
            i = len(s) if j < 0 else j + 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out).strip()


# ================================================================ 日志文件
class FileLog:
    def __init__(self, directory):
        self.path = os.path.join(directory, "wxsign-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
        self.short = os.path.join(os.path.basename(directory), os.path.basename(self.path))
        self.ok = True
        try:
            os.makedirs(directory, exist_ok=True)
            self.f = open(self.path, "a", encoding="utf-8")
        except Exception:  # noqa: BLE001
            self.ok = False
            self.f = None

    def write(self, msg, tag=""):
        if not self.f:
            return
        try:
            self.f.write("[%s] %s%s\n" % (ts(), tag, msg))
            self.f.flush()
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        if self.f:
            try:
                self.f.close()
            except Exception:  # noqa: BLE001
                pass


def log_dir():
    """优先 exe 同目录的 logs/；不可写则退到 %LOCALAPPDATA%\\wxsign\\logs。"""
    cand = os.path.join(HERE, "logs")
    try:
        os.makedirs(cand, exist_ok=True)
        probe = os.path.join(cand, ".w")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return cand
    except Exception:  # noqa: BLE001
        return os.path.join(os.environ.get("LOCALAPPDATA", HERE), "wxsign", "logs")


# ================================================================ 环境探测
class PROC(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


def list_processes(names):
    """返回 {exe名: [完整路径, ...]}（只统计在 names 里的）"""
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    out = {}
    if snap == -1:
        return out
    e = PROC()
    e.dwSize = ctypes.sizeof(PROC)
    try:
        if not k32.Process32First(snap, ctypes.byref(e)):
            return out
        while True:
            name = e.szExeFile.decode("latin1").lower()
            if name in names:
                path = ""
                h = k32.OpenProcess(0x1000, False, e.th32ProcessID)
                if h:
                    buf = ctypes.create_unicode_buffer(520)
                    n = wintypes.DWORD(520)
                    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                        path = buf.value
                    k32.CloseHandle(h)
                out.setdefault(name, []).append(path)
            if not k32.Process32Next(snap, ctypes.byref(e)):
                break
    finally:
        k32.CloseHandle(snap)
    return out


class _VS(ctypes.Structure):
    _fields_ = [("sig", wintypes.DWORD), ("struc", wintypes.DWORD),
                ("ms", wintypes.DWORD), ("ls", wintypes.DWORD),
                ("pms", wintypes.DWORD), ("pls", wintypes.DWORD)]


def file_version(path):
    try:
        v = ctypes.windll.version
        size = v.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not v.GetFileVersionInfoW(path, 0, size, buf):
            return None
        r = ctypes.c_void_p()
        ln = ctypes.c_uint()
        if not v.VerQueryValueW(buf, "\\", ctypes.byref(r), ctypes.byref(ln)):
            return None
        f = ctypes.cast(r, ctypes.POINTER(_VS)).contents
        return "%d.%d.%d.%d" % (f.ms >> 16, f.ms & 0xFFFF, f.ls >> 16, f.ls & 0xFFFF)
    except Exception:  # noqa: BLE001
        return None


def short_version(v):
    """4.1.15.56 -> 4.1.15.56（保留全部四段，末位有意义）"""
    return v or "未检测到"


def find_wmpf_versions():
    if not os.path.isdir(WMPF_ROOT):
        return []
    out = []
    for name in os.listdir(WMPF_ROOT):
        p = os.path.join(WMPF_ROOT, name, "extracted", "runtime", "flue.dll")
        if name.isdigit() and os.path.isfile(p):
            out.append(int(name))
    return sorted(out)


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
    """返回 (info, fixes)。info 是面板需要的一切；fixes 是需要用户处理的事项。"""
    procs = list_processes({"wechatappex.exe", "weixin.exe", "wechat.exe"})
    browsers = procs.get("wechatappex.exe", [])
    wechat_exe = (procs.get("weixin.exe") or procs.get("wechat.exe") or [""])[0]

    wx_ver = file_version(wechat_exe) if wechat_exe else None

    vers = find_wmpf_versions()
    wmpf = vers[-1] if vers else None
    wmpf_ok = (wmpf == KNOWN_VERSION)

    wmpf_dir = resolve_wmpf_dir()
    node = resolve_node()

    problems, fixes = [], []
    if not browsers:
        problems.append("微信未运行")
        fixes.append("先启动并登录微信 PC 版，然后再运行本程序")
    if not vers:
        problems.append("缺 WMPF 运行时")
        fixes.append("微信未初始化 WMPF（随便打开一次小程序即可生成），或微信版本过旧")
    elif not wmpf_ok:
        problems.append("WMPF 版本不符")
        fixes.append("当前 WMPF %s，本项目偏移基于 %s。请安装 Release 里的 "
                     "WeChatWin_4.1.15.exe 并关闭自动更新" % (wmpf, KNOWN_VERSION))
    if not wmpf_dir:
        problems.append("缺 WMPFDebugger")
        fixes.append("git clone https://github.com/evi0s/WMPFDebugger")
        fixes.append("或：set WZ_WMPF_DIR=D:\\tools\\WMPFDebugger")
    if not node:
        problems.append("缺 Node")
        fixes.append("装 Node 22+，或：set WZ_NODE=C:\\Program Files\\nodejs\\node.exe")

    info = {
        "wechat": short_version(wx_ver),
        "wmpf": str(wmpf) if wmpf else "未检测到",
        "wmpf_ok": wmpf_ok,
        "ok": not problems,
        "note": "全部就绪" if not problems else "、".join(problems),
        "action": "准备中…",
        "hint": [],
        "sign": "",
        "sign_kind": "none",
        "logfile": "",
    }
    return info, fixes, wmpf_dir, node


# ================================================================ 状态
class State:
    """把 run.py / signcore 的日志翻译成面板上的两句话。"""

    def __init__(self, info):
        self.info = dict(info)
        self.channel = False
        self.hint_idle = ["在微信打开「小程序」面板，搜索「小程序示例」",
                          "微信官方示例，体积很小；打开后可缩到后台"]
        self.hint_live = ["通道已建立，无需再操作",
                          "小程序可以缩到后台或最小化"]
        self.info["hint"] = self.hint_idle

    def snapshot(self):
        return self.info

    def set_action(self, text):
        self.info["action"] = text

    def set_sign(self, text, kind):
        self.info["sign"] = text
        self.info["sign_kind"] = kind

    def set_channel(self, up):
        if up == self.channel:
            return False
        self.channel = up
        self.info["hint"] = self.hint_live if up else self.hint_idle
        return True

    # ---------------- 日志 → 面板 ----------------
    def feed(self, msg):
        """返回 True 表示面板需要重绘。"""
        m = msg.strip()

        if "[签到]" in m:
            return self._feed_sign(m)

        if "代理就绪" in m or "拉起代理" in m:
            self.set_action("调试代理已就绪")
        elif "代理没在监听" in m:
            self.set_action("调试代理未运行，正在拉起…")
        elif "代理起不来" in m:
            self.set_action("调试代理启动失败，稍后重试")
        elif "通道未就绪" in m:
            changed = self.set_channel(False)
            self.set_action("等待小程序连接")
            return changed or True
        elif "正常值守" in m:
            self.set_channel(True)
            oid = _oid_in(m)
            self.set_action("监视中" + (" · 号 %s" % oid[:8] if oid else ""))
        elif "需要取号" in m:
            self.set_action("正在取号…（不要关闭小程序）")
        elif "拿到新号" in m:
            oid = _oid_in(m)
            self.set_channel(True)
            self.set_action("换号成功" + (" · 号 %s" % oid[:8] if oid else ""))
        elif "本轮失败" in m:
            self.set_action("取号失败，稍后重试")
        elif "冷却中" in m:
            self.set_action("取号冷却中（防止被限流）")
        elif "签到侧退出" in m:
            self.set_action("签到服务重启中…")
        elif "启动签到侧" in m or "签到侧已启动" in m:
            self.set_action("签到服务已启动")
        elif "号已失效" in m:
            self.set_action("号已失效，正在换号…")
        elif "查询失败" in m:
            self.set_action("查询失败，重试中…")
        elif "还没有 openid" in m:
            self.set_action("等待取号…")
        elif "空窗" in m:
            self.set_action("监视中（暂时没有签到，30 秒一次）")
        else:
            return False
        return True

    def _feed_sign(self, m):
        now = time.strftime("%H:%M:%S")
        # 401 / 失效要先判：它的文本里不会出现"签到成功"，但可能带 signId
        if "失效" in m or "401" in m:
            if self.channel:
                self.set_action("号已失效，正在换号…")
            else:
                self.set_action("等待小程序连接（号已失效，连上后自动换号）")
            return True
        if "签到成功" in m:
            self.set_sign("✓ %s 签到成功" % now, "ok")
            self.set_action("刚刚签到成功")
        elif "已签到（幂等）" in m or "已经签到" in m:
            self.set_sign("· %s 已签到（之前签过）" % now, "dup")
            self.set_action("监视中")
        elif "签到失败" in m:
            self.set_sign("✕ %s 签到失败" % now, "fail")
            self.set_action("签到失败，等待下一次")
        elif "退出 code" in m:
            self.set_action("签到服务重启中…")
        else:
            return False
        return True


def _oid_in(text):
    import re
    m = re.search(r"\b([0-9a-f]{32})\b", text)
    return m.group(1) if m else ""


# ================================================================ 签到线程
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
                                      log=lambda m: self.log("[签到] %s" % m),
                                      stop=self.stop)
            if self.stop.is_set():
                return
            if code == 2:
                bad_oid = signcore.read_openid(self.openid_file)
                self.log("[签到] 号失效，等续号…")
            else:
                self.log("[签到] 退出 code=%s，5s 后重试" % code)
                time.sleep(5)
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


# ================================================================ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只做环境自检")
    ap.add_argument("--plain", action="store_true", help="纯文本输出（不用颜色/重绘）")
    ap.add_argument("--no-sign", dest="with_sign", action="store_false")
    ap.add_argument("--dry-run", action="store_true", help="签到只打印不提交")
    ap.add_argument("--url", default=g.DEFAULT_URL)
    ap.add_argument("--out", default=os.path.join(HERE, "openid.txt"))
    ap.add_argument("--config", default=os.path.join(HERE, "signer", "config.json"))
    ap.add_argument("--no-pause", action="store_true", help="退出时不等待回车")
    ap.set_defaults(with_sign=True)
    args = ap.parse_args()

    term = Term(plain=args.plain)
    log = FileLog(log_dir())
    panel = Panel(term)

    info, fixes, wmpf_dir, node = preflight()

    # 把各处输出接进日志文件，保证面板干净
    ck.OUT = lambda m: log.write(m, "[ck] ")
    ck.ProxySupervisor.SINK = lambda line: log.write(line, "[proxy] ")

    state = State(info)
    state.set_action("环境自检完成" if info["ok"] else "环境未就绪")
    state.info["logfile"] = log.short if log.ok else "（日志不可写）"

    if term.plain:
        print("%s %s   by %s" % (APP, VER, AUTHOR))
        print(REPO)
        print()
        print("微信 %s   ·   WMPF %s" % (info["wechat"], info["wmpf"]))
        if info["ok"]:
            print("自检  正常")
        else:
            print("自检  异常  %s" % info["note"])
        print()
    else:
        sys.stdout.write(term.clear_screen())
    panel.render(state.snapshot())

    log.write("=" * 56)
    log.write("%s %s  by %s" % (APP, VER, AUTHOR))
    log.write("微信 %s / WMPF %s / 自检 %s" % (info["wechat"], info["wmpf"], info["note"]))
    log.write("WMPFDebugger=%s  Node=%s" % (wmpf_dir or "-", node or "-"))
    log.write("=" * 56)

    if args.check:
        _finish(term, args, "自检完成：%s" % info["note"])
        return 0 if info["ok"] else 1

    if not info["ok"]:
        _show_fixes(term, panel, fixes)
        log.write("环境未就绪，退出")
        _finish(term, args, "请先处理上面的事项")
        return 1

    # ---- 起代理 + 签到 ----
    state.set_action("启动签到服务…")
    panel.render(state.snapshot())

    signer = None
    if args.with_sign:
        cfg = signcore.load_cfg(args.config, warn=lambda m: log.write(m, "[cfg] "))
        if args.dry_run:
            cfg["submit"] = False
        signer = Signer(args.out, cfg, lambda m: _on_event(m, state, panel, log))
        signer.start()
        log.write("签到侧已启动（%d 秒轮询）" % int(cfg.get("poll", 5)))
    else:
        log.write("只取号模式（--no-sign）")

    ns = argparse.Namespace(
        url=args.url, out=args.out, poll=10.0, cooldown=300.0, observe=18.0,
        check=False, once=False, with_sign=False,
        wmpf_dir=wmpf_dir, node=node,
    )

    try:
        asyncio.run(runmod.run(ns, lambda m: _on_event(m, state, panel, log),
                               force_check=(signer.take_stale if signer else None)))
    except KeyboardInterrupt:
        log.write("用户中断（Ctrl+C）")
    finally:
        if signer:
            signer.halt()

    _finish(term, args, "已停止。调试代理仍在运行，小程序不受影响")


def _on_event(msg, state, panel, log):
    log.write(msg)                       # 全部进日志
    try:
        if state.feed(msg):              # 只有需要变化时才重绘
            panel.render(state.snapshot())
    except Exception:                    # 面板出错不能影响主流程
        pass


def _show_fixes(term, panel, fixes):
    """自检失败时，在面板下方直接给出修复步骤（这是唯一允许出现在面板外的文字）。"""
    if not fixes:
        return
    print()
    for i, line in enumerate(fixes, 1):
        print("  %d. %s" % (i, line))
    print()


def _finish(term, args, message):
    print()
    print("  " + term.c(message, term.DIM))
    if FROZEN and not args.no_pause:
        try:
            input("\n  按回车退出…")
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
