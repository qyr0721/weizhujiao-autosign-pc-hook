#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
offsets.py — WMPF 偏移配置的版本自适应与失效自愈

为什么需要
  WMPFDebugger 靠 frida 往 flue.dll 的固定 RVA 打 patch，配置按版本分文件：

      <WMPFDebugger>/frida/config/win32/addresses.<WMPF版本>.json

  微信更新会换掉 WMPF 运行时目录（版本号变），有时甚至在同一目录里直接替换
  flue.dll，旧 RVA 随即失效。原来的做法把 25799 写成硬门槛：版本不符就报
  「环境未就绪」直接退出，连已经写好的自动探测代码都执行不到。

  这里把「版本」从门槛降级为**配置文件的 key**：

    1. 启动时算当前版本 + flue.dll 指纹，检查对应配置是否可用
    2. 缺失 / 指纹不符 / 配置损坏 / 被标记失效 -> 自动跑一次 --auto-detect
    3. 探测结果原子写回，附带版本与指纹元数据（单独 sidecar，不改上游格式）
    4. 运行中代理输出明确的偏移错误 -> 写 stale 标记，主循环下一轮再探一次
    5. 连续失败达上限就停止折腾，退回上游 --auto-detect 临时模式继续可用

  探测能力本身来自 WMPFDebugger：
      node node_modules/tsx/dist/cli.mjs src/index.ts --auto-detect
  它往 stdout 打一行 `detected hook offsets: {...json...}`，本模块负责
  限时读取、解析、落盘。不需要微信 SDK，不需要 IDA。

  但扫描有失效的时候：微信改了特征，--auto-detect 就再也命中不了。此时
  「没有偏移」并不等于「拿不到偏移」——偏移只是一组 RVA，已知版本的数值
  可以直接写。所以在探测之前先查 known_offsets.py 的内置库：

      内置有该版本 -> 直接落盘（离线、秒级、微信不用开）
      内置没有     -> 跑 --auto-detect（原行为）
      内置那份用过后仍被判定失效 -> 不再重复写同一份，交给探测

  这一条是「扫描不灵」时的唯一出路，所以顺序是内置优先，而不是探测优先。

  注意：a8key 观测钩子（tools/probe_a8key.py 里的 DEFAULT_HOOKS）是另一套
  RVA，只用于调试观测、不参与签到。它的重扫很慢（要静态分析 237MB 的
  flue.dll），所以不放进启动流程，用 `python offsets.py --rescan-a8key` 显式做，
  结果写成 a8key_hooks.<ver>.json 由 probe_a8key.py 自动按版本加载。

用法
  python offsets.py --check             # 只看当前偏移状态
  python offsets.py --detect            # 强制重探主偏移（忽略冷却，跳过内置库）
  python offsets.py --seed              # 用内置偏移库直接写入当前版本（不扫描）
  python offsets.py --import f.json     # 导入外部 addresses json
  python offsets.py --list-builtin      # 内置库覆盖了哪些版本
  python offsets.py --rescan-a8key      # 重扫 a8key 观测钩子 RVA
