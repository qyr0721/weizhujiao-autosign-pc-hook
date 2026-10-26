#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ui_test.py — 面板布局与状态机（不需要微信，纯字符串断言）

  python test/ui_test.py

测什么
  · 每一行显示宽度一致（中文算 2 列），边框不会歪
  · 超长文本按显示宽度截断，不撑破边框
  · 日志文本 → 面板两句话 的映射
  · 纯文本模式不输出框线
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wizard as W  # noqa: E402
import uikit as U    # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s   %s" % (name, extra))


def base_info(**kw):
    d = {
        "wechat": "4.1.15.56", "wmpf": "25799", "wmpf_ok": True,
        "ok": True, "note": "全部就绪",
        "action": "等待小程序连接",
        "hint": ["在微信打开：「小程序」→ 搜索「小程序示例」",
                 "微信官方示例，体积很小；打开后可缩到后台"],
        "sign": "", "sign_kind": "none",
        "logfile": "logs\\wxsign-20261025-0251.log",
    }
    d.update(kw)
    return d


def test_first_run_auth_hint():
    """首次使用（还没有 openid）要在面板上提示手动授权。

    实测：全新环境下微信里没有可复用的会话，取号永远失败，
    面板只说「等待取号」用户不知道该干什么。必须在启动时就说清楚。
    """
    import shutil
    import tempfile

    print("\n=== 首次使用授权提示 ===")

    # 1. 判断「有没有号」
    d = tempfile.mkdtemp()
    p = os.path.join(d, "openid.txt")
    ok(W._has_valid_oid(p) is False, "文件不存在 → 需要授权")
    with open(p, "w", encoding="utf-8") as f:
        f.write("")
    ok(W._has_valid_oid(p) is False, "空文件 → 需要授权")
    with open(p, "w", encoding="utf-8") as f:
        f.write("not-an-openid\n")
    ok(W._has_valid_oid(p) is False, "格式不对 → 需要授权")
    with open(p, "w", encoding="utf-8") as f:
        f.write("60536dd83a0a067b61c3cf1eaa3a7cf9\n# url=...\n")
    ok(W._has_valid_oid(p) is True, "32 位 hex → 已有号，不提示")
    shutil.rmtree(d, ignore_errors=True)

    # 2. 面板里出现三步
    pnl = W.Panel(U.Term(plain=True))
    rows = pnl.compose(base_info(need_auth=True, action="首次使用"))
    text = "\n".join(rows)
    ok("首次使用" in text, "need_auth 时显示首次使用标题")
    ok("签到(C)" in text, "写明要点「签到(C)」")
    ok("确认授权" in text, "写明要确认授权")
    ok(sum(1 for r in rows if "1." in r or "2." in r or "3." in r) >= 3, "三步都在")

    # 3. 有号时不显示
    rows2 = pnl.compose(base_info(need_auth=False, action="监视中"))
    ok("首次使用" not in "\n".join(rows2), "已有号时不显示授权提示")

    # 4. 拿到号后自动撤掉
    st = W.State(base_info(need_auth=True))
    ok(st.info["need_auth"] is True, "初始需要授权")
    st.feed("  ✔ 拿到新号（S3-Page.navigate）：60536dd83a0a067b61c3cf1eaa3a7cf9")
    ok(st.info["need_auth"] is False, "拿到新号后提示消失")

    # 5. 未授权时动作行要说清楚（[签到] 前缀走 _feed_sign）
    st2 = W.State(base_info(need_auth=True))
    st2.feed("  [签到] 还没有 openid，等续号侧产出")
    ok("手动授权" in st2.info["action"], "未授权时说「等待手动授权」", st2.info["action"])

    st3 = W.State(base_info(need_auth=False))
    st3.feed("  [签到] 还没有 openid，等续号侧产出")
    ok("等待取号" in st3.info["action"], "已授权后仍是「等待取号」", st3.info["action"])


