#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
probe_dialer.py — 找出"谁在拨 ws://localhost:9421"，为彻底去掉点火提供依据

用法：
  python probe_dialer.py                 # 附加浏览器主进程，挂 hook，等 60s
  python probe_dialer.py --seconds 120
  python probe_dialer.py --auto-detect   # 不用 CIM，改用 frida 枚举（本机可能挂死）

观察流程（关键）：
  1. 先跑本脚本           -> 打印 [ready]，说明 hook 装好了
  2. 在微信里开任意小程序   -> 若打印 [dialer]，那几行就是调试客户端的拨号点
  3. 把调用栈里的 模块!符号+偏移 记下来 —— 这就是可以"直接调用"或用 frida
     Interceptor 复用的地方（不含小程序上下文时调用是否安全，需要单独评估）

为什么值得做：如果能证明拨号点是通用的远程调试客户端构造函数，就可以在
代理启动时主动调用一次，从而完全不需要用户开小程序。
"""
import argparse
import json
import subprocess
import sys
import time

import frida

HERE_JS = "probe_dialer.js"


def find_browser_pid():
    """CIM 枚举（frida-python 的 metadata 枚举在本机会挂死，见交接文档 §6）。"""
    ps = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_Process -Filter \"Name='WeChatAppEx.exe'\" | "
        "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, timeout=40).stdout
    except Exception as exc:  # noqa: BLE001
        sys.exit("枚举进程失败: %s" % exc)
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "mbcs"):
        try:
            text = raw.decode(enc)
            break
        except Exception:  # noqa: BLE001
            continue
    if not text or not text.strip():
        sys.exit("没有 WeChatAppEx.exe，先启动微信")
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    for p in data:
        cmd = p.get("CommandLine") or ""
        if "--flue-runtime-dir" in cmd:
            return p["ProcessId"], cmd
    # 兜底：取子进程众数的父
    pids = [p.get("ParentProcessId") for p in data]
    if not pids:
        sys.exit("没找到浏览器主进程")
    mode = max(set(pids), key=pids.count)
    return mode, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--js", default=HERE_JS)
    args = ap.parse_args()

    pid, cmd = find_browser_pid()
    print("[*] 浏览器主进程 pid=%s" % pid)
    if cmd:
        print("    %s" % cmd[:150])

    dev = frida.get_local_device()
    session = dev.attach(pid)
    print("[*] 已附加，加载 hook ...")
    with open(args.js, "r", encoding="utf-8") as f:
        src = f.read()

    hits = []
    script = session.create_script(src)

    def on_message(message, data):
        if message.get("type") == "error":
            print("[x] script error: %s" % message.get("description"))
            return
        payload = message.get("payload") or {}
        kind = payload.get("type")
        if kind == "ready":
            print("[+] ready: pid=%s flue=%s modules=%s port=%s"
                  % (payload.get("pid"), payload.get("flue"),
                     payload.get("modules"), payload.get("port")))
            print("    现在去微信里打开任意小程序，等 [dialer] ...")
        elif kind == "miss":
            print("[!] 找不到导出 %s" % payload.get("name"))
        elif kind == "dialer":
            hits.append(payload)
            print("\n[DIALER] via %s" % payload.get("via"))
            for i, frame in enumerate(payload.get("bt") or []):
                print("   #%-2d %s" % (i, frame))
        else:
            print("[?] %s" % payload)

    # frida-python 17.x 用 script.on("message", cb)；Node 绑定才是 script.message.connect
    script.on("message", on_message)
    script.load()

    end = time.time() + args.seconds
    try:
        while time.time() < end:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] 中断")

    print("\n[=] 结束，命中 %d 个不同拨号点" % len(hits))
    if not hits:
        print("    没有命中：可能 60s 内没开小程序，或调试客户端不是走 ws2_32.connect")
        print("    （那就换思路：hook winhttp.dll!WinHttpWebSocketCompleteUpgrade）")
    try:
        script.unload()
        session.detach()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
