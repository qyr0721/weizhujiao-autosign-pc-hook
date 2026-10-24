#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
probe_a8key.py — 在运行中的浏览器进程里观察 a8key 导航拦截层的实际行为

回答三个问题：
  1. 一次导航到 open.weixin.qq.com/connect/oauth2/authorize 时，
     A8KeyNavigationThrottle::CheckURL 到底有没有被调用？
  2. 有没有走到 Redirect（即"被接管并改写"）？
  3. 有没有被 A8KeyRateLimiter 拦成 duplicate / cache expired？

这三点决定了下一步怎么改写：是"简单导航就能触发"，还是"必须强行促成 Redirect"。

钩子来自 tools/pe_scan.py 对 flue.dll（25799）的静态分析，RVA 版本敏感。

用法：
  python probe_a8key.py                          # 只挂钩子，等你手动操作微信
  python probe_a8key.py --cdp-nav "<oauth url>"  # 勾完钩子后主动让某个标签页导航
  python probe_a8key.py --seconds 120

注意：--cdp-nav 需要调试通道（常驻的小程序），否则会提示通道未建立。
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))      # tools/
ROOT = os.path.dirname(HERE)                            # 仓库根
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)


def find_browser_pid():
    """定位带 --flue-runtime-dir 的 WeChatAppEx 浏览器主进程。

    用 CIM 枚举（frida-python 自带的 metadata 枚举在本机会挂死）。
    """
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
    pids = [p.get("ParentProcessId") for p in data]
    if not pids:
        sys.exit("没能识别浏览器主进程")
    # 众数父子进程即为浏览器主进程
    best = max(set(pids), key=pids.count)
    for p in data:
        if p.get("ProcessId") == best:
            return best, p.get("CommandLine") or ""
    return pids[0], ""

# 来自 a8key_scan.json（flue.dll 25799）。换版本用 tools/pe_scan.py 重扫后替换。
DEFAULT_HOOKS = [
    {"rva": 0x025FE320, "name": "A8KeyThrottle::CheckURL", "kind": "fn"},
    {"rva": 0x025FEE99, "name": "mark:redirect_limit", "kind": "mark"},
    {"rva": 0x025FF0B4, "name": "mark:IsRedirectLimitReached", "kind": "mark"},
    {"rva": 0x02600092, "name": "mark:CheckURL_logA", "kind": "mark"},
    {"rva": 0x026000FA, "name": "mark:CheckURL_logB", "kind": "mark"},
    {"rva": 0x02600690, "name": "A8KeyThrottle::Redirect", "kind": "fn"},
    {"rva": 0x026016D9, "name": "mark:Redirect_log", "kind": "mark"},
    {"rva": 0x02601F70, "name": "A8KeyRateLimiter", "kind": "fn"},
    {"rva": 0x026040D0, "name": "host_check_A", "kind": "fn"},
    {"rva": 0x02642BD0, "name": "host_check_B", "kind": "fn"},
    {"rva": 0x02643060, "name": "host_check_C", "kind": "fn"},
    {"rva": 0x02657FE0, "name": "host_check_D", "kind": "fn"},
    # 页面 -> 原生 的 JS API 总入口：能直接看到 api 名与参数 JSON
    {"rva": 0x02789300, "name": "ApiProcessor::MMV8CallNative", "kind": "fn", "read": "std2"},
    {"rva": 0x027B9A40, "name": "MMV8CallNative#2", "kind": "fn", "read": "std2"},
    # 引用 "OpenUrl" 的两个函数，疑似打开 URL 的 IPC 处理
    {"rva": 0x035383D0, "name": "OpenUrl_A", "kind": "fn"},
    {"rva": 0x0359DFB0, "name": "OpenUrl_B", "kind": "fn"},
]

COLORS = {"fn": "", "mark": "  "}


def load_hooks(path):
    if not path:
        return DEFAULT_HOOKS
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    hooks = []
    for fn in data.get("functions", []):
        hooks.append({"rva": fn["rva"], "name": "func_%X" % fn["rva"], "kind": "fn"})
    return hooks


