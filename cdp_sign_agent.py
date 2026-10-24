#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
cdp_sign_agent.py — WMPF 内置浏览器「页内签到」宿主驱动

作用：唯一持有 ws://127.0.0.1:62000 的 CDP 客户端，完成
      发现(签到页) -> 附着(flatten session) -> 注入(page_agent.js) -> 轮询状态。

前置（顺序不能反）：
  1. cd WMPFDebugger && node node_modules/tsx/dist/cli.mjs src/index.ts
     看到 "[frida] you can now open any miniapps"
  2. 在微信里打开任意一个小程序（点燃通道），保持不关
  3. 点微助教【签到】，让签到页的 H5 落地
  4. python cdp_sign_agent.py

返回码：
  0  正常结束（--once 成功 / 收到 --for 轮数）
  2  超时：通道通但没有匹配的签到页
  3  连不上 62000（代理没跑）
  4  通道未建立（小程序没开 / scene 不在白名单）
  5  注入后读不到 __wzSignAgent（页面上下文异常）

注意：同一时刻只能有一个 CDP 客户端。跑本脚本前先关掉 DevTools 页面。
"""
import argparse
import asyncio
import json
import os
import re
import sys

import websockets

CDP_URL = "ws://127.0.0.1:62000"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_AGENT = os.path.join(HERE, "page_agent.js")


class Cdp:
    """极简 CDP 客户端：id 配对 + sessionId 路由 + 事件缓存。"""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self.session_id = None
        self.events = []

    async def send(self, method, params=None, session_id=None, timeout=20):
        self._id += 1
        msg_id = self._id
        payload = {"id": msg_id, "method": method, "params": params or {}}
        sid = session_id if session_id is not None else self.session_id
        if sid:
            payload["sessionId"] = sid
        await self.ws.send(json.dumps(payload))
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("%s 无响应" % method)
            raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})
            # 不是本次应答：当作事件留着（session 事件也走这条信道）
            self.events.append(msg)


def connect():
    return websockets.connect(CDP_URL, max_size=None, open_timeout=10)


async def evaluate(cdp, expr, timeout=25, await_promise=True):
    res = await cdp.send("Runtime.evaluate", {
        "expression": expr,
        "returnByValue": True,
        "awaitPromise": await_promise,
        "userGesture": True,
    }, timeout=timeout)
    if res.get("exceptionDetails"):
        det = res["exceptionDetails"]
        raise RuntimeError("页面内异常: %s" % (det.get("text") or det))
    return res.get("result", {}).get("value")


async def pick_target(cdp, pattern, timeout=60.0):
    """轮询 getTargets，直到出现匹配的 page target。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_n = -1
    while loop.time() < deadline:
        try:
            res = await cdp.send("Target.getTargets", {}, timeout=12)
        except TimeoutError:
            # 9421 上没有下游客户端 —— 通道未建立
            return None, "no-downstream"
        infos = res.get("targetInfos", [])
        if len(infos) != last_n:
            last_n = len(infos)
            print("[*] target 数: %d" % len(infos))
        pages = [t for t in infos if t.get("type") in ("page", "webview")]
        hits = [t for t in pages if pattern.search(t.get("url", ""))]
        if hits:
            # 优先非 attached 的、URL 更长的（带 query 的那个才是真签到页）
            hits.sort(key=lambda t: len(t.get("url", "")), reverse=True)
            return hits[0], None
        await asyncio.sleep(1.5)
    return None, "timeout"


async def attach(cdp, target_id):
    res = await cdp.send("Target.attachToTarget",
                         {"targetId": target_id, "flatten": True}, timeout=20)
    sid = res.get("sessionId")
    if not sid:
        raise RuntimeError("attachToTarget 未返回 sessionId（上游可能不支持 flatten）")
    cdp.session_id = sid
    return sid


async def inject(cdp, agent_path, persist=False):
    with open(agent_path, "r", encoding="utf-8") as f:
        src = f.read()
    # 主世界执行：IIFE 返回字符串，便于确认注入结果
    out = await evaluate(cdp, src, timeout=30)
    ok = await evaluate(cdp, "window.__wzSignAgent && window.__wzSignAgent.version")
    if not ok:
        return None
    if persist:
        try:
            await cdp.send("Page.addScriptToEvaluateOnNewDocument",
                           {"source": src}, timeout=15)
            print("[*] 已注册 addScriptToEvaluateOnNewDocument（跨刷新存活）")
        except Exception as exc:  # noqa: BLE001
            print("[!] addScriptToEvaluateOnNewDocument 不可用: %s" % exc)
    return out


