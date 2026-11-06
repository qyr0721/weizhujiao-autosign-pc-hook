#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
gui_test.py — 图形界面与教程渲染（纯 Python，不需要启动服务 / 微信）

  python test/gui_test.py

测什么
  · 教程 Markdown 渲染：标题与目录、表格、代码块、引用、列表、两种图片写法
  · 行内元素：粗体、行内代码、链接、HTML 转义（防注入）
  · 教程页与主界面模板的占位符都被正确替换
  · run._nap 能被 GUI 的停止信号中断（原来直接 sleep 要等一整个轮询周期）
"""
import asyncio
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import app        # noqa: E402
import run as runmod  # noqa: E402

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


def test_inline():
    print("\n=== 行内元素 ===")
    ok("<strong>加粗</strong>" in app._inline("这是**加粗**"), "粗体")
    ok("<code>poll</code>" in app._inline("字段 `poll`"), "行内代码")
    ok('<a href="https://example.com"' in app._inline("[说明](https://example.com)"),
       "链接")
    ok('target="_blank"' in app._inline("[说明](https://example.com)"), "链接新标签打开")
    ok("&lt;script&gt;" in app._inline("<script>alert(1)</script>"), "HTML 被转义")
    ok("<script>" not in app._inline("<script>alert(1)</script>"), "不产生可执行标签")


def test_images():
    print("\n=== 两种插图写法 ===")
    md = app._inline('<img src="docs/img/a.png" width="420" alt="授权页">')
    ok('src="/img/a.png"' in md and 'max-width:420px' in md, "HTML <img> 标签")
    ok('alt="授权页"' in md, "HTML img 的 alt 保留")
    md2 = app._inline("![小程序](docs/img/b.png)")
    ok('src="/img/b.png"' in md2, "Markdown 图片语法")
    ok('alt="小程序"' in md2, "Markdown 图片的 alt 保留")
    ok("<a href=" not in md2, "Markdown 图片不能退化成链接")


def test_markdown():
    print("\n=== 教程结构渲染 ===")
    md = ("# 标题\n\n## 第一步\n\n说明文字。\n\n"
          "| 列A | 列B |\n| --- | --- |\n| 值1 | 值2 |\n\n"
          "> 引用一行\n> 引用二行\n\n"
          "- 项目一\n- 项目二\n\n"
          "```\n代码块\n```\n\n---\n\n### 小节\n")
    body, nav = app.markdown_to_html(md)
    ok("<h1 " in body and "<h2 " in body and "<h3 " in body, "三级标题")
    ok('href="#sec-' in nav and nav.count("<a ") == 2, "目录只收 h2/h3", nav)
    ok('<table class="md-table">' in body and "<th>列A</th>" in body, "表格")
    ok("<blockquote>" in body and "引用二行" in body, "引用块合并")
    ok("<ul>" in body and body.count("<li>") == 2, "无序列表")
    ok("<pre><code>" in body and "代码块" in body, "代码块")
    ok("<hr>" in body, "分隔线")

    real, real_nav = app.markdown_to_html(app.tutorial_text())
    ok(real.count('<img class="md-img"') == 2, "真实教程的两张配图都渲染为图片",
       str(real.count('<img class="md-img"')))
    ok("/img/oauth-allow.png" in real and "/img/miniapp-open-from-recent.png" in real,
       "配图指向本地 /img 路由")
    ok(len(real_nav) > 200 and 'href="#sec-' in real_nav, "真实教程生成目录")


def test_pages():
    print("\n=== 页面模板与资源 ===")
    page = app.tutorial_page()
    ok("__BODY__" not in page and "__NAV__" not in page, "教程页占位符已替换")
    ok("使用教程" in page and '<article>' in page, "教程页含正文容器")
    ok("/img/oauth-allow.png" in page, "教程页含授权页配图")
    import html as H
    home = (app.INDEX_HTML.replace("__APP_NAME__", H.escape(app.APP_NAME))
            .replace("__VERSION__", H.escape(app.VERSION)))
    ok("__APP_NAME__" not in home and "__VERSION__" not in home, "首页占位符可替换")
    for label in ("启动签到", "停止", "使用教程", "运行日志", "退出程序", "/tutorial"):
        ok(label in home, "首页含「%s」" % label)
    ok(os.path.isfile(app.resource_path("TUTORIAL.md")), "教程资源可定位")
    ok(os.path.isfile(app.image_path("oauth-allow.png")), "配图资源可定位")


def test_nap():
    print("\n=== 可中断等待 ===")
    stop = threading.Event()
    t0 = time.time()
    got = asyncio.run(runmod._nap(0.12, stop.is_set))
    ok(got is True and time.time() - t0 < 1.0, "未请求停止时正常等待结束")

    stop.set()
    t1 = time.time()
    got2 = asyncio.run(runmod._nap(10, stop.is_set))
    ok(got2 is False and time.time() - t1 < 1.0,
       "停止信号能立刻打断长等待", "耗时 %.2fs" % (time.time() - t1))


def main():
    test_inline()
    test_images()
    test_markdown()
    test_pages()
    test_nap()
    print("\n%d 通过，%d 失败" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