async def cdp_nav(url, match):
    import cdp_sign_agent as base
    try:
        ws = base.connect()
    except (OSError, ConnectionRefusedError):
        print("[!] 连不上 62000，跳过主动导航（通道需要常驻小程序）")
        return False
    async with ws as sock:
        cdp = base.Cdp(sock)
        try:
            infos = (await cdp.send("Target.getTargets", {}, timeout=12)).get("targetInfos", [])
        except TimeoutError:
            print("[!] 62000 通了但没有下游（小程序没连上 9421），跳过主动导航")
            return False
        pages = [t for t in infos if t.get("type") in ("page", "webview")
                 and "liteapp" not in (t.get("url") or "")]
        if match:
            pages = [t for t in pages if match in (t.get("url") or "")]
        if not pages:
            print("[!] 没有可用标签页，跳过主动导航")
            return False
        pages.sort(key=lambda t: len(t.get("url") or ""), reverse=True)
        tgt = pages[0]
        print("[*] 主动导航 -> %s" % (tgt.get("url") or "")[:90])
        await base.attach(cdp, tgt["targetId"])
        try:
            res = await cdp.send("Page.navigate", {"url": url}, timeout=25)
            print("[*] Page.navigate 返回: %s" % json.dumps(res, ensure_ascii=False)[:160])
        except Exception as exc:  # noqa: BLE001
            print("[!] 导航失败: %s" % exc)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--js", default=os.path.join(HERE, "hook_a8key.js"))
    ap.add_argument("--hooks-json", default="", help="用 pe_scan 的 json 生成函数钩子")
    ap.add_argument("--cdp-nav", default="", help="注入后主动让某标签页导航到此 URL")
    ap.add_argument("--match", default="", help="只选 URL 含此子串的标签页")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    import frida

    pid, cmd = find_browser_pid()
    print("[*] 浏览器主进程 pid=%s" % pid)
    if cmd:
        print("    %s" % cmd[:130])

    hooks = load_hooks(args.hooks_json)
    with open(args.js, "r", encoding="utf-8") as f:
        src = f.read()
    cfg = {"module": "flue.dll", "verbose": not args.quiet, "snapshot": True, "hooks": hooks}
    src = src.replace("@@CONFIG@@", json.dumps(cfg))

    session = frida.get_local_device().attach(pid)
    script = session.create_script(src)
    hits = {}

    def on_message(message, _data):
        if message.get("type") == "error":
            print("[x] script error: %s" % message.get("description"))
            return
        p = message.get("payload") or {}
        t = p.get("type")
        if t == "base":
            print("[+] 模块 %s base=%s，准备挂 %d 个钩子" % (p.get("module"), p.get("base"), p.get("hooks")))
        elif t == "installed":
            print("[+] 钩子安装完成 %s/%s" % (p.get("ok"), p.get("total")))
        elif t == "fail":
            print("[!] 挂钩失败 %s@0x%X: %s" % (p.get("name"), p.get("rva"), p.get("err")))
        elif t == "hit":
            key = p.get("name")
            hits[key] = hits.get(key, 0) + 1
            n = hits[key]
            # 前 3 次详细打，之后只计数，避免刷屏
            if n <= 3:
                extra = ""
                if p.get("api"):
                    extra = "  jsapi=%s" % p["api"]
                    if p.get("apiArgs"):
                        extra += "  args=%s" % str(p["apiArgs"])[:120]
                elif p.get("urls"):
                    extra = "  url~%s" % p["urls"][0][:100]
                print("  [hit] %-28s tid=%s%s" % (key, p.get("tid"), extra))
            elif n == 4:
                print("  [hit] %-28s ...（后续只计数）" % key)
        elif t == "fatal":
            print("[x] %s" % p.get("msg"))

    script.on("message", on_message)
    script.load()

    print("\n[*] 现在做你想观察的动作（或等 --cdp-nav）。Ctrl+C 结束。")
    nav_done = False
    end = time.time() + args.seconds
    try:
        while time.time() < end:
            if args.cdp_nav and not nav_done:
                time.sleep(1.0)
                asyncio.run(cdp_nav(args.cdp_nav, args.match))
                nav_done = True
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] 中断")

    print("\n[=] 命中汇总：")
    if not hits:
        print("    （无命中：这段逻辑没被触发，或 RVA 与当前版本不匹配）")
    for k, v in sorted(hits.items(), key=lambda x: -x[1]):
        print("    %-30s %d 次" % (k, v))
    try:
        script.unload()
        session.detach()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
