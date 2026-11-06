#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
app.py — 图形界面入口（本地 Web 应用）

双击运行后会在本机开一个只监听 127.0.0.1 的小型服务，并自动打开浏览器。
界面提供：一键启动/停止、环境自检、实时日志、首次授权指引、
以及**内置的使用教程**（点「使用教程」即可打开 TUTORIAL.md，完全离线）。

为什么不是 tkinter / 不是终端
  · 内嵌 Python 运行时不带 tkinter，改用零依赖的本地 Web UI，
    界面可以用完整的 HTML/CSS 做卡片、状态灯、动效，比终端面板可读得多。
  · 端口只绑定 127.0.0.1，不对外暴露；不上传任何数据。
  · 已有一个实例在跑时，再次双击 exe 只会重新打开界面，不会重复启动后台。

用法
  python app.py                  # 打开图形界面并启动服务
  python app.py --no-browser     # 只起服务，不自动开浏览器
  python app.py --check          # 只做环境自检并打印结果（供 CI / 排查）
  python app.py --cli            # 回退到原来的终端面板（wizard.py）
  python app.py --port 8765      # 指定端口

源码运行时读仓库根目录的 TUTORIAL.md 与 docs/img；
PyInstaller 打包时这些资源通过 --add-data 放进 sys._MEIPASS。
"""
import argparse
import asyncio
import html
import json
import os
import re
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen

BASE = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
MEIPASS = getattr(sys, "_MEIPASS", BASE)
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

import channel_keeper as ck     # noqa: E402
import get_openid as g          # noqa: E402
import installer                # noqa: E402
import run as runmod            # noqa: E402
import signcore                 # noqa: E402
import uikit as U               # noqa: E402
import wizard as W              # noqa: E402

APP_NAME = U.APP
VERSION = U.VER
DEFAULT_PORT = 8765
INSTANCE_ID = "wxsign-local-ui"
QUIT_EVENT = threading.Event()


def safe_print(msg=""):
    """--windowed 打包后没有控制台，直接 print 可能抛异常；失败就静默忽略。"""
    try:
        sys.stdout.write(str(msg) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 资源路径
def resource_path(rel):
    """优先取打包资源，源码运行时回退到仓库目录。"""
    p = os.path.join(MEIPASS, rel)
    if os.path.isfile(p):
        return p
    return os.path.join(BASE, rel)


def tutorial_text():
    try:
        with open(resource_path("TUTORIAL.md"), "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "# 使用教程\n\n未找到 TUTORIAL.md，请在 GitHub 仓库页面查看。\n"


def image_path(name):
    return resource_path(os.path.join("docs", "img", os.path.basename(name)))


# ---------------------------------------------------------------- 状态桥
class Bridge:
    """把业务逻辑（线程 / asyncio）与 HTTP 界面隔开。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.lines = deque(maxlen=2000)
        self.seq = 0
        self.stop_evt = threading.Event()
        self.running = False
        self.worker = None
        self.signer = None
        self.state = None
        self.info = {}
        self.fixes = []
        self.log = U.FileLog(U.log_dir(BASE))
        ck.OUT = lambda m: self.emit(m, "[ck] ")
        ck.ProxySupervisor.SINK = lambda line: self.emit("[proxy] " + line)
        self.refresh()

    # ---- 日志 / 状态映射 ----
    def emit(self, msg, tag=""):
        text = str(msg)
        with self.lock:
            self.seq += 1
            self.lines.append((self.seq, "[%s] %s%s"
                               % (time.strftime("%H:%M:%S"), tag, text)))
        try:
            self.log.write(text, tag)
        except Exception:  # noqa: BLE001
            pass
        st = self.state
        if st is not None:
            try:
                st.feed(text)
            except Exception:  # noqa: BLE001
                pass

    def logs_since(self, seq):
        with self.lock:
            return [{"seq": s, "text": t} for s, t in self.lines if s > seq]

    def refresh(self):
        """重新做一次环境自检，不启动后台。"""
        try:
            info, fixes, self.wmpf_dir, self.node = W.preflight(base=BASE)
            info["need_auth"] = not W._has_valid_oid(
                os.path.join(BASE, "openid.txt"))
            with self.lock:
                self.info, self.fixes = info, fixes
            if self.state is None:
                self.state = W.State(info)
                self.state.set_action("准备就绪")
            else:
                self.state.info.update({k: v for k, v in info.items()
                                        if k in ("wechat", "wmpf", "wmpf_ok",
                                                 "ok", "note", "need_auth")})
        except Exception as exc:  # noqa: BLE001
            self.emit("自检失败：%s: %s" % (type(exc).__name__, exc))
            with self.lock:
                self.info = {"wechat": "-", "wmpf": "-", "ok": False,
                             "note": str(exc), "need_auth": True}
                self.fixes = []

    def status(self):
        with self.lock:
            info = dict(self.info)
            seq = self.seq
        st = self.state
        if st is None:
            action, hint, sign, kind, channel = "准备就绪", [], "", "none", False
        else:
            snap = st.snapshot()
            action, hint = snap.get("action", ""), snap.get("hint", [])
            sign, kind, channel = snap.get("sign", ""), snap.get("sign_kind", "none"), st.channel
        return {
            "app": APP_NAME, "version": VERSION, "instance": INSTANCE_ID,
            "running": self.running, "action": action, "hint": hint,
            "sign": sign, "sign_kind": kind, "channel": channel,
            "need_auth": info.get("need_auth", False),
            "wechat": info.get("wechat", "-"), "wmpf": info.get("wmpf", "-"),
            "ok": info.get("ok", False), "note": info.get("note", ""),
            "auth_steps": W.AUTH_STEPS, "logseq": seq,
            "logfile": self.log.short if self.log.ok else "（日志不可写）",
        }

    # ---- 生命周期 ----
    def start(self):
        if self.running:
            return False
        self.stop_evt.clear()
        self.running = True
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()
        return True

    def stop(self):
        if not self.running:
            return False
        self.stop_evt.set()
        self.emit("收到停止请求…")
        return True

    def _work(self):
        try:
            self.emit("正在检查运行环境…")
            self.refresh()
            if not self.info.get("ok"):
                self.emit("环境不完整，开始自动部署组件（首次运行可能需要几分钟）…")
                setup = installer.Setup(
                    self.log, BASE,
                    notify=lambda a, p="": self.emit(a + ("  " + p if p else "")))
                setup.run()
                self.refresh()
            if not self.info.get("ok"):
                self.emit("环境仍未就绪：" + str(self.info.get("note", "")))
                for f in self.fixes:
                    self.emit("  · " + f)
                return

            info = dict(self.info)
            info["need_auth"] = not W._has_valid_oid(
                os.path.join(BASE, "openid.txt"))
            st = W.State(info)
            st.set_action("启动签到服务…")
            self.state = st
            if info["need_auth"]:
                self.emit("首次使用：请先在微信里手动授权一次（界面上有步骤）")

            cfg_path = W._prepare_config(None, self.log, base=BASE)
            cfg = signcore.load_cfg(cfg_path, warn=lambda m: self.emit(m, "[cfg] "))
            if cfg.get("lat") is None:
                self.emit("配置：%s（未设坐标，GPS 签到不带位置）" % cfg_path)
            else:
                self.emit("配置：%s（坐标 %.6f, %.6f）"
                          % (cfg_path, float(cfg["lat"]), float(cfg["lon"])))

            signer = W.Signer(os.path.join(BASE, "openid.txt"), cfg, self.emit)
            self.signer = signer
            signer.start()

            ns = argparse.Namespace(
                url=g.DEFAULT_URL, out=os.path.join(BASE, "openid.txt"),
                poll=5.0, cooldown=0.0, observe=18.0,
                check=False, once=False, with_sign=False,
                wmpf_dir=self.wmpf_dir, node=self.node, auto_offsets=True,
            )
            self.emit("界面已就绪，开始值守。")
            asyncio.run(runmod.run(
                ns, self.emit,
                force_check=signer.take_stale,
                stop_check=self.stop_evt.is_set))
        except Exception as exc:  # noqa: BLE001
            self.emit("运行异常：%s: %s" % (type(exc).__name__, exc))
        finally:
            if self.signer:
                self.signer.halt()
                self.signer = None
            if self.state:
                self.state.set_action("已停止")
            self.running = False
            self.emit("签到服务已停止（调试代理保持运行，小程序不受影响）")

    # ---- 打开本地文件 ----
    def open_path(self, which):
        try:
            if which == "log":
                p = self.log.path
            elif which == "config":
                p = W._prepare_config(None, self.log, base=BASE)
            elif which == "folder":
                p = BASE
            else:
                return False
            if not os.path.exists(p):
                return False
            os.startfile(p)  # noqa: S606 - Windows 专用，已限制在程序目录内
            return True
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------- 教程渲染
_IMG_TAG = re.compile(r'<img\s+[^>]*>', re.I)
_MD_IMG = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
_IMG_SRC = re.compile(r'src=["\']([^"\']+)["\']', re.I)
_BOLD = re.compile(r'\*\*(.+?)\*\*')
_CODE = re.compile(r'`([^`]+)`')
_LINK = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')


