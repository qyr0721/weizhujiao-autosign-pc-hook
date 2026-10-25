#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
installer.py — 运行环境部署（供 wizard 与主程序内部调用）

职责：补齐 wxsign 运行所缺的组件。只处理缺失项，不覆盖已存在的内容。

  Node 运行时      下载便携版压缩包并解压至 <base>\\runtime\\node，不写入系统目录
  WMPFDebugger     使用 git clone；未检测到 git 时改用 codeload 压缩包
  依赖 tsx / frida 执行 npm install（frida 预编译绑定约 130 MB）
  偏移配置         执行一次 --auto-detect 并将结果写入 frida\\config\\win32\\

本模块不负责界面：进度通过构造时注入的 notify 回调上报，由调用方决定如何显示。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uikit as U  # noqa: E402

NODE_FALLBACK = "22.20.0"
NODE_MIRRORS = [
    "https://npmmirror.com/mirrors/node/v{ver}/node-v{ver}-win-x64.zip",
    "https://nodejs.org/dist/v{ver}/node-v{ver}-win-x64.zip",
]
WMPF_REPO = "https://github.com/evi0s/WMPFDebugger.git"
WMPF_ZIP = "https://codeload.github.com/evi0s/WMPFDebugger/zip/refs/heads/main"
UA = {"User-Agent": "wxsign-installer/1.2"}


# ================================================================ 工具
def human(n):
    if n >= 1 << 20:
        return "%.1f MB" % (n / (1 << 20))
    if n >= 1 << 10:
        return "%.0f KB" % (n / (1 << 10))
    return "%d B" % n


