#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
known_offsets.py — 内置 hook 偏移库（离线可用的 RVA 快照）

为什么需要
  WMPFDebugger 的 --auto-detect 是拿静态特征去 flue.dll 里扫位置。微信偶尔
  会把特征改掉（换编译器、改结构、加壳），扫描从此命中不了，于是虽然版本号
  认得出来，却生成不了 addresses.<ver>.json，程序就卡在「偏移探测失败 →
  临时模式 / 通道迟迟不建立」，看起来像整个项目失效。

  但偏移本身只是一组 RVA，不是必须现场算出来的东西：只要有同一版本的一份
  已知值（自测探测成功、上游 PR、IDA 人工分析、别人给的 json），直接落盘就能
  用，完全不需要扫描。

  于是这里按 WMPF 版本内置快照，offsets.py 在扫描之前先查它：

      内置有该版本  -> 直接写 addresses.<ver>.json（离线、秒级、微信都不用开）
      内置没有      -> 退回原来的 --auto-detect

数据格式
  与上游 WMPFDebugger 的 frida/config/win32/addresses.<ver>.json 完全一致，
  原样落盘，不做任何字段改名或换算：
      Version / LoadStartHookOffset / CDPFilterHookOffset / CastToJsonHookOffset
      MiniAppConfigStructOffsets.{LaunchConfigOffsets, RemoteDebugConfigOffsets,
                                 SceneOffset, WebSocketURLStringOffset,
                                 RemoteDebugModeOffset}
  不加转换层是刻意的：转换一旦和上游漂移，frida 脚本就会按错位 RVA 打 patch，
  代价是 access violation，比「没有配置」糟得多。

新增一个版本
  1. 拿到该版本 flue.dll 的偏移（--auto-detect 自测成功 / 上游 PR / IDA）
  2. 在 BUILTIN 里加一项，键是 WMPF 版本号字符串（目录名，如 "26134"）
  3. python -c "import known_offsets as K; print(K.validate(K.get('26134')))"
  4. python offsets.py --check 应显示「内置偏移：26134（可直写）」
  详见 docs/OFFSETS.md。

用法
  import known_offsets as KO
  KO.has("26134")      # 有没有该版本
  KO.get("26134")      # 深拷贝一份配置（不会污染内置表）
  KO.latest()          # 最新内置版本号
  KO.validate(data)    # (ok, 说明)：字段齐不齐、RVA 是不是合理的十六进制
"""
import copy
import re

BUILTIN = {
    # WMPF 26134（微信 PC 4.1.x 对应运行时）
    # 来源：人工确认的 addresses.26134.json；上游 --auto-detect 在该版本已扫不出
    "26134": {
        "Version": 26134,
        "LoadStartHookOffset": "0x2DEDC70",
        "CDPFilterHookOffset": "0x39D4E80",
        "CastToJsonHookOffset": "0xBB418E0",
        "MiniAppConfigStructOffsets": {
            "LaunchConfigOffsets": [64, 1616, 8],
            "RemoteDebugConfigOffsets": [1544, 16],
            "SceneOffset": 480,
            "WebSocketURLStringOffset": 648,
            "RemoteDebugModeOffset": 748,
        },
    },
}

# 必备的三个 hook RVA 与结构体偏移字段。缺任何一项，frida 脚本要么打到错位置，
# 要么读结构体时越界，所以写盘前必须全部齐备。
REQUIRED_HOOKS = ("LoadStartHookOffset", "CDPFilterHookOffset", "CastToJsonHookOffset")
REQUIRED_STRUCT = ("LaunchConfigOffsets", "RemoteDebugConfigOffsets", "SceneOffset",
                   "WebSocketURLStringOffset", "RemoteDebugModeOffset")
# LaunchConfigOffsets 是三级指针链，RemoteDebugConfigOffsets 是两级
STRUCT_LEN = {"LaunchConfigOffsets": 3, "RemoteDebugConfigOffsets": 2}

_HEX_RE = re.compile(r"^0[xX][0-9a-fA-F]+$")
# flue.dll 只有 200 多 MB，RVA 落在 32 位内；超出这个范围一定是录错了
MAX_RVA = 0x100000000


def versions():
    """内置库覆盖的版本号（升序）。"""
    return sorted(BUILTIN.keys(), key=lambda v: int(v) if str(v).isdigit() else 0)


def latest():
    """最新内置版本号，没有则 None。"""
    vs = versions()
    return vs[-1] if vs else None


def version_text():
    """给日志用的一行说明，如 "26134" 或 "26134、26135"。"""
    return "、".join(versions()) or "空"


def has(version):
    return str(version) in BUILTIN


def get(version):
    """取该版本的配置副本；没有则 None。深拷贝，调用方改它不会污染内置表。"""
    data = BUILTIN.get(str(version))
    return copy.deepcopy(data) if data else None


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate(data):
    """检查一份 addresses 配置是否可用。返回 (ok, 说明)。

    只做结构与取值范围检查，不做「RVA 是否真的指向那个函数」的判断——
    后者要反汇编 flue.dll，属于人工确认的范畴（见 docs/OFFSETS.md）。
    但结构错（少字段、写成十进制、数组长度不对）在这里就能拦住，
    避免把半截配置写进 frida 目录后才在启动时崩。
    """
    if not isinstance(data, dict) or not data:
        return False, "配置不是非空对象"

    ver = data.get("Version")
    if ver is not None:
        try:
            int(ver)
        except (TypeError, ValueError):
            return False, "Version 不是数字：%r" % (ver,)

    for key in REQUIRED_HOOKS:
        v = data.get(key)
        if v is None:
            return False, "缺少 %s" % key
        if not isinstance(v, str) or not _HEX_RE.match(v):
            return False, "%s 不是 0x 开头的十六进制字符串：%r" % (key, v)
        n = int(v, 16)
        if not 0 < n < MAX_RVA:
            return False, "%s 的值越界：%s" % (key, v)

    struct = data.get("MiniAppConfigStructOffsets")
    if not isinstance(struct, dict):
        return False, "缺少 MiniAppConfigStructOffsets"
    for key in REQUIRED_STRUCT:
        if key not in struct:
            return False, "MiniAppConfigStructOffsets 缺少 %s" % key
    for key, ln in STRUCT_LEN.items():
        arr = struct.get(key)
        if not isinstance(arr, list) or len(arr) != ln:
            return False, "%s 应为长度 %d 的数组：%r" % (key, ln, arr)
        for item in arr:
            if not _is_int(item) or item < 0 or item > 0x10000:
                return False, "%s 里有不合法的偏移：%r" % (key, item)
    for key in ("SceneOffset", "WebSocketURLStringOffset", "RemoteDebugModeOffset"):
        item = struct.get(key)
        if not _is_int(item) or item < 0 or item > 0x10000:
            return False, "%s 不是合法的结构体偏移：%r" % (key, item)

    return True, "ok"


def summary():
    """CLI 用：[(版本, 是否通过校验, 说明)]。"""
    out = []
    for ver in versions():
        good, why = validate(BUILTIN[ver])
        out.append((ver, good, why))
    return out