def _inline(text):
    """渲染一行里的行内元素；返回 HTML。"""
    holders = []

    def hold_img(m):
        tag = m.group(0)
        src = _IMG_SRC.search(tag)
        if not src:
            return ""
        name = os.path.basename(src.group(1))
        alt = re.search(r'alt=["\']([^"\']*)["\']', tag)
        width = re.search(r'width=["\']([^"\']*)["\']', tag)
        h = ('<img class="md-img" src="/img/%s" alt="%s"%s>'
             % (html.escape(name), html.escape(alt.group(1) if alt else ""),
                ' style="max-width:%spx"' % width.group(1) if width else ""))
        holders.append(h)
        return "\x00IMG%d\x00" % (len(holders) - 1)

    def hold_md_img(m):
        name = os.path.basename(m.group(2))
        h = ('<img class="md-img" src="/img/%s" alt="%s">'
             % (html.escape(name), html.escape(m.group(1))))
        holders.append(h)
        return "\x00IMG%d\x00" % (len(holders) - 1)

    # 两种图片写法都要支持：HTML <img> 与 Markdown ![alt](src)
    s = _IMG_TAG.sub(hold_img, text)
    s = _MD_IMG.sub(hold_md_img, s)
    s = html.escape(s, quote=False)
    s = _LINK.sub(lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>'
                  % (html.escape(m.group(2), quote=True), m.group(1)), s)
    s = _BOLD.sub(r'<strong>\1</strong>', s)
    s = _CODE.sub(r'<code>\1</code>', s)
    for i, h in enumerate(holders):
        s = s.replace("\x00IMG%d\x00" % i, h)
    return s


