#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
get_openid.py — 一条命令把「后台触发 OAuth → 取号」跑完

它自己做的事：
  1. 确保代理在跑，并检测 9421 通道（有小程序连着才算通）
  2. 挑一个合适的标签页（优先 teachermate 的页，其次任意非 liteapp 的 H5）
  3. 按「代价从低到高」依次尝试触发跳转，每步都盯 15s：
       S0  当前页 URL 里已经带 openid      -> 直接收货
       S1  页内 location.href = 授权链接    （渲染器发起，最接近真实点击）
       S2  页内 WeixinJSBridge.invoke('openUrlWithExtra', {url})
       S3  CDP Page.navigate                （浏览器侧发起，最后手段）
  4. 全程监听所有 target 的 URL 与 frameNavigated，一旦出现
     student/sign?openid=<32hex> 就落盘
  5. --watch 模式下常驻：通道一出现就自动干活，成功后按 --cooldown 冷却

为什么要「每步只试一次」：flue 里有
  wechat_redirect limit reached, cancelling navigation
和 A8KeyRateLimiter（duplicate / cache），连续重试会被取消或命中缓存。
所以这里刻意不做循环重试，失败就等冷却。

用法：
  python get_openid.py                       # 跑一次
  python get_openid.py --watch               # 常驻，通道一有就取号
  python get_openid.py --url "<授权链接>"     # 换链接
  python get_openid.py --out openid.txt
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

import channel_keeper as ck        # noqa: E402
import cdp_sign_agent as base      # noqa: E402

DEFAULT_URL = (
    "https://open.weixin.qq.com/connect/oauth2/authorize"
    "?appid=wxa153455f3ef1d9f9"
    "&redirect_uri=https%3A%2F%2Fv18.teachermate.cn%2Fapi%2Fv1%2Fwechat%2Fr%3Fm%3Ds_signin"
    "&response_type=code&scope=snsapi_base&state=&connect_redirect=1#wechat_redirect"
)

OID_RE = re.compile(r"[?&]openid=([0-9a-fA-F]{32})")
CODE_RE = re.compile(r"[?&]code=([0-9A-Za-z_\-]{8,})")
EXCLUDE = ("liteapp.weixin.qq.com", "servicewechat.com", "wxa.wxs.qq.com",
           "weixin://resourceid", "about:", "devtools://")

API = "https://www.teachermate.com.cn/wechat-api/v1/class-attendance"


def current_oid(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readline().strip()
    except OSError:
        return ""


def oid_state(oid):
    """401 = 已死（假号/被顶掉）；200 = 未知但至少没被拒；-1 = 网络错

    注意：200 不能证明号可用（过期/无签到都 200），只有 401 是明确信号。
    """
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(API + "/student/active_signs",
                                     headers={"openId": oid})
        with urllib.request.urlopen(req, timeout=12) as r:
            return "ok" if r.status == 200 else str(r.status)
    except urllib.error.HTTPError as e:
        return "ok" if e.code == 200 else str(e.code)
    except Exception:  # noqa: BLE001
        return "neterr"


async def oid_state_async(oid):
    """oid_state 的异步版。

    oid_state 是同步 HTTP（timeout=12）。直接在事件循环里调用会卡死整个
    程序——期间 CDP 既收不到事件也发不出命令，取号的观察窗口等于白等。
    所以丢到线程池里跑。
    """
    return await asyncio.to_thread(oid_state, oid)


def ts():
    return time.strftime("%H:%M:%S")


class Log:
    def __init__(self, path=""):
        self.path = path
        self.f = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, msg):
        line = "[%s] %s" % (ts(), msg)
        print(line, flush=True)
        if self.f:
            self.f.write(line + "\n")
            self.f.flush()


