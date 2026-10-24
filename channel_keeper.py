#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
channel_keeper.py — WMPF 调试通道守护进程

解决两个痛点：
  1) 「必须先开小程序点火，且全程不能关」-> 断开后自动点火、自动重连
  2) 「代理重启后要手动再开一次小程序」  -> 拉起代理后自动走一遍点火流程

状态机（默认 = 常驻小程序模式）：
  PROXY_DOWN   : 62000 端口不通（进程没跑/崩了）        -> 拉起代理（有次数上限）
  CHANNEL_DOWN : 代理在，但对 Target.getTargets 无响应  -> **只提示，不操作界面**
  CHANNEL_UP   : 通道可用                                -> 若签到页在，托管 cdp_sign_agent.py

设计前提：**小程序在后台常驻不关**，通道就一直活着。因此本进程的职责是
"守住已有的通道 + 自动接管签到页"，而不是反复去打开小程序。

若你确实想让它自己点火（不推荐，会抢焦点），显式给下面任一参数：
  1. --ignite-cmd "<shell 命令>"     复用你自己写好的 UI 自动化
  2. --ignite-deeplink <weixin://..> ShellExecute 打开小程序深链
  3. --ignite-uia <模板名>            调用 auth_refresher 的模板点击

注意：**代理重启会导致 9421 上的旧连接断开**，此时必须重新触发一次小程序
（切后台再切回，或重开）。所以本进程对代理只做"死了才拉、拉几次就停"。

用法：
  python channel_keeper.py --ignite-cmd "python ../weizhujiao-autosign-pc/recon_pc_wechat.py --open-miniapp"
  python channel_keeper.py --check-only          # 只报状态
