#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
signcore_test.py — 签到核心（signcore.sign_loop）行为契约

重点验证本轮改动：**取消时段闸门**
  · 不再有 class_hours / in_hours（任何时刻都发请求）
  · 空窗期用自适应退避降频（idle_after → idle_poll）
  · 一出现签到立刻恢复 poll

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
    print("=== signcore：取消时段闸门 ===")

    # 1. 结构上不应再有时段闸门
    ok(not hasattr(signcore, "in_hours"), "已移除 in_hours()")
    ok("class_hours" not in signcore.DEFAULTS, "DEFAULTS 不再含 class_hours")
    ok("idle_poll" in signcore.DEFAULTS and "idle_after" in signcore.DEFAULTS,
       "新增 idle_poll / idle_after")

    # 2. 空窗期：先 poll，后 idle_poll（不论当前几点）
    d, f = new_env()
    cfg = signcore.load_cfg(None)
    cfg.update({"poll": 0.1, "idle_poll": 0.4, "idle_after": 3})
    calls, box = run_loop(cfg, f, 1.7, lambda n, m, p, b: (200, "[]"))
    g = gaps(calls)
    ok(len(calls) >= 4, "空窗期持续轮询（%d 次请求）" % len(calls), str(g))
    ok(g[:2] and max(g[:2]) < 0.25, "起步用 poll（间隔 %s）" % (g[:2],), str(g))
    ok(g and min(g[-2:]) > 0.25, "空窗后退避到 idle_poll（后段间隔 %s）" % (g[-2:],), str(g))
    ok(all(c["p"] == "/student/active_signs" for c in calls), "始终查询 active_signs")
    shutil.rmtree(d, ignore_errors=True)

    # 3. 出现签到：立即恢复 poll，且只提交一次
    d, f = new_env()
    sign_cfg = signcore.load_cfg(None)
    sign_cfg.update({"poll": 0.1, "idle_poll": 0.5, "idle_after": 2})

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
    ok(tail and min(tail[-2:]) < 0.25, "有签到后恢复 poll（后段 %s）" % (tail[-2:],), str(tail))
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
    cfg.update({"poll": 0.1, "idle_poll": 0.1, "idle_after": 99})

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
