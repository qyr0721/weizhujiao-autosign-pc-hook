#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
uikit.py — 终端显示与 Windows 探测的公共件（wizard.py / setup.py 共用）

终端
  dwidth / fit      按显示宽度计算与截断（中文 2 列；歧义字符按 1 列，布局里避开它们）
  Term             颜色 + 光标（plain 时全部退化为空串）
  Redraw           定点重绘：只重画变化的行，不闪

系统
  list_processes   按 exe 名枚举进程 + 完整路径（Toolhelp，不依赖 psutil）
  file_version     读 PE 文件版本（GetFileVersionInfoW）
  wechat_version   从 Weixin.exe 读微信版本
  find_wmpf_versions / resolve_wmpf_dir / resolve_node
"""
import ctypes
import ctypes.wintypes as wintypes
import os
import sys
import time
import unicodedata

APP = "微助教自动签到"
VER = "v1.2"
AUTHOR = "qyr0721"
REPO = "github.com/qyr0721/weizhujiao-autosign-pc-hook"

KNOWN_VERSION = 25799        # hook_a8key.js 内置 RVA 对应的 WMPF 版本
KNOWN_WECHAT = "4.1.15.56"   # 与该 WMPF 版本配套的微信版本
WMPF_ROOT = os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat",
                         "xplugin", "plugins", "RadiumWMPF")


def ts():
    return time.strftime("%H:%M:%S")


# ================================================================ 控制台
def fix_console():
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


def enable_ansi(title=""):
    """打开 VT 处理（Win10+）。失败说明这终端不支持 ANSI，改用纯文本。"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        if not k32.SetConsoleMode(h, mode.value | 0x0004):
            return False
        if title:
            try:
                k32.SetConsoleTitleW(title)
            except Exception:  # noqa: BLE001
                pass
        return True
    except Exception:  # noqa: BLE001
        return False


def dwidth(text):
    """终端显示宽度（中文/全角算 2 列）。"""
    w = 0
    for ch in text:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def fit(text, width):
    """按显示宽度截断并右补空格。

    截断标记用 ASCII 的 "..."（宽度确定），不用 "…"：
    后者东亚宽度有歧义，不同终端按 1 或 2 列渲染，会把边框挤歪。
    """
    out = []
    used = 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + cw > width:
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

    def __init__(self, plain=False, title=""):
        self.plain = plain or not sys.stdout.isatty() or not enable_ansi(title)

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


class Redraw:
    """固定顶部块：只重画变化的行；行数变少时把多出来的行擦掉。"""

    def __init__(self, term, top=1):
        self.t = term
        self.top = top
        self.lines = []

    def draw(self, lines, force=False):
        if self.t.plain:
            return
        out = []
        for i, ln in enumerate(lines):
            if not force and i < len(self.lines) and self.lines[i] == ln:
                continue
            out.append(self.t.move(self.top + i) + ln + self.t.clear_line())
        for i in range(len(lines), len(self.lines)):
            out.append(self.t.move(self.top + i) + self.t.clear_line())
        self.lines = list(lines)
        out.append(self.t.move(self.top + len(lines)))
        sys.stdout.write("".join(out))
        sys.stdout.flush()


def title_box(term, lines, inner=58):
    """闭合标题框。只放中英文数字（宽度确定），所以框恒准。"""
    bar = term.c("│", term.DIM)
    out = [term.c("┌" + "─" * inner + "┐", term.DIM)]
    for text, color in lines:
        body = fit("  " + text, inner)
        if color:
            body = term.c(body, color)
        out.append(bar + body + bar)
    out.append(term.c("└" + "─" * inner + "┘", term.DIM))
    return out


def rule(term, inner=58):
    return term.c("  " + "─" * (inner // 2), term.DIM)


# ================================================================ 日志文件
class FileLog:
    def __init__(self, directory, prefix="wxsign"):
        self.path = os.path.join(directory, "%s-%s.log" % (prefix, time.strftime("%Y%m%d-%H%M%S")))
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


def log_dir(base):
    """优先程序目录的 logs/；不可写则退到 %LOCALAPPDATA%\\wxsign\\logs。"""
    cand = os.path.join(base, "logs")
    try:
        os.makedirs(cand, exist_ok=True)
        probe = os.path.join(cand, ".w")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return cand
    except Exception:  # noqa: BLE001
        return os.path.join(os.environ.get("LOCALAPPDATA", base), "wxsign", "logs")


# ================================================================ 系统探测
class _PROC(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


def list_processes(names):
    """返回 {exe名: [完整路径, ...]}，只统计 names 里的（小写比较）。"""
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    out = {}
    if snap == -1:
        return out
    e = _PROC()
    e.dwSize = ctypes.sizeof(_PROC)
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


def wechat_version():
    """微信 PC 版本。从 Weixin.exe / WeChat.exe 读（WeChatAppEx.exe 是 WMPF 客户端，版本不同）。"""
    procs = list_processes({"weixin.exe", "wechat.exe"})
    for key in ("weixin.exe", "wechat.exe"):
        for path in procs.get(key) or []:
            v = file_version(path)
            if v:
                return v, path
    # 进程没起时退回默认安装位置
    for cand in (r"C:\Program Files\Tencent\Weixin\Weixin.exe",
                 r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
                 r"C:\Program Files\Tencent\WeChat\WeChat.exe"):
        if os.path.isfile(cand):
            v = file_version(cand)
            if v:
                return v, cand
    return None, ""


def find_wmpf_versions():
    """已落地的 WMPF 运行时版本（目录名即版本号）。"""
    if not os.path.isdir(WMPF_ROOT):
        return []
    out = []
    for name in os.listdir(WMPF_ROOT):
        p = os.path.join(WMPF_ROOT, name, "extracted", "runtime", "flue.dll")
        if name.isdigit() and os.path.isfile(p):
            out.append(int(name))
    return sorted(out)


def resolve_wmpf_dir(base, extra=None):
    """找 WMPFDebugger：环境变量 → 程序目录 → 上一级 → 调用方给的兜底路径。"""
    env = os.environ.get("WZ_WMPF_DIR")
    if env and os.path.isdir(env):
        return env
    cands = [os.path.join(base, "WMPFDebugger"),
             os.path.join(os.path.dirname(base), "WMPFDebugger")]
    for parent in (base, os.path.dirname(base)):
        try:
            for name in os.listdir(parent):
                if name.lower() == "wmpfdebugger":
                    cands.append(os.path.join(parent, name))
        except OSError:
            pass
    if extra:
        cands.append(extra)
    for cand in cands:
        if cand and os.path.isdir(os.path.join(cand, "node_modules", "tsx")):
            return cand
    return None


def resolve_node(base):
    """找 node.exe：环境变量 → PATH → 程序目录下的 runtime/node。"""
    env = os.environ.get("WZ_NODE")
    if env and os.path.isfile(env):
        return env
    import shutil
    p = shutil.which("node")
    if p:
        return p
    for rel in (os.path.join("runtime", "node", "node.exe"),
                os.path.join("node", "node.exe")):
        cand = os.path.join(base, rel)
        if os.path.isfile(cand):
            return cand
    return None