"""
import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

import known_offsets as KO

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "tools")

OK = "ok"              # 该版本的配置可用
MISSING = "missing"    # 该版本还没有配置（微信更新后的典型情况）
STALE = "stale"        # 有配置但判定已失效
UNKNOWN = "unknown"    # 拿不到 WMPF 版本 / WMPFDebugger 路径

DETECT_MARKER = "detected hook offsets:"
META_SUFFIX = ".meta.json"
STALE_SUFFIX = ".stale"
FAILS_SUFFIX = ".fails.json"
MAX_FAILS = 2          # 连续失败到这个次数就不在每轮循环里反复重探了

BUILTIN_SOURCE = "builtin"   # 元数据里标记「这份配置来自内置偏移库」
# 这些来源的配置已经有人认真确认过（扫描成功 / 人工导入），
# 即便后来被判失效也不能拿内置值覆盖：覆盖会掩盖真实原因，还可能反复横跳。
NO_SEED_SOURCES = (BUILTIN_SOURCE, "auto-detect", "import")

# 只认明确指向「偏移/版本/hook 失效」的代理输出。
# 通道抖动（no-reply / ws-err / miniapp disconnected）不算，否则会把
# 「用户没从最近使用打开小程序」误判成偏移坏掉，白白重启代理杀掉小程序。
FAILURE_PATTERNS = (
    (re.compile(r"version config not found", re.I), "上游缺少该版本的偏移配置"),
    (re.compile(r"\b(hook|offsets?|address(es)?)\b[^\n]{0,80}\b"
                r"(not found|missing|invalid|mismatch|outdated|unsupported)\b", re.I),
     "偏移地址失效"),
    (re.compile(r"Interceptor\.(replace|attach)[^\n]{0,80}"
                r"\b(fail|error|invalid|unable|exception)\b", re.I),
     "frida 挂钩失败"),
    (re.compile(r"access violation|EXCEPTION_ACCESS_VIOLATION|0xC0000005", re.I),
     "写入错位地址导致进程异常"),
    (re.compile(r"flue\.dll[^\n]{0,80}\b(mismatch|unsupported|unknown version)\b", re.I),
     "flue.dll 版本不匹配"),
    (re.compile(r"failed to (apply|install|load)[^\n]{0,40}hook", re.I),
     "hook 安装失败"),
    (re.compile(r"auto-detect[^\n]{0,40}\b(fail|error|timeout)\b", re.I),
     "上游自动探测失败"),
)

A8KEY_KEYWORDS = ("open.weixin.qq.com", "oauth2/authorize", "connect/oauth2",
                  "A8KeyNavigationThrottle::CheckURL", "a8key_redirect_limit")


def ts():
    return time.strftime("%H:%M:%S")


def log_noop(msg):
    pass


def decode(raw):
    """子进程输出可能是 UTF-8，也可能是 GBK（cmd 默认代码页）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ------------------------------------------------------------------ 版本与路径
def wmpf_root():
    return os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat",
                        "xplugin", "plugins", "RadiumWMPF")


def find_versions(root=None):
    """本机已落地的 WMPF 版本（目录名即版本号）。与 uikit 的判定保持一致。"""
    root = root or wmpf_root()
    if not os.path.isdir(root):
        return []
    out = []
    for name in os.listdir(root):
        if name.isdigit() and os.path.isfile(flue_dll_path(name, root)):
            out.append(int(name))
    return sorted(out)


def active_version(versions=None, root=None):
    """当前生效版本取最大的一个（与 wizard.preflight 一致）。"""
    if versions is None:
        versions = find_versions(root)
    return str(versions[-1]) if versions else None


def flue_dll_path(version, root=None):
    return os.path.join(root or wmpf_root(), str(version),
                        "extracted", "runtime", "flue.dll")


def fingerprint(version, root=None):
    """flue.dll 的轻量指纹。

    不读 237MB 内容算 hash，只用 (size, mtime_ns)：文件被替换就一定会变，
    而启动开销是两次 stat。取不到文件时返回 None，此时不做指纹判定。
    """
    try:
        st = os.stat(flue_dll_path(version, root))
    except OSError:
        return None
    return "%d-%d" % (st.st_size, st.st_mtime_ns)


def config_dir(wmpf_dir):
    return os.path.join(wmpf_dir, "frida", "config",
                        "win32" if sys.platform == "win32" else sys.platform)


def config_path(wmpf_dir, version):
    return os.path.join(config_dir(wmpf_dir), "addresses.%s.json" % version)


def a8key_hooks_path(version):
    return os.path.join(HERE, "a8key_hooks.%s.json" % version)


# ------------------------------------------------------------------ 状态判定
def _load_meta(path):
    """读 sidecar 元数据。返回 (meta, err)，err 为 None / "missing" / "broken"。

    区分「没有文件」和「文件坏了」很重要：前者是手工放置的旧配置（可用，
    补一份元数据即可），后者说明写盘被打断，不能当成可用配置。
    """
    if not os.path.isfile(path):
        return None, "missing"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, "broken"
    if not isinstance(data, dict):
        return None, "broken"
    return data, None


