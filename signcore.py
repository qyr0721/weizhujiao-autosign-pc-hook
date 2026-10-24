#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
signcore.py — 签到核心（被 signer/sign_pc.py 与 wizard.py 共用）

契约与 Android root 版（weizhujiao-autosign-android）一致：
  GET  {base}/student/active_signs      headers: UA / Referer / openId
  POST {base}/student-sign-in           {"courseId":N,"signId":N[,"lat":x,"lon":y]}
  幂等：含 "signRank" 成功；"repeat sign in" / "已经签到" / errorCode 305 视为已签

401 处理：写 openid.stale（文件模式）或 set(stale_event)（进程内模式），返回 2。
"""
import json
import os
import random
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

API = "https://www.teachermate.com.cn/wechat-api/v1/class-attendance"
REF = "https://app.teachermate.com.cn/wechat-pro-ssr/student/sign"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
      "NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) "
      "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541f38) XWEB/25799")

DEFAULTS = {
    "poll": 5,            # 有签到在挂时 / 刚启动时的轮询间隔
    "idle_poll": 30,      # 连续空窗期的轮询间隔（自适应退避，不依赖时钟）
    "idle_after": 6,      # 连续多少次空结果后进入退避
    "jitter_deg": 0.00003,
    "lat": None,
    "lon": None,
}


def ts():
    return time.strftime("%H:%M:%S")


def load_cfg(path, warn=None):
    cfg = dict(DEFAULTS)
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            if "class_hours" in data:
                msg = ("config 里的 class_hours 已废弃（不再按时段待命），已忽略；"
                       "空窗降频用 idle_poll / idle_after")
                if warn is not None:
                    warn(msg)
                else:
                    print("[!] " + msg, flush=True)
                data.pop("class_hours", None)
            cfg.update(data)
        except Exception:  # noqa: BLE001
            pass
    return cfg


def read_openid(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return ""


def req(method, path, oid, body=None, timeout=15):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Referer": REF, "openId": oid}
    if data is not None:
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return -1, "NETERR %s" % exc


def sleep_guarded(sec, stop):
    """可被 stop 打断的睡眠。"""
    end = time.time() + sec
    while time.time() < end:
        if stop is not None and stop.is_set():
            return False
        time.sleep(min(0.5, max(0.0, end - time.time())))
    return True


def jitter(cfg):
    lat, lon = cfg.get("lat"), cfg.get("lon")
    if lat is None or lon is None:
        return None
    d = cfg.get("jitter_deg", 0)
    return (round(lat + (random.random() - 0.5) * d, 6),
            round(lon + (random.random() - 0.5) * d, 6))


def is_ok(text):
    return "signRank" in text


def is_dup(text):
    t = text.replace(" ", "")
    return ("repeat sign in" in text) or ("已经签到" in text) or ('"errorCode":305' in t)


def sign_loop(cfg, openid_file, once=False, dry_run=False,
              stale_event=None, log=None, stop=None):
    """主循环。返回退出码：0 正常 / 2 号失效（需续号）/ 3 无号 / 4 查询失败

    不设时段闸门：有没有课由服务端 active_signs 决定；
    空窗期靠自适应退避降频（idle_after → idle_poll），一发现签到立刻恢复 poll。
    """
    log = log or (lambda m: print("[%s] %s" % (ts(), m), flush=True))
    stale_marker = os.path.join(os.path.dirname(os.path.abspath(openid_file)), "openid.stale")
    signed = set()
    empty_streak = 0
    last_note = 0.0

    if not stale_event and os.path.isfile(stale_marker):
        os.remove(stale_marker)

    while True:
        if stop is not None and stop.is_set():
            return 0

        oid = read_openid(openid_file)
        if not oid:
            log("还没有 openid，等续号侧产出")
            if once:
                return 3
            sleep_guarded(5, stop)
            continue

        status, body = req("GET", "/student/active_signs", oid)
        if status == 401:
            log("openid 已失效（401，被顶掉）——请求续号")
            if stale_event is not None:
                stale_event.set()
            else:
                with open(stale_marker, "w", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            return 2
        if status != 200:
            log("查询失败 HTTP %s %s" % (status, body[:100]))
            if once:
                return 4
            sleep_guarded(cfg.get("poll", 5) * 2, stop)
            continue

        try:
            items = json.loads(body)
        except json.JSONDecodeError:
            items = []
        if not isinstance(items, list):
            items = []

        for it in items:
            sid, cid = it.get("signId"), it.get("courseId")
            if sid is None or sid in signed:
                continue
            payload = {"courseId": cid, "signId": sid}
            pos = jitter(cfg) if it.get("isGPS") else None
            if pos:
                payload["lat"], payload["lon"] = pos

            if dry_run:
                log("[dry] 将提交 %s" % json.dumps(payload, ensure_ascii=False))
                continue

            st, resp = req("POST", "/student-sign-in", oid, payload)
            if is_ok(resp):
                signed.add(sid)
                log("✔ 签到成功 signId=%s %s" % (sid, resp[:90]))
            elif is_dup(resp):
                signed.add(sid)
                log("· 已签到（幂等）signId=%s" % sid)
            else:
                log("✘ 签到失败 signId=%s HTTP %s %s" % (sid, st, resp[:100]))

        if once:
            log("本轮结束：进行中 %d 个，已签 %d 个" % (len(items), len(signed)))
            return 0

        # 自适应退避：连续空窗 → 降到 idle_poll；一旦有签到 → 立刻回到 poll
        if items:
            empty_streak = 0
        else:
            empty_streak += 1
        if empty_streak >= int(cfg.get("idle_after", 6)):
            delay = cfg.get("idle_poll", 30)
            if time.time() - last_note > 600:
                last_note = time.time()
                log("空窗 %d 次，降至 %ds 一次（有签到会立刻恢复 %ds）"
                    % (empty_streak, int(delay), int(cfg.get("poll", 5))))
        else:
            delay = cfg.get("poll", 5)

        if not sleep_guarded(delay, stop):
            return 0
