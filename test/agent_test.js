/**
 * agent_test.js — 在 Node 里用 vm + 桩件真跑 page_agent.js
 *
 * 目的：page_agent.js 是整套方案的执行核心，但注入到真页面之前从未被执行过。
 *      这里用最小宿主环境（location / fetch / XHR / document）把它跑起来，
 *      验证 10 条行为契约。不依赖微信、不依赖浏览器。
 *
 * 跑：node test/agent_test.js
 */
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const AGENT = path.join(__dirname, "..", "page_agent.js");
const OID = "53f57e732b3b66c7a72c17173bcdf32e";
const SIGN_URL = "https://v18.teachermate.cn/wechat-pro-ssr/student/sign?openid=" + OID;

let pass = 0;
let fail = 0;
function ok(cond, name, detail) {
    if (cond) {
        pass += 1;
        console.log("  PASS  " + name);
    } else {
        fail += 1;
        console.log("  FAIL  " + name + (detail ? "  <- " + detail : ""));
    }
}

/* ---------------- 可编程的宿主环境 ---------------- */
function makeHost(scenario) {
    const fetchCalls = [];

    const fetchStub = (url, opt) => {
        const u = String(url);
        const o = opt || {};
        fetchCalls.push({ url: u, opt: o });

        let status = 200;
        let body = "[]";
        if (u.indexOf("active_signs") >= 0) {
            status = scenario.pollStatus || 200;
            body = JSON.stringify(scenario.activeSigns || []);
        } else if (u.indexOf("student-sign-in") >= 0) {
            status = scenario.signStatus || 200;
            body = scenario.signBody ||
                JSON.stringify({ signRank: 1, score: 1 });
        }
        return Promise.resolve({
            status: status,
            text: () => Promise.resolve(typeof body === "string" ? body : JSON.stringify(body)),
        });
    };
    fetchStub.calls = fetchCalls;

    function XHR() {
        this._h = {};
    }
    XHR.prototype.open = function (m, u) { this._m = m; this._u = u; };
    XHR.prototype.setRequestHeader = function (k, v) { this._h[k] = v; };
    XHR.prototype.send = function () {};

    const location = {
        href: SIGN_URL,
        search: "?openid=" + OID,
        origin: "https://v18.teachermate.cn",
    };

    const sandbox = {
        location,
        document: { cookie: "a=1", body: { innerText: "" }, title: "签到" },
        navigator: { userAgent: "test-ua" },
        console: { log: () => {} },
        fetch: fetchStub,
        XMLHttpRequest: XHR,
        setTimeout: () => 0,          // 不真正调度，避免定时器干扰
        clearTimeout: () => {},
        URL,
        URLSearchParams,
        Promise,
        JSON,
        Math,
        Date,
        Object,
        Array,
        String,
        Number,
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    return { sandbox, fetchCalls, location };
}

function load(scenario, cfg) {
    const host = makeHost(scenario);
    // 默认关掉"上课时段"闸门：否则凌晨跑测试会正确地拒绝轮询，干扰断言
    host.sandbox.window.__WZ_CONFIG = Object.assign({ onlyClassHours: false }, cfg || {});
    const src = fs.readFileSync(AGENT, "utf8");
    const ctx = vm.createContext(host.sandbox);
    const ret = vm.runInContext(src, ctx, { filename: "page_agent.js" });
    return Object.assign(host, { ctx, ret });
}

const signCalls = (calls) => calls.filter((c) => c.url.indexOf("student-sign-in") >= 0);

(async () => {
    /* --- 1. 安装 --- */
    let h = load({ activeSigns: [] });
    ok(typeof h.ret === "string" && h.ret.indexOf("installed") === 0, "注入返回 installed", h.ret);
    ok(h.sandbox.window.__wzSignAgent && h.sandbox.window.__wzSignAgent.version === "1.0.0",
       "挂上 __wzSignAgent 且版本正确");
    h.sandbox.window.__wzSignAgent.api.stop();

    /* --- 2. 从 location 自举凭证 --- */
    const st0 = h.sandbox.window.__wzSignAgent.api.status();
    ok(st0.hasCred === true, "从 URL query 自举到凭证");
    ok(st0.cred === undefined && JSON.stringify(st0).indexOf(OID) < 0,
       "status() 不外泄凭证本体");

    /* --- 3. 轮询：无签到 --- */
    await h.sandbox.window.__wzSignAgent.api.signNow();
    const poll1 = h.fetchCalls.filter((c) => c.url.indexOf("active_signs") >= 0);
    ok(poll1.length === 1, "signNow 发出 1 次 active_signs");
    ok(poll1[0].url === "https://www.teachermate.com.cn/wechat-api/v1/class-attendance/student/active_signs" ||
       poll1[0].url.indexOf("/wechat-api/v1/class-attendance/student/active_signs") > 0,
       "active_signs 路径正确", poll1[0].url);
    ok(poll1[0].opt.credentials === "include", "带上 credentials:include");
    ok(poll1[0].opt.headers.openId === OID, "带上 openId 头", JSON.stringify(poll1[0].opt.headers));

    /* --- 4. 命中签到 -> 提交 --- */
    h = load({ activeSigns: [{ signId: "s1", courseId: "c1" }] });
    h.sandbox.window.__wzSignAgent.api.stop();
    await h.sandbox.window.__wzSignAgent.api.signNow();
    const posts = signCalls(h.fetchCalls);
    ok(posts.length === 1, "命中后发出 1 次 student-sign-in");
    const body = JSON.parse(posts[0].opt.body);
    ok(body.signId === "s1" && body.courseId === "c1", "提交体含 signId/courseId", JSON.stringify(body));
    ok(posts[0].opt.method === "POST", "提交方法为 POST");
    ok(posts[0].opt.headers["Content-Type"] === "application/json", "提交带 Content-Type");

    /* --- 5. 幂等：同一个 signId 不再重复提交 --- */
    await h.sandbox.window.__wzSignAgent.api.signNow();
    ok(signCalls(h.fetchCalls).length === 1, "同一 signId 不重复提交（幂等）");

    /* --- 6. 已签到响应也算成功 --- */
    h = load({ activeSigns: [{ signId: "s2", courseId: "c2" }],
               signBody: '{"errorCode":305,"message":"repeat sign in"}' });
    h.sandbox.window.__wzSignAgent.api.stop();
    await h.sandbox.window.__wzSignAgent.api.signNow();
    const st6 = h.sandbox.window.__wzSignAgent.api.status();
    ok((st6.signed || []).indexOf("s2") >= 0, "errorCode 305 / repeat 视为已签到");

    /* --- 7. 401 -> auth=expired，且不导航 --- */
    h = load({ pollStatus: 401 });
    h.sandbox.window.__wzSignAgent.api.stop();
    const hrefBefore = h.location.href;
    await h.sandbox.window.__wzSignAgent.api.signNow();
    const st7 = h.sandbox.window.__wzSignAgent.api.status();
    ok(st7.auth === "expired", "401 时 auth=expired", st7.auth);
    ok(h.location.href === hrefBefore, "401 时绝不自行导航（保留可用页面）");

    /* --- 8. GPS 签到带坐标 --- */
    h = load({ activeSigns: [{ signId: "s3", courseId: "c3", isGPS: 1 }] },
             { pos: { lat: 30.1234, lon: 120.5678 } });
    h.sandbox.window.__wzSignAgent.api.stop();
    await h.sandbox.window.__wzSignAgent.api.signNow();
    const posts8 = signCalls(h.fetchCalls);
    const b8 = JSON.parse(posts8[0].opt.body);
    ok(b8.lat === 30.1234 && b8.lon === 120.5678, "isGPS 且配置了 pos 时带坐标", JSON.stringify(b8));

    /* --- 9. fetch 钩子捕获页面自身请求（注意用带 .com 的真实 API 域名） --- */
    h = load({ activeSigns: [] });
    h.sandbox.window.__wzSignAgent.api.stop();
    await h.sandbox.window.fetch(
        "https://www.teachermate.com.cn/wechat-api/v1/class-attendance/student/active_signs",
        { method: "GET", headers: { openId: "NEWOID0123456789abcdef" } }
    );
    const st9raw = h.sandbox.window.__wzSignAgent.state;
    ok(st9raw.lastReq && st9raw.lastReq.url.indexOf("www.teachermate.com.cn") > 0,
       "fetch 钩子捕获到 teachermate.com.cn 的请求（域名正则覆盖）",
       JSON.stringify(st9raw.lastReq && st9raw.lastReq.url));
    ok(st9raw.cred === "NEWOID0123456789abcdef", "钩子把新 openId 采纳为当前凭证", st9raw.cred);
    // 凭证轮换：再跑一轮应使用新 openId
    await h.sandbox.window.__wzSignAgent.api.signNow();
    const lastPoll = h.fetchCalls.filter((c) => c.url.indexOf("active_signs") >= 0).pop();
    ok(lastPoll.opt.headers.openId === "NEWOID0123456789abcdef",
       "钩子捕获到的新 openId 被后续请求沿用", lastPoll.opt.headers.openId);

    /* --- 10. XHR 钩子 --- */
    h = load({ activeSigns: [] });
    h.sandbox.window.__wzSignAgent.api.stop();
    const xhr = new h.sandbox.XMLHttpRequest();
    xhr.open("GET", "https://www.teachermate.com.cn/wechat-api/v1/class-attendance/student/active_signs");
    xhr.setRequestHeader("openId", "XHROID000000000000000000");
    xhr.send();
    await h.sandbox.window.__wzSignAgent.api.signNow();
    const xhrPoll = h.fetchCalls.filter((c) => c.url.indexOf("active_signs") >= 0).pop();
    ok(xhrPoll.opt.headers.openId === "XHROID000000000000000000",
       "XHR 钩子捕获的 openId 被沿用", xhrPoll.opt.headers.openId);

    console.log("\n" + (fail === 0 ? "ALL PASS" : fail + " FAILED") +
                "  (pass=" + pass + ", fail=" + fail + ")");
    process.exit(fail === 0 ? 0 : 1);
})();
