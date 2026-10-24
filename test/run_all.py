#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_all.py — 一次性跑完所有可离线执行的测试

  python test/run_all.py

三套：
  1. mux       多客户端 CDP 复用（id 去重/单播/广播/断线回收）   [需要 tsx]
  2. agent     页内 agent 的 21 条行为契约                    [需要 node]
  3. classify  授权触发判定 + URL 构造（纯函数）               [纯 python]
  4. signcore  签到核心契约：无时段闸门 / 空闲退避 / 幂等 / 401    [纯 python]

不包含需要真实微信通道的测试（那部分见 auto_probe.py）。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WMPF = "C:/Users/Admin/WorkBuddy/2026-10-24-16-54-55/WMPFDebugger"
NODE = "C:/Users/Admin/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
TSX = os.path.join(WMPF, "node_modules", "tsx", "dist", "cli.mjs")


def run(name, cmd, cwd):
    print("\n=== %s ===" % name)
    print("$ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd, capture_output=True)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    tail = "\n".join((out + err).strip().splitlines()[-4:])
    print(tail)
    print("-> exit=%d" % r.returncode)
    return r.returncode == 0


def main():
    node = NODE if os.path.exists(NODE) else "node"
    results = []

    if os.path.exists(TSX):
        results.append(("mux", run("mux_test", [node, TSX, "test/mux_test.ts"], WMPF)))
    else:
        print("!! 跳过 mux_test：找不到 tsx")
        results.append(("mux", False))

    results.append(("agent", run("agent_test", [node, "test/agent_test.js"], ROOT)))
    results.append(("classify", run("classify_test", [sys.executable, "test/classify_test.py"], ROOT)))
    results.append(("signcore", run("signcore_test", [sys.executable, "test/signcore_test.py"], ROOT)))

    print("\n================ 汇总 ================")
    bad = 0
    for name, ok in results:
        print("  %-10s %s" % (name, "PASS" if ok else "FAIL"))
        if not ok:
            bad += 1
    print("%d/%d 套通过" % (len(results) - bad, len(results)))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