def pick_tab(targets, prefer_sign=False):
    """prefer_sign=True 时优先选签到页本身（用户要求"用签到页取"）"""
    pages = [t for t in targets if t.get("type") in ("page", "webview")]
    tm = [t for t in pages if "teachermate" in (t.get("url") or "")]
    if prefer_sign and tm:
        tm.sort(key=lambda t: len(t.get("url") or ""), reverse=True)
        return tm[0], "sign-page(forced)"
    if tm:
        tm.sort(key=lambda t: len(t.get("url") or ""), reverse=True)
        return tm[0], "teachermate"
    h5 = [t for t in pages
          if (t.get("url") or "").startswith("http")
          and not any(x in (t.get("url") or "") for x in EXCLUDE)]
    if h5:
        h5.sort(key=lambda t: len(t.get("url") or ""), reverse=True)
        return h5[0], "plain-h5"
    # 不能退回 weixin://resourceid 这类内部页：EXCLUDE 已点名排除，
    # 它们导航不到授权链接，选中等于白跑一轮三个策略（实测 54s 后 all-failed）。
    # 宁可返回 None 让调用方去新建页面，也不要假装选中了可用标签页。
    return None, "none"


def scan_targets_for_oid(targets, exclude=""):
    for t in targets:
        m = OID_RE.search(t.get("url") or "")
        if m and m.group(1) != exclude:
            return m.group(1), t.get("url")
    return None, None


async def create_tab(cdp, url, log):
    """没有可导航页面时，用 CDP 新建一页直接放授权链接。

    全新的微信账号往往只有 weixin:// 内部页（pick_tab 会拒收），
    这时必须自己开一页，否则取号无从下手。
    """
    try:
        res = await cdp.send("Target.createTarget", {"url": url}, timeout=15)
        tid = (res or {}).get("targetId")
        if not tid:
            log("  新建页面失败：无 targetId")
            return None, "none"
        log("  已新建页面用于取号：%s" % str(tid)[:12])
        return {"targetId": tid, "url": url}, "created"
    except Exception as exc:  # noqa: BLE001
        log("  新建页面失败：%s" % str(exc)[:120])
        return None, "none"


