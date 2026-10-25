#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
diag_timing.py — 量出「代理就绪 → 监视中」之间每一段的耗时

用法
  python tools/diag_timing.py
"""
import os
import sys
import time
import asyncio

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

import channel_keeper as ck            # noqa: E402
import get_openid as g                 # noqa: E402

T0 = [0.0]


def mark(label):
    now = time.time()
    print("  [%7.2fs] %s" % (now - T0[0], label), flush=True)
    return now


def main():
    T0[0] = time.time()
    print("== 环境 ==", flush=True)
    vers = ck.find_wmpf_versions()
    print("  WMPF 版本: %s" % (vers or "(无)"), flush=True)

    sup = ck.ProxySupervisor(ck.DEFAULT_WMPF, ck.DEFAULT_NODE)
    print("  WMPFDebugger: %s" % sup.wmpf_dir, flush=True)
    print("  node: %s" % sup.node, flush=True)
    need = sup._need_autodetect()
    print("  需要 --auto-detect: %s   extra=%s" % (need, sup.extra), flush=True)

    cfg_dir = os.path.join(sup.wmpf_dir, "frida", "config",
                           "win32" if sys.platform == "win32" else sys.platform)
    if os.path.isdir(cfg_dir):
        cfgs = sorted(n for n in os.listdir(cfg_dir)
                      if n.startswith("addresses.") and n.endswith(".json"))
        print("  已有偏移配置: %s" % (cfgs or "(无)"), flush=True)
    else:
        print("  偏移配置目录不存在: %s" % cfg_dir, flush=True)

    print("\n== 端口 ==", flush=True)
    print("  62000 (CDP 代理): %s" % ck.port_open(ck.CDP_PORT), flush=True)
    print("  9421  (调试通道): %s" % ck.port_open(ck.DEBUG_PORT), flush=True)

    print("\n== 阶段耗时 ==", flush=True)
    mark("起点")

    # 1) 拉起/复用代理 —— 复现 run.py 的 port_open_or_spawn
    t1 = time.time()
    ok = sup.ensure()
    dt_ensure = time.time() - t1
    print("  [%7.2fs] ensure()=%s  (耗时 %.2fs)" % (dt_ensure, ok, dt_ensure),
          flush=True)
    if not ok:
        print("  代理起不来，后续无法测；先检查 WMPFDebugger / node。", flush=True)
        return 1

    # 2) 首次通道探测 —— 复现 run() 循环第一圈
    t2 = time.time()
    state, targets, why = asyncio.run(ck.probe_channel())
    dt_probe = time.time() - t2
    print("  [%7.2fs] probe_channel -> %s (%s) targets=%d  耗时 %.2fs"
          % (time.time() - T0[0], state, why, len(targets), dt_probe), flush=True)

    # 3) openid 状态检查（同步阻塞，会卡住事件循环）
    out = os.path.join(HERE, "openid.txt")
    cur = g.current_oid(out)
    print("  openid: %s" % (cur[:8] + "…" if cur else "(无)"), flush=True)
    if cur:
        t3 = time.time()
        st = g.oid_state(cur)
        dt_oid = time.time() - t3
        print("  [%7.2fs] oid_state=%s  耗时 %.2fs" % (time.time() - T0[0], st, dt_oid),
              flush=True)

    # 4) 到「监视中」之前还会 sleep(poll)
    print("\n== 汇总 ==", flush=True)
    print("  ensure(拉起代理等端口)  : %.2fs" % dt_ensure, flush=True)
    print("  probe_channel          : %.2fs" % dt_probe, flush=True)
    poll = 10.0
    print("  之后 sleep(poll=%s)     : %.2fs" % (poll, poll), flush=True)
    print("  => 若首圈就命中「正常值守」，监视中应在 %.2fs 出现"
          % (dt_ensure + dt_probe), flush=True)
    print("  => 若首圈通道未就绪，则 +%.2fs（第二圈）" % poll, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
