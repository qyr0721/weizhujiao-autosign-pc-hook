#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
signcore_test.py — 签到核心（signcore.sign_loop）行为契约

重点验证
  · 不再有 class_hours / in_hours（任何时刻都发请求）
  · 轮询间隔固定为 poll，不因空窗改变，也不依赖本地时间

  python test/signcore_test.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import signcore  # noqa: E402

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


def new_env(oid="deadbeef" * 4):
    d = tempfile.mkdtemp(prefix="wzsg_")
    path = os.path.join(d, "openid.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(oid + "\n")
    return d, path


def run_loop(cfg, openid_file, seconds, responder):
    """跑 sign_loop 一段时间，返回 (调用记录, 退出码)。"""
    calls = []

    def fake_req(method, path, oid, body=None, timeout=15):
        calls.append({"t": time.time(), "m": method, "p": path, "b": body})
        return responder(len(calls), method, path, body)

    orig = signcore.req
    signcore.req = fake_req
    stop = threading.Event()
    box = {}

    def target():
        try:
            box["rc"] = signcore.sign_loop(cfg, openid_file, log=lambda m: None, stop=stop)
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    th = threading.Thread(target=target, daemon=True)
    th.start()
    time.sleep(seconds)
    stop.set()
    th.join(timeout=5)
    signcore.req = orig
    return calls, box


def gaps(calls):
    return [round(calls[i + 1]["t"] - calls[i]["t"], 3) for i in range(len(calls) - 1)]


def main():
    print("=== signcore：固定间隔轮询 ===")

    # 1. 结构上不应再有时段闸门，也不再有空闲退避
    ok(not hasattr(signcore, "in_hours"), "已移除 in_hours()")
    ok("class_hours" not in signcore.DEFAULTS, "DEFAULTS 不再含 class_hours")
    ok("idle_poll" not in signcore.DEFAULTS and "idle_after" not in signcore.DEFAULTS,
       "DEFAULTS 不再含 idle_poll / idle_after")

    # 2. 间隔恒定：无论有没有签到，都按 poll 走
    d, f = new_env()
    cfg = signcore.load_cfg(None)
    cfg.update({"poll": 0.1})
    calls, box = run_loop(cfg, f, 1.2, lambda n, m, p, b: (200, "[]"))
    g = gaps(calls)
    ok(len(calls) >= 4, "空窗期持续轮询（%d 次请求）" % len(calls), str(g))
    ok(g and max(g) < 0.25, "空窗期间隔仍为 poll（%s）" % (g,), str(g))
    ok(all(c["p"] == "/student/active_signs" for c in calls), "始终查询 active_signs")
    shutil.rmtree(d, ignore_errors=True)

    # 3. 出现签到：立即提交，且只提交一次
    d, f = new_env()
    sign_cfg = signcore.load_cfg(None)
    sign_cfg.update({"poll": 0.1})

    def responder(n, m, p, b):
        if p == "/student/active_signs":
            # 前 3 次空窗，第 4 次起有签到
            return (200, "[]") if n <= 3 else (200, json.dumps(
                [{"signId": 11, "courseId": 22, "isGPS": False}]))
        return 200, '{"signRank": 7}'

    calls, box = run_loop(sign_cfg, f, 1.4, responder)
    posts = [c for c in calls if c["p"] == "/student-sign-in"]
    ok(len(posts) == 1, "签到只提交一次（幂等，共 %d 次 POST）" % len(posts))
    ok(posts and posts[0]["b"] == {"courseId": 22, "signId": 11},
       "提交体与契约一致", str(posts[0]["b"] if posts else None))
    tail = gaps(calls)
    ok(tail and max(tail) < 0.25, "有签到后间隔仍为 poll（%s）" % (tail,), str(tail))
    shutil.rmtree(d, ignore_errors=True)

    # 4. 401：写 stale 标记并返回 2
    d, f = new_env()
    calls, box = run_loop(signcore.load_cfg(None), f, 0.9,
                          lambda n, m, p, b: (401, "unauthorized"))
    ok(box.get("rc") == 2, "401 返回退出码 2（请求续号）", str(box))
    ok(os.path.isfile(os.path.join(d, "openid.stale")), "401 写了 openid.stale")
    shutil.rmtree(d, ignore_errors=True)

    # 5. 幂等：repeat sign in 记为已签，不反复重试
    d, f = new_env()
    cfg = signcore.load_cfg(None)
    cfg.update({"poll": 0.1})

    def dup(n, m, p, b):
        if p == "/student/active_signs":
            return 200, json.dumps([{"signId": 5, "courseId": 6}])
        return 200, '{"errorCode":305,"msg":"repeat sign in"}'

    calls, box = run_loop(cfg, f, 0.8, dup)
    posts = [c for c in calls if c["p"] == "/student-sign-in"]
    ok(len(posts) == 1, "repeat sign in 只提交一次（%d）" % len(posts))
    shutil.rmtree(d, ignore_errors=True)

    # 6. 无号：once 模式返回 3
    d = tempfile.mkdtemp(prefix="wzsg_")
    empty = os.path.join(d, "openid.txt")
    rc = signcore.sign_loop(signcore.load_cfg(None), empty, once=True, log=lambda m: None)
    ok(rc == 3, "无 openid 时 once 返回 3", str(rc))
    shutil.rmtree(d, ignore_errors=True)

    print("\n%d 通过，%d 失败" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