def test_config_bootstrap():
    """从 GitHub 下载的包里没有 signer/config.json，程序必须自己生成。

    否则用户根本不知道坐标该填在哪（这是真实反馈：下载后找不到配置文件）。
    """
    import json
    import shutil
    import tempfile

    class Log:
        def __init__(self):
            self.lines = []

        def write(self, m, tag=""):
            self.lines.append(tag + str(m))

    print("\n=== 配置文件自动生成 ===")

    # 1. 什么都没有 → 生成 signer/config.json
    d = tempfile.mkdtemp()
    log = Log()
    p = W._prepare_config(None, log, base=d)
    ok(os.path.isfile(p), "自动生成配置文件", p)
    ok(p.endswith(os.path.join("signer", "config.json")), "路径是 signer/config.json", p)
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    ok(data.get("lat") is None and data.get("lon") is None, "模板里坐标为 null", str(data))
    ok("jitter_deg" in data and "poll" in data, "模板含 jitter_deg / poll", str(data))
    ok(any("已生成配置文件" in s for s in log.lines), "日志写明生成了文件", str(log.lines))

    # 2. 已经存在 → 原样返回，不覆盖
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"lat": 30.5, "lon": 120.5}, f)
    p2 = W._prepare_config(None, log, base=d)
    with open(p2, encoding="utf-8") as f:
        again = json.load(f)
    ok(p2 == p and again["lat"] == 30.5, "已存在时不被覆盖", str(again))

    # 3. 只有同级 config.json → 用它
    d2 = tempfile.mkdtemp()
    root_cfg = os.path.join(d2, "config.json")
    with open(root_cfg, "w", encoding="utf-8") as f:
        json.dump({"lat": 31.1, "lon": 121.1}, f)
    p3 = W._prepare_config(None, log, base=d2)
    ok(p3 == root_cfg, "识别程序目录下的 config.json", p3)
    ok(not os.path.isdir(os.path.join(d2, "signer")), "不再重复造 signer 目录")

    # 4. 显式指定的路径优先
    d3 = tempfile.mkdtemp()
    custom = os.path.join(d3, "my.json")
    p4 = W._prepare_config(custom, log, base=d3)
    ok(p4 == custom and os.path.isfile(custom), "显式路径优先且会创建", p4)

    for x in (d, d2, d3):
        shutil.rmtree(x, ignore_errors=True)


