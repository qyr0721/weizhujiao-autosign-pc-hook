#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
offsets_test.py — 偏移版本自适应与失效自愈（纯 Python，不需要微信）

  python test/offsets_test.py

测什么
  · 版本变化（配置缺失）/ 指纹变化（同版本 flue.dll 被换）/ 配置损坏都能识别
  · 自动探测结果原子写回，带指纹元数据；成功后不再重复探测
  · 探测失败写 stale 标记并累加次数，达上限退回 --auto-detect 兜底
  · 只认明确的偏移错误，通道抖动不误判（误判会白白重启代理杀掉小程序）
  · ProxySupervisor 会按配置可用性增删 --auto-detect
  · wizard.preflight 不再把「WMPF 版本不符」当成致命错误
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import offsets as O          # noqa: E402
import channel_keeper as ck  # noqa: E402
import uikit as U            # noqa: E402
import wizard as W           # noqa: E402

PASS = 0
FAIL = 0
VER = "25799"


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s   %s" % (name, extra))


def make_env(tmp, cfg=None):
    """造一个隔离的 WMPFDebugger + WMPF 运行时目录。"""
    wmpf = os.path.join(tmp, "WMPFDebugger")
    root = os.path.join(tmp, "RadiumWMPF")
    dll = O.flue_dll_path(VER, root)
    os.makedirs(os.path.dirname(dll), exist_ok=True)
    with open(dll, "wb") as f:
        f.write(b"fake flue.dll")
    os.makedirs(O.config_dir(wmpf), exist_ok=True)
    path = O.config_path(wmpf, VER)
    if cfg is not None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(cfg if isinstance(cfg, str) else json.dumps(cfg))
    return wmpf, root, dll, path


def st_of(wmpf, root, version=VER):
    return O.state(wmpf, version=version, root=root)


def test_state_and_writeback():
    print("\n=== 状态判定与写回 ===")
    tmp = tempfile.mkdtemp(prefix="wzoff_")
    try:
        wmpf, root, dll, cfg = make_env(tmp)
        ok(O.state(None)["state"] == O.UNKNOWN, "没有 WMPFDebugger → UNKNOWN")
        ok(O.state(wmpf, version=None, versions=[])["state"] == O.UNKNOWN,
           "没有 WMPF 版本 → UNKNOWN")

        st = st_of(wmpf, root)
        ok(st["state"] == O.MISSING, "新版本还没有配置 → MISSING（微信更新后的典型情况）",
           st["state"])
        ok(st["config"].endswith("addresses.%s.json" % VER), "配置按版本命名", st["config"])

        # 手工放一份旧配置：没有元数据也要能用，但要补上指纹
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"OnLoadStart": 123}, f)
        st = st_of(wmpf, root)
        ok(st["state"] == O.OK and st["need_meta"], "旧配置可用且标记需补元数据",
           str(st["state"]))
        ok(O.ensure_meta(st) is True, "ensure_meta 会为旧配置补写指纹元数据")
        ok(st_of(wmpf, root)["need_meta"] is False, "补写后不再标记需补元数据")

        O.write_config(cfg, {"OnLoadStart": 456, "SendToClientFilter": 789},
                       cfg + O.META_SUFFIX, VER, O.fingerprint(VER, root))
        st = st_of(wmpf, root)
        ok(st["state"] == O.OK and not st["need_meta"], "写回后状态 OK", str(st["state"]))
        with open(cfg, encoding="utf-8") as f:
            ok(json.load(f)["OnLoadStart"] == 456, "配置内容已更新")
        with open(cfg + O.META_SUFFIX, encoding="utf-8") as f:
            meta = json.load(f)
        ok(meta["version"] == VER and meta["fingerprint"], "元数据带版本与指纹", str(meta))
        ok(not os.path.isfile(cfg + O.STALE_SUFFIX), "写回会清掉 stale 标记")

        # 同版本号下 flue.dll 被替换（微信热更新常见）
        time.sleep(0.02)
        with open(dll, "ab") as f:
            f.write(b"replaced")
        os.utime(dll, (time.time() + 5, time.time() + 5))
        st = st_of(wmpf, root)
        ok(st["state"] == O.STALE and "flue.dll" in st["reason"],
           "同版本但 dll 被换 → STALE", str(st["reason"]))

        O.write_config(cfg, {"OnLoadStart": 1}, cfg + O.META_SUFFIX,
                       VER, O.fingerprint(VER, root))
        O.mark_stale(cfg + O.STALE_SUFFIX, "测试标记")
        ok(st_of(wmpf, root)["state"] == O.STALE, "stale 标记优先于其它判定")
        O.clear_stale(cfg + O.STALE_SUFFIX)
        ok(st_of(wmpf, root)["state"] == O.OK, "清掉标记后恢复 OK")

        with open(cfg, "w", encoding="utf-8") as f:
            f.write("{broken json")
        ok(st_of(wmpf, root)["state"] == O.STALE, "配置损坏 → STALE")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({}, f)
        ok(st_of(wmpf, root)["state"] == O.STALE, "空配置 → STALE")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_parse_and_signals():
    print("\n=== 探测输出解析与失效信号 ===")
    ok(O.parse_detect_text("[info] booting\ndetected hook offsets: {\"a\": 1}\n")
       == {"a": 1}, "解析 detected hook offsets")
    ok(O.parse_detect_text("no marker here") is None, "没有标记行返回 None")
    ok(O.parse_detect_text("detected hook offsets: {broken") is None, "坏 JSON 不抛异常")
    ok(O.parse_detect_text("detected hook offsets: []") is None, "非 dict 不收")

    ok(O.failure_reason("[frida] version config not found") is not None,
       "识别缺版本配置")
    ok(O.failure_reason("Interceptor.replace failed: invalid address") is not None,
       "识别 frida 挂钩失败")
    ok(O.failure_reason("EXCEPTION_ACCESS_VIOLATION at 0x...") is not None,
       "识别写入错位地址导致的崩溃")
    ok(O.failure_reason("detected hook offsets: {\"a\": 1}") is None,
       "探测成功行不算失效")
    ok(O.failure_reason("CHANNEL_DOWN no-reply") is None, "通道无应答不算偏移失效")
    ok(O.failure_reason("ws-err: connection reset") is None, "WS 抖动不算偏移失效")
    ok(O.failure_reason("[miniapp] miniapp client connected") is None,
       "小程序连接事件不算偏移失效")