def state(wmpf_dir, version=None, root=None, versions=None):
    """纯查询：当前版本的偏移配置是否可用。不写任何文件。

    返回 dict：state / version / config / meta / stale / fails / fingerprint /
               reason / need_meta / source / builtin

    source 是这份配置的来历（builtin / auto-detect / import / legacy），
    自愈逻辑靠它决定「要不要用内置值覆盖」。
    """
    if version is None:
        version = active_version(versions=versions, root=root)
    out = {"state": UNKNOWN, "version": version, "config": None, "meta": None,
           "stale": None, "fails": None, "fingerprint": None,
           "reason": "", "need_meta": False, "source": None,
           "builtin": KO.has(version) if version else False}
    if not wmpf_dir:
        out["reason"] = "未找到 WMPFDebugger"
        return out
    if not version:
        out["reason"] = "未检测到 WMPF 运行时"
        return out

    cfg = config_path(wmpf_dir, version)
    out.update({
        "config": cfg,
        "meta": cfg + META_SUFFIX,
        "stale": cfg + STALE_SUFFIX,
        "fails": cfg + FAILS_SUFFIX,
        "fingerprint": fingerprint(version, root),
    })
    meta, meta_err = _load_meta(out["meta"])
    out["source"] = (meta or {}).get("source")

    if os.path.isfile(out["stale"]):
        try:
            with open(out["stale"], "r", encoding="utf-8") as f:
                why = f.read().strip()[:120]
        except OSError:
            why = ""
        out["state"] = STALE
        out["reason"] = "上次运行标记了失效：%s" % (why or "未记录原因")
        return out

    if not os.path.isfile(cfg):
        out["state"] = MISSING
        out["reason"] = "WMPF %s 还没有偏移配置" % version
        return out

    try:
        with open(cfg, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        out["state"] = STALE
        out["reason"] = "偏移配置损坏：%s" % exc
        return out
    if not isinstance(data, dict) or not data:
        out["state"] = STALE
        out["reason"] = "偏移配置为空或格式不对"
        return out

    if meta_err == "broken":
        out["state"] = STALE
        out["reason"] = "指纹元数据损坏"
        return out
    if meta_err == "missing":
        # 手工放置 / 旧版程序生成的文件：没有指纹可比对。
        # 视为可用，但补一份元数据，之后就能感知 flue.dll 被替换。
        out["state"] = OK
        out["need_meta"] = True
        out["reason"] = "旧配置（无指纹元数据）"
        return out
    if str(meta.get("version")) != str(version):
        out["state"] = STALE
        out["reason"] = "元数据版本 %s != 当前 %s" % (meta.get("version"), version)
        return out
    fp = out["fingerprint"]
    if fp and meta.get("fingerprint") and meta["fingerprint"] != fp:
        out["state"] = STALE
        out["reason"] = "flue.dll 已变更（同版本号下被替换）"
        return out

    out["state"] = OK
    out["reason"] = "WMPF %s 偏移配置可用" % version
    return out


def needs_runtime_autodetect(wmpf_dir, version=None, root=None):
    """配置不可用时，让 WMPFDebugger 带 --auto-detect 启动（临时兜底）。"""
    return state(wmpf_dir, version=version, root=root)["state"] != OK



def ensure_meta(st):
    """给「可用但没有指纹元数据」的旧配置补一份 sidecar，返回是否补写。

    只写元数据、不动上游配置内容。补上之后，同版本号下 flue.dll 被替换
    就能被感知；启动入口在状态 OK 时调用它，否则元数据永远没机会生成。
    """
    if st.get("need_meta") and st.get("meta"):
        write_meta(st["meta"], st["version"], st["fingerprint"], "legacy")
        return True
    return False


# ------------------------------------------------------------------ 标记与元数据
def mark_stale(stale_path, reason):
    try:
        with open(stale_path, "w", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), reason))
    except OSError:
        pass


def clear_stale(stale_path):
    try:
        if stale_path and os.path.isfile(stale_path):
            os.remove(stale_path)
    except OSError:
        pass


