#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run.py — 一键入口：代理常驻 + 通道守护 + 自动取号/续号

它做什么
  1. 确保代理在跑（已在跑就不再起，避免重复 frida attach / 端口冲突）
  2. 盯着 9421 通道：没有小程序连着时只提示，不反复折腾
  3. 通道可用时检查 openid：
       - 没有号        -> 取一次
       - 号已 401      -> 用签到页换一次新的
       - 号还好        -> 什么都不做（**定时乱续会把旧号顶掉**）
  4. 全程常驻。退出时**不关闭代理**——代理一断，微信会立刻把小程序关掉。

为什么退出不关代理
  小程序是以调试态拉起的（scene=1101 / remoteDebugMode=1），9421 那条 WebSocket
  就是它的生命线。关代理 = 杀小程序。所以这里默认只退出本进程，代理留着。

用法
  python run.py                # 常驻
  python run.py --check        # 只看状态就退出
  python run.py --once         # 跑一轮判断（不常驻）
  python run.py --url "<授权链接>"
"""
import argparse
import asyncio
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

import channel_keeper as ck          # noqa: E402
import get_openid as g               # noqa: E402


def ts():
    return time.strftime("%H:%M:%S")


class Log:
    def __init__(self, path=""):
        self.f = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, msg):
        line = "[%s] %s" % (ts(), msg)
        print(line, flush=True)
        if self.f:
            self.f.write(line + "\n")
            self.f.flush()

    def close(self):
        if self.f:
            self.f.close()


BANNER = """
============================================================
 微助教自动签到 · 一键启动
------------------------------------------------------------
 签到：signer/sign_pc.py 常驻，5 秒轮询，命中即提交（幂等）
 续号：只在「没有号」或「号已 401」时才换，不会乱续
       （新授权会把旧号顶掉，定时续等于不停废号）
 代理：自动检查 / 拉起（已在跑就复用，不会重复注入）
 通道：需要一个微信小程序连着。若提示「通道未就绪」，在微信里
       打开任意小程序一次；连上后可缩到后台不管。
 退出：Ctrl+C。退出不会关闭代理，小程序不受影响。