def _slug(n):
    return "sec-%d" % n


def markdown_to_html(md):
    """把 TUTORIAL.md 渲染成带侧栏目录的 HTML 片段。

    支持：标题、引用、有序/无序列表、代码块、表格、分隔线、
    行内粗体/代码/链接，以及教程里使用的 <img> 标签。
    """
    lines = md.splitlines()
    out, toc = [], []
    i, sec = 0, 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre><code>%s</code></pre>" % "\n".join(buf))
            continue

        if stripped.startswith("|") and i + 1 < len(lines) \
                and re.match(r'^\s*\|[\s:|-]+\|\s*$', lines[i + 1]):
            head = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append('<table class="md-table"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>'
                       % ("".join("<th>%s</th>" % _inline(c) for c in head),
                          "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in r)
                                  for r in rows)))
            continue

        m = re.match(r'^(#{1,4})\s+(.*)$', stripped)
        if m:
            level = len(m.group(1))
            sec += 1
            sid = _slug(sec)
            title = re.sub(r'\*\*|`', '', m.group(2))
            if level >= 2:
                toc.append((level, sid, title))
            out.append('<h%d id="%s">%s</h%d>' % (level, sid, _inline(m.group(2)), level))
            i += 1
            continue

        if stripped == "---":
            out.append("<hr>")
            i += 1
            continue

        if stripped.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote>%s</blockquote>"
                       % "<br>".join(_inline(x) for x in buf if x))
            continue

        if re.match(r'^\s*([-*]|\d+\.)\s+', line):
            ordered = bool(re.match(r'^\s*\d+\.\s+', line))
            items = []
            while i < len(lines) and re.match(r'^\s*([-*]|\d+\.)\s+', lines[i]):
                item = re.sub(r'^\s*([-*]|\d+\.)\s+', '', lines[i]).strip()
                items.append("<li>%s</li>" % _inline(item))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join(items), tag))
            continue

        if not stripped:
            i += 1
            continue

        para = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith(("#", ">", "|", "```"))
                    or nxt == "---"
                    or re.match(r'^\s*([-*]|\d+\.)\s+', lines[i])
                    or _IMG_TAG.match(nxt) or _MD_IMG.match(nxt)):
                break
            para.append(nxt)
            i += 1
        body = " ".join(para)
        if _IMG_TAG.search(body) or _MD_IMG.search(body):
            out.append('<p class="md-img-wrap">%s</p>' % _inline(body))
        else:
            out.append("<p>%s</p>" % _inline(body))

    nav = "".join('<a class="toc-l%d" href="#%s">%s</a>' % (lv, sid, html.escape(t))
                  for lv, sid, t in toc)
    return "\n".join(out), nav