async def one_attempt(args, log):
    """返回 (openid or None, 说明)"""
    # websockets>=13：connect() 返回的对象本身就是异步上下文管理器，
    # 绝不能 await（await 得到的是协议对象，不支持 async with）
    async with base.connect() as sock:
        cdp = base.Cdp(sock)
        try:
            await cdp.send("Target.setDiscoverTargets", {"discover": True}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            infos = (await cdp.send("Target.getTargets", {}, timeout=10)).get("targetInfos", [])
        except (TimeoutError, asyncio.TimeoutError):
            return None, "channel-down"

        # S0：可能已经落在签到页上了。注意要**跳过已被顶掉的号**（401），
        # 否则会把刚取到的新号又覆盖回旧的死号。
        # --from-sign 时跳过 S0：用户要求"用签到页去换新号"，直接走触发。
        if not args.from_sign:
            for t in infos:
                m = OID_RE.search(t.get("url") or "")
                if not m:
                    continue
                cand = m.group(1)
                if await oid_state_async(cand) == "401":
                    log("S0 候选 %s… 已 401（被顶掉），跳过" % cand[:8])
                    continue
                log("S0 命中：URL 里已有 openid（%s）" % (t.get("url") or "")[:80])
                return cand, "S0-already"

        tab, why = pick_tab(infos, prefer_sign=args.from_sign)
        created = False
        if not tab:
            tab, why = await create_tab(cdp, args.url, log)
            created = bool(tab)
        if not tab:
            log("  没有可导航的页面，也没能新建。请在微信里打开「微助教」小程序后重试")
            return None, "no-tab"
        log("选中标签页(%s)：%s" % (why, (tab.get("url") or "")[:90]))

        await base.attach(cdp, tab["targetId"])
        for dom in ("Page.enable", "Runtime.enable"):
            try:
                await cdp.send(dom, {}, timeout=8)
            except Exception:  # noqa: BLE001
                pass

        strategies = []
        url_lit = json.dumps(args.url)

        # S1 页内 location.href（渲染器发起，最像真实点击）
        strategies.append((
            "S1-location.href",
            lambda: base.evaluate(cdp, "location.href = %s; 'ok'" % url_lit, await_promise=False)
        ))
        # S2 页内 JS API（openUrlWithExtra 存在与否都试一次，失败会抛）
        strategies.append((
            "S2-openUrlWithExtra",
            lambda: base.evaluate(
                cdp,
                "(function(){try{"
                "if(!window.WeixinJSBridge||!window.WeixinJSBridge.invoke) return 'no-bridge';"
                "window.WeixinJSBridge.invoke('openUrlWithExtra',{url:%s},function(r){console.log('OWE',JSON.stringify(r))});"
                "return 'invoked';}catch(e){return 'err:'+e}})()" % url_lit,
                await_promise=False)
        ))
        # S3 CDP Page.navigate（浏览器侧发起）
        strategies.append((
            "S3-Page.navigate",
            lambda: cdp.send("Page.navigate", {"url": args.url}, timeout=20)
        ))

        # 实测结论：S3 会走 a8key 节流器 -> Redirect -> 原生 OAuth，是最可靠的一条，
        # 提到前面；S1/S2 作为兜底（某些页面上下文里 CDP 导航会被 abort）。
        strategies.sort(key=lambda kv: {"S3-Page.navigate": 0,
                                        "S1-location.href": 1,
                                        "S2-openUrlWithExtra": 2}.get(kv[0], 9))

        if created:
            # createTarget 建页时已经带着授权链接发起导航，不必再触发，
            # 直接进观察窗口等它跳到带 openid 的地址。
            strategies = [("S4-createTarget", lambda: asyncio.sleep(0))]

        for name, act in strategies:
            log("尝试 %s ..." % name)
            try:
                res = await act()
                if isinstance(res, str) and res not in ("ok", "invoked"):
                    log("  %s -> %s（跳过）" % (name, res))
                    continue
                log("  %s -> 已触发，观察 %ss" % (name, args.observe))
            except Exception as exc:  # noqa: BLE001
                log("  %s 触发失败: %s" % (name, str(exc)[:120]))
                continue

            # 观察窗口：既看事件，也轮询 target 列表（两种信号互相兜底）
            # 先扫一遍再等：导航刚触发时 openid 可能已经落下了，
            # 原来先 sleep(1.5) 再扫，等于每轮都白白多等 1.5s。
            old = current_oid(args.out) if args.from_sign else ""
            deadline = time.time() + args.observe
            while True:
                for m in cdp.events:
                    u = ""
                    if m.get("method") == "Page.frameNavigated":
                        u = (m.get("params", {}).get("frame", {}) or {}).get("url", "")
                    mm = OID_RE.search(u or "")
                    if mm and mm.group(1) != old \
                            and await oid_state_async(mm.group(1)) != "401":
                        log("  [事件] 抓到 openid: %s" % (u or "")[:100])
                        return mm.group(1), name
                cdp.events.clear()
                try:
                    now = (await cdp.send("Target.getTargets", {}, timeout=8)).get("targetInfos", [])
                except Exception:  # noqa: BLE001
                    now = []
                oid, where = scan_targets_for_oid(now, exclude=old)
                if oid and await oid_state_async(oid) == "401":
                    # 残留的旧签到页会带着已被顶掉的死号，必须跳过
                    log("  [轮询] %s… 已 401（残留旧页），跳过" % oid[:8])
                    oid, where = None, None
                if oid:
                    log("  [轮询] 抓到 openid: %s" % (where or "")[:100])
                    return oid, name
                if time.time() >= deadline:
                    break
                await asyncio.sleep(1.0)
            log("  %s 观察期内未出现 openid" % name)

        return None, "all-failed"


async def _hold():
    """退出会连带关闭代理；9421 一断，运行时会直接结束小程序。
    --hold 用它把进程（和代理）钉住，保住小程序。"""
    while True:
        await asyncio.sleep(3600)


def save(path, oid, strategy, url):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(oid + "\n")
        f.write("# strategy=%s\n# at=%s\n# url=%s\n"
                % (strategy, time.strftime("%Y-%m-%d %H:%M:%S"), url))
    os.replace(tmp, path)


async def run(args, log):
    sup = ck.ProxySupervisor(args.wmpf_dir, args.node)
    if not sup.ensure():
        log("[x] 代理起不来")
        return 3

    last_try = 0.0
    attempt = 0
    last_down_msg = 0.0
    last_check_msg = 0.0
    try:
        while True:
            attempt += 1
            state, targets, why = await ck.probe_channel()
            if state != ck.CHANNEL_UP:
                # 每 60s 提醒一次，避免刷屏
                if time.time() - last_down_msg > 60:
                    last_down_msg = time.time()
                    log("通道未就绪（%s/%s）。" % (state, why))
                    log("  → 代理已在跑，但小程序是在代理之前启动的，OnLoadStart 没被 patch。")
                    log("  → 请在小程序里**退回首页再进去一次**（或从「最近使用」重新打开）。")
                if not args.watch:
                    return 2
                await asyncio.sleep(args.poll)
                continue

            # 只在必要时续号：实测「新授权会把旧号顶成 401」，所以不能定时乱续，
            # 否则会把正在用的号弄死。只有明确 401（或没有号）才动手。
            cur = current_oid(args.out)
            if cur and not args.force:
                st = await oid_state_async(cur)
                if st != "401":
                    if time.time() - last_check_msg > 120:
                        last_check_msg = time.time()
                        log("当前号 %s… 状态=%s（未被拒），不重复授权以免顶掉" % (cur[:8], st))
                    if not args.watch:
                        return 0
                    await asyncio.sleep(args.poll)
                    continue
                log("当前号 %s… 已 401，开始续号" % cur[:8])

            # 冷却：避免撞 a8key 重定向限流
            if time.time() - last_try < args.cooldown:
                wait = int(args.cooldown - (time.time() - last_try))
                log("冷却中，%ds 后再试" % wait)
                await asyncio.sleep(min(wait, args.poll))
                continue

            last_try = time.time()
            log("=== 第 %d 次尝试 ===" % attempt)
            # 常驻进程绝不能因为单轮异常而死掉：一死代理跟着死，用户就得重进小程序
            try:
                oid, how = await one_attempt(args, log)
            except (OSError, ConnectionRefusedError) as exc:
                log("连接 62000 失败（%s），代理可能已退出" % exc)
                oid, how = None, "proxy-down"
            except Exception as exc:  # noqa: BLE001
                log("本轮异常：%s: %s" % (type(exc).__name__, str(exc)[:150]))
                oid, how = None, "exception"
            if oid:
                save(args.out, oid, how, args.url)
                log("[+] 取号成功（%s）：%s" % (how, oid))
                log("    已写入 %s" % args.out)
                if not args.watch:
                    # 单次模式：退出会连带关闭代理 -> 9421 断开 -> 小程序被运行时关掉
                    if args.hold:
                        log("    --hold：保持常驻，代理不断，小程序不会被关。")
                        await _hold()
                    log("    （若不想小程序被关，下次加 --hold 保持代理）")
                    return 0
                if args.exit_after and args.force_exit:
                    log("    --force-exit：退出。注意 9421 会断开，小程序很可能被关闭。")
                    return 0
                if args.exit_after:
                    log("    ⚠ 不自动退出：退出会关闭代理 → 9421 断开 → 小程序被运行时关闭。")
                    log("    确认要退请加 --force-exit。本进程继续常驻，保持通道。")
                else:
                    log("    进入冷却，%ds 后再检查是否需要续号" % int(args.cooldown))
            else:
                log("[-] 本轮失败（%s）" % how)

            if not args.watch:
                return 4 if not oid else 0
            await asyncio.sleep(args.poll)
    finally:
        sup.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default=os.path.join(HERE, "openid.txt"))
    ap.add_argument("--watch", action="store_true", help="常驻：通道一就绪就取号")
    ap.add_argument("--poll", type=float, default=10.0, help="watch 模式下探测间隔")
    ap.add_argument("--cooldown", type=float, default=600.0, help="两次触发的冷却秒数（防限流）")
    ap.add_argument("--force", action="store_true", help="即使当前号未被 401 也强制续号（会顶掉旧号）")
    ap.add_argument("--from-sign", action="store_true",
                    help="用签到页本身当靶子（跳过 S0，直接导航去换新号）")
    ap.add_argument("--exit-after", action="store_true", help="成功一次即退出（配合 --watch 当一次性任务）")
    ap.add_argument("--force-exit", action="store_true", help="允许退出（会断开 9421，小程序很可能被关掉）")
    ap.add_argument("--hold", action="store_true", help="取到号后保持常驻，保住代理与小程序")
    ap.add_argument("--observe", type=float, default=15.0, help="每次触发后观察多少秒")
    ap.add_argument("--log", default=os.path.join(HERE, "get_openid.log"))
    ap.add_argument("--wmpf-dir", default=ck.DEFAULT_WMPF)
    ap.add_argument("--node", default=ck.DEFAULT_NODE)
    args = ap.parse_args()
    log = Log(args.log)
    try:
        sys.exit(asyncio.run(run(args, log)))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
