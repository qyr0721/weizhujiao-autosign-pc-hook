#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
classify_test.py — 测 trigger_oauth.py 的判定与 URL 构造（纯函数，不需要微信）

为什么值得测：判定表直接决定"能不能触发授权"这个结论的可靠性。
STUB（空壳页）和 INTERCEPTED（真授权页）必须严格区分，否则会把
"没被接管"误判成"可以自动化续期"。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import trigger_oauth as t  # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, name, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  " + name)
    else:
        FAIL += 1
        print("  FAIL  " + name + ("  <- %s" % detail if detail else ""))


STUB_BODY = ("<script>var ua=navigator.userAgent.toLowerCase();"
             "var isWeixin=ua.indexOf('micromessenger')!=-1;if(!isWeixin){}</script>")

# 1. 原生弹了新窗口 —— 最高优先级，即使 url 序列里还有 waerrpage
v, _ = t.classify(["https://x/"], "", "",
                  ["https://open.weixin.qq.com/connect/oauth2/authorize?x"])
ok(v == "NATIVE_WINDOW", "新 target 优先判为 NATIVE_WINDOW", v)

# 2. 业务域名白名单拦截
v, why = t.classify(["https://mp.weixin.qq.com/mp/waerrpage?appid=x&type=jump2h5err"],
                    "", "", [])
ok(v == "BLOCKED", "waerrpage / jump2h5err 判为 BLOCKED", v)

# 3. 停在 authorize 但页面是那个 UA 分流空壳
v, why = t.classify(["https://open.weixin.qq.com/connect/oauth2/authorize?x"],
                    STUB_BODY, "", [])
ok(v == "STUB", "极小 body + micromessenger 判为 STUB（未被接管）", "%s / %s" % (v, why))

# 4. 真授权页
real_body = "微信授权 登录后即可使用该服务 " + ("内容" * 400)
v, why = t.classify(["https://open.weixin.qq.com/connect/oauth2/authorize?x"],
                    real_body, "微信登录", [])
ok(v == "INTERCEPTED", "大 body 且含授权文案判为 INTERCEPTED", "%s / %s" % (v, why))

# 5. 边界：body 大但没有任何授权特征，不应谎报 INTERCEPTED
v, _ = t.classify(["https://open.weixin.qq.com/connect/oauth2/authorize?x"],
                  "x" * 3000, "", [])
ok(v == "UNKNOWN", "大 body 但无授权特征 → UNKNOWN（不谎报成功）", v)

# 6. 跳到别处
v, _ = t.classify(["https://v18.teachermate.cn/wechat-pro-ssr/student/sign?openid=1"],
                  "abc", "", [])
ok(v == "UNKNOWN", "跳到业务域判为 UNKNOWN", v)


# 7. URL 构造：redirect_uri 必须被编码，scope 可切换
class A:
    authorize_url = ""
    appid = "wxTEST"
    redirect = "https://app.teachermate.com.cn/sign?a=1"
    scope = "snsapi_userinfo"


u = t.build_url(A())
ok("appid=wxTEST" in u, "URL 带 appid")
ok("https%3A%2F%2Fapp.teachermate.com.cn" in u, "redirect_uri 已 URL 编码")
ok("&" not in u.split("redirect_uri=")[1].split("&response_type")[0],
   "redirect_uri 内部无裸 &")
ok("scope=snsapi_userinfo" in u, "scope 可配置")
ok(u.endswith("#wechat_redirect"), "以 #wechat_redirect 结尾")

# 8. --authorize-url 整条覆盖
A.authorize_url = "https://open.weixin.qq.com/connect/oauth2/authorize?appid=RAW"
ok(t.build_url(A()) == A.authorize_url, "--authorize-url 覆盖生效")

print("\n%s  (pass=%d, fail=%d)" % ("ALL PASS" if FAIL == 0 else "%d FAILED" % FAIL, PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