def main():
    term = U.Term(plain=True)
    panel = W.Panel(term)

    print("=== 宽度工具 ===")
    ok(U.dwidth("abc") == 3, "ASCII 宽度")
    ok(U.dwidth("中文") == 4, "中文按 2 列")
    ok(U.dwidth("a中b") == 4, "混排宽度")
    ok(U.dwidth(U.fit("短", 10)) == 10, "fit 补齐到指定宽度")
    ok(U.dwidth(U.fit("超长文本" * 30, 20)) == 20, "fit 截断后宽度不变")
    ok("..." in U.fit("超长文本" * 30, 20), "截断处有 ASCII 省略标记")

    print("\n=== 布局对齐 ===")
    rows = panel.compose(base_info())
    box = rows[0:5]                      # 标题框：唯一带右边框的区域
    widths = {U.dwidth(r) for r in box}
    ok(len(widths) == 1, "标题框所有行同宽（%s）" % widths, str(sorted(widths)))
    ok(widths == {(W.INNER + 2)}, "宽度等于 TOTAL=%d" % (W.INNER + 2), str(widths))
    ok(box[0].startswith("┌") and box[-1].startswith("└"), "标题框上下边框正确")
    ok(all(r.startswith("│") and r.endswith("│") for r in box[1:4]), "标题框左右边框闭合")

    long_info = base_info(action="正在取号…" + "很长" * 60,
                          sign="12:34:56 签到成功 " + "x" * 100)
    rows2 = panel.compose(long_info)
    ok({U.dwidth(r) for r in rows2[0:5]} == {(W.INNER + 2)}, "超长内容不影响标题框对齐")

    print("\n=== 状态机：日志 → 面板 ===")
    st = W.State(base_info())

    ok(st.feed("正常值守：号 4026a8fb6a7302adcb175ce689b62be4 状态=ok，通道 26 个 target"),
       "值守消息触发重绘")
    ok("监视中" in st.info["action"], "值守 → 监视中", st.info["action"])
    ok("4026a8fb" in st.info["action"], "动作里带号前缀", st.info["action"])
    ok(st.channel is True, "通道标记为已连接")
    ok(st.info["hint"] == st.hint_live, "连上后提示语切换")

    st.feed("通道未就绪（no-reply）——微信里还没有小程序连上 9421。")
    ok(st.channel is False, "通道断开回落到未连接")
    ok("等待小程序" in st.info["action"], "断连 → 等待小程序", st.info["action"])
    ok(st.info["hint"] == st.hint_idle, "提示语切回打开小程序")

    st.feed("  [签到] ✔ 签到成功 signId=12345 {\"signRank\": 7}")
    ok(st.info["sign_kind"] == "ok", "签到成功 → 绿色 ok")
    ok("签到成功" in st.info["sign"], "签到行含成功字样", st.info["sign"])

    st.feed("  [签到] · 已签到（幂等）signId=12345")
    ok(st.info["sign_kind"] == "dup", "幂等 → dup 样式")

    st.feed("  [签到] ✘ 签到失败 signId=99 HTTP 500")
    ok(st.info["sign_kind"] == "fail", "失败 → 红色")

    st.feed("  [签到] openid 已失效（401，被顶掉）——请求续号")
    ok("换号" in st.info["action"], "401 → 提示换号", st.info["action"])

    # 通道没通时报 401，不能假装在换号（换不了）
    st2 = W.State(base_info())
    st2.feed("  [签到] openid 已失效（401，被顶掉）——请求续号")
    ok("等待小程序连接" in st2.info["action"], "通道未通时 401 归入等待", st2.info["action"])
    ok("换号" in st2.info["action"], "但仍说明连上后会换号", st2.info["action"])

    st.feed("  ✔ 拿到新号（S3-Page.navigate）：60536dd83a0a067b61c3cf1eaa3a7cf9")
    ok("换号成功" in st.info["action"], "拿到新号 → 换号成功", st.info["action"])
    ok("60536dd8" in st.info["action"], "新号前缀显示出来", st.info["action"])

    st.feed("需要取号：当前号已 401")
    ok("取号" in st.info["action"], "取号中状态", st.info["action"])

    ok(st.feed("完全无关的一行") is False, "无关日志不触发重绘")

    print("\n=== 纯文本模式 ===")
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        p2 = W.Panel(U.Term(plain=True))
        p2.render(base_info(action="监视中"))
    out = buf.getvalue()
    ok("│" not in out and "╭" not in out, "plain 模式不输出框线", repr(out[:40]))
    ok("监视中" in out, "plain 模式仍显示动作", repr(out))

    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        p2.render(base_info(action="监视中"))       # 内容未变
    ok(buf2.getvalue() == "", "内容未变时不重复输出", repr(buf2.getvalue()))

    print("\n=== ANSI 定点重绘 ===")

    class FakeTTY:
        def __init__(self):
            self.buf = []

        def write(self, s):
            self.buf.append(s)

        def flush(self):
            pass

        def isatty(self):
            return True

        def reconfigure(self, **kw):
            pass

    fake = FakeTTY()
    orig_stdout, orig_ansi = sys.stdout, U.enable_ansi
    U.enable_ansi = lambda title="": True
    sys.stdout = fake
    try:
        p3 = W.Panel(U.Term(plain=False))
        p3.render(base_info(action="第一步"))
        first = "".join(fake.buf)
        fake.buf.clear()
        p3.render(base_info(action="第一步"))       # 完全没变
        same = "".join(fake.buf)
        fake.buf.clear()
        p3.render(base_info(action="第二步"))       # 只变一行
        diff = "".join(fake.buf)
    finally:
        sys.stdout = orig_stdout
        U.enable_ansi = orig_ansi

    ok("\033[" in first, "ANSI 模式输出转义序列")
    ok("\033[1;1H" in first, "首行定位到 1,1")
    ok("第一步" in first, "动作文本进入输出")
    ok(same.count("│") == 0, "未变化时不重画任何行", repr(same[:40]))
    ok("第二步" in diff, "变化时画出新内容")
    ok(diff.count("微助教自动签到") == 0, "只重画变化的行，标题不重画", repr(diff[:60]))

    test_first_run_auth_hint()
    test_config_bootstrap()

    print("\n%d 通过，%d 失败" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
