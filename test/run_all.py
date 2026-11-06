#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_all.py — 一次性跑完所有可离线执行的测试

  python test/run_all.py

七套：
  1. mux       多客户端 CDP 复用（id 去重/单播/广播/断线回收）      [需要 tsx]
  2. classify  授权触发判定 + URL 构造（纯函数）                    [纯 python]
  3. signcore  签到核心契约：固定间隔轮询 / 幂等 / 401               [纯 python]
  4. ui        面板布局对齐 / 日志→状态映射 / ANSI 定点重绘          [纯 python]
  5. installer 环境部署：zip slip 防护 / 解码 / 失败识别             [纯 python]
  6. loop      主循环时序：换号后不空等 / oid_state 不阻塞事件循环    [纯 python]
  7. offsets   偏移版本自适应：版本/指纹变化、自动探测、失效兜底      [纯 python]

不包含需要真实微信通道的部分（那需要一台装着微信的机器）。
缺少可选依赖（tsx / mux_test.ts）的套会标成 SKIP，不算失败。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Windows 控制台默认 GBK、CI runner 默认 cp1252，直接 print 中文测试名会
# UnicodeEncodeError 崩掉整套测试（GitHub Actions 上 1 秒失败就是这个）。
# 统一成 UTF-8；subprocess 启动的子进程在 run() 里单独传环境变量。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# 可用环境变量覆盖，避免把作者机器路径写死
WMPF = os.environ.get("WZ_WMPF_DIR") or os.path.join(os.path.dirname(ROOT), "WMPFDebugger")
NODE = os.environ.get("WZ_NODE", "node")
TSX = os.path.join(WMPF, "node_modules", "tsx", "dist", "cli.mjs")
MUX_TEST = os.path.join(ROOT, "test", "mux_test.ts")


def run(name, cmd, cwd):
    print("\n=== %s ===" % name)
    print("$ " + " ".join(cmd))
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, env=env)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    tail = "\n".join((out + err).strip().splitlines()[-4:])
    print(tail)
    print("-> exit=%d" % r.returncode)
    return r.returncode == 0


def main():
    results = []

    if os.path.exists(TSX) and os.path.isfile(MUX_TEST):
        results.append(("mux", run("mux_test", [NODE, TSX, "test/mux_test.ts"], WMPF)))
    else:
        why = ("找不到 test/mux_test.ts（仓库未附带）" if not os.path.isfile(MUX_TEST)
               else "找不到 tsx（设 WZ_WMPF_DIR 指向 WMPFDebugger）")
        print("!! 跳过 mux_test：%s" % why)
        results.append(("mux", None))

    results.append(("classify", run("classify_test", [sys.executable, "test/classify_test.py"], ROOT)))
    results.append(("signcore", run("signcore_test", [sys.executable, "test/signcore_test.py"], ROOT)))
    results.append(("ui", run("ui_test", [sys.executable, "test/ui_test.py"], ROOT)))
    results.append(("installer", run("installer_test", [sys.executable, "test/installer_test.py"], ROOT)))
    results.append(("loop", run("loop_test", [sys.executable, "test/loop_test.py"], ROOT)))
    results.append(("offsets", run("offsets_test", [sys.executable, "test/offsets_test.py"], ROOT)))

    print("\n================ 汇总 ================")
    bad, passed, skipped = 0, 0, 0
    for name, res in results:
        print("  %-10s %s" % (name, "SKIP" if res is None else ("PASS" if res else "FAIL")))
        if res is None:
            skipped += 1
        elif res:
            passed += 1
        else:
            bad += 1
    print("%d/%d 套通过，%d 套跳过" % (passed, len(results), skipped))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
