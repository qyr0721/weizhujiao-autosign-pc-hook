#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_all.py — 一次性跑完所有可离线执行的测试

  python test/run_all.py

三套：
  1. mux       多客户端 CDP 复用（id 去重/单播/广播/断线回收）      [需要 tsx]
  2. classify  授权触发判定 + URL 构造（纯函数）                    [纯 python]
  3. signcore  签到核心契约：固定间隔轮询 / 幂等 / 401               [纯 python]
  4. ui        面板布局对齐 / 日志→状态映射 / ANSI 定点重绘          [纯 python]
  5. installer 环境部署：zip slip 防护 / 解码 / 失败识别             [纯 python]
  6. loop      主循环时序：换号后不空等 / oid_state 不阻塞事件循环    [纯 python]

不包含需要真实微信通道的部分（那需要一台装着微信的机器）。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 可用环境变量覆盖，避免把作者机器路径写死
WMPF = os.environ.get("WZ_WMPF_DIR") or os.path.join(os.path.dirname(ROOT), "WMPFDebugger")
NODE = os.environ.get("WZ_NODE", "node")
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
    results = []

    if os.path.exists(TSX):
        results.append(("mux", run("mux_test", [NODE, TSX, "test/mux_test.ts"], WMPF)))
    else:
        print("!! 跳过 mux_test：找不到 tsx（设 WZ_WMPF_DIR 指向 WMPFDebugger）")
        results.append(("mux", False))

    results.append(("classify", run("classify_test", [sys.executable, "test/classify_test.py"], ROOT)))
    results.append(("signcore", run("signcore_test", [sys.executable, "test/signcore_test.py"], ROOT)))
    results.append(("ui", run("ui_test", [sys.executable, "test/ui_test.py"], ROOT)))
    results.append(("installer", run("installer_test", [sys.executable, "test/installer_test.py"], ROOT)))
    results.append(("loop", run("loop_test", [sys.executable, "test/loop_test.py"], ROOT)))

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
