#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
oid_probe.py — openid 有效性探针 / 生命周期测量

背景（2026-10-25 实测，含用户更正）：
  · `active_signs` 的 200 **不能**当作"openid 有效"的证据：
    无论 openid 过期、还是真的没有进行中的签到，**都返回 200**。
    只有**假的 / 被顶掉**的 openid 才返回 401。
  · 签到页 SSR 也不校验 openid —— 把 openid 归一化后，真号与随机 32hex 的
    HTML 字节级完全相同（只是把值回显进 session.openId）。
  · 结论：**没有廉价的验号接口**。轮询 active_signs 的用途是"发现签到窗口"，
    不是判断凭证健康度；要确认能不能用，只能真的去签一次。
  · 仍然成立的两条：接口不校验 cookie / Referer / Origin；32 位 hex 是微助教
    签发的站点会话标识（SSR 里叫 session.openId），不是微信 openid。

用法：
  # 单次校验
  python oid_probe.py --oid 53f57e732b3b66c7a72c17173bcdf32e
  python oid_probe.py --file ../weizhujiao-autosign-pc/cdp_oid.txt

  # 持续观测，记录 200/401 翻转时刻（用来测真实生命周期）
  python oid_probe.py --oid xxx --loop 60 --log oid_timeline.jsonl

  # 顺带看一眼签到页能否按 openid 渲染（无 cookie）
  python oid_probe.py --oid xxx --page

返回码：0 有效 / 1 无效(401) / 2 网络或其它错误
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

API = "https://www.teachermate.com.cn/wechat-api/v1/class-attendance"
PAGE = "https://v18.teachermate.cn/wechat-pro-ssr/student/sign?openid="
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
      "NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) "
      "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541f38) XWEB/25799")


def get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return -1, "NETERR %s" % exc


def check(oid, verbose=True):
    """返回 (状态, items)。状态 ∈ ok / invalid / error

    注意语义：ok 只表示"接口通了、可能没有进行中的签到"，
    **不代表 openid 没过期**。invalid 才是明确信号（假号/被顶掉）。
    """
    status, body = get(API + "/student/active_signs",
                       {"User-Agent": UA, "openId": oid})
    if status == 200:
        try:
            items = json.loads(body)
        except json.JSONDecodeError:
            items = None
        if verbose:
            n = len(items) if isinstance(items, list) else "?"
            print("  HTTP 200  接口通  进行中签到=%s（200 不代表号有效）" % n)
            if isinstance(items, list):
                for it in items:
                    print("    - signId=%s courseId=%s isGPS=%s name=%s"
                          % (it.get("signId"), it.get("courseId"),
                             it.get("isGPS"), (it.get("courseName") or "")[:24]))
        return "ok", items
    if status == 401:
        if verbose:
            print("  HTTP 401  假号 / 已被顶掉（这是唯一能明确判死的信号）")
        return "invalid", None
    if verbose:
        print("  HTTP %s  %s" % (status, body[:120]))
    return "error", None


def page_check(oid):
    status, html = get(PAGE + oid)
    if status != 200:
        print("  签到页 HTTP %s（%s 字节）" % (status, len(html)))
        return
    mark = '"session":{"openId":"%s"}' % oid
    rendered = mark.replace('"', "") in html.replace('"', "")
    has_state = "__INITIAL_STATE__" in html
    print("  签到页 HTTP 200  %d 字节  含 INITIAL_STATE=%s  会话与 openid 一致=%s"
          % (len(html), has_state, rendered))
    i = html.find("__INITIAL_STATE__")
    if i > 0:
        seg = html[i:i + 260].replace("\n", " ")
        print("  " + seg[:230])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oid", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("--loop", type=float, default=0, help="每 N 秒探一次，0=只探一次")
    ap.add_argument("--log", default="", help="把时间线写到 jsonl")
    ap.add_argument("--page", action="store_true", help="顺带验证签到页 SSR")
    args = ap.parse_args()

    oid = args.oid.strip()
    if not oid and args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            oid = f.read().strip()
    if not oid:
        sys.exit("需要 --oid 或 --file")

    print("openid = %s…(%d 位)" % (oid[:8], len(oid)))
    if args.page:
        page_check(oid)
        print()

    if not args.loop:
        state, _ = check(oid)
        return 0 if state == "ok" else (1 if state == "invalid" else 2)

    logf = open(args.log, "a", encoding="utf-8") if args.log else None
    print("持续观测，每 %ss 一次（Ctrl+C 结束）" % args.loop)
    last = None
    t0 = time.time()
    try:
        while True:
            state, items = check(oid, verbose=(state != last))
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            if state != last:
                el = int(time.time() - t0)
                print("[%s] 状态变化 -> %s（已观测 %ds）" % (ts, state, el))
                if logf:
                    logf.write(json.dumps({"ts": ts, "state": state,
                                           "elapsed": el,
                                           "count": len(items or [])}) + "\n")
                    logf.flush()
                last = state
                if state == "invalid":
                    print("  已失效，停止观测")
                    return 1
            time.sleep(args.loop)
    except KeyboardInterrupt:
        return 0
    finally:
        if logf:
            logf.close()


if __name__ == "__main__":
    sys.exit(main())
