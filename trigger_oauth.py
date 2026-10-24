#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
trigger_oauth.py — 重测「能否从客户端触发微信 H5 授权」

为什么重测：上一轮的结论「三条路封死」是在 liteapp.weixin.qq.com（小程序容器页）
上测出来的。那是 AppIndex 的宿主页面，不是服务号 H5，导航策略本就不同。
EXTENSION.md 给了 attachToTarget，我们现在可以精确附着到「某个具体 H5 标签页」，
在正确的上下文里重做这个实验。

三种发起方式，机理不同，必须分开测：
  href  页面内 location.href = <authorize>   （渲染器发起，走正常导航管线）
  open  页面内 window.open(<authorize>)       （可能被原生层接管成新窗口）
  nav   CDP Page.navigate                     （浏览器侧发起，上轮用的是这个）

判定（--test-h5 模式）：
  INTERCEPTED   URL 留在 open.weixin.qq.com 且页面不是空壳 → 原生层接管了，好
  NATIVE_WINDOW 出现了新 target（原生弹的新窗口）        → 最好
  BLOCKED       落到 mp.weixin.qq.com/mp/waerrpage       → 白名单拦截
  STUB          页面是那个 ~1KB 的 UA 分流空壳            → 没被接管
  UNKNOWN       其它

安全默认：--test-h5 只挑「非 liteapp / 非 servicewechat / 非签到页」的普通 H5 标签，
测坏了也不影响签到。要作用在签到页上必须显式 --allow-sign-page。

用法：
  python trigger_oauth.py --list
  python trigger_oauth.py --test-h5 --variant href
  python trigger_oauth.py --test-h5 --variant nav
