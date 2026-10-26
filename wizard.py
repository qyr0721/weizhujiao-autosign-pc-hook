#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wizard.py — wxsign.exe 的源码：面板式启动器

面板只显示：程序名 / 作者 / 项目地址 / 微信版本 / 自检 / 正在做什么 / 签到结果。
其余全部写日志（程序目录 logs\\wxsign-YYYYMMDD-HHMMSS.log），不污染界面。

用法
  wxsign.exe               常驻
  wxsign.exe --check       只自检
  wxsign.exe --plain       不要颜色/重绘（重定向到文件时自动降级）
"""
import argparse
import asyncio
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    HERE = os.path.dirname(sys.executable)

import channel_keeper as ck          # noqa: E402
import get_openid as g               # noqa: E402
import installer                     # noqa: E402
import run as runmod                 # noqa: E402
import signcore                      # noqa: E402
import uikit as U                    # noqa: E402

APP, VER, AUTHOR, REPO = U.APP, U.VER, U.AUTHOR, U.REPO
INNER = 58


# ================================================================ 面板
class Panel:
    """标题用闭合边框，正文开放布局。

    正文会出现 ✓ · → 这类「东亚宽度歧义」字符，不同终端按 1 或 2 列渲染，
    加右边框必然在某些机器上歪一列。标题行只用中英文数字（宽度确定），
    所以框是准的；正文左对齐，怎么渲染都整齐。
    """

    def __init__(self, term):
        self.t = term
        self.view = U.Redraw(term)

    def compose(self, info):
        t = self.t
        self._dyn = {"action": info["action"], "sign": info["sign"] or "—"}
        rows = U.title_box(t, [
            ("%s  %s" % (APP, VER), t.BOLD),
            ("by %s" % AUTHOR, t.DIM),
            (REPO, t.DIM),
        ], INNER)
        rows.append("")

        ver = "  微信 %s   ·   WMPF %s" % (info["wechat"], info["wmpf"])
        rows.append(t.c(ver, t.YELLOW if not info["wmpf_ok"] else None))
        if info["ok"]:
            rows.append("  自检  " + t.c("正常", t.GREEN))
        else:
            rows.append("  自检  " + t.c("异常", t.RED) + "  " + t.c(info["note"], t.DIM))
        rows.append("")

        # 首次使用：微信里还没有可复用的会话，必须先手动授权一次。
        # 只提示一次，拿到号之后 need_auth 会被清掉。
        if info.get("need_auth"):
            rows.append(t.c("  ⚠ 首次使用，需要先手动授权一次", t.YELLOW))
            rows.append(t.c("    否则取不到凭证，会一直停在「等待取号」", t.YELLOW))
            rows.append("")
            for step in AUTH_STEPS:
                rows.append("    " + step)
            rows.append("")
            rows.append(t.c("    完成后不必重启，程序会自动继续。", t.DIM))
            rows.append("")

        rows.append(U.rule(t, INNER))
        rows.append("")

        rows.append(t.c("  │ ", t.DIM) + t.c(info["action"], t.CYAN))
        rows.append("")
        for hint in info["hint"]:
            rows.append(t.c("    " + hint, t.DIM))
        rows.append("")
        rows.append(U.rule(t, INNER))

        sign, kind = info["sign"], info["sign_kind"]
        color = {"ok": t.GREEN, "dup": t.DIM, "fail": t.RED}.get(kind)
        rows.append("  " + t.c("签到", t.DIM) + "  " + t.c(sign or "—", color))
        rows.append(U.rule(t, INNER))
        rows.append("")

        rows.append("  " + t.c("日志", t.DIM) + "  " + t.c(info["logfile"], t.DIM))
        rows.append(t.c("  Ctrl+C 退出，不影响正在运行的小程序", t.DIM))
        return rows

    def render(self, info):
        rows = self.compose(info)
        if not self.t.plain:
            self.view.draw(rows)
            return
        # 无 ANSI：只输出变化的那一行
        dyn = getattr(self, "_dyn", {"action": "", "sign": ""})
        prev = getattr(self, "_plain_prev", {"action": None, "sign": None})
        out = []
        if dyn["action"] != prev["action"]:
            out.append("  ▸ %s" % dyn["action"])
        if dyn["sign"] != prev["sign"]:
            out.append("  签到  %s" % dyn["sign"])
        self._plain_prev = dict(dyn)
        if out:
            print("\n".join(out))
            sys.stdout.flush()


# ================================================================ 环境自检
def preflight(base=HERE):
    """返回 (info, fixes, wmpf_dir, node)。info 是面板需要的一切。"""
    procs = U.list_processes({"wechatappex.exe"})
    browsers = procs.get("wechatappex.exe") or []

    wx_ver, _ = U.wechat_version()
    vers = U.find_wmpf_versions()
    wmpf = vers[-1] if vers else None
    wmpf_ok = (wmpf == U.KNOWN_VERSION)

    # ck.DEFAULT_WMPF 带本机的兜底路径，源码运行时用得上；
    # 用 exe 的话，setup 会把 WMPFDebugger 装到 exe 同目录，同样能找到。
    wmpf_dir = U.resolve_wmpf_dir(base, extra=ck.DEFAULT_WMPF)
    node = U.resolve_node(base)

    problems, fixes = [], []
    if not browsers:
        problems.append("微信未运行")
        fixes.append("先启动并登录微信 PC 版，然后再运行本程序")
    if not vers:
        problems.append("缺 WMPF 运行时")
        fixes.append("微信未初始化 WMPF（随便打开一次小程序即可生成），或微信版本过旧")
    elif not wmpf_ok:
        problems.append("WMPF 版本不符")
        fixes.append("当前 WMPF %s，本项目偏移基于 %s。请安装 Release 里的 "
                     "WeChatWin_4.1.15.exe 并关闭自动更新" % (wmpf, U.KNOWN_VERSION))
    if not wmpf_dir:
        problems.append("缺 WMPFDebugger")
        fixes.append("缺少 WMPFDebugger 或其依赖，程序将尝试自动部署")
    if not node:
        problems.append("缺 Node")
        fixes.append("缺少 Node 运行时，程序将尝试自动部署；亦可手动安装 Node 22+")

    info = {
        "wechat": wx_ver or "未检测到",
        "wmpf": str(wmpf) if wmpf else "未检测到",
        "wmpf_ok": wmpf_ok,
        "ok": not problems,
        "note": "全部就绪" if not problems else "、".join(problems),
        "action": "准备中…",
        "hint": [],
        "sign": "",
        "sign_kind": "none",
        "logfile": "",
    }
    return info, fixes, wmpf_dir, node


# ================================================================ 状态
class State:
    """把 run.py / signcore 的日志翻译成面板上的两句话。"""

    def __init__(self, info):
        self.info = dict(info)
        self.channel = False
        self.hint_idle = ["在微信打开「小程序」面板，搜索「小程序示例」",
                          "微信官方示例，体积很小；打开后可缩到后台"]
        self.hint_live = ["通道已建立，无需再操作",
                          "小程序可以缩到后台或最小化"]
        self.info["hint"] = self.hint_idle

    def snapshot(self):
        return self.info

    def set_action(self, text):
        self.info["action"] = text

    def set_sign(self, text, kind):
        self.info["sign"] = text
        self.info["sign_kind"] = kind

    def set_channel(self, up):
        if up == self.channel:
            return False
        self.channel = up
        self.info["hint"] = self.hint_live if up else self.hint_idle
        return True

    def set_need_auth(self, need):
        """拿到号之后把首次授权提示撤掉。"""
        if self.info.get("need_auth") == need:
            return False
        self.info["need_auth"] = need
        return True

    def feed(self, msg):
        """返回 True 表示面板需要重绘。"""
        m = msg.strip()

        if "[签到]" in m:
            return self._feed_sign(m)

        if "auto-detecting hook offsets" in m:
            self.set_action("正在注入调试 hook…（约 10 秒，此期间勿开小程序）")
        elif "you can now open any miniapps" in m or "script loaded" in m:
            self.set_action("调试 hook 已注入，现在可以打开小程序")
        elif "代理就绪" in m or "拉起代理" in m:
            self.set_action("调试代理已就绪")
        elif "代理没在监听" in m:
            self.set_action("调试代理未运行，正在拉起…")
        elif "代理起不来" in m:
            self.set_action("调试代理启动失败，稍后重试")
        elif "通道未就绪" in m:
            self.set_channel(False)
            self.set_action("等待小程序连接")
        elif "正常值守" in m:
            self.set_channel(True)
            self.set_need_auth(False)
            oid = _oid_in(m)
            self.set_action("监视中" + (" · 号 %s" % oid[:8] if oid else ""))
        elif "需要取号" in m:
            self.set_action("正在取号…（不要关闭小程序）")
        elif "拿到新号" in m:
            oid = _oid_in(m)
            self.set_channel(True)
            self.set_need_auth(False)
            self.set_action("换号成功" + (" · 号 %s" % oid[:8] if oid else ""))
        elif "S0 命中" in m:
            self.set_need_auth(False)
            self.set_action("已取得凭证…")
        elif "本轮失败" in m:
            self.set_action("取号失败，稍后重试")
        elif "冷却中" in m:
            self.set_action("正在重新取号…")
        elif "签到侧退出" in m:
            self.set_action("签到服务重启中…")
        elif "启动签到侧" in m or "签到侧已启动" in m:
            self.set_action("签到服务已启动")
        elif "查询失败" in m:
            self.set_action("查询失败，重试中…")
        elif "还没有 openid" in m:
            # 首次使用且尚未授权时，别只说「等待取号」——用户不知道该干什么
            if self.info.get("need_auth"):
                self.set_action("等待手动授权（见上方步骤）")
            else:
                self.set_action("等待取号…")
        else:
            return False
        return True

    def _feed_sign(self, m):
        now = time.strftime("%H:%M:%S")
        # 签到侧的日志都带 [签到] 前缀，所以「还没有 openid」只会走到这里。
        # 首次使用时要明确告诉用户去手动授权，而不是干等。
        if "还没有 openid" in m:
            if self.info.get("need_auth"):
                self.set_action("等待手动授权（见上方步骤）")
            else:
                self.set_action("等待取号…")
            return True
        # 401 / 失效要先判：它的文本里不会出现"签到成功"，但可能带 signId
        if "失效" in m or "401" in m:
            if self.channel:
                self.set_action("号已失效，正在换号…")
            else:
                self.set_action("等待小程序连接（号已失效，连上后自动换号）")
            return True
        if "签到成功" in m:
            self.set_sign("%s  签到成功" % now, "ok")
            self.set_action("刚刚签到成功")
        elif "已签到（幂等）" in m or "已经签到" in m:
            self.set_sign("%s  已签到（之前签过）" % now, "dup")
            self.set_action("监视中")
        elif "签到失败" in m:
            self.set_sign("%s  签到失败" % now, "fail")
            self.set_action("签到失败，等待下一次")
        elif "退出 code" in m:
            self.set_action("签到服务重启中…")
        else:
            return False
        return True


def _oid_in(text):
    import re
    m = re.search(r"\b([0-9a-f]{32})\b", text)
    return m.group(1) if m else ""


def _has_valid_oid(path):
    """openid.txt 里是否已有一个 32 位 hex 的号。

    用来判断「首次使用」：没有号就说明这台机器还没完成过那次手动授权，
    微信客户端里没有可复用的会话，程序再怎么取号也拿不到。
    """
    import re
    try:
        with open(path, "r", encoding="utf-8") as f:
            first = f.readline().strip()
    except OSError:
        return False
    return bool(re.fullmatch(r"[0-9a-f]{32}", first))


AUTH_STEPS = [
    "1. 打开微信「微助教服务号」，找到老师发的签到通知",
    "2. 右键点击通知卡片，在弹出菜单中点「签到(C)」",
    "3. 跳转到授权页后，点「确认授权」",
]


# ================================================================ 签到线程
class Signer:
    """进程内跑签到核心；401 时置位事件让主循环续号，续完再自动拉起。"""

    def __init__(self, openid_file, cfg, log_fn):
        self.openid_file = openid_file
        self.cfg = cfg
        self.log = log_fn
        self.stale = threading.Event()
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        bad_oid = None
        while not self.stop.is_set():
            code = signcore.sign_loop(self.cfg, self.openid_file,
                                      stale_event=self.stale,
                                      log=lambda m: self.log("[签到] %s" % m),
                                      stop=self.stop)
            if self.stop.is_set():
                return
            if code == 2:
                bad_oid = signcore.read_openid(self.openid_file)
                self.log("[签到] 号失效，等续号…")
            else:
                self.log("[签到] 退出 code=%s，5s 后重试" % code)
                time.sleep(5)
            deadline = time.time() + 90
            while not self.stop.is_set() and time.time() < deadline:
                cur = signcore.read_openid(self.openid_file)
                if cur and cur != bad_oid:
                    break
                time.sleep(2)

    def take_stale(self):
        if self.stale.is_set():
            self.stale.clear()
            return True
        return False

    def halt(self):
        self.stop.set()


# ================================================================ 自动部署
def auto_setup(log, state, panel, term):
    """部署缺失的运行组件（便携版行为）。

    返回 True 表示 Node 与 WMPFDebugger 现已齐备，可以继续启动。
    偏移配置失败不视为致命：channel_keeper 会在缺配置时自动附加 --auto-detect。
    """
    log.write("运行环境不完整，开始自动部署")
    state.info["hint"] = ["正在检查缺失组件…"]
    state.set_action("准备部署运行环境…")
    panel.render(state.snapshot())

    holder = {}

    def notify(action, progress=""):
        state.set_action(action + ("  " + progress if progress else ""))
        if holder.get("setup"):
            state.info["hint"] = [
                "    %s %s %s" % (m, U.fit(lbl, 16), det)
                for m, lbl, det in holder["setup"].summary()]
        panel.render(state.snapshot())

    setup = installer.Setup(log, HERE, notify=notify)
    holder["setup"] = setup
    setup.run()

    done = bool(U.resolve_node(HERE)
                and U.resolve_wmpf_dir(HERE, extra=ck.DEFAULT_WMPF))
    log.write("自动部署结束：Node 与 WMPFDebugger %s"
              % ("可用" if done else "仍不可用"))
    if not done:
        state.info["hint"] = [
            "    %s %s %s" % (m, U.fit(lbl, 16), det)
            for m, lbl, det in setup.summary()]
    return done


# ================================================================ 偏移配置补全
def _ensure_offsets_once(log, state, panel, wmpf_dir, node):
    """缺偏移配置就补一次（非致命）。

    仓库自带的配置只覆盖旧版本；本机 WMPF 25799 没有配置时，代理会带
    --auto-detect 启动，注入要多等约 12 秒，而且这 12 秒里打开小程序连不上。
    所以启动时只要发现配置缺失就探测一次并写回，下次启动即可秒注入。
    """
    try:
        st = installer.Setup(log, HERE, notify=lambda a, p="": None)
        st.wmpf_dir = wmpf_dir
        st.wmpf = str(U.find_wmpf_versions()[-1]) if U.find_wmpf_versions() else "未检测到"
        st.node = node                      # 不设这个，join(cmd) 会炸在 None
        cfg = st._offset_path()
        if not cfg or os.path.isfile(cfg):
            return
        if not U.list_processes({"wechatappex.exe"}):
            log.write("偏移配置缺失，且微信未运行——本次将用 --auto-detect（注入慢约 12 秒）")
            return
        state.set_action("探测 hook 偏移（约 10 秒，仅首次）…")
        panel.render(state.snapshot())
        if st.ensure_offsets():
            log.write("偏移配置已生成，下次启动注入更快")
    except Exception as exc:  # noqa: BLE001
        log.write("补偏移配置失败（不影响运行）：%s" % exc)


# ================================================================ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只做环境自检")
    ap.add_argument("--plain", action="store_true", help="纯文本输出（不用颜色/重绘）")
    ap.add_argument("--no-sign", dest="with_sign", action="store_false")
    ap.add_argument("--dry-run", action="store_true", help="签到只打印不提交")
    ap.add_argument("--url", default=g.DEFAULT_URL)
    ap.add_argument("--out", default=os.path.join(HERE, "openid.txt"))
    ap.add_argument("--config", default=os.path.join(HERE, "signer", "config.json"))
    ap.add_argument("--no-pause", action="store_true", help="退出时不等待回车")
    ap.add_argument("--no-setup", action="store_true", help="不自动部署环境")
    ap.set_defaults(with_sign=True)
    args = ap.parse_args()

    term = U.Term(plain=args.plain, title="%s %s" % (APP, VER))
    log = U.FileLog(U.log_dir(HERE))
    panel = Panel(term)

    info, fixes, wmpf_dir, node = preflight()
    # 没有号 = 这台机器还没完成过那次手动授权。取号本身依赖微信客户端里
    # 已有的会话，没有它再怎么点也拿不到，所以启动就要说清楚。
    info["need_auth"] = not _has_valid_oid(args.out)

    ck.OUT = lambda m: log.write(m, "[ck] ")

    state = State(info)
    if info["need_auth"] and info["ok"]:
        state.set_action("首次使用——按上方步骤在微信里授权一次")
    else:
        state.set_action("环境自检完成" if info["ok"] else "环境未就绪")
    # 代理输出同时喂给面板：hook 注入有最长约 12 秒的窗口（--auto-detect），
    # 这期间打开小程序连不上，必须让面板说清楚「什么时候才能开」。
    ck.ProxySupervisor.SINK = lambda line: _on_event("[proxy] " + line, state, panel, log)
    state.info["logfile"] = log.short if log.ok else "（日志不可写）"

    if term.plain:
        print("%s %s   by %s" % (APP, VER, AUTHOR))
        print(REPO)
        print()
        print("微信 %s   ·   WMPF %s" % (info["wechat"], info["wmpf"]))
        print("自检  %s" % ("正常" if info["ok"] else "异常  " + info["note"]))
        print()
        if info["need_auth"]:
            print("⚠ 首次使用，需要先手动授权一次")
            print("  否则取不到凭证，会一直停在「等待取号」")
            print()
            for step in AUTH_STEPS:
                print("  " + step)
            print()
            print("  完成后不必重启，程序会自动继续。")
            print()
    else:
        sys.stdout.write(term.clear_screen())
    panel.render(state.snapshot())

    if info["need_auth"]:
        log.write("首次使用：还没有 openid，需在微信里手动授权一次")
        for step in AUTH_STEPS:
            log.write("  " + step)

    log.write("=" * 56)
    log.write("%s %s  by %s" % (APP, VER, AUTHOR))
    log.write("微信 %s / WMPF %s / 自检 %s" % (info["wechat"], info["wmpf"], info["note"]))
    log.write("WMPFDebugger=%s  Node=%s" % (wmpf_dir or "-", node or "-"))
    log.write("=" * 56)

    if args.check:
        _finish(term, args, "自检完成：%s" % info["note"])
        return 0 if info["ok"] else 1

    # 环境不完整时先自动部署（便携版行为：一个 exe，缺什么补什么）
    if not info["ok"] and not args.no_setup:
        deployed = auto_setup(log, state, panel, term)
        if deployed:
            info, fixes, wmpf_dir, node = preflight()
            state.info.update({k: v for k, v in info.items()
                               if k in ("wechat", "wmpf", "wmpf_ok", "ok", "note")})
            state.info["hint"] = state.hint_idle
            state.set_action("环境部署完成" if info["ok"] else "环境仍未就绪")
            panel.render(state.snapshot())

    if not info["ok"]:
        _show_fixes(term, panel, fixes)
        log.write("环境未就绪，退出")
        _finish(term, args, "请先处理上面的事项（或运行 wxsign-setup.exe）")
        return 1

    # 偏移配置是「非致命」项——Node 与 WMPFDebugger 齐了 preflight 就算通过，
    # 于是自动部署再也不会跑，ensure_offsets() 永远没机会执行，配置永远缺，
    # 每次启动都要 --auto-detect（注入多等约 12 秒）。
    # 这里只要配置缺失就单独补一次，失败不阻塞启动。
    if not args.no_setup and wmpf_dir and node:
        _ensure_offsets_once(log, state, panel, wmpf_dir, node)

    state.set_action("启动签到服务…")
    panel.render(state.snapshot())

    signer = None
    if args.with_sign:
        cfg = signcore.load_cfg(args.config, warn=lambda m: log.write(m, "[cfg] "))
        if args.dry_run:
            cfg["submit"] = False
        signer = Signer(args.out, cfg, lambda m: _on_event(m, state, panel, log))
        signer.start()
        log.write("签到侧已启动（%d 秒轮询）" % int(cfg.get("poll", 5)))
    else:
        log.write("只取号模式（--no-sign）")

    ns = argparse.Namespace(
        url=args.url, out=args.out, poll=5.0, cooldown=0.0, observe=18.0,
        check=False, once=False, with_sign=False,
        wmpf_dir=wmpf_dir, node=node,
    )

    try:
        asyncio.run(runmod.run(ns, lambda m: _on_event(m, state, panel, log),
                               force_check=(signer.take_stale if signer else None)))
    except KeyboardInterrupt:
        log.write("用户中断（Ctrl+C）")
    finally:
        if signer:
            signer.halt()

    _finish(term, args, "已停止。调试代理仍在运行，小程序不受影响")


def _on_event(msg, state, panel, log):
    log.write(msg)                       # 全部进日志
    try:
        if state.feed(msg):              # 只有需要变化时才重绘
            panel.render(state.snapshot())
    except Exception:                    # 面板出错不能影响主流程
        pass


def _show_fixes(term, panel, fixes):
    """自检失败时，在面板下方直接给出修复步骤。"""
    if not fixes:
        return
    print()
    for i, line in enumerate(fixes, 1):
        print("  %d. %s" % (i, line))
    print()


def _finish(term, args, message):
    print()
    print("  " + term.c(message, term.DIM))
    if FROZEN and not args.no_pause:
        try:
            input("\n  按回车退出…")
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    U.fix_console()
    sys.exit(main())