def test_recover():
    print("\n=== 自动探测与失败兜底 ===")
    tmp = tempfile.mkdtemp(prefix="wzoff2_")
    orig_detect = O.detect
    try:
        wmpf, root, dll, cfg = make_env(tmp)

        calls = []

        def good_detect(node, wmpf_dir, log=None, timeout=90, version=None):
            calls.append(version)
            return {"OnLoadStart": 1}, "ok"

        O.detect = good_detect
        result, ver, why = O.recover_if_needed(
            wmpf, "fake-node", version=VER, root=root)
        ok(result == "detected" and ver == VER, "版本变化 → 自动探测并写回", str((result, why)))
        ok(len(calls) == 1, "只探测一次")
        ok(os.path.isfile(cfg), "配置文件已落盘")
        result2, _, _ = O.recover_if_needed(
            wmpf, "fake-node", version=VER, root=root)
        ok(result2 == "ok" and len(calls) == 1, "配置可用后不再重复探测", str(result2))

        # 制造运行时失效，然后让探测连续失败
        O.mark_stale(cfg + O.STALE_SUFFIX, "模拟代理报错")
        O.detect = lambda *a, **k: (None, "模拟探测失败")
        r1, _, _ = O.recover_if_needed(wmpf, "fake-node", version=VER, root=root)
        r2, _, _ = O.recover_if_needed(wmpf, "fake-node", version=VER, root=root)
        r3, _, why3 = O.recover_if_needed(wmpf, "fake-node", version=VER, root=root)
        ok(r1 == "failed" and r2 == "failed", "失败时返回 failed", str((r1, r2)))
        ok(os.path.isfile(cfg + O.STALE_SUFFIX), "失败会留下 stale 标记")
        ok(O.read_fails(cfg + O.FAILS_SUFFIX) == O.MAX_FAILS, "失败次数被记录")
        ok(r3 == "defer" and "auto-detect" in why3,
           "达到上限后退回 --auto-detect 兜底，不再死循环", str((r3, why3)))

        # force 用于 CLI 手动重试，不受连续失败限制
        O.detect = good_detect
        r4, _, _ = O.recover_if_needed(wmpf, "fake-node", version=VER,
                                       root=root, force=True)
        ok(r4 == "detected", "--detect 强制重试可突破失败上限", str(r4))
        ok(O.read_fails(cfg + O.FAILS_SUFFIX) == 0, "成功后失败计数清零")
    finally:
        O.detect = orig_detect
        shutil.rmtree(tmp, ignore_errors=True)


