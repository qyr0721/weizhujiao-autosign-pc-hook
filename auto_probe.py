#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
auto_probe.py — 一次跑完两个关键实验，并直接出报告

为什么要有它：拨号栈必须在「小程序启动的那一刻」才有命中，页内能力又必须
「通道已建立 + H5 标签还在」时才对。人工掐这两次时机几乎必然错过。
所以做成：先挂钩子，再等通道，通道一来立刻取证，最后落盘报告。

产出的三个答案：
  A. 谁在拨 ws://localhost:9421        -> 决定能否彻底免点火
  B. 每个 H5 标签的真实上下文能力       -> 决定用哪种续期手段
  C. WeixinJSBridge.invoke('login') 能否拿到 code
     -> 若能，则"页内续期、永久免人工授权"成立，且全程没有导航风险

用法：
  python auto_probe.py                     # 挂钩子 + 起代理，等你开一次小程序
  python auto_probe.py --seconds 600       # 最多等 10 分钟
  python auto_probe.py --no-dialer         # 只要 H5 体检
  python auto_probe.py --no-login          # 不调 login（更保守）

返回码：0 拿到报告 / 2 超时未等到通道 / 3 代理起不来
"""
import argparse
import asyncio
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import channel_keeper as ck          # noqa: E402
import cdp_sign_agent as base        # noqa: E402
from probe_dialer import find_browser_pid  # noqa: E402

EXCLUDE = ("liteapp.weixin.qq.com", "servicewechat.com", "wxa.wxs.qq.com",
           "weixin://resourceid", "about:")
HTTPISH = ("http://", "https://")


def ts():
    return time.strftime("%H:%M:%S")


def say(msg):
    print("[%s] %s" % (ts(), msg), flush=True)


# ------------------------------------------------------------------ 拨号钩子
class DialerWatcher:
    """在代理之前挂上 ws2_32 钩子，这样"下一 次点火"的拨号栈不会漏。"""

    def __init__(self, js_path):
        self.js_path = js_path
        self.hits = []
        self.ready = threading.Event()
        self.error = None
        self._stop = False
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self.ready.wait(20)

    def _run(self):
        try:
            import frida

            pid, _cmd = find_browser_pid()
            with open(self.js_path, "r", encoding="utf-8") as f:
                src = f.read()
            session = frida.get_local_device().attach(pid)
            script = session.create_script(src)

            def on_message(message, _data):
                if message.get("type") == "error":
                    self.error = message.get("description")
                    return
                payload = message.get("payload") or {}
                if payload.get("type") == "dialer":
                    self.hits.append(payload)
                    say("[DIALER] via %s" % payload.get("via"))
                    for i, fr in enumerate((payload.get("bt") or [])[:10]):
                        print("          #%-2d %s" % (i, fr), flush=True)
                elif payload.get("type") == "ready":
                    say("[+] 拨号钩子就绪 pid=%s flue=%s" % (payload.get("pid"), payload.get("flue")))
                    self.ready.set()

            script.on("message", on_message)
            script.load()
            while not self._stop:
                time.sleep(0.4)
            script.unload()
            session.detach()
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.ready.set()

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=8)


# ------------------------------------------------------------------ H5 体检
def eligible(t):
    url = t.get("url") or ""
    if not url.startswith(HTTPISH):
        return False
    if any(x in url for x in EXCLUDE):
        return False
    return t.get("type") in ("page", "webview", "iframe")


async def probe_h5(cdp, target, probe_src, use_login, timeout=15):
    await base.attach(cdp, target["targetId"])
    for dom in ("Runtime.enable", "Page.enable"):
        try:
            await cdp.send(dom, {}, timeout=8)
        except Exception:  # noqa: BLE001
            pass
    if not use_login:
        await base.evaluate(cdp, "window.__H5P = {login:false}", await_promise=False)
    raw = await base.evaluate(cdp, probe_src, timeout=timeout)
    cdp.session_id = None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"raw": str(raw)[:400]}


# ------------------------------------------------------------------ 报告
def render_report(dialer, targets, probes, state_reason):
    lines = []
    lines.append("# auto_probe 报告\n")
    lines.append("生成时间：%s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))

    lines.append("## A. 拨号点（谁在拨 :9421）\n")
    if dialer is None:
        lines.append("_未启用（--no-dialer）_\n")
    elif dialer.error:
        lines.append("钩子异常：`%s`\n" % dialer.error)
    elif not dialer.hits:
        lines.append("未命中。可能这次没有新开小程序，或调试客户端不走 `ws2_32.connect`。\n")
        lines.append("下一步：改钩 `winhttp.dll!WinHttpWebSocketCompleteUpgrade`。\n")
    else:
        for i, h in enumerate(dialer.hits, 1):
            lines.append("### 命中 %d（via `%s`）\n" % (i, h.get("via")))
            lines.append("```")
            for fr in (h.get("bt") or []):
                lines.append(fr)
            lines.append("```\n")
        lines.append("判读：栈里出现 `flue.dll!<符号>+偏移` 的那一帧就是拨号点。\n")
        lines.append("- 若是通用「远程调试客户端启动」函数 → 可在代理启动时直接调用，**点火消失**。")
        lines.append("- 若只在小程序上下文可达 → 保留自动点火。")
        lines.append("- 若同一函数在 H5 打开时也被调用 → **连小程序都不需要**（需再跑一次 H5 场景对照）。\n")

    lines.append("\n## B. 标签页清单\n")
    lines.append("| type | attached | url |")
    lines.append("|---|---|---|")
    for t in targets:
        lines.append("| %s | %s | %s |" % (t.get("type"), t.get("attached"),
                                            (t.get("url") or "")[:110]))
    lines.append("")

    lines.append("\n## C. H5 上下文体检\n")
    if not probes:
        lines.append("_没有可检的 H5 标签，或通道未建立_（reason=%s）\n" % state_reason)
    for url, info in probes:
        lines.append("### %s\n" % url[:110])
        if "raw" in info:
            lines.append("解析失败：`%s`\n" % info["raw"])
            continue
        lines.append("| 项 | 值 |")
        lines.append("|---|---|")
        for k in ("hasBridge", "bridgeInvoke", "hasWx", "wxConfig", "miniProgram",
                  "cookieLen", "bodyLen", "login", "loginErrMsg", "codeLen"):
            if k in info:
                lines.append("| %s | %s |" % (k, info.get(k)))
        lines.append("")
        if info.get("wxKeys"):
            lines.append("wx 上的键：`%s`\n" % ", ".join(str(x) for x in info["wxKeys"][:16]))

    lines.append("\n## D. 结论速判\n")
    any_code = [u for u, i in probes if i.get("login") == "code"]
    any_bridge = [u for u, i in probes if i.get("bridgeInvoke")]
    if any_code:
        lines.append("**页内 login 拿到了 code** → 可以走 `WeixinJSBridge.invoke('login')` + "
                     "站点 code→session 兑换实现续期，**不需要 OAuth、不需要人工点击**。")
        lines.append("下一步：抓签到页 401 之后它自己的网络请求，找带 `code` 的兑换端点。")
    elif any_bridge:
        lines.append("**bridge 在，但 login 没给 code**（看 `loginErrMsg`）。")
        lines.append("常见原因：该页未通过 `wx.config` 鉴权，或该 appid 未开通登录能力。")
        lines.append("下一步：先跑 `--no-login` 确认 bridge 与 wx 状态，再试 `trigger_oauth.py` 的 href 变体。")
    elif probes:
        lines.append("**页内没有 WeixinJSBridge** → 这个标签不是真正的微信 H5 上下文"
                     "（可能是容器页或被限制的页面）。换一个由聊天/菜单打开的服务号页面再测。")
    else:
        lines.append("信息不足：先让通道建立（开一次小程序），并确保至少有一个普通 H5 标签页。")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ 主流程
async def run(args):
    dialer = None
    if not args.no_dialer:
        if not os.path.exists(args.dialer_js):
            say("[!] 找不到 %s，跳过拨号钩子" % args.dialer_js)
        else:
            dialer = DialerWatcher(args.dialer_js)
            say("[*] 挂拨号钩子（必须在点火之前）...")
            if not dialer.start():
                say("[!] 钩子未就绪：%s" % (dialer.error or "timeout"))

    sup = ck.ProxySupervisor(args.wmpf_dir, args.node)
    if not sup.ensure():
        if dialer:
            dialer.stop()
        say("[3] 代理起不来")
        return 3

    say("[*] 等通道建立（请在微信里打开任意小程序）...")
    loop = asyncio.get_event_loop()
    deadline = loop.time() + args.seconds
    targets, reason = [], "timeout"
    while loop.time() < deadline:
        state, targets, reason = await ck.probe_channel()
        if state == ck.CHANNEL_UP:
            say("[+] 通道已建立，targets=%d" % len(targets))
            break
        await asyncio.sleep(3)

    if not targets:
        say("[2] 等不到通道（%s）。仍会写出报告，含已捕获的拨号信息。" % reason)
        path = write(args, dialer, [], [], reason)
        if dialer:
            dialer.stop()
        sup.stop()
        say("[=] 报告：%s" % path)
        return 2

    with open(args.probe_js, "r", encoding="utf-8") as f:
        probe_src = f.read()

    probes = []
    try:
        ws = base.connect()
    except (OSError, ConnectionRefusedError) as exc:
        say("[3] 连不上 62000: %s" % exc)
        if dialer:
            dialer.stop()
        sup.stop()
        return 3

    async with ws as sock:
        cdp = base.Cdp(sock)
        try:
            await cdp.send("Target.setDiscoverTargets", {"discover": True}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        cands = [t for t in targets if eligible(t)]
        say("[*] 可体检的 H5 标签：%d 个" % len(cands))
        if args.limit:
            cands = cands[:args.limit]
        for t in cands:
            say("    -> %s" % (t.get("url") or "")[:100])
            try:
                info = await probe_h5(cdp, t, probe_src, not args.no_login)
                probes.append((t.get("url") or "", info))
                say("       bridge=%s wx=%s login=%s"
                    % (info.get("bridgeInvoke"), info.get("hasWx"), info.get("login")))
            except Exception as exc:  # noqa: BLE001
                say("       失败: %s" % exc)
                probes.append((t.get("url") or "", {"raw": str(exc)[:200]}))

    if dialer:
        dialer.stop()
    sup.stop()

    path = write(args, dialer, targets, probes, "ok")
    say("[=] 报告：%s" % path)
    return 0


def write(args, dialer, targets, probes, reason):
    md = render_report(dialer, targets, probes, reason)
    with open(args.report, "w", encoding="utf-8") as f:
        f.write(md)
    raw = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dialer": (dialer.hits if dialer else None),
        "dialer_error": (dialer.error if dialer else None),
        "targets": targets,
        "h5": [{"url": u, "info": i} for u, i in probes],
        "reason": reason,
    }
    with open(os.path.splitext(args.report)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    return args.report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=300, help="等通道的最长秒数")
    ap.add_argument("--report", default=os.path.join(HERE, "probe_report.md"))
    ap.add_argument("--dialer-js", default=os.path.join(HERE, "probe_dialer.js"))
    ap.add_argument("--probe-js", default=os.path.join(HERE, "h5_probe.js"))
    ap.add_argument("--no-dialer", action="store_true")
    ap.add_argument("--no-login", action="store_true", help="不调用 invoke('login')")
    ap.add_argument("--limit", type=int, default=6, help="最多体检几个标签页")
    ap.add_argument("--wmpf-dir", default=ck.DEFAULT_WMPF)
    ap.add_argument("--node", default=ck.DEFAULT_NODE)
    args = ap.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
