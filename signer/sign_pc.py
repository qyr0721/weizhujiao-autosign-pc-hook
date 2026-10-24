#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sign_pc.py — PC 侧签到器（消费本项目取号结果）

契约与 Android 模块（weizhujiao-autosign）完全一致，保证行为对齐：
  GET  {base}/student/active_signs     headers: UA / Referer / openId
  POST {base}/student-sign-in          {"courseId":N,"signId":N[,"lat":x,"lon":y]}
  幂等：响应含 "signRank" 视为成功；"repeat sign in" / "已经签到" / errorCode 305 视为已签到

与取号侧的分工：
  · openid 从 openid.txt 读（run.py 产出），自己不取号
  · 一旦发现 401，写 openid.stale 并 **退出码 2** —— run.py 看到就立刻续号并重启本进程

用法：
  python signer/sign_pc.py                 # 常驻
  python signer/sign_pc.py --once          # 只查一轮
  python signer/sign_pc.py --dry-run       # 只打印不提交
配置：signer/config.json（可选，缺省用内置值；示例见 config.example.json）
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

API = "https://www.teachermate.com.cn/wechat-api/v1/class-attendance"
REF = "https://app.teachermate.com.cn/wechat-pro-ssr/student/sign"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
      "NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) "
      "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541f38) XWEB/25799")

DEFAULTS = {
    "poll": 5,                 # 轮询间隔（秒）
    "jitter_deg": 0.00003,     # 坐标抖动幅度（度）
    "lat": None, "lon": None,  # GPS 签到用；留空则只在 isGPS 时提交空坐标体
    "class_hours": [7, 23],    # 只在该时段工作
    "notify": True,            # 成功/失败打印醒目标记
}


def ts():
    return time.strftime("%H:%M:%S")


def log(msg):
    print("[%s] %s" % (ts(), msg), flush=True)


def load_cfg(path):
    cfg = dict(DEFAULTS)
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception as exc:  # noqa: BLE001
            log("配置读取失败（用默认值）：%s" % exc)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--openid-file", default=os.path.join(ROOT, "openid.txt"))
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    stale_marker = os.path.join(ROOT, "openid.stale")
    signed = set()
    last_idle = 0.0

    if os.path.isfile(stale_marker):
        os.remove(stale_marker)

    while True:
        oid = read_openid(args.openid_file)
        if not oid:
            log("没有 openid（等取号侧产出 %s）" % os.path.basename(args.openid_file))
            if args.once:
                return 3
            time.sleep(5)
            continue

        if not in_hours(cfg):
            if time.time() - last_idle > 300:
                last_idle = time.time()
                log("非上课时段，待命")
            if args.once:
                return 0
            time.sleep(30)
            continue

        status, body = req("GET", "/student/active_signs", oid)
        if status == 401:
            log("openid 已 401（被顶掉/失效）——通知取号侧续号")
            with open(stale_marker, "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            return 2
        if status != 200:
            log("查询失败 HTTP %s %s" % (status, body[:100]))
            if args.once:
                return 4
            time.sleep(cfg["poll"] * 2)
            continue

        try:
            items = json.loads(body)
        except json.JSONDecodeError:
            items = []
        if not isinstance(items, list):
            items = []

        for it in items:
            sid, cid = it.get("signId"), it.get("courseId")
            if sid is None:
                continue
            if sid in signed:
                continue
            payload = {"courseId": cid, "signId": sid}
            pos = jitter(cfg) if it.get("isGPS") else None
            if pos:
                payload["lat"], payload["lon"] = pos

            if args.dry_run:
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

        if args.once:
            log("本轮结束：进行中 %d 个，已签 %d 个" % (len(items), len(signed)))
            return 0
        time.sleep(cfg["poll"])


if __name__ == "__main__":
    sys.exit(main())