def test_observe_and_proxy():
    print("\n=== 代理日志观察与启动参数 ===")
    tmp = tempfile.mkdtemp(prefix="wzoff3_")
    try:
        wmpf, root, dll, cfg = make_env(tmp, cfg={"OnLoadStart": 1})
        O.write_config(cfg, {"OnLoadStart": 1}, cfg + O.META_SUFFIX,
                       VER, O.fingerprint(VER, root))
        st = st_of(wmpf, root)
        ok(O.observe_proxy_line("miniapp client connected", wmpf_dir=wmpf,
                                version=VER) is False, "普通日志不触发")
        ok(O.observe_proxy_line("version config not found", wmpf_dir=wmpf,
                                version=VER) is True, "偏移错误被识别")
        ok(os.path.isfile(st["stale"]), "失效信号落盘为 stale 标记")
        ok(st_of(wmpf, root)["state"] == O.STALE, "下一轮状态检查能看到失效")
        O.clear_stale(st["stale"])

        ok(O.needs_runtime_autodetect(wmpf, version=VER) is False,
           "配置可用时不需要临时 auto-detect")
        sup = ck.ProxySupervisor(wmpf, "fake-node", version=VER)
        ok("--auto-detect" not in sup.extra, "构造时不加 --auto-detect")
        os.remove(cfg)
        ok(O.needs_runtime_autodetect(wmpf, version=VER) is True,
           "配置缺失时需要临时 auto-detect")
        sup.refresh_args()
        ok("--auto-detect" in sup.extra, "refresh_args 会补上兜底参数")
        O.write_config(cfg, {"OnLoadStart": 2}, cfg + O.META_SUFFIX,
                       VER, O.fingerprint(VER, root))
        sup.refresh_args()
        ok("--auto-detect" not in sup.extra, "写回后会去掉兜底参数，秒注入")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_preflight_no_longer_blocks_new_version():
    print("\n=== 版本不符不再阻断启动（用户痛点） ===")
    tmp = tempfile.mkdtemp(prefix="wzoff4_")
    saved = (U.list_processes, U.wechat_version, U.find_wmpf_versions,
             U.resolve_wmpf_dir, U.resolve_node)
    try:
        wmpf, root, dll, cfg = make_env(tmp)
        U.list_processes = lambda names: {"wechatappex.exe": ["C:/fake/WeChatAppEx.exe"]}
        U.wechat_version = lambda: ("4.9.9.99", "C:/fake/Weixin.exe")
        U.find_wmpf_versions = lambda: [25888]       # 不是内置的 25799
        U.resolve_wmpf_dir = lambda base, extra=None: wmpf
        U.resolve_node = lambda base: "C:/fake/node.exe"

        info, fixes, wd, nd = W.preflight(base=tmp)
        ok(info["ok"] is True, "版本不符时自检仍为就绪", str(info["note"]))
        ok("25888" in str(info["wmpf"]), "面板显示实际版本", str(info["wmpf"]))
        ok(any("自动探测" in n for n in info.get("notes", [])),
           "提示将自动探测偏移", str(info.get("notes")))
        ok(not any("版本不符" in p for p in fixes),
           "不再要求用户降级/锁定微信版本", str(fixes))
        ok(not any("WeChatWin" in p for p in fixes),
           "不再提示安装指定版本微信", str(fixes))
    finally:
        (U.list_processes, U.wechat_version, U.find_wmpf_versions,
         U.resolve_wmpf_dir, U.resolve_node) = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_async_hooks():
    print("\n=== 启动 / 运行时自愈钩子 ===")
    import argparse
    import asyncio
    import run as runmod

    logs, calls = [], []
    orig = (O.state, O.recover_if_needed, ck.port_open)

    class FakeSup:
        proc = None

        def __init__(self):
            self.refreshed = False

        def refresh_args(self):
            self.refreshed = True
            return []

    def fake_recover(wmpf_dir, node, log=None, **kw):
        calls.append((wmpf_dir, node))
        return "detected", "25888", "已写入"

    args = argparse.Namespace(wmpf_dir="fake-wmpf", node="fake-node")
    try:
        O.state = lambda wmpf_dir, **kw: {
            "state": O.MISSING, "version": "25888", "reason": "模拟版本变化"}
        O.recover_if_needed = fake_recover
        ck.port_open = lambda port: False

        sup = FakeSup()
        asyncio.run(runmod._startup_offsets(args, logs.append, sup))
        ok(len(calls) == 1, "启动阶段配置缺失会触发一次探测")
        ok(sup.refreshed, "探测后刷新代理启动参数（去掉多余的 --auto-detect）")
        ok(any("已自动更新" in m for m in logs), "日志写明偏移已自动更新", str(logs))

        calls.clear()
        logs.clear()
        O.state = lambda wmpf_dir, **kw: {"state": O.OK, "reason": "ok"}
        asyncio.run(runmod._startup_offsets(args, logs.append, sup))
        ok(len(calls) == 0, "配置可用时启动不再重复探测")

        O.state = lambda wmpf_dir, **kw: {
            "state": O.STALE, "version": "25888", "reason": "模拟代理报偏移错误"}
        sup2 = FakeSup()
        asyncio.run(runmod._recover_offsets_runtime(
            args, logs.append, sup2, O.state("x")))
        ok(len(calls) == 1, "运行时失效会触发重探")
        ok(any("外部托管" in m for m in logs),
           "外部托管的代理不被强杀，只提示手动重启", str(logs))
    finally:
        O.state, O.recover_if_needed, ck.port_open = orig


def main():
    test_state_and_writeback()
    test_parse_and_signals()
    test_recover()
    test_observe_and_proxy()
    test_preflight_no_longer_blocks_new_version()
    test_async_hooks()
    print("\n%d 通过，%d 失败" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