def read_fails(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int((json.load(f) or {}).get("count", 0))
    except (OSError, ValueError, TypeError):
        return 0


def write_fails(path, count):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"count": count, "at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def write_meta(meta_path, version, fp, source, extra=None):
    """写 sidecar 元数据。extra 用于附加来历信息（如内置库的来源版本）。"""
    try:
        os.makedirs(os.path.dirname(meta_path), exist_ok=True)
        payload = {"version": str(version), "fingerprint": fp, "source": source,
                   "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        for k, v in (extra or {}).items():
            payload.setdefault(k, v)
        tmp = meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, meta_path)
    except OSError:
        pass


def _dump_config(data):
    """按上游风格序列化：对象缩进 2，纯数字数组留在一行（[64, 1616, 8]）。

    只是为了和 WMPFDebugger 仓库里的 addresses.*.json 长得一样，方便人工比对；
    frida 侧只按 JSON 解析，格式本身不影响行为。
    """
    text = json.dumps(data, ensure_ascii=False, indent=2)

    def inline(m):
        items = [x.strip() for x in m.group(1).split(",")]
        return "[" + ", ".join(items) + "]"

    return re.sub(r"\[\s*\n\s*([0-9,\s]+?)\s*\n\s*\]", inline, text)


def write_config(cfg_path, data, meta_path, version, fp, source="auto-detect",
                 extra=None):
    """原子写回。配置本身保持上游 WMPFDebugger 的格式，元数据另放 sidecar。"""
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_dump_config(data))
    os.replace(tmp, cfg_path)
    write_meta(meta_path, version, fp, source, extra)
    clear_stale(cfg_path + STALE_SUFFIX)
    write_fails(cfg_path + FAILS_SUFFIX, 0)


# ------------------------------------------------------------------ 内置偏移
def builtin_offsets(version):
    """本机版本是否能用内置偏移。返回 (data or None, 说明)。

    只认版本完全相同的那种：内置值对应的是特定 flue.dll 的 RVA，
    版本不同还硬写就是往错位地址打 patch。
    """
    if not version:
        return None, "未检测到 WMPF 版本"
    data = KO.get(version)
    if data is None:
        return None, "内置偏移库没有 WMPF %s（现有：%s）" % (version, KO.version_text())
    good, why = KO.validate(data)
    if not good:
        return None, "内置 WMPF %s 偏移不合法：%s" % (version, why)
    return data, "内置 WMPF %s 偏移" % version


def seed_builtin(st, log=None, force=False):
    """把内置偏移写成本机版本的 addresses.<ver>.json。返回 (ok, 说明)。

    不扫描、不 attach 进程，微信没开也能写，是扫描失效时的兜底。
    force 允许版本不符时强行写入（人工确认过的场景），默认拒绝。
    """
    log = log or log_noop
    cfg, version = st.get("config"), str(st.get("version") or "")
    if not cfg or not version:
        return False, "未检测到 WMPF 版本，无法落地内置偏移"

    src = version
    data = KO.get(version)
    if data is None:
        best = KO.latest()
        if not (force and best):
            return False, ("内置偏移库没有 WMPF %s（现有：%s）"
                           % (version, KO.version_text()))
        src = best
        data = KO.get(best)
        log("[offsets] 警告：拿 WMPF %s 的内置偏移写 WMPF %s 的配置，"
            "版本不符时可能写坏内存（--force 已确认）" % (src, version))

    good, why = KO.validate(data)
    if not good:
        return False, "内置偏移不合法：%s" % why
    if src != version:
        # 文件名即版本，配置里的 Version 也跟上，免得 frida 侧比对时产生歧义
        data["Version"] = int(version) if version.isdigit() else version

    write_config(cfg, data, st.get("meta") or (cfg + META_SUFFIX), version,
                 st.get("fingerprint"), source=BUILTIN_SOURCE,
                 extra={"builtin_from": src})
    return True, "WMPF %s 偏移已由内置库写入 %s" % (src, cfg)


def install_builtin(wmpf_dir, version=None, root=None, force=False, log=None):
    """按当前版本落地内置偏移（CLI / 部署流程入口）。返回 (ok, 说明, 版本)。"""
    log = log or log_noop
    st = state(wmpf_dir, version=version, root=root)
    if st["state"] == UNKNOWN:
        return False, st["reason"], None
    ok, why = seed_builtin(st, log=log, force=force)
    if ok:
        log("[offsets] " + why)
    return ok, why, st["version"]


