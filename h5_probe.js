/*
 * h5_probe.js — 在 H5 标签页里做一次"能力体检"，结果以 JSON 字符串返回
 *
 * 回答三个问题（都不导航、不破坏当前授权）：
 *   1) 这个页面到底是不是"真·微信服务号 H5"上下文（JSSDK / bridge 是否注入）
 *   2) 页内能不能拿到 code —— WeixinJSBridge.invoke('login') 是否可用
 *      （这是"绕开 OAuth 在页内续期"的关键前提，比导航试验安全得多）
 *   3) 页面自报的 UA / 来源，用于和其它 target 区分
 *
 * 由 auto_probe.py 通过 Runtime.evaluate(awaitPromise=true) 注入。
 * 需要参数时，先 evaluate 一句 window.__H5P = {login:false} 即可。
 */
(function () {
    "use strict";

    var opt = window.__H5P || {};
    var wantLogin = opt.login !== false;

    function base() {
        var wx = window.wx;
        var bridge = window.WeixinJSBridge;
        var out = {
            href: location.href,
            origin: location.origin,
            title: (document.title || "").slice(0, 80),
            ua: (navigator.userAgent || "").slice(0, 220),
            hasBridge: typeof bridge !== "undefined",
            bridgeInvoke: !!(bridge && typeof bridge.invoke === "function"),
            hasWx: typeof wx !== "undefined",
            wxConfig: !!(wx && typeof wx.config === "function"),
            wxReady: !!(wx && typeof wx.ready === "function"),
            wxKeys: (wx && typeof wx === "object") ? Object.keys(wx).slice(0, 24) : [],
            miniProgram: !!(wx && wx.miniProgram),
            cookieLen: (document.cookie || "").length,
            bodyLen: document.body ? document.body.innerText.length : 0
        };
        return out;
    }

    var result = base();

    if (!wantLogin || !result.bridgeInvoke) {
        result.login = result.bridgeInvoke ? "skipped" : "no-bridge";
        return JSON.stringify(result);
    }

    // invoke('login') 是异步回调；用 Promise 让 awaitPromise 等到结果
    return new Promise(function (resolve) {
        var done = false;
        function finish(patch) {
            if (done) return;
            done = true;
            for (var k in patch) result[k] = patch[k];
            resolve(JSON.stringify(result));
        }
        var timer = setTimeout(function () {
            finish({ login: "timeout" });
        }, 4000);
        try {
            window.WeixinJSBridge.invoke("login", {}, function (res) {
                clearTimeout(timer);
                var code = res && (res.code || (res.err_msg ? null : null));
                finish({
                    login: code ? "code" : "no-code",
                    loginErrMsg: res && res.err_msg ? String(res.err_msg).slice(0, 120) : null,
                    codeLen: code ? String(code).length : 0,
                    codeHead: code ? String(code).slice(0, 6) : null
                });
            });
        } catch (e) {
            clearTimeout(timer);
            finish({ login: "throw", loginErrMsg: String(e).slice(0, 120) });
        }
    });
})();