def tutorial_page():
    body, nav = markdown_to_html(tutorial_text())
    return (TUTORIAL_SHELL
            .replace("__APP_NAME__", html.escape(APP_NAME))
            .replace("__VERSION__", html.escape(VERSION))
            .replace("__NAV__", nav)
            .replace("__BODY__", body))


# ---------------------------------------------------------------- 前端页面
INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__APP_NAME__</title>
<style>
:root{
  --bg:#f3f5fb; --card:#fff; --ink:#1e2740; --muted:#7b849c;
  --line:#e8ecf6; --brand:#5b6cf9; --brand2:#7c5ce7;
  --ok:#12b981; --warn:#f59e0b; --bad:#ef4444; --idle:#aab3c7;
  --shadow:0 10px 34px rgba(38,50,90,.07); --radius:18px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:26px 26px 40px}
header{background:linear-gradient(120deg,var(--brand),var(--brand2));
  border-radius:var(--radius);padding:24px 28px;color:#fff;
  box-shadow:0 14px 32px rgba(91,108,249,.24);display:flex;
  align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:14px}
.logo{width:46px;height:46px;border-radius:14px;background:rgba(255,255,255,.2);
  display:grid;place-items:center;font-size:24px;font-weight:600;
  border:1px solid rgba(255,255,255,.32)}
.brand h1{margin:0;font-size:20px;letter-spacing:.3px;font-weight:650}
.brand p{margin:4px 0 0;font-size:12.5px;opacity:.82}
.hdr-right{display:flex;align-items:center;gap:12px}
.ver{font-size:12px;background:rgba(255,255,255,.18);padding:5px 11px;
  border-radius:999px;border:1px solid rgba(255,255,255,.28)}
.pill{display:flex;align-items:center;gap:8px;background:rgba(255,255,255,.16);
  padding:8px 15px;border-radius:999px;font-size:13px;font-weight:600}
.dot{width:9px;height:9px;border-radius:50%;background:var(--idle);flex:none}
.dot.on{background:#4ade80;box-shadow:0 0 0 0 rgba(74,222,128,.6);animation:pulse 1.8s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 9px rgba(74,222,128,0)}100%{box-shadow:0 0 0 0 rgba(74,222,128,0)}}
.grid{display:grid;grid-template-columns:310px 1fr;gap:18px;margin-top:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:20px 22px;animation:rise .45s ease both}
@keyframes rise{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:none}}
.card h2{margin:0 0 15px;font-size:13px;letter-spacing:1.3px;color:var(--muted);
  text-transform:uppercase;font-weight:700}
.row{display:flex;align-items:center;justify-content:space-between;
  padding:10px 0;border-bottom:1px dashed var(--line);font-size:13.5px}
.row:last-child{border-bottom:0}
.row .k{color:var(--muted)}
.row .v{font-weight:600;display:flex;align-items:center;gap:8px}
.mini{width:7px;height:7px;border-radius:50%;background:var(--idle);flex:none}
.mini.g{background:var(--ok)} .mini.y{background:var(--warn)} .mini.r{background:var(--bad)}
.hero{padding:26px 28px}
.hero .label{font-size:12px;color:var(--muted);letter-spacing:1.4px;font-weight:700}
.hero .action{font-size:27px;font-weight:700;margin:12px 0 6px;line-height:1.35;
  background:linear-gradient(120deg,var(--brand),var(--brand2));
  -webkit-background-clip:text;background-clip:text;color:transparent}
.hero .hint{color:var(--muted);font-size:13.5px;line-height:1.9;min-height:24px}
.signbar{margin-top:16px;padding:13px 16px;border-radius:12px;background:#f7f9ff;
  border:1px solid var(--line);font-size:13.5px;display:flex;
  justify-content:space-between;align-items:center;gap:12px}
