/*
 * page_agent.js — 微助教签到页内 agent
 *
 * 用法：由 cdp_sign_agent.py 通过 Runtime.evaluate 注入到「签到页」这个 tab 的
 *      主世界（main world）里执行。注入后：
 *        window.__wzSignAgent.version                 读版本号（验证注入成功）
 *        window.__wzSignAgent.state                   读运行状态（主机轮询用）
 *        window.__wzSignAgent.api.status()            同上，函数式
 *        window.__wzSignAgent.api.signNow()           立即跑一轮
 *        window.__wzSignAgent.api.stop() / .start()
 *
 * 设计约束（与方案 §3.4 对应）：
 *   1) 绝不把凭证搬出页面：openid 只在页内使用，主机侧只读 state（不含凭证）
 *   2) 幂等优先：已签到一律按成功处理，不重试
 *   3) 401 不自动续期：只上报 auth="expired"，避免把可用页面导航死
 *   4) 域名/路径从页面上真实出现的请求反推，不硬编码
 */
(function () {
    "use strict";

    var VERSION = "1.0.0";
    var existing = window.__wzSignAgent;
    if (existing && existing.version === VERSION) {
        return "already-installed:" + VERSION;
    }

    var userCfg = window.__WZ_CONFIG || {};
    // 默认基址：与 signer.py 同一份契约。存在的意义是"页面在我注入前就加载完 "
    // 时也能立刻开工——不能只依赖从已发生请求里反推，否则会永远学不到基址。
    var DEFAULT_API = "https://www.teachermate.com.cn/wechat-api/v1/class-attendance";
    var CFG = {
        apiBase: userCfg.apiBase || "",              // 空 = 先用默认值，被捕获的请求修正
        pollMs: Math.max(3000, userCfg.pollMs || 5000),
        maxLog: 40,
        allowRenauth: false,                          // 方案 §3.4-5：默认禁用
        onlyClassHours: userCfg.onlyClassHours !== false,
        classHours: userCfg.classHours || [7, 23],
        submit: userCfg.submit !== false              // false = 只观察不提交（首次联调用）
    };

    // 域名要同时覆盖 teachermate.cn 与 teachermate.com.cn（API 在 www.teachermate.com.cn，
    // 页面在 v18.teachermate.cn）。写成 /teachermate\.cn/ 会漏掉带 .com 的那个。
    var SIGN_RE = /teachermate\.(?:com\.)?cn/i;
    var API_HINT = /wechat-api\/v1\/class-attendance/i;

    var state = {
        version: VERSION,
        installedAt: Date.now(),
        auth: "unknown",        // unknown | ok | expired
        cred: "",               // 只在页内，不外发
        apiBase: CFG.apiBase || DEFAULT_API,
        lastReq: null,          // {url, method, headers, body, ts}
        lastPoll: null,         // {ts, status, count}
        lastSign: null,         // {ts, status, resp}
        signed: {},             // signId -> ts
        attempt: 0,
        fails: 0,
        log: []
    };

    function log(msg, extra) {
        var item = { t: Date.now(), m: String(msg) };
        if (extra !== undefined) {
            try { item.x = JSON.parse(JSON.stringify(extra)); } catch (e) { item.x = String(extra); }
        }
        state.log.push(item);
        if (state.log.length > CFG.maxLog) state.log.shift();
        try { console.log("[WZ]", msg, extra === undefined ? "" : extra); } catch (e) {}
    }

    function absUrl(u) {
        try { return new URL(u, location.href).href; } catch (e) { return String(u); }
    }

    // 日志里绝不能出现带凭证的完整 URL —— status() 会被主机侧拉走并落盘
    function safeHref() {
        try { return location.origin + (location.pathname || ""); } catch (e) { return "?"; }
    }

    function inClassHours() {
        if (!CFG.onlyClassHours) return true;
        var h = new Date().getHours();
        return h >= CFG.classHours[0] && h < CFG.classHours[1];
    }

    /* ---------------- 凭证 / 基址 采集 ---------------- */

    function credFromLocation() {
        try {
            var q = new URLSearchParams(location.search);
            var v = q.get("openid") || q.get("openId") || q.get("oid");
            return v || "";
        } catch (e) { return ""; }
    }

    function adoptCred(v) {
        if (!v || typeof v !== "string") return;
        if (v.length < 16) return;
        if (state.cred && state.cred !== v) log("cred rotated");
        state.cred = v;
        state.auth = "ok";
    }

    function adoptApiBase(url) {
        if (CFG.apiBase) { state.apiBase = CFG.apiBase; return; }
        var i = url.indexOf("/wechat-api/");
        if (i > 0) state.apiBase = url.slice(0, i) + "/wechat-api/v1/class-attendance";
    }

    function capture(url, method, headers, body) {
        var full = absUrl(url);
        if (!SIGN_RE.test(full)) return;
        adoptApiBase(full);
        var hdr = headers || {};
        var oid = hdr.openId || hdr.openid || hdr["open-id"] || "";
        if (oid) adoptCred(oid);
        if (API_HINT.test(full)) {
            state.lastReq = {
                url: full, method: (method || "GET").toUpperCase(),
                headers: hdr, body: body || null, ts: Date.now()
            };
        }
    }

    function headerObj(h) {
        var out = {};
        if (!h) return out;
        try {
            if (typeof h.forEach === "function" && !Array.isArray(h)) {
                h.forEach(function (v, k) { out[k] = v; });
            } else if (Array.isArray(h)) {
                for (var i = 0; i < h.length; i++) out[h[i][0]] = h[i][1];
            } else {
                for (var k in h) if (Object.prototype.hasOwnProperty.call(h, k)) out[k] = h[k];
            }
        } catch (e) {}
        return out;
    }

    /* ---------------- 钩子 ---------------- */

    function installFetchHook() {
        if (!window.fetch || window.fetch.__wz) return;
        var orig = window.fetch;
        var wrapped = function (input, init) {
            try {
                var url = typeof input === "string" ? input : (input && input.url) || "";
                var method = (init && init.method) || (input && input.method) || "GET";
                var hdrs = headerObj((init && init.headers) || (input && input.headers));
                var body = init && init.body;
                capture(url, method, hdrs, typeof body === "string" ? body : null);
            } catch (e) {}
            return orig.apply(this, arguments);
        };
        wrapped.__wz = true;
        window.fetch = wrapped;
    }

    function installXhrHook() {
        var X = window.XMLHttpRequest;
        var P = X && X.prototype;
        // 注意：这里必须容错。若 P / P.open 不存在就往下走，会在装配阶段抛异常，
        // 导致整个 agent 装不上（真页面上 XHR 可能被延迟定义或被改写）。
        if (!P || typeof P.open !== "function" || typeof P.send !== "function") return;
        if (P.open.__wz) return;
        var origOpen = P.open, origSet = P.setRequestHeader, origSend = P.send;
        P.open = function (method, url) {
            try { this.__wz = { method: method, url: url, headers: {} }; } catch (e) {}
            return origOpen.apply(this, arguments);
        };
        P.open.__wz = true;
        P.setRequestHeader = function (k, v) {
            try { if (this.__wz) this.__wz.headers[k] = v; } catch (e) {}
            return origSet.apply(this, arguments);
        };
        P.send = function (body) {
            try {
                if (this.__wz) capture(this.__wz.url, this.__wz.method, this.__wz.headers,
                                       typeof body === "string" ? body : null);
            } catch (e) {}
            return origSend.apply(this, arguments);
        };
    }

    /* ---------------- 业务请求 ---------------- */

    function api(path) {
        var base = state.apiBase;
        if (!base) return "";
        return base + path;
    }

    function req(method, path, body) {
        var url = api(path);
        if (!url) return Promise.reject(new Error("apiBase-unknown"));
        var opt = {
            method: method,
            credentials: "include",
            headers: { "openId": state.cred }
        };
        if (body) {
            opt.headers["Content-Type"] = "application/json";
            opt.body = JSON.stringify(body);
        }
        return fetch(url, opt).then(function (r) {
            return r.text().then(function (txt) {
                return { status: r.status, text: txt };
            });
        });
    }

    function isSigned(text) {
        return /repeat sign in/i.test(text) || /已经签到|已签到/.test(text) ||
               /"errorCode"\s*:\s*305/.test(text);
    }

    function submitOne(item) {
        var signId = item.signId, courseId = item.courseId;
        if (!signId) return Promise.resolve();
        if (state.signed[signId]) return Promise.resolve();

        var body = { courseId: courseId, signId: signId };
        // GPS 签到：沿用页面/配置提供的坐标，agent 不凭空造点
        var pos = userCfg.pos || null;
        if (item.isGPS && pos && typeof pos.lat === "number" && typeof pos.lon === "number") {
            body.lat = pos.lat;
            body.lon = pos.lon;
        }

        log("sign-in", { signId: signId, courseId: courseId, gps: !!item.isGPS });
        return req("POST", "/student-sign-in", body).then(function (r) {
            state.lastSign = { ts: Date.now(), status: r.status, resp: String(r.text).slice(0, 300) };
            if (r.status === 200 && (/signRank/i.test(r.text) || isSigned(r.text))) {
                state.signed[signId] = Date.now();
                state.fails = 0;
                log("sign-in ok", { signId: signId });
            } else if (r.status === 401) {
                state.auth = "expired";
                log("sign-in 401");
            } else {
                state.fails += 1;
                log("sign-in fail", { status: r.status, body: String(r.text).slice(0, 160) });
            }
        }).catch(function (e) {
            state.fails += 1;
            log("sign-in err", String(e));
        });
    }

    function pollOnce() {
        if (!state.cred) {
            adoptCred(credFromLocation());
        }
        if (!state.cred) { state.auth = "unknown"; return Promise.resolve(); }
        if (!inClassHours()) return Promise.resolve();

        state.attempt += 1;
        return req("GET", "/student/active_signs", null).then(function (r) {
            state.lastPoll = { ts: Date.now(), status: r.status, count: 0 };
            if (r.status === 401) {
                state.auth = "expired";
                log("poll 401");
                if (CFG.allowRenauth) log("renauth skipped by design");
                return;
            }
            if (r.status !== 200) {
                state.fails += 1;
                log("poll http " + r.status);
                return;
            }
            state.auth = "ok";
            var items = [];
            try { items = JSON.parse(r.text) || []; } catch (e) { items = []; }
            if (!Array.isArray(items)) items = [];
            state.lastPoll.count = items.length;
            if (!items.length) return;
            if (!CFG.submit) { log("observe-only", { count: items.length }); return; }
            var chain = Promise.resolve();
            items.forEach(function (it) {
                chain = chain.then(function () { return submitOne(it); });
            });
            return chain;
        }).catch(function (e) {
            state.fails += 1;
            log("poll err", String(e));
        });
    }

    /* ---------------- 调度 ---------------- */

    var timer = null;
    function start() {
        if (timer) return;
        var tick = function () {
            pollOnce().then(function () {
                timer = setTimeout(tick, CFG.pollMs + Math.floor(Math.random() * 1200));
            }, function () {
                timer = setTimeout(tick, CFG.pollMs);
            });
        };
        timer = setTimeout(tick, 800);
        log("started", { pollMs: CFG.pollMs });
    }
    function stop() {
        if (timer) { clearTimeout(timer); timer = null; log("stopped"); }
    }

    /* ---------------- 装配 ---------------- */

    installFetchHook();
    installXhrHook();
    adoptCred(credFromLocation());
    state.apiBase = CFG.apiBase || DEFAULT_API;
    log("installed", { href: safeHref(), cred: !!state.cred });
    start();

    window.__wzSignAgent = {
        version: VERSION,
        get state() { return state; },
        get config() { return CFG; },
        api: {
            status: function () {
                return {
                    version: VERSION, auth: state.auth, hasCred: !!state.cred,
                    apiBase: state.apiBase, attempt: state.attempt, fails: state.fails,
                    lastPoll: state.lastPoll, lastSign: state.lastSign,
                    signed: Object.keys(state.signed), log: state.log.slice(-10)
                };
            },
            signNow: pollOnce,
            stop: stop,
            start: start,
            setPos: function (lat, lon) { userCfg.pos = { lat: lat, lon: lon }; }
        }
    };

    return "installed:" + VERSION;
})();