def import_config(path, wmpf_dir, version=None, root=None, force=False, log=None):
    """导入外部 addresses json（上游 PR / 别人给的偏移）。返回 (ok, 说明)。

    写盘前先过一遍结构校验：宁可报「少个字段」，也不要把坏配置塞给 frida。
    """
    log = log or log_noop
    if not os.path.isfile(path):
        return False, "找不到文件：%s" % path
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return False, "JSON 解析失败：%s" % exc
    good, why = KO.validate(data)
    if not good:
        return False, "偏移数据不合法：%s" % why

    st = state(wmpf_dir, version=version, root=root)
    if st["state"] == UNKNOWN:
        return False, st["reason"]
    local = str(st["version"])
    src_ver = str(data.get("Version") or "")
    if src_ver != local:
        if not force:
            return False, ("该文件是 WMPF %s 的偏移，本机是 %s；"
                           "确认可用再加 --force" % (src_ver or "未知", local))
        log("[offsets] 警告：把 WMPF %s 的偏移写进 WMPF %s（--force 已确认）"
            % (src_ver or "未知", local))
        data["Version"] = int(local) if local.isdigit() else local
    write_config(st["config"], data, st["meta"], local, st["fingerprint"],
                 source="import")
    return True, "已导入偏移（源版本 %s）到 %s" % (src_ver or "未知", st["config"])


# ------------------------------------------------------------------ 失效信号
def failure_reason(line):
    """代理输出里是否包含明确的偏移失效信号。返回原因字符串或 None。"""
    text = str(line or "")
    if not text or DETECT_MARKER in text:
        return None
    for pat, why in FAILURE_PATTERNS:
        if pat.search(text):
            return why
    return None


def observe_proxy_line(line, wmpf_dir=None, version=None, log=None):
    """代理 stdout 每行的钩子。命中失效信号就落一个 stale 标记。

    这里只做「记下来」，不做重探：pump 是后台线程，重探要停代理、
    跑 10 秒子进程，交给主循环用 to_thread 做才不会卡住界面。
    """
    reason = failure_reason(line)
    if not reason:
        return False
    if wmpf_dir:
        st = state(wmpf_dir, version=version)
        if st["stale"] and st["version"]:
            mark_stale(st["stale"], reason)
    if log:
        log("[offsets] 代理报告偏移失效：%s（已标记，下一轮自动重探）" % reason)
    return True


# ------------------------------------------------------------------ 探测
def parse_detect_text(text):
    """从 auto-detect 输出里取回偏移 JSON。"""
    for line in str(text or "").splitlines():
        i = line.find(DETECT_MARKER)
        if i < 0:
            continue
        rest = line[i + len(DETECT_MARKER):].strip()
        try:
            data, _ = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            continue
        if isinstance(data, dict) and data:
            return data
    return None