"""
import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time

CDP_PORT = 62000
DEBUG_PORT = 9421

# 路径可通过环境变量覆盖，避免绑定某台机器。
#   WZ_NODE      node.exe 路径
#   WZ_WMPF_DIR  WMPFDebugger 仓库路径
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_NODE = os.environ.get("WZ_NODE") or "C:/Users/Admin/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"


def _default_wmpf():
    env = os.environ.get("WZ_WMPF_DIR")
    if env:
        return env
    # 常见布局：与本项目同级
    for cand in (os.path.join(_HERE, "..", "WMPFDebugger"),
                 os.path.join(_HERE, "WMPFDebugger"),
                 "C:/Users/Admin/WorkBuddy/2026-10-24-16-54-55/WMPFDebugger"):
        if os.path.isdir(cand):
            return os.path.abspath(cand)
    return os.path.join(_HERE, "..", "WMPFDebugger")


DEFAULT_WMPF = _default_wmpf()

PROXY_DOWN = "PROXY_DOWN"
CHANNEL_DOWN = "CHANNEL_DOWN"
CHANNEL_UP = "CHANNEL_UP"


def ts():
    return time.strftime("%H:%M:%S")


# 外部可注入的输出口：面板模式（wizard/exe）把它接到日志文件，
# 避免调试信息打乱界面。默认仍然是直接打印。
OUT = None


def say(msg):
    if OUT is not None:
        OUT(msg)
        return
    print("[%s] %s" % (ts(), msg), flush=True)


def port_open(port, host="127.0.0.1", timeout=0.6):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------ 代理托管
class ProxySupervisor:
    # 代理 stdout 逐行的去处。面板模式（wizard/exe）会替换成写日志。
    SINK = None

    def __init__(self, wmpf_dir, node, extra_args=None):
        self.wmpf_dir = wmpf_dir
        self.node = node
        self.extra = extra_args or []
        self.proc = None
        self.restarts = 0
        self.reported = False

    def ensure(self):
        if port_open(CDP_PORT):
            return True
        if self.proc is not None and self.proc.poll() is None:
            return False
        if self.proc is not None:
            say("[x] 代理已退出 code=%s，准备重启" % self.proc.returncode)
            self.proc = None
        script = os.path.join(self.wmpf_dir, "node_modules", "tsx", "dist", "cli.mjs")
        entry = os.path.join(self.wmpf_dir, "src", "index.ts")
        if not os.path.exists(script) or not os.path.exists(entry):
            say("[x] 找不到 WMPFDebugger 依赖/入口: %s" % self.wmpf_dir)
            return False
        cmd = [self.node, script, entry] + self.extra
        say("[*] 拉起代理: %s" % " ".join(cmd[-3:]))
        self.proc = subprocess.Popen(
            cmd, cwd=self.wmpf_dir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        import threading

        def pump(pipe):
            for raw in iter(pipe.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                sink = ProxySupervisor.SINK
                if sink is not None:
                    sink(line)
                else:
                    print("    | %s" % line, flush=True)

        threading.Thread(target=pump, args=(self.proc.stdout,), daemon=True).start()
        # 等端口起来（frida attach 需要 2~8s）
        for _ in range(40):
            if port_open(CDP_PORT):
                say("[+] 代理已就绪")
                return True
            time.sleep(0.5)
        say("[x] 代理 20s 内未监听 %d" % CDP_PORT)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ------------------------------------------------------------------ 通道探测
async def probe_channel(timeout=6.0):
    """只用 62000 就能判断三态。依赖 mux 代理（多客户端安全）。

    注意：只有「端口确实关了」才算 PROXY_DOWN。WS 层异常一律降级为
    CHANNEL_DOWN —— 否则一次瞬时抖动会触发重复拉起代理（重复 frida attach
    + 端口二次绑定），把守护进程本身变成不稳定源。
    """
    if not port_open(CDP_PORT):
        return PROXY_DOWN, [], "port-closed"
    import websockets

    try:
        async with websockets.connect("ws://127.0.0.1:%d" % CDP_PORT,
                                      open_timeout=4, max_size=None) as ws:
            await ws.send(json.dumps({"id": 9001, "method": "Target.getTargets", "params": {}}))
            loop = asyncio.get_event_loop()
            end = loop.time() + timeout
            while True:
                left = end - loop.time()
                if left <= 0:
                    return CHANNEL_DOWN, [], "no-reply"
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
                if msg.get("id") == 9001:
                    return CHANNEL_UP, msg.get("result", {}).get("targetInfos", []) or [], "ok"
    except asyncio.TimeoutError:
        return CHANNEL_DOWN, [], "no-reply"
    except Exception as exc:  # noqa: BLE001
        # 只有端口真的没了才判定代理死亡
        if not port_open(CDP_PORT):
            return PROXY_DOWN, [], "port-closed"
        return CHANNEL_DOWN, [], "ws-err:%s" % exc


def has_ignite(args):
    return bool(args.ignite_cmd or args.ignite_deeplink or args.ignite_uia)


# ------------------------------------------------------------------ 点火
class Igniter:
    def __init__(self, args):
        self.args = args
        self.last = 0.0
        self.interval = args.ignite_min
        self.attempts = 0
        self.told = False        # 同一断连期内只提示一次，不刷屏

    def due(self):
        return (time.time() - self.last) >= self.interval

    def fire(self):
        if not has_ignite(self.args):
            # 常驻模式：小程序由用户保持后台不关，这里只提示一次，绝不自行操作界面
            if not self.told:
                self.told = True
                say("[!] 通道断了。若小程序已被关闭，请把它切到前台再切回（或重新打开一次）；")
                say("    本进程不会去点界面。要自动点火请显式加 --ignite-cmd/--ignite-deeplink/--ignite-uia。")
            return False
        self.last = time.time()
        self.attempts += 1
        if self.args.ignite_cmd:
            say("[*] 点火(cmd): %s" % self.args.ignite_cmd)
            try:
                subprocess.Popen(self.args.ignite_cmd, shell=True)
                return True
            except Exception as exc:  # noqa: BLE001
                say("[!] 点火命令失败: %s" % exc)
                return False
        if self.args.ignite_deeplink:
            say("[*] 点火(deeplink): %s" % self.args.ignite_deeplink)
            try:
                os.startfile(self.args.ignite_deeplink)
                return True
            except Exception as exc:  # noqa: BLE001
                say("[!] deeplink 失败: %s" % exc)
                return False
        if self.args.ignite_uia:
            say("[*] 点火(uia): 模板 %s" % self.args.ignite_uia)
            try:
                import importlib.util

                path = os.path.join(self.args.uia_dir, "auth_refresher.py")
                spec = importlib.util.spec_from_file_location("auth_refresher", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                cfg = mod.load_config(os.path.join(self.args.uia_dir, "config.json"))
                mod.locate_click(cfg, self.args.ignite_uia, timeout=8)
                return True
            except Exception as exc:  # noqa: BLE001
                say("[!] uia 点火失败: %s" % exc)
                return False
        return False


# ------------------------------------------------------------------ agent 托管
class AgentSupervisor:
    def __init__(self, args):
        self.args = args
        self.proc = None

    def ensure(self, has_target):
        if not self.args.agent:
            return
        if self.proc and self.proc.poll() is None:
            return
        if not has_target:
            return
        say("[*] 发现签到页，启动 agent")
        cmd = [sys.executable, self.args.agent, "--for", str(self.args.agent_rounds)]
        self.proc = subprocess.Popen(cmd, cwd=os.path.dirname(self.args.agent) or ".")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ------------------------------------------------------------------ 主循环
async def run(args):
    sup = ProxySupervisor(args.wmpf_dir, args.node, args.proxy_arg or [])
    ign = Igniter(args)
    agt = AgentSupervisor(args)

    if args.check_only:
        state, targets, why = await probe_channel()
        print("state = %s, targets = %d, reason = %s" % (state, len(targets), why))
        return 0 if state == CHANNEL_UP else (3 if state == PROXY_DOWN else 2)

    if not sup.ensure():
        if not port_open(CDP_PORT):
            say("[x] 代理起不来，退出")
            return 3

    last_state = None
    down_since = None
    started = time.time()
    try:
        while True:
            if args.run_for and (time.time() - started) >= args.run_for:
                say("[*] 达到 --run-for，正常退出")
                break
            state, targets, why = await probe_channel()
            if state != last_state:
                say("[=] %s (%s)%s" % (state, why,
                                       "  targets=%d" % len(targets) if targets else ""))
                if state == CHANNEL_UP:
                    down_since = None
                    ign.interval = args.ignite_min
                    ign.attempts = 0
                    ign.told = False
                    sup.reported = False
                elif state == CHANNEL_DOWN and down_since is None:
                    down_since = time.time()
                last_state = state

            if state == PROXY_DOWN:
                agt.stop()
                if args.no_proxy_spawn:
                    if not sup.reported:
                        sup.reported = True
                        say("[!] 62000 没在监听（--no-proxy-spawn）。请自己启动 src/index.ts。")
                elif sup.restarts >= args.max_restarts:
                    if not sup.reported:
                        sup.reported = True
                        say("[x] 代理已重启 %d 次，停止自动重启。请手动检查。" % sup.restarts)
                else:
                    died = sup.proc is not None and sup.proc.poll() is not None
                    if sup.ensure() and died:
                        sup.restarts += 1
                        say("[!] 代理已重启（第 %d 次）。注意：**9421 上的旧连接已断，需要重新触发一次小程序**——"
                            "把小程序切到后台再切回，或重新打开。" % sup.restarts)

            elif state == CHANNEL_DOWN:
                agt.stop()
                if not has_ignite(args):
                    ign.fire()          # 常驻模式：只提示一次，不操作界面
                elif down_since and (time.time() - down_since) >= args.grace and ign.due():
                    if ign.fire():
                        # 退避：失败次数越多间隔越长，封顶
                        ign.interval = min(ign.interval * 1.6, 150.0)
                        down_since = time.time()

            else:
                hit = any(args.match in t.get("url", "") for t in targets
                          if t.get("type") in ("page", "webview"))
                agt.ensure(hit)

            await asyncio.sleep(args.interval)

    except KeyboardInterrupt:
        say("[*] 退出")
    finally:
        agt.stop()
        sup.stop()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wmpf-dir", default=DEFAULT_WMPF)
    ap.add_argument("--node", default=DEFAULT_NODE)
    ap.add_argument("--proxy-arg", action="append", help="透传给 src/index.ts 的参数，可重复")
    ap.add_argument("--interval", type=float, default=5.0, help="状态探测间隔")
    ap.add_argument("--grace", type=float, default=15.0, help="断连后多久开始点火（仅显式配置点火时生效）")
    ap.add_argument("--ignite-min", type=float, default=25.0, help="两次点火最小间隔")
    ap.add_argument("--no-proxy-spawn", action="store_true", help="不自己拉起代理（代理由外部常驻运行）")
    ap.add_argument("--max-restarts", type=int, default=3, help="代理最多自动重启几次")
    ap.add_argument("--ignite-cmd", default="", help="点火 shell 命令")
    ap.add_argument("--ignite-deeplink", default="", help="小程序深链 weixin://...")
    ap.add_argument("--ignite-uia", default="", help="auth_refresher 模板名")
    ap.add_argument("--uia-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "2026-10-24-16-54-55", "weizhujiao-autosign-pc"))
    ap.add_argument("--agent", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "cdp_sign_agent.py"))
    ap.add_argument("--agent-rounds", type=int, default=0)
    ap.add_argument("--match", default="student/sign")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--run-for", type=float, default=0, help="跑 N 秒后正常退出（自测用）")
    args = ap.parse_args()

    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