def decode(raw):
    """子进程输出可能是 UTF-8，也可能是 GBK（cmd 默认代码页）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def download(url, dest, on_progress=None, timeout=60):
    """流式下载到 dest；失败抛异常，由调用方换源重试。"""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
        os.replace(tmp, dest)
    return done


def latest_node_version():
    """取 Node 22.x 的最新版本号；查询失败时返回兜底值。"""
    for url in ("https://npmmirror.com/mirrors/node/index.json",
                "https://nodejs.org/dist/index.json"):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            vers = [d["version"][1:] for d in data
                    if d.get("version", "").startswith("v22.")]
            if vers:
                return vers[0]
        except Exception:  # noqa: BLE001
            continue
    return NODE_FALLBACK


def extract_zip(zip_path, dest):
    """解压并剥离压缩包顶层目录（GitHub 与 Node 的压缩包均采用该结构）。"""
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        top = ""
        first = names[0].split("/")[0] if names else ""
        if first and all(n.startswith(first + "/") for n in names):
            top = first + "/"
        for name in names:
            if not name.startswith(top):
                continue
            rel = name[len(top):]
            if not rel or rel.endswith("/"):
                continue
            if ".." in rel or rel.startswith("/"):
                continue                       # zip slip 防护
            out = os.path.join(dest, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(name) as src, open(out, "wb") as f:
                shutil.copyfileobj(src, f)


# ================================================================ 步骤
class Step:
    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.state = "pending"      # pending | run | ok | skip | fail
        self.detail = ""

    def mark(self):
        return {"ok": "✓", "skip": "·", "fail": "✗", "run": "…"}.get(self.state, " ")


class Setup:
    """环境部署。notify(action, progress) 用于上报进度，可为空。"""

    def __init__(self, log, base, notify=None):
        self.log = log
        self.base = base
        self.notify = notify or (lambda *a: None)
        self.steps = [
            Step("node", "Node 运行时"),
            Step("debugger", "WMPFDebugger"),
            Step("deps", "依赖 tsx / frida"),
            Step("offsets", "偏移配置"),
        ]
        self.by = {s.key: s for s in self.steps}
        self.wx_ver = "未检测到"
        self.wmpf = "未检测到"
        self.wmpf_ok = True
        self.wmpf_dir = None
        self.node = None

    def say(self, action, progress=""):
        self.notify(action, progress)

    # ---------- 检测 ----------
    def probe(self):
        wx, _ = U.wechat_version()
        self.wx_ver = wx or "未检测到"
        vers = U.find_wmpf_versions()
        wmpf = vers[-1] if vers else None
        self.wmpf = str(wmpf) if wmpf else "未检测到"
        self.wmpf_ok = (wmpf == U.KNOWN_VERSION)

        self.node = U.resolve_node(self.base)
        self.wmpf_dir = U.resolve_wmpf_dir(self.base)

        self.by["node"].state = "ok" if self.node else "pending"
        self.by["node"].detail = "已就绪" if self.node else "缺失"

        self.by["debugger"].state = "ok" if self.wmpf_dir else "pending"
        self.by["debugger"].detail = "已存在" if self.wmpf_dir else "缺失"

        deps_ok = bool(self.wmpf_dir and os.path.isfile(
            os.path.join(self.wmpf_dir, "node_modules", "tsx", "dist", "cli.mjs")))
        self.by["deps"].state = "ok" if deps_ok else "pending"
        self.by["deps"].detail = "已安装" if deps_ok else "缺失"

        cfg = self._offset_path()
        cfg_ok = bool(cfg and os.path.isfile(cfg))
        self.by["offsets"].state = "ok" if cfg_ok else "pending"
        self.by["offsets"].detail = "已存在" if cfg_ok else "需探测一次"

        self.log.write("环境检测：微信=%s WMPF=%s Node=%s WMPFDebugger=%s 依赖=%s 偏移=%s"
                       % (self.wx_ver, self.wmpf, self.node or "-",
                          self.wmpf_dir or "-", deps_ok, cfg_ok))
        return self

    def missing(self):
        return [s for s in self.steps if s.state == "pending"]

    # ---------- Node ----------
    def install_node(self):
        s = self.by["node"]
        s.state = "run"
        self.say("读取 Node 版本信息…")

        ver = latest_node_version()
        dest = os.path.join(self.base, "runtime", "node")
        zip_path = os.path.join(self.base, "runtime", "node.zip")
        os.makedirs(os.path.dirname(zip_path), exist_ok=True)

        last = ""
        for tpl in NODE_MIRRORS:
            url = tpl.format(ver=ver)
            self.log.write("下载 Node %s：%s" % (ver, url))
            try:
                def prog(done, total):
                    pct = ("%d%%" % (done * 100 // total)) if total else human(done)
                    self.say("下载 Node %s" % ver, pct)

                n = download(url, zip_path, on_progress=prog, timeout=120)
                self.log.write("下载完成 %s" % human(n))
                break
            except Exception as exc:  # noqa: BLE001
                last = "%s: %s" % (url.split("/")[2], exc)
                self.log.write("下载失败：%s" % last)
        else:
            s.state = "fail"
            s.detail = "下载失败"
            self.say("Node 下载失败")
            return False

        self.say("解压 Node %s…" % ver)
        try:
            extract_zip(zip_path, dest)
            os.remove(zip_path)
        except Exception as exc:  # noqa: BLE001
            self.log.write("解压失败：%s" % exc)
            s.state = "fail"
            s.detail = "解压失败"
            return False

        exe = os.path.join(dest, "node.exe")
        if not os.path.isfile(exe):
            s.state = "fail"
            s.detail = "未找到 node.exe"
            return False

        self.node = exe
        s.state = "ok"
        s.detail = ver
        self.log.write("Node 就绪：%s" % exe)
        return True

    # ---------- WMPFDebugger ----------
    def install_debugger(self):
        s = self.by["debugger"]
        s.state = "run"
        dest = os.path.join(self.base, "WMPFDebugger")

        if os.path.isdir(dest) and os.listdir(dest):
            self.log.write("目录已存在，沿用：%s" % dest)
        else:
            git = shutil.which("git")
            done = False
            if git:
                self.say("获取 WMPFDebugger 源码…")
                self.log.write("git clone %s" % WMPF_REPO)
                try:
                    r = subprocess.run([git, "clone", "--depth", "1", WMPF_REPO, dest],
                                       capture_output=True, timeout=600)
                    done = (r.returncode == 0)
                    if not done:
                        self.log.write("git clone 失败：%s"
                                       % decode(r.stderr or b"")[-300:])
                except Exception as exc:  # noqa: BLE001
                    self.log.write("git clone 异常：%s" % exc)

            if not done:
                self.say("下载 WMPFDebugger…")
                zip_path = dest + ".zip"
                try:
                    def prog(done_b, total):
                        pct = ("%d%%" % (done_b * 100 // total)) if total else human(done_b)
                        self.say("下载 WMPFDebugger", pct)
                    download(WMPF_ZIP, zip_path, on_progress=prog, timeout=120)
                    extract_zip(zip_path, dest)
                    os.remove(zip_path)
                    done = os.path.isfile(os.path.join(dest, "package.json"))
                except Exception as exc:  # noqa: BLE001
                    self.log.write("压缩包下载失败：%s" % exc)

            if not done:
                s.state = "fail"
                s.detail = "下载失败"
                self.say("WMPFDebugger 下载失败")
                return False

        self.wmpf_dir = dest
        s.state = "ok"
        s.detail = "已就绪"
        self.log.write("WMPFDebugger 就绪：%s" % dest)
        return True

    # ---------- 依赖 ----------
    def _find_npm(self):
        if self.node:
            for name in ("npm.cmd", "npm"):
                cand = os.path.join(os.path.dirname(self.node), name)
                if os.path.isfile(cand):
                    return cand
        return shutil.which("npm")

    def install_deps(self):
        s = self.by["deps"]
        if not self.wmpf_dir:
            s.state = "fail"
            s.detail = "需先部署 WMPFDebugger"
            return False
        if os.path.isfile(os.path.join(self.wmpf_dir, "node_modules", "tsx", "dist", "cli.mjs")):
            s.state = "skip"
            s.detail = "已安装"
            return True

        npm = self._find_npm()
        if not npm:
            s.state = "fail"
            s.detail = "未找到 npm"
            return False

        s.state = "run"
        for i, extra in enumerate(([], ["--registry=https://registry.npmmirror.com"]), 1):
            self.say("安装依赖（下载 frida 绑定，需数分钟）…", "第 %d 次" % i)
            cmd = [npm, "install", "--no-audit", "--no-fund"] + extra
            self.log.write("执行：%s" % " ".join(cmd))
            # 便携版 Node 不在 PATH 中，npm 装 frida 时会调用 `node scripts/install.js`
            env = os.environ.copy()
            if self.node:
                env["PATH"] = os.path.dirname(self.node) + os.pathsep + env.get("PATH", "")
            try:
                proc = subprocess.Popen(
                    cmd, cwd=self.wmpf_dir, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                for raw in iter(proc.stdout.readline, b""):
                    line = decode(raw).rstrip()
                    if line:
                        self.log.write(line, "[npm] ")
                proc.wait(timeout=1800)
            except Exception as exc:  # noqa: BLE001
                self.log.write("npm 异常：%s" % exc)

            if os.path.isfile(os.path.join(self.wmpf_dir, "node_modules", "tsx", "dist", "cli.mjs")):
                s.state = "ok"
                s.detail = "已安装"
                self.log.write("依赖安装完成")
                return True

        s.state = "fail"
        s.detail = "npm install 未成功"
        self.say("依赖安装失败")
        return False

    # ---------- 偏移配置 ----------
    def _offset_path(self):
        if not self.wmpf_dir or self.wmpf == "未检测到":
            return None
        return os.path.join(self.wmpf_dir, "frida", "config",
                            "win32", "addresses.%s.json" % self.wmpf)

    def ensure_offsets(self):
        """仓库自带配置仅覆盖旧版本；新 WMPF 需探测一次并写回，否则每次启动都要重探。"""
        s = self.by["offsets"]
        cfg = self._offset_path()
        if not cfg:
            s.state = "fail"
            s.detail = "需先部署 WMPFDebugger"
            return False
        if os.path.isfile(cfg):
            s.state = "ok"
            s.detail = "已存在"
            return True

        if not U.list_processes({"wechatappex.exe"}):
            s.state = "fail"
            s.detail = "需先启动微信"
            self.say("微信未运行，无法探测偏移")
            return False

        tsx = os.path.join(self.wmpf_dir, "node_modules", "tsx", "dist", "cli.mjs")
        entry = os.path.join(self.wmpf_dir, "src", "index.ts")
        if not (os.path.isfile(tsx) and os.path.isfile(entry)):
            s.state = "fail"
            s.detail = "入口文件缺失"
            return False

        s.state = "run"
        self.say("探测 hook 偏移（需微信运行，约 10 秒）…")
        env = os.environ.copy()
        if self.node:
            env["PATH"] = os.path.dirname(self.node) + os.pathsep + env.get("PATH", "")
        cmd = [self.node, tsx, entry, "--auto-detect"]
        self.log.write("执行：%s" % " ".join(cmd))

        found = ""
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.wmpf_dir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.time() + 90
            while time.time() < deadline:
                raw = proc.stdout.readline()
                if not raw:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.2)
                    continue
                line = decode(raw).rstrip()
                self.log.write(line, "[auto-detect] ")
                if "detected hook offsets:" in line:
                    found = line.split("detected hook offsets:", 1)[1].strip()
                    break
        except Exception as exc:  # noqa: BLE001
            self.log.write("auto-detect 异常：%s" % exc)
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    proc.kill()

        if not found:
            s.state = "fail"
            s.detail = "探测失败"
            self.say("偏移探测失败")
            return False

        try:
            data, _ = json.JSONDecoder().raw_decode(found)
        except ValueError:
            self.log.write("解析探测结果失败：%s" % found[:200])
            s.state = "fail"
            s.detail = "结果解析失败"
            return False
        try:
            os.makedirs(os.path.dirname(cfg), exist_ok=True)
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            self.log.write("写入配置失败：%s" % exc)
            s.state = "fail"
            s.detail = "写入失败"
            return False

        s.state = "ok"
        s.detail = "已生成 %s" % self.wmpf
        self.log.write("偏移配置已写入：%s" % cfg)
        return True

    # ---------- 入口 ----------
    def run(self):
        """部署全部缺失项。返回 0 表示环境可用。"""
        self.probe()
        if not self.missing():
            self.say("环境已就绪，无需部署")
            return 0

        ok = True
        if self.by["node"].state == "pending":
            ok = self.install_node() and ok
        if self.by["debugger"].state == "pending":
            ok = self.install_debugger() and ok
        if self.by["deps"].state == "pending":
            ok = self.install_deps() and ok
        if self.by["offsets"].state == "pending":
            ok = self.ensure_offsets() and ok

        self.probe()
        self.say("环境部署完成" if ok else "存在未完成的步骤")
        return 0 if ok else 1

    def summary(self):
        return [(s.mark(), s.label, s.detail) for s in self.steps]