def _terminate(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def detect(node, wmpf_dir, log=None, timeout=90, version=None):
    """跑一次 --auto-detect，返回 (data or None, 说明)。

    用「后台线程读 stdout + 队列限时取」而不是直接 readline 阻塞，
    否则上游挂住不退出时 timeout 形同虚设，整个启动流程会被拖死。
    """
    log = log or log_noop
    tsx = os.path.join(wmpf_dir, "node_modules", "tsx", "dist", "cli.mjs")
    entry = os.path.join(wmpf_dir, "src", "index.ts")
    if not node or not os.path.isfile(node):
        return None, "找不到 node.exe"
    if not os.path.isfile(tsx) or not os.path.isfile(entry):
        return None, "找不到 WMPFDebugger 的 tsx / src/index.ts"

    cmd = [node, tsx, entry, "--auto-detect"]
    log("[offsets] 自动探测 hook 偏移（WMPF %s，最多 %ds，此期间勿开小程序）"
        % (version or "?", int(timeout)))
    # 便携版 Node 不在系统 PATH 里，frida/tsx 的安装脚本会调 `node`；
    # 与 installer 原实现保持一致，把 node 目录补进 PATH。
    env = os.environ.copy()
    env["PATH"] = os.path.dirname(node) + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.Popen(
            cmd, cwd=wmpf_dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001
        return None, "启动 auto-detect 失败：%s" % exc

    lines = []
    q = queue.Queue()

    def pump():
        for raw in iter(proc.stdout.readline, b""):
            q.put(decode(raw).rstrip())
        q.put(None)

    threading.Thread(target=pump, daemon=True).start()

    found = None
    deadline = time.time() + timeout
    eof = False
    while time.time() < deadline and not eof:
        try:
            item = q.get(timeout=0.3)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        if item is None:
            eof = True
            break
        lines.append(item)
        if len(lines) <= 40:
            log("[detect] " + item[:200])
        found = parse_detect_text(item)
        if found:
            break
    _terminate(proc)

    if found:
        return found, "ok"
    tail = " | ".join(lines[-3:])[-180:]
    return None, "未拿到 detected hook offsets（%s）" % (tail or "无输出")


def recover_if_needed(wmpf_dir, node, log=None, force=False, version=None,
                      root=None, versions=None, timeout=90):
    """自愈入口。返回 (结果, 版本, 说明)。

    结果：ok 已可用 / seeded 已用内置偏移落地 / detected 刚扫描成功 /
          skipped 条件不足 / defer 连续失败改用上游兜底 / failed 本轮失败

    顺序是「内置库 -> 扫描」，不是反过来：
      · 扫描有失效的时候（微信改特征），内置库却是纯写盘，永远能走通；
      · 内置库命中时不需要微信在跑，也不需要等 10 秒 attach。
    force=True（--detect）跳过内置库强制重扫，否则手动重探永远到不了扫描那一步。
    """
    log = log or log_noop
    st = state(wmpf_dir, version=version, root=root, versions=versions)
    if st["state"] == UNKNOWN:
        return "skipped", None, st["reason"]

    if st["state"] == OK and not force:
        if st["need_meta"]:
            write_meta(st["meta"], st["version"], st["fingerprint"], "legacy")
        return "ok", st["version"], st["reason"]

    # 1) 内置偏移优先。来源是 builtin / auto-detect / import 时不再覆盖：
    #    那几种都已经有人确认过，覆盖只会掩盖问题、并和重探来回横跳。
    if not force and st["source"] not in NO_SEED_SOURCES:
        ok, why = seed_builtin(st, log=log)
        if ok:
            log("[offsets] " + why)
            return "seeded", st["version"], why
        log("[offsets] %s —— 改为自动探测" % why)

    fails = read_fails(st["fails"])
    if fails >= MAX_FAILS and not force:
        return "defer", st["version"], (
            "连续 %d 次探测失败，本轮交给 WMPFDebugger --auto-detect 兜底；"
            "可运行 python offsets.py --detect 或 --seed 手动处理" % fails)

    log("[offsets] 偏移需要更新：%s" % st["reason"])
    data, why = detect(node, wmpf_dir, log=log, timeout=timeout,
                       version=st["version"])
    if data:
        write_config(st["config"], data, st["meta"], st["version"],
                     st["fingerprint"])
        log("[offsets] 已写入 %s（%d 项偏移）" % (st["config"], len(data)))
        return "detected", st["version"], "已写入 %s" % st["config"]

    mark_stale(st["stale"], why)
    write_fails(st["fails"], fails + 1)
    log("[offsets] 本轮探测失败：%s" % why)
    return "failed", st["version"], why



# ------------------------------------------------------------------ a8key 观测钩子
def rescan_a8key(version=None, dll=None, out=None, log=None, timeout=300):
    """用 tools/pe_scan.py 重扫 a8key 导航拦截层的 RVA。

    这一步要静态分析 237MB 的 flue.dll，慢且只服务于调试观测，
    所以不塞进启动流程；由 CLI 或版本变化后按需显式调用。
    """
    log = log or log_noop
    if getattr(sys, "frozen", False):
        # exe 里 sys.executable 是 wxsign.exe 而不是 python，
        # 拿它去跑 pe_scan.py 只会把自己又启动一遍。
        return False, None, ("打包版 exe 不支持该命令；请用源码运行 "
                            "python offsets.py --rescan-a8key")
    version = version or active_version()
    if not version:
        return False, None, "未检测到 WMPF 运行时"
    dll = dll or flue_dll_path(version)
    out = out or a8key_hooks_path(version)
    if not os.path.isfile(dll):
        return False, None, "找不到 flue.dll：%s" % dll
    script = os.path.join(TOOLS, "pe_scan.py")
    if not os.path.isfile(script):
        return False, None, "找不到 tools/pe_scan.py"

    cmd = [sys.executable, script, dll, "--json", out,
           "--kw"] + list(A8KEY_KEYWORDS)
    log("[offsets] 静态重扫 a8key RVA（WMPF %s，可能要几分钟）" % version)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, None, "扫描超过 %ds，已中止" % timeout
    except Exception as exc:  # noqa: BLE001
        return False, None, "启动扫描失败：%s" % exc
    if r.returncode != 0 or not os.path.isfile(out):
        tail = decode(r.stderr or r.stdout or b"")[-200:]
        return False, None, "扫描失败（code=%s）：%s" % (r.returncode, tail)
    try:
        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return False, None, "扫描结果解析失败：%s" % exc
    funcs = data.get("functions") or []
    if not funcs:
        return False, None, "没有扫到 a8key 相关函数（版本变化太大，需人工确认关键字）"
    log("[offsets] 已写出 %s（%d 个函数）" % (out, len(funcs)))
    return True, out, "已写出 %s" % out


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只看当前偏移状态")
    ap.add_argument("--detect", action="store_true", help="强制重新探测主偏移")
    ap.add_argument("--seed", action="store_true",
                    help="用内置偏移库直接写入当前版本（不扫描，不需要微信）")
    ap.add_argument("--import", dest="import_file", default="",
                    help="导入外部 addresses json（如上游 PR 给的偏移）")
    ap.add_argument("--list-builtin", action="store_true", help="列出内置偏移库")
    ap.add_argument("--force", action="store_true",
                    help="版本不符时仍强行写入（危险，需人工确认）")
    ap.add_argument("--rescan-a8key", action="store_true", help="重扫 a8key 观测钩子 RVA")
    ap.add_argument("--wmpf-dir", default="", help="WMPFDebugger 目录")
    ap.add_argument("--node", default="", help="node.exe 路径")
    ap.add_argument("--version", default="", help="指定 WMPF 版本（默认自动检测）")
    ap.add_argument("--dll", default="", help="指定 flue.dll（仅 --rescan-a8key）")
    ap.add_argument("--timeout", type=float, default=90)
    args = ap.parse_args()

    log = lambda m: print("[%s] %s" % (ts(), m), flush=True)

    if args.list_builtin:
        rows = KO.summary()
        if not rows:
            print("内置偏移库为空")
            return 1
        print("内置偏移库（WMPF 版本 -> 是否可用）")
        for ver, good, why in rows:
            print("  %-8s %s  %s" % (ver, "✓" if good else "✗", why))
        return 0

    wmpf_dir = args.wmpf_dir
    node = args.node
    if not wmpf_dir or not node:
        try:
            import uikit as U
            wmpf_dir = wmpf_dir or U.resolve_wmpf_dir(HERE)
            node = node or U.resolve_node(HERE)
        except Exception:  # noqa: BLE001
            pass
        try:
            import channel_keeper as ck
            wmpf_dir = wmpf_dir or ck.DEFAULT_WMPF
            node = node or ck.DEFAULT_NODE
        except Exception:  # noqa: BLE001
            pass

    if args.seed:
        ok, why, _ver = install_builtin(wmpf_dir, version=args.version or None,
                                       force=args.force, log=log)
        print(("成功：%s" % why) if ok else ("失败：%s" % why))
        return 0 if ok else 1

    if args.import_file:
        ok, why = import_config(args.import_file, wmpf_dir,
                                version=args.version or None,
                                force=args.force, log=log)
        print(("成功：%s" % why) if ok else ("失败：%s" % why))
        return 0 if ok else 1

    if args.rescan_a8key:
        good, path, why = rescan_a8key(version=args.version or None,
                                       dll=args.dll or None, log=log,
                                       timeout=args.timeout or 300)
        print(("已生成：%s" % path) if good else ("失败：%s" % why))
        return 0 if good else 1

    st = state(wmpf_dir, version=args.version or None)
    print("WMPF 版本   : %s" % (st["version"] or "未检测到"))
    print("偏移配置    : %s" % (st["config"] or "-"))
    print("flue.dll 指纹: %s" % (st["fingerprint"] or "-"))
    print("配置来源    : %s" % (st["source"] or "-"))
    if st["builtin"]:
        print("内置偏移    : 有（WMPF %s，可直接 --seed 写入）" % st["version"])
    else:
        print("内置偏移    : 无（现有版本：%s，只能靠 --auto-detect）" % KO.version_text())
    print("状态        : %s  (%s)" % (st["state"], st["reason"]))
    if args.check:
        return 0 if st["state"] == OK else 1

    if args.detect:
        result, ver, why = recover_if_needed(
            wmpf_dir, node, log=log, force=True,
            version=args.version or None, timeout=args.timeout)
        print("结果：%s / WMPF %s / %s" % (result, ver or "-", why))
        if result == "detected":
            print("提示：a8key 观测钩子的 RVA 也会变，需要时跑 python offsets.py --rescan-a8key")
        return 0 if result in ("ok", "detected", "seeded") else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