"""
import argparse
import asyncio
import json
import re
import sys

import cdp_sign_agent as base

AUTH = ("https://open.weixin.qq.com/connect/oauth2/authorize"
        "?appid={appid}&redirect_uri={redirect}&response_type=code"
        "&scope={scope}&state=1#wechat_redirect")

EXCLUDE = ("liteapp.weixin.qq.com", "servicewechat.com", "weixin://",
           "wxa.wxs.qq.com", "mp.weixin.qq.com/mp/waerrpage")


def build_url(args):
    if args.authorize_url:
        return args.authorize_url
    redirect = args.redirect.replace(":", "%3A").replace("/", "%2F")
    return AUTH.format(appid=args.appid, redirect=redirect, scope=args.scope)


def pick(targets, args):
    pages = [t for t in targets if t.get("type") in ("page", "webview")]
    if args.target:
        for t in pages:
            if args.target in t.get("targetId", "") or args.target in t.get("url", ""):
                return t
        return None
    if args.test_h5:
        cand = [t for t in pages
                if not any(x in t.get("url", "") for x in EXCLUDE)
                and "student/sign" not in t.get("url", "")]
        cand.sort(key=lambda t: len(t.get("url", "")), reverse=True)
        return cand[0] if cand else None
    sign = [t for t in pages if "student/sign" in t.get("url", "")]
    return sign[0] if sign else None


def classify(urls, body, title, new_targets):
    if new_targets:
        return "NATIVE_WINDOW", "出现了新 target: %s" % new_targets[0][:90]
    last = urls[-1] if urls else ""
    if "waerrpage" in last or "jump2h5err" in last:
        return "BLOCKED", "落到错误页（业务域名白名单）"
    if "open.weixin.qq.com" in last:
        text = (body or "") + (title or "")
        if len(body or "") < 1500 and "micromessenger" in (body or "").lower():
            return "STUB", "UA 分流空壳（%d 字节），未被原生层接管" % len(body or "")
        if "授权" in text or "authorize" in text.lower() or "微信" in text:
            return "INTERCEPTED", "原生授权页已渲染"
        return "UNKNOWN", "停在 authorize 但页面特征不明（%d 字节）" % len(body or "")
    if last == "" :
        return "UNKNOWN", "拿不到 URL"
    return "UNKNOWN", "跳到了别处: %s" % last[:90]


async def drain(ws, seconds):
    """收集这段时间内所有上行消息（会话事件也在其中）。"""
    got = []
    loop = asyncio.get_event_loop()
    end = loop.time() + seconds
    while True:
        left = end - loop.time()
        if left <= 0:
            return got
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=left)
        except asyncio.TimeoutError:
            return got
        try:
            got.append(json.loads(raw))
        except json.JSONDecodeError:
            pass


async def run(args):
    url = build_url(args)
    try:
        ws = base.connect()
    except (OSError, ConnectionRefusedError) as exc:
        print("[x] 连不上 62000（代理没跑？）: %s" % exc)
        return 3

    async with ws as sock:
        cdp = base.Cdp(sock)
        try:
            await cdp.send("Target.setDiscoverTargets", {"discover": True}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            infos = (await cdp.send("Target.getTargets", {}, timeout=12)).get("targetInfos", [])
        except TimeoutError:
            print("[4] 62000 通了但小程序侧无响应 —— 先开一个小程序点燃通道")
            return 4

        if args.list:
            print("共 %d 个 target:" % len(infos))
            for t in infos:
                print("  [%-8s] %s" % (t.get("type"), (t.get("url") or "")[:120]))
            return 0

        tgt = pick(infos, args)
        if not tgt:
            print("[2] 没找到合适的 H5 标签页（拿 --list 看看有哪些）")
            return 2

        is_sign = "student/sign" in tgt.get("url", "")
        print("[+] 目标: %s" % tgt.get("url", "")[:120])
        if is_sign and not args.allow_sign_page:
            print("[!] 这是签到页，导航会毁掉当前授权。确认无误请加 --allow-sign-page")
            return 2

        before_ids = {t.get("targetId") for t in infos}
        await base.attach(cdp, tgt["targetId"])
        for dom in ("Page.enable", "Runtime.enable"):
            try:
                await cdp.send(dom, {}, timeout=8)
            except Exception:  # noqa: BLE001
                pass

        start_url = await base.evaluate(cdp, "location.href")
        print("[*] 起始 URL: %s" % str(start_url)[:110])
        print("[*] 变体=%s" % args.variant)
        print("[*] 授权 URL: %s" % url[:130])

        if args.variant == "nav":
            res = await cdp.send("Page.navigate", {"url": url}, timeout=25)
            print("[*] Page.navigate -> %s" % json.dumps(res, ensure_ascii=False))
        elif args.variant == "href":
            expr = "location.href = %s; 'ok'" % json.dumps(url)
            print("[*] evaluate -> %s" % await base.evaluate(cdp, expr, await_promise=False))
        else:
            expr = "window.open(%s, '_blank'); 'ok'" % json.dumps(url)
            print("[*] evaluate -> %s" % await base.evaluate(cdp, expr, await_promise=False))

        events = await drain(sock, args.wait)

        urls = [start_url]
        for m in events:
            if m.get("method") == "Page.frameNavigated":
                u = m.get("params", {}).get("frame", {}).get("url", "")
                if u:
                    urls.append(u)
                    print("    [nav] %s" % u[:120])
            elif m.get("method") == "Target.targetCreated":
                ti = m.get("params", {}).get("targetInfo", {})
                if ti.get("targetId") not in before_ids:
                    print("    [new-target] %s %s" % (ti.get("type"), (ti.get("url") or "")[:110]))

        new_targets = []
        try:
            after = (await cdp.send("Target.getTargets", {}, timeout=10)).get("targetInfos", [])
            new_targets = [t.get("url", "") for t in after if t.get("targetId") not in before_ids]
        except Exception:  # noqa: BLE001
            pass

        body = title = ""
        try:
            body = await base.evaluate(cdp, "document.body ? document.body.innerText.slice(0,800) : ''")
            title = await base.evaluate(cdp, "document.title")
            final = await base.evaluate(cdp, "location.href")
            urls.append(final)
        except Exception as exc:  # noqa: BLE001
            print("[!] 读页面失败（可能已被原生接管/销毁）: %s" % exc)

        verdict, why = classify(urls, body, title, new_targets)
        print("\n===== 判定: %s =====" % verdict)
        print("      %s" % why)
        print("      最终 URL: %s" % (urls[-1] if urls else "?"))
        if body:
            print("      body[:160]: %s" % body[:160].replace("\n", " "))
        print(VERDICT_HINT.get(verdict, ""))
        return 0


VERDICT_HINT = {
    "INTERCEPTED": "  -> 授权可被触发。把这条变体写进 page_agent 的续期逻辑（默认仍关，先观察一周）。",
    "NATIVE_WINDOW": "  -> 最佳结果：原生弹窗。需要监听 Target.targetCreated 抓新窗口再附着。",
    "BLOCKED": "  -> 该标签页有域名白名单限制。换一个普通 H5 标签再测，或走 JSSDK login 兜底。",
    "STUB": "  -> 仍未被接管：说明这条变体没走到原生拦截。换 variant 再试（href -> open -> nav）。",
    "UNKNOWN": "  -> 信息不足，看上面的 [nav] 序列判断。",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--test-h5", action="store_true", help="挑一个普通 H5 标签（安全默认）")
    ap.add_argument("--target", default="", help="targetId 或 URL 片段")
    ap.add_argument("--variant", choices=["href", "open", "nav"], default="href")
    ap.add_argument("--appid", default="wx0f2e5c9f0e2c4b7a")
    ap.add_argument("--redirect", default="https://app.teachermate.com.cn/wechat-pro-ssr/student/sign")
    ap.add_argument("--scope", default="snsapi_base")
    ap.add_argument("--authorize-url", default="", help="整条覆盖，忽略 appid/redirect")
    ap.add_argument("--wait", type=float, default=15)
    ap.add_argument("--allow-sign-page", action="store_true")
    args = ap.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