============================================================
"""


def has_sign_tab(targets):
    return any("teachermate" in (t.get("url") or "")
               and t.get("type") in ("page", "webview") for t in targets)


async def run(args, log, force_check=None):
    sup = ck.ProxySupervisor(args.wmpf_dir, args.node)

    if not port_open_or_spawn(sup, log):
        return 3

    if args.check:
        state, targets, why = await ck.probe_channel()
        cur = g.current_oid(args.out)
        st = g.oid_state(cur) if cur else "none"
        print("proxy   : up (%s)" % ("own" if sup.proc else "external"))
        print("channel : %s (%s), targets=%d" % (state, why, len(targets)))
        print("openid  : %s  state=%s" % (cur or "(无)", st))
        return 0 if state == ck.CHANNEL_UP else 2

    last_down = 0.0
    last_beat = 0.0
    last_try = 0.0
    ticks = 0
    prev_state = None
    signer = None
    if args.with_sign:
        signer = start_signer(args, log)

    while True:
        ticks += 1

        # 签到侧监督：它只在 401 时才退出（code=2），此时立刻续号
        force = False
        if force_check is not None and force_check():
            force = True
            log("签到侧报告号已失效，立刻续号（跳过冷却）")
        if signer is not None and signer.poll() is not None:
            rc = signer.returncode
            force = (rc == 2)
            log("签到侧退出 code=%s%s" % (rc, "（发现 401，准备续号）" if force else ""))
            signer = None

        try:
            # 通道不通时探测会吃满 timeout（mux 不响应），此时用短超时快速判定
            # 「还没连上」；通道通时保持长超时，避免误判成断连。
            state, targets, why = await ck.probe_channel(
                timeout=6.0 if prev_state == ck.CHANNEL_UP else 2.0)
        except Exception as exc:  # noqa: BLE001
            log("探测异常：%s" % exc)
            state, targets, why = ck.CHANNEL_DOWN, [], "probe-error"
        prev_state = state

        if state == ck.PROXY_DOWN:
            log("代理没在监听（%s），尝试拉起..." % why)
            if not port_open_or_spawn(sup, log):
                log("代理起不来，30s 后重试")
                await asyncio.sleep(30)
            # 拉起成功就立刻重探。30s 是「起不来」时的退避，不是成功后的固定间隔；
            # 写在分支外面会让每次冷启动都白等 30s（实测到「等待小程序连接」要 37s）。
            continue

        if state == ck.CHANNEL_DOWN:
            if time.time() - last_down > 60:
                last_down = time.time()
                log("通道未就绪（%s）——微信里还没有小程序连上 9421。" % why)
                log("  请在微信里打开任意小程序一次；连上后可以缩到后台不管。")
                if signer is None and args.with_sign:
                    log("  签到侧不受通道影响，照常工作。")
            # 签到不依赖通道，通道断时也要保证它在跑
            if signer is None and args.with_sign and not force:
                signer = start_signer(args, log)
            if args.once:
                return 2
            # 通道不通时轮询要勤快：10s 间隔会让「小程序连上」到「面板切换」
            # 白等最多一个周期（实测 8s）。断连期间用短间隔，连上后回到 poll。
            await asyncio.sleep(min(args.poll, 2.0))
            continue

        # 通道可用
        cur = g.current_oid(args.out)
        # oid_state 是同步 HTTP，直接调用会卡住事件循环（挡住 CDP 与面板刷新）
        st = (await g.oid_state_async(cur)) if cur else "none"

        if cur and st != "401" and not force:
            if signer is None and args.with_sign:
                signer = start_signer(args, log)
            if time.time() - last_beat > 300:
                last_beat = time.time()
                log("正常值守：号 %s… 状态=%s，通道 %d 个 target" % (cur[:8], st, len(targets)))
            if args.once:
                return 0
            await asyncio.sleep(args.poll)
            continue

        # 需要（重新）取号
        log("需要取号：%s" % ("没有号" if not cur else "当前号已 401"))
        if time.time() - last_try < args.cooldown and not force:
            left = int(args.cooldown - (time.time() - last_try))
            log("  冷却中（防 a8key 重定向限流），%ds 后再试" % left)
            if args.once:
                return 4
            await asyncio.sleep(min(left, args.poll))
            continue

        last_try = time.time()
        from_sign = has_sign_tab(targets)
        shim = type("A", (), {})()
        shim.from_sign = from_sign
        shim.url = args.url
        shim.out = args.out
        shim.observe = args.observe

        log("  方式：%s" % ("用签到页换号" if from_sign else "用任意 H5 页触发"))
        try:
            oid, how = await g.one_attempt(shim, log)
        except Exception as exc:  # noqa: BLE001
            log("  异常：%s: %s" % (type(exc).__name__, str(exc)[:140]))
            oid, how = None, "exception"

        if oid:
            g.save(args.out, oid, how, args.url)
            log("  ✔ 拿到新号（%s）：%s" % (how, oid))
            log("    已写入 %s" % args.out)
            # 续号完成，把签到侧拉起来（它启动时会清掉 openid.stale）
            if args.with_sign and signer is None:
                signer = start_signer(args, log)
        else:
            log("  ✘ 本轮失败（%s），%ds 后重试" % (how, int(args.cooldown)))

        if args.once:
            return 0 if oid else 4
        # 刚换到新号：立刻回循环顶部刷新，不再空等一个轮询周期。
        # 下一圈 openid 有效，会直接打出「正常值守」，面板随即进入「监视中」。
        if oid:
            continue
        await asyncio.sleep(args.poll)


def start_signer(args, log):
    """签到侧是独立进程：它只吃 openid.txt，不依赖调试通道。
    退出码 2 = 发现 401，此时主循环立刻续号再把它拉起来。"""
    script = os.path.join(HERE, "signer", "sign_pc.py")
    if not os.path.isfile(script):
        log("找不到 signer/sign_pc.py，跳过签到侧")
        return None
    log("启动签到侧：signer/sign_pc.py")
    try:
        return subprocess.Popen([sys.executable, script, "--openid-file", args.out])
    except Exception as exc:  # noqa: BLE001
        log("签到侧启动失败：%s" % exc)
        return None


def port_open_or_spawn(sup, log):
    if sup.proc is not None and sup.proc.poll() is not None:
        log("代理已退出 code=%s" % sup.proc.returncode)
        sup.proc = None
    ok = sup.ensure()
    if ok:
        log("代理就绪：%s" % ("复用已有" if sup.proc is None else "已拉起"))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=g.DEFAULT_URL)
    ap.add_argument("--out", default=os.path.join(HERE, "openid.txt"))
    ap.add_argument("--poll", type=float, default=10.0)
    ap.add_argument("--cooldown", type=float, default=300.0)
    ap.add_argument("--observe", type=float, default=18.0)
    ap.add_argument("--check", action="store_true", help="只看状态")
    ap.add_argument("--once", action="store_true", help="跑一轮就退出（不常驻）")
    ap.add_argument("--no-sign", dest="with_sign", action="store_false",
                    help="不启动签到侧（只取号）")
    ap.set_defaults(with_sign=True)
    ap.add_argument("--log", default=os.path.join(HERE, "run.log"))
    ap.add_argument("--wmpf-dir", default=ck.DEFAULT_WMPF)
    ap.add_argument("--node", default=ck.DEFAULT_NODE)
    args = ap.parse_args()

    log = Log(args.log)
    if not args.check and not args.once:
        print(BANNER)
    try:
        code = asyncio.run(run(args, log))
    except KeyboardInterrupt:
        log("已停止。**代理仍在运行**（这样小程序不会被关掉）。")
        log("如需连代理一起关，请手动结束 node 进程。")
        code = 0
    finally:
        log.close()
    sys.exit(code or 0)


if __name__ == "__main__":
    main()