.signbar b.ok{color:var(--ok)} .signbar b.dup{color:var(--muted)} .signbar b.fail{color:var(--bad)}
.btns{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}
button{font-family:inherit;cursor:pointer;border:0;border-radius:11px;
  font-size:13.5px;font-weight:600;padding:11px 18px;transition:.18s ease}
button:disabled{opacity:.45;cursor:not-allowed;transform:none!important}
.primary{background:linear-gradient(120deg,var(--brand),var(--brand2));color:#fff;
  box-shadow:0 8px 20px rgba(91,108,249,.28);padding:12px 26px}
.primary:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 12px 26px rgba(91,108,249,.36)}
.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
.ghost:hover:not(:disabled){border-color:var(--brand);color:var(--brand);transform:translateY(-2px)}
.danger{background:#fff1f1;color:#d94b4b;border:1px solid #ffdede}
.notice{margin-top:18px;background:#fffaf0;border:1px solid #ffe6b8;
  border-radius:14px;padding:16px 20px;font-size:13.5px;line-height:1.9}
.notice b{color:#b45309}
.notice ol{margin:8px 0 0;padding-left:20px}
.logcard{margin-top:18px}
.loghead{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.loghead h2{margin:0}
#log{height:230px;overflow:auto;background:#111827;color:#d7def0;border-radius:12px;
  padding:14px 16px;font:12px/1.75 "Cascadia Mono",Consolas,"Microsoft YaHei",monospace;
  white-space:pre-wrap;word-break:break-all}
#log .ok{color:#7ee2b8} #log .warn{color:#fcd34d} #log .err{color:#fca5a5}
#log .info{color:#a5b4fc}
footer{text-align:center;color:var(--muted);font-size:12px;margin-top:22px;line-height:1.9}
.toast{position:fixed;right:26px;bottom:26px;background:#1e2740;color:#fff;
  padding:12px 18px;border-radius:12px;font-size:13.5px;opacity:0;
  transform:translateY(10px);transition:.25s;pointer-events:none;box-shadow:var(--shadow)}
.toast.show{opacity:1;transform:none}
@media(max-width:900px){.grid{grid-template-columns:1fr}.hero .action{font-size:22px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">✓</div>
      <div><h1>__APP_NAME__</h1><p>自动签到 · 凭证自动续期 · 版本自适应</p></div>
    </div>
    <div class="hdr-right">
      <span class="ver" id="ver">__VERSION__</span>
      <span class="pill"><span class="dot" id="runDot"></span><span id="runText">未启动</span></span>
    </div>
  </header>

  <div class="grid">
    <div>
      <div class="card">
        <h2>运行环境</h2>
        <div class="row"><span class="k">微信版本</span><span class="v" id="wechat">—</span></div>
        <div class="row"><span class="k">WMPF 运行时</span><span class="v" id="wmpf">—</span></div>
        <div class="row"><span class="k">环境自检</span><span class="v"><span class="mini" id="okDot"></span><span id="okText">检查中</span></span></div>
        <div class="row"><span class="k">调试通道</span><span class="v"><span class="mini" id="chDot"></span><span id="chText">未连接</span></span></div>
        <div class="row"><span class="k">日志文件</span><span class="v" id="logfile" style="font-size:11px;color:var(--muted)">—</span></div>
      </div>
      <div class="card" style="margin-top:18px">
        <h2>快捷操作</h2>
        <div class="btns" style="margin-top:0;flex-direction:column;align-items:stretch">
          <button class="ghost" onclick="openTutorial()">📖 使用教程（内置，可离线）</button>
          <button class="ghost" onclick="openFile('config')">⚙ 打开签到配置</button>
          <button class="ghost" onclick="openFile('log')">📄 打开日志文件</button>
          <button class="ghost" onclick="openFile('folder')">📁 打开程序目录</button>
          <button class="ghost" onclick="recheck()">🔄 重新自检</button>
          <button class="ghost" onclick="doQuit()" style="color:#d94b4b">⏻ 退出程序</button>
        </div>
      </div>
    </div>

    <div>
      <div class="card hero">
        <div class="label">当前状态</div>
        <div class="action" id="action">准备就绪</div>
        <div class="hint" id="hint">点击下方「启动签到」开始值守。</div>
        <div class="signbar">
          <span>最近一次签到</span><b id="sign">—</b>
        </div>
        <div class="btns">
          <button class="primary" id="startBtn" onclick="doStart()">▶ 启动签到</button>
          <button class="danger" id="stopBtn" onclick="doStop()" disabled>■ 停止</button>
        </div>
        <div class="notice" id="authBox" style="display:none">
          <b>⚠ 首次使用，需要先在微信里手动授权一次</b>
          <ol id="authSteps"></ol>
          <div>完成后不必重启，程序会自动继续。</div>
        </div>
        <div class="notice" id="chBox" style="display:none;background:#f3f8ff;border-color:#d6e6ff">
          <b style="color:#2563eb">小程序要从「最近使用」打开</b>
          <div>先在顶部搜索框搜到目标小程序（只是让它进列表），<b>不要点搜索结果</b>，
          再回到「小程序」面板 →「最近使用」点开。直接点搜索结果打开的实例检测不到，需关掉重开。</div>
        </div>
      </div>

      <div class="card logcard">
        <div class="loghead"><h2>运行日志</h2>
          <button class="ghost" style="padding:6px 12px;font-size:12px" onclick="clearLog()">清空显示</button></div>
        <div id="log"></div>
      </div>
    </div>
  </div>

  <footer>本界面仅监听 127.0.0.1，不对外暴露、不上传数据 · 退出时调试代理保持运行，小程序不受影响<br>
    __APP_NAME__ __VERSION__ · 仅供学习与技术研究使用</footer>
</div>
<div class="toast" id="toast"></div>
<script>
let seq=0, toastTimer=null;
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove('show'),2600);}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function cls(t){if(/签到成功|已自动更新|正常值守|通道已建立/.test(t))return 'ok';
  if(/失败|异常|401|起不来/.test(t))return 'err';
  if(/等待|正在|未就绪|冷却/.test(t))return 'warn';return 'info';}
async function api(path,method='GET',body=null){
  const o={method};if(body)o.body=JSON.stringify(body);
  const r=await fetch(path,o);return r.json();}
function renderStatus(s){
  document.getElementById('ver').textContent=s.version||'';
  document.getElementById('wechat').textContent=s.wechat||'-';
  document.getElementById('wmpf').textContent=s.wmpf||'-';
  document.getElementById('okText').textContent=s.ok?'正常':(s.note||'异常');
  document.getElementById('okDot').className='mini '+(s.ok?'g':'r');
  document.getElementById('chText').textContent=s.channel?'已连接':'未连接';
  document.getElementById('chDot').className='mini '+(s.channel?'g':'');
  document.getElementById('logfile').textContent=s.logfile||'';
  document.getElementById('runDot').className='dot'+(s.running?' on':'');
  document.getElementById('runText').textContent=s.running?'运行中':'未启动';
  document.getElementById('action').textContent=s.action||'准备就绪';
  document.getElementById('hint').innerHTML=(s.hint||[]).map(esc).join('<br>')||'点击下方「启动签到」开始值守。';
  const sg=document.getElementById('sign');sg.textContent=s.sign||'—';
  sg.className=s.sign_kind||'';
  document.getElementById('startBtn').disabled=!!s.running;
  document.getElementById('stopBtn').disabled=!s.running;
  document.getElementById('authBox').style.display=s.need_auth&&!s.running?'block':'none';
  if(s.need_auth&&!s.running){document.getElementById('authSteps').innerHTML=
    (s.auth_steps||[]).map(x=>'<li>'+esc(x.replace(/^\d+\.\s*/,''))+'</li>').join('');}
  document.getElementById('chBox').style.display=(!s.channel&&s.running)?'block':'none';
}
function appendLogs(rows){
  if(!rows.length)return;
  const box=document.getElementById('log');
  const atBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-30;
  for(const r of rows){const d=document.createElement('div');d.className=cls(r.text);
    d.textContent=r.text;box.appendChild(d);seq=Math.max(seq,r.seq);}
  while(box.childNodes.length>600)box.removeChild(box.firstChild);
  if(atBottom)box.scrollTop=box.scrollHeight;
}
function clearLog(){document.getElementById('log').innerHTML='';}
async function tick(){
  try{renderStatus(await api('/api/status'));appendLogs(await api('/api/logs?since='+seq));}
  catch(e){document.getElementById('action').textContent='连接中断，请重新启动程序';}
}
async function doStart(){const r=await api('/api/start','POST',{});toast(r.ok?'已启动':'启动失败：'+(r.msg||''));tick();}
async function doStop(){const r=await api('/api/stop','POST',{});toast(r.ok?'正在停止…':'当前没有运行中的任务');tick();}
async function recheck(){await api('/api/check','POST',{});toast('已重新自检');tick();}
async function doQuit(){
  if(!confirm('退出后界面关闭、签到停止；调试代理会保留，小程序不受影响。确定退出吗？'))return;
  try{await api('/api/quit','POST',{});}catch(e){}
  document.getElementById('action').textContent='程序已退出，可以关闭此标签页';
  document.getElementById('startBtn').disabled=true;
  document.getElementById('runText').textContent='已退出';
  document.getElementById('runDot').className='dot';
  toast('程序已退出');
}
async function openFile(k){const r=await api('/api/open','POST',{which:k});if(!r.ok)toast('打开失败，请手动到程序目录查看');}
function openTutorial(){window.open('/tutorial','_blank');}
tick();setInterval(tick,1000);
</script>
</body></html>"""

TUTORIAL_SHELL = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>使用教程 · __APP_NAME__</title>
<style>
:root{--bg:#f5f7fc;--card:#fff;--ink:#1e2740;--muted:#6f7893;--line:#e7ebf5;
--brand:#5b6cf9;--brand2:#7c5ce7;--radius:16px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
 line-height:1.85;-webkit-font-smoothing:antialiased}
.top{background:linear-gradient(120deg,var(--brand),var(--brand2));color:#fff;
 padding:18px 30px;display:flex;justify-content:space-between;align-items:center;
 font-size:15px;font-weight:600;box-shadow:0 8px 24px rgba(91,108,249,.22)}
.top a{color:#fff;text-decoration:none;background:rgba(255,255,255,.18);
 padding:7px 14px;border-radius:999px;font-size:12.5px}
.layout{display:grid;grid-template-columns:250px 1fr;gap:24px;max-width:1180px;
 margin:24px auto;padding:0 24px 60px}
nav{position:sticky;top:24px;align-self:start;background:#fff;border:1px solid var(--line);
 border-radius:var(--radius);padding:18px 16px;max-height:calc(100vh - 60px);overflow:auto}
nav h3{margin:0 0 12px;font-size:12px;letter-spacing:1.2px;color:var(--muted)}
nav a{display:block;color:#475069;text-decoration:none;font-size:12.8px;
 padding:6px 9px;border-radius:8px;line-height:1.5}
nav a:hover{background:#f1f3ff;color:var(--brand)}
nav a.toc-l3{padding-left:22px;color:var(--muted);font-size:12px}
article{background:#fff;border:1px solid var(--line);border-radius:var(--radius);
 padding:34px 42px;box-shadow:0 10px 34px rgba(38,50,90,.06);min-width:0}
h1{font-size:27px;margin:0 0 20px;padding-bottom:16px;border-bottom:2px solid #eef1fa}
h2{font-size:20px;margin:34px 0 14px;padding-left:12px;border-left:4px solid var(--brand)}
h3{font-size:16px;margin:24px 0 10px;color:#39435e}
h4{font-size:14px;margin:18px 0 8px}
p{margin:10px 0;font-size:14px}
blockquote{margin:14px 0;padding:14px 18px;background:#f7f9ff;
 border-left:4px solid var(--brand);border-radius:0 10px 10px 0;color:#414b68;font-size:13.8px}
blockquote strong{color:var(--brand)}
ul,ol{padding-left:24px;font-size:14px}li{margin:6px 0}
code{background:#f1f3f9;padding:2px 6px;border-radius:5px;
 font:12.5px Consolas,monospace;color:#d1495b}
pre{background:#111827;color:#dbe3f4;padding:16px 18px;border-radius:11px;
 overflow:auto;font:12.5px/1.7 Consolas,monospace}
pre code{background:none;color:inherit;padding:0}
hr{border:0;border-top:1px solid var(--line);margin:28px 0}
a{color:var(--brand)}
.md-table{border-collapse:collapse;width:100%;margin:16px 0;font-size:13.4px;display:block;overflow:auto}
.md-table th,.md-table td{border:1px solid var(--line);padding:9px 12px;text-align:left;vertical-align:top}
.md-table th{background:#f5f7ff;font-weight:650}
.md-img{display:block;max-width:100%;height:auto;margin:16px 0;border:1px solid var(--line);
 border-radius:12px;box-shadow:0 6px 20px rgba(38,50,90,.07)}
.md-img-wrap{text-align:center}
@media(max-width:860px){.layout{grid-template-columns:1fr}nav{position:static;max-height:none}article{padding:24px}}
</style></head><body>
<div class="top"><span>📖 使用教程 · __APP_NAME__ __VERSION__</span><a href="/">← 返回控制台</a></div>
<div class="layout">
  <nav><h3>目录</h3>__NAV__</nav>
  <article>__BODY__</article>
</div></body></html>"""


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    bridge = None

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML
                       .replace("__APP_NAME__", html.escape(APP_NAME))
                       .replace("__VERSION__", html.escape(VERSION)))
        elif path == "/tutorial":
            self._send(200, tutorial_page())
        elif path.startswith("/img/"):
            name = os.path.basename(path)
            p = image_path(name)
            if not os.path.isfile(p) or not name.lower().endswith(".png"):
                return self._send(404, "not found", "text/plain")
            with open(p, "rb") as f:
                self._send(200, f.read(), "image/png")
        elif path == "/api/ping":
            self._json({"app": INSTANCE_ID, "version": VERSION})
        elif path == "/api/status":
            self._json(self.bridge.status())
        elif path == "/api/logs":
            qs = parse_qs(urlparse(self.path).query)
            try:
                since = int(qs.get("since", ["0"])[0])
            except ValueError:
                since = 0
            self._json({"rows": self.bridge.logs_since(since)})
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        b = self.bridge
        if path == "/api/start":
            self._json({"ok": b.start()})
        elif path == "/api/stop":
            self._json({"ok": b.stop()})
        elif path == "/api/check":
            b.refresh()
            self._json({"ok": True, "status": b.status()})
        elif path == "/api/quit":
            b.stop()
            QUIT_EVENT.set()
            self._json({"ok": True})
        elif path == "/api/open":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:  # noqa: BLE001
                payload = {}
            self._json({"ok": b.open_path(payload.get("which", ""))})
        else:
            self._json({"ok": False, "msg": "unknown endpoint"}, 404)


def pick_port(preferred):
    """从 preferred 起找可用端口；若已是本程序实例则返回 None 交给调用方复用。"""
    import socket
    for port in range(preferred, preferred + 20):
        with socket.socket() as s:
            s.settimeout(0.35)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def existing_instance(port):
    try:
        with urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=1) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == INSTANCE_ID
    except Exception:  # noqa: BLE001
        return False


