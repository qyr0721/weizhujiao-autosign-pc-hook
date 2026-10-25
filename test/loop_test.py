#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
loop_test.py — 主循环时序（不需要微信：CDP 与网络全部打桩）

  python test/loop_test.py

测什么
  · 取号成功后立刻进入「监视中」，不再空等一个轮询周期（省掉 poll=10s 的空等）
  · oid_state 走线程池：HTTP 在飞时事件循环仍然响应，多个调用是并行而非串行
  · 日志 → 面板：拿到新号 →「换号成功」，正常值守 →「监视中 · 号 xxxxxxxx」
"""
import argparse
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import run as runmod             # noqa: E402
import get_openid as g           # noqa: E402
import channel_keeper as ck      # noqa: E402
import wizard as W               # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s   %s" % (name, extra))


# 时序测试会打桩 oid_state_async；这里先把真身留住，
# 否则并发测试测到的会是被打过桩的版本（假阳性）。
REAL_OID_STATE_ASYNC = g.oid_state_async

OLD = "0" * 32
NEW = "a" * 32
POLL = 10.0
OUT = os.path.join(HERE, ".loop_openid.txt")
# 拿号到「监视中」的预算：必须远小于一个轮询周期，否则就是白等了。
# 桩函数都是毫秒级，给 3s 的余量足够宽容。
BUDGET = 3.0


def _drive_loop(hook, until_beat):
    """跑 run() 主循环，直到出现「正常值守」再取消。"""
    ns = argparse.Namespace(
        url=g.DEFAULT_URL, out=OUT, poll=POLL, cooldown=300.0, observe=18.0,
        check=False, once=False, with_sign=False,
        wmpf_dir=ck.DEFAULT_WMPF, node=ck.DEFAULT_NODE,
    )

    async def drive():
        task = asyncio.create_task(runmod.run(ns, hook))
        try:
            for _ in range(200):
                await asyncio.sleep(0.05)
                if until_beat():
                    break
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(drive())


def test_no_idle_wait_after_new_oid():
    """核心时序：换到新号后不要 sleep(poll)，直接回循环顶部刷新状态。"""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(OLD + "\n")

    t0 = time.time()
    msgs = []
    beat = {"at": None, "text": ""}

    def hook(msg):
        at = time.time() - t0
        msgs.append((at, msg))
        if "正常值守" in msg and beat["at"] is None:
            beat["at"], beat["text"] = at, msg

    async def fake_probe(timeout=6.0):
        await asyncio.sleep(0.02)
        return ck.CHANNEL_UP, [{"url": "https://x/teachermate/sign", "type": "page"}], "ok"

    async def fake_oid_state(oid):
        await asyncio.sleep(0.01)
        return "ok" if oid == NEW else "401"

    async def fake_one_attempt(args, log):
        await asyncio.sleep(0.02)
        return NEW, "S3-Page.navigate"

    ck.probe_channel = fake_probe
    ck.ProxySupervisor.ensure = lambda self: True
    g.oid_state_async = fake_oid_state
    g.one_attempt = fake_one_attempt

    _drive_loop(hook, lambda: beat["at"] is not None)

    print("  时间线：" + " → ".join("%.2fs %s" % (a, m.split("：")[0]) for a, m in msgs))

    ok(beat["at"] is not None, "走到「监视中」（打出正常值守）", str(msgs))
    if beat["at"] is not None:
        ok(beat["at"] < BUDGET,
           "换号后立刻刷新，未空等一个轮询周期（%.2fs < %.1fs）" % (beat["at"], POLL),
           "实测 %.2fs" % beat["at"])
        ok(beat["at"] < POLL - 1,
           "相比改动前的 %.1fs 至少省下一个 poll" % (beat["at"] + POLL),
           "实测 %.2fs" % beat["at"])

    seq = [m for _a, m in msgs]
    i_new = next((i for i, m in enumerate(seq) if "拿到新号" in m), -1)
    i_beat = next((i for i, m in enumerate(seq) if "正常值守" in m), -1)
    ok(i_new >= 0 and i_beat > i_new, "顺序：拿到新号 → 正常值守", seq)
    ok(any("需要取号" in m for _a, m in msgs), "老号 401 时确实走了取号路径", seq)


def test_oid_state_off_eventloop():
    """oid_state 是同步 HTTP；必须丢线程池，否则会卡住事件循环挡住 CDP。

    用一个会睡 0.2s 的假 HTTP 代替真实网络，这样离线也能测。
    """
    saved = g.oid_state

    def slow_http(oid):
        time.sleep(0.2)
        return "ok"

    g.oid_state = slow_http

    async def scenario():
        # 先确认假 HTTP 真的生效（单个调用 ≈ 0.2s），否则后面全是假阳性
        t = time.time()
        await REAL_OID_STATE_ASYNC(OLD)
        one = time.time() - t

        # 判据 1：HTTP 在飞时，并发的 sleep(0.05) 能否按时醒来
        t0 = time.time()

        async def sleeper():
            await asyncio.sleep(0.05)
            return time.time()

        s = asyncio.create_task(sleeper())
        await REAL_OID_STATE_ASYNC(OLD)
        delay = (await s) - t0

        # 判据 2：3 个并发调用的总耗时（并行 ≈ 0.2s，串行 ≈ 0.6s）
        t1 = time.time()
        await asyncio.gather(REAL_OID_STATE_ASYNC(OLD),
                             REAL_OID_STATE_ASYNC(OLD),
                             REAL_OID_STATE_ASYNC(OLD))
        three = time.time() - t1
        return one, delay, three

    try:
        one, delay, three = asyncio.run(scenario())
    finally:
        g.oid_state = saved

    ok(one > 0.15, "假 HTTP 确实被调用了（单调用 %.3fs ≈ 0.2s）" % one,
       "实测 %.3fs —— 桩没生效，后面的断言不可信" % one)
    ok(delay < 0.15, "HTTP 在飞时事件循环仍响应（%.3fs ≈ 0.05，不是 0.25）" % delay,
       "实测 %.3fs" % delay)
    ok(three < one * 2, "3 个并发调用并行执行（%.3fs ≈ 单一 %.3fs，不是 3 倍）"
       % (three, one), "实测 %.3fs" % three)


def test_no_30s_after_spawn():
    """冷启动的两段空等（来自 15:29:48 实测日志）：
      · 拉起代理成功后不该再睡 30s（该 30s 是「起不来」的退避）
      · 通道不通时轮询要勤快，探测用短超时，别让「连上」到「面板切换」白等
    """
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(NEW + "\n")            # 号有效 → 走「正常值守」路径，不触发取号

    t0 = time.time()
    msgs = []
    marks = {}

    def hook(msg):
        at = time.time() - t0
        msgs.append((at, msg))
        if "代理就绪" in msg and "ready" not in marks:
            marks["ready"] = at
        if "通道未就绪" in msg and "down" not in marks:
            marks["down"] = at
        if "正常值守" in msg and "beat" not in marks:
            marks["beat"] = at

    calls = {"n": 0}
    timeouts = []

    async def fake_probe(timeout=6.0):
        calls["n"] += 1
        timeouts.append(timeout)
        await asyncio.sleep(0.02)
        if calls["n"] == 1:
            return ck.PROXY_DOWN, [], "port-closed"      # 冷启动：代理还没起来
        if calls["n"] == 2:
            return ck.CHANNEL_DOWN, [], "no-reply"       # 起来了，小程序还没连
        return ck.CHANNEL_UP, [{"url": "https://x/teachermate/sign", "type": "page"}], "ok"

    async def fake_oid_state(oid):
        await asyncio.sleep(0.01)
        return "ok"

    ck.probe_channel = fake_probe
    ck.ProxySupervisor.ensure = lambda self: True        # 假装瞬间拉起代理
    g.oid_state_async = fake_oid_state

    _drive_loop(hook, lambda: marks.get("beat") is not None)

    print("  时间线：" + " → ".join("%.2fs %s" % (a, m.split("：")[0]) for a, m in msgs))

    ok({"ready", "down", "beat"} <= set(marks),
       "走完 代理就绪 → 等待小程序连接 → 监视中", str(marks))
    if {"ready", "down"} <= set(marks):
        gap1 = marks["down"] - marks["ready"]
        ok(gap1 < 1.0, "拉起代理后立刻重探，未空等 30s（%.2fs）" % gap1,
           "实测 %.2fs —— 30s 退避被误用到成功路径" % gap1)
    if {"down", "beat"} <= set(marks):
        gap2 = marks["beat"] - marks["down"]
        ok(gap2 < 5.0, "通道不通时勤快轮询，连上后很快切「监视中」（%.2fs）" % gap2,
           "实测 %.2fs" % gap2)
    ok(calls["n"] == 3, "三态各探测一次（共 %d 次）" % calls["n"], str(calls))
    # 等待通道期间（None/PROXY_DOWN/CHANNEL_DOWN）都该用短超时
    ok(timeouts == [2.0, 2.0, 2.0],
       "等待通道期间探测统一用短超时 2s %s" % timeouts, str(timeouts))


def test_pick_tab_rejects_internal_pages():
    """全新微信账号里往往只有 weixin:// 内部页，它们导航不到授权链接。

    实测（另一台机器的 wxsign-20261025-170823.log）：fallback 选中
    weixin://resourceid/... 后 S1/S2/S3 全部打空，54s 后 all-failed。
    现在必须拒收，返回 None 让调用方去新建页面。
    """
    internal = [{"targetId": "t1", "type": "page",
                 "url": "weixin://resourceid/SubscriptionDisorder/disorder.html?visible=0"}]
    tab, why = g.pick_tab(list(internal))
    ok(tab is None, "拒收 weixin:// 内部页（不许 fallback）", "%s %s" % (tab, why))

    mixed = internal + [{"targetId": "t2", "type": "webview",
                         "url": "https://app.teachermate.com.cn/wechat-pro-ssr/student/sign"}]
    tab, why = g.pick_tab(list(mixed))
    ok(tab is not None and tab["targetId"] == "t2",
       "有 http 页时优先选它", "%s %s" % (tab, why))

    tm = [{"targetId": "t3", "type": "page",
           "url": "https://app.teachermate.com.cn/wechat-pro-ssr/student/sign?x=1"}]
    tab, why = g.pick_tab(tm + mixed)
    ok(tab is not None and tab["targetId"] == "t3",
       "teachermate 页优先级最高", "%s %s" % (tab, why))

    ok(g.pick_tab([])[0] is None, "无页面时返回 None", str(g.pick_tab([])))


def test_panel_mapping():
    """日志 → 面板状态：这两条决定了用户看到什么。"""
    st = W.State({"wechat": "4.1.15.56", "wmpf": "25799", "wmpf_ok": True,
                  "ok": True, "note": "x", "hint": [], "logfile": "x.log",
                  "action": "", "sign": "", "sign_kind": "", "wmpf": 25799})

    st.feed("  ✔ 拿到新号（S3-Page.navigate）：%s" % NEW)
    ok("换号成功" in st.info["action"], "拿到新号 → 换号成功", st.info["action"])

    st.feed("正常值守：号 %s… 状态=ok，通道 3 个 target" % NEW)
    ok("监视中" in st.info["action"], "正常值守 → 监视中", st.info["action"])
    ok(NEW[:8] in st.info["action"], "监视中带出号前 8 位", st.info["action"])
    ok(st.channel, "正常值守同时把通道标为已连接", str(st.channel))


def main():
    print("=== loop 时序 ===")
    test_no_idle_wait_after_new_oid()
    test_no_30s_after_spawn()
    test_oid_state_off_eventloop()
    test_pick_tab_rejects_internal_pages()
    test_panel_mapping()
    print("\n  %d 通过，%d 失败" % (PASS, FAIL))
    if os.path.exists(OUT):
        os.remove(OUT)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
