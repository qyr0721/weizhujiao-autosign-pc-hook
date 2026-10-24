#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
signcore.py — 签到核心（被 signer/sign_pc.py 与 wizard.py 共用）

契约与 Android 版（weizhujiao-autosign）一致：
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
    "poll": 5,
    "jitter_deg": 0.00003,
    "lat": None,
    "lon": None,
    "class_hours": [7, 23],
}


def ts():
    return time.strftime("%H:%M:%S")


def load_cfg(path):
    cfg = dict(DEFAULTS)
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
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


def in_hours(cfg):
    h = time.localtime().tm_hour
    lo, hi = cfg.get("class_hours") or [0, 24]
    return lo <= h < hi


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
    """主循环。返回退出码：0 正常 / 2 号失效（需续号）/ 3 无号 / 4 查询失败"""
    log = log or (lambda m: print("[%s] %s" % (ts(), m), flush=True))
    stale_marker = os.path.join(os.path.dirname(os.path.abspath(openid_file)), "openid.stale")
    signed = set()
    last_idle = 0.0

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
            time.sleep(5)
            continue

        if not in_hours(cfg):
            if time.time() - last_idle > 300:
                last_idle = time.time()
                log("非上课时段，待命（class_hours=%s）" % (cfg.get("class_hours"),))
            if once:
                return 0
            time.sleep(30)
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
            time.sleep(cfg.get("poll", 5) * 2)
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

        for _ in range(int(cfg.get("poll", 5)) * 2):
            if stop is not None and stop.is_set():
                return 0
            time.sleep(0.5)