async def run(args):
    pattern = re.compile(args.match)
    try:
        ws = await connect()
    except (OSError, ConnectionRefusedError) as exc:
        print("[x] 连接 %s 失败（代理没在跑？）: %s" % (CDP_URL, exc))
        return 3

    async with ws as sock:
        cdp = Cdp(sock)
        try:
            await cdp.send("Target.setDiscoverTargets", {"discover": True}, timeout=12)
        except Exception:  # noqa: BLE001
            pass

        print("[*] 等待匹配 /%s/ 的页面 ..." % args.match)
        tgt, why = await pick_target(cdp, pattern, timeout=args.timeout)
        if tgt is None:
            if why == "no-downstream":
                print("[x] 62000 通了但小程序侧无响应 —— 先开一个小程序点燃通道")
                return 4
            print("[2] 超时：通道正常，但没有签到页（去点一下微助教【签到】）")
            return 2

        url = tgt.get("url", "")
        print("[+] 命中 target %s" % tgt.get("targetId"))
        print("    url = %s" % url[:140])

        await attach(cdp, tgt["targetId"])
        print("[+] session = %s" % cdp.session_id)

        for dom in ("Page.enable", "Runtime.enable"):
            try:
                await cdp.send(dom, {}, timeout=10)
            except Exception as exc:  # noqa: BLE001
                print("[!] %s 失败: %s" % (dom, exc))

        # 会话隔离自检：读到的必须是签到页，而不是小程序页
        href = await evaluate(cdp, "location.href")
        if href != url and (not href or "teachermate" not in str(href)):
            print("[!] 会话隔离可能失效：location.href = %s" % href)
        else:
            print("[+] 会话隔离 OK: %s" % str(href)[:100])

        if args.observe_only:
            print("[*] observe-only：只注入钩子，不提交签到")
            src = "window.__WZ_CONFIG=%s;" % json.dumps({"submit": False})
            await evaluate(cdp, src, await_promise=False)
        elif args.pos:
            lat, lon = [float(x) for x in args.pos.split(",")]
            src = "window.__WZ_CONFIG=%s;" % json.dumps({"pos": {"lat": lat, "lon": lon}})
            await evaluate(cdp, src, await_promise=False)

        out = await inject(cdp, args.agent, persist=args.persist)
        if out is None:
            print("[5] 注入后读不到 __wzSignAgent（页面上下文异常/被隔离）")
            return 5
        print("[+] agent 就绪: %s" % out)

        if args.once:
            await asyncio.sleep(1.0)
            st = await evaluate(cdp, "window.__wzSignAgent.api.status()")
            print(json.dumps(st, ensure_ascii=False, indent=2))
            return 0

        # 持续轮询状态，只在变化时打印，避免刷屏
        last = None
        loops = 0
        print("[*] 持续观测中（Ctrl+C 结束，--for 限制轮数）")
        while True:
            await asyncio.sleep(args.interval)
            loops += 1
            try:
                st = await evaluate(cdp, "window.__wzSignAgent.api.status()", timeout=15)
            except Exception as exc:  # noqa: BLE001
                print("[!] 读状态失败: %s" % exc)
                if args.for_ and loops >= args.for_:
                    return 0
                continue
            sig = (st.get("auth"), st.get("attempt"), st.get("lastPoll"),
                   tuple(st.get("signed") or []))
            if sig != last:
                last = sig
                lp = st.get("lastPoll") or {}
                print("[%s] auth=%s attempt=%s poll=%s/%s signed=%d fails=%s"
                      % (time_str(), st.get("auth"), st.get("attempt"),
                         lp.get("status"), lp.get("count"),
                         len(st.get("signed") or []), st.get("fails")))
                for item in (st.get("log") or [])[-3:]:
                    print("      - %s" % item.get("m"))
                if st.get("lastSign"):
                    print("      lastSign=%s" % json.dumps(st["lastSign"], ensure_ascii=False)[:200])
            if args.for_ and loops >= args.for_:
                print("[+] 达到 --for 轮数，退出")
                return 0


def time_str():
    import time
    return time.strftime("%H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", default=r"teachermate\.cn/.*student/sign|student/sign\?openid",
                    help="定位签到页 target 的 URL 正则")
    ap.add_argument("--agent", default=DEFAULT_AGENT, help="page_agent.js 路径")
    ap.add_argument("--timeout", type=float, default=60, help="等待签到页出现的秒数")
    ap.add_argument("--interval", type=float, default=2.0, help="状态轮询间隔")
    ap.add_argument("--once", action="store_true", help="注入后打印一次状态即退出")
    ap.add_argument("--for", dest="for_", type=int, default=0, help="观测 N 轮后退出")
    ap.add_argument("--observe-only", action="store_true", help="只抓不签（首次联调用）")
    ap.add_argument("--persist", action="store_true", help="注册跨刷新存活脚本（待验证）")
    ap.add_argument("--pos", default="", help="GPS 签到坐标 lat,lon（沿用你自己的值）")
    args = ap.parse_args()

    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\n[*] 中断退出")
        sys.exit(0)


if __name__ == "__main__":
    main()