def main():
    ap = argparse.ArgumentParser(description="%s 图形界面" % APP_NAME)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--check", action="store_true", help="只做自检并打印结果")
    ap.add_argument("--cli", action="store_true", help="回退到终端面板 wizard.py")
    args = ap.parse_args()

    if args.cli:
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--cli"]
        return W.main()

    if args.check:
        info, fixes, wmpf, node = W.preflight(base=BASE)
        safe_print(json.dumps({
            "app": APP_NAME, "version": VERSION, "wechat": info["wechat"],
            "wmpf": info["wmpf"], "ok": info["ok"], "note": info["note"],
            "wmpf_dir": wmpf or "-", "node": node or "-",
            "tutorial": os.path.isfile(resource_path("TUTORIAL.md")),
        }, ensure_ascii=False, indent=2))
        return 0 if info["ok"] else 1

    port = pick_port(args.port)
    url = "http://127.0.0.1:%d/" % port
    if existing_instance(port):
        if not args.no_browser:
            webbrowser.open(url)
        safe_print("已有实例在运行，已打开界面：%s" % url)
        return 0

    bridge = Bridge()
    Handler.bridge = bridge
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    safe_print("%s %s 界面已启动：%s" % (APP_NAME, VERSION, url))
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        while not QUIT_EVENT.wait(1):
            pass
    except KeyboardInterrupt:
        safe_print("\n正在退出…")
    finally:
        bridge.stop()
        server.shutdown()
    safe_print("已退出。调试代理仍在运行，小程序不受影响。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
