/*
 * hook_a8key.js — 盯住微信内置浏览器的 a8key 导航拦截层
 *
 * 背景（静态分析 flue.dll 得出）：
 *   ../flue/browser/a8key/ 下是微信的 a8key 机制，核心是
 *   A8KeyNavigationThrottle::CheckURL / ::Redirect —— 它决定一次导航
 *   要不要被"改写并重定向"。open.weixin.qq.com 与 oauth2/authorize
 *   这两个常量就出现在这套逻辑里，说明 OAuth 的接管点在这里。
 *
 * 因此这里挂两类钩子：
 *   1) 函数入口（精确 RVA，来自 .pdata 的函数边界）
 *   2) 函数内部的"标记点"（引用日志字符串的那条指令地址），用来判断流程走到哪一步
 *
 * 全程只读：不改任何寄存器/内存，不构造调用。目的先"看见"，再决定怎么改写。
 *
 * RVA 对 flue.dll 版本敏感（本表是 25799）。换版本请用 tools/pe_scan.py 重扫。
 */
const CFG = @@CONFIG@@;

const HOOKS = CFG.hooks || [];
const VERBOSE = CFG.verbose !== false;
const SNAPSHOT = CFG.snapshot !== false;

function modBase() {
    const m = Process.findModuleByName(CFG.module || "flue.dll");
    return m ? m.base : null;
}

/* 安全读：只在可读区间内、且长度合理时读 */
function safeRead(addr, len) {
    try {
        if (!addr || addr.isNull()) return null;
        const r = Process.findRangeByAddress(addr);
        if (!r || !r.protection.includes("r")) return null;
        if (addr.add(len).compare(r.base.add(r.size)) > 0) return null;
        return addr.readByteArray(len);
    } catch (e) {
        return null;
    }
}

/* 在指针附近捞可打印串（URL 常常就在对象里） */
function sniffStrings(addr, len) {
    const buf = safeRead(addr, len);
    if (!buf) return [];
    const bytes = new Uint8Array(buf);
    const out = [];
    let cur = "";
    for (let i = 0; i < bytes.length; i++) {
        const c = bytes[i];
        if (c >= 0x20 && c < 0x7f) {
            cur += String.fromCharCode(c);
        } else {
            if (cur.length >= 12) out.push(cur);
            cur = "";
        }
        if (out.length >= 4) break;
    }
    if (cur.length >= 12) out.push(cur);
    // 只留像 URL / 路径的
    return out.filter(s => /https?:\/\/|weixin\.qq\.com|oauth2|teachermate/.test(s)).slice(0, 3);
}

/* 读 MSVC 的 std::string：{buf/ptr(16B), size(8B), cap(8B)}，size<16 时内联 */
function readStdString(p) {
    try {
        if (!p || p.isNull()) return null;
        const r = Process.findRangeByAddress(p);
        if (!r || !r.protection.includes("r")) return null;
        const size = p.add(16).readU64().toNumber();
        if (size < 0 || size > 8192) return null;
        const dataPtr = size < 16 ? p : p.readPointer();
        const buf = safeRead(dataPtr, Math.min(size, 1024));
        if (!buf) return null;
        const bytes = new Uint8Array(buf);
        let out = "";
        for (let i = 0; i < Math.min(bytes.length, 600); i++) {
            const c = bytes[i];
            if (c === 0) break;
            out += (c >= 0x20 && c < 0x7f) ? String.fromCharCode(c) : ".";
        }
        return out;
    } catch (e) {
        return null;
    }
}

function labelFor(h) {
    return h.name + "@0x" + h.rva.toString(16);
}

function install() {
    const base = modBase();
    if (!base) {
        send({ type: "fatal", msg: "找不到 flue.dll" });
        return;
    }
    send({ type: "base", module: CFG.module || "flue.dll", base: base.toString(), hooks: HOOKS.length });

    let ok = 0;
    for (const h of HOOKS) {
        const addr = base.add(h.rva);
        try {
            Interceptor.attach(addr, {
                onEnter(args) {
                    const msg = {
                        type: "hit",
                        name: h.name,
                        kind: h.kind || "fn",
                        rva: h.rva,
                        tid: this.threadId,
                        args: [args[0], args[1], args[2], args[3]].map(a => {
                            try { return a.toString(); } catch (e) { return "?"; }
                        })
                    };
                    if (SNAPSHOT && (h.kind === "fn")) {
                        if (h.read === "std2") {
                            // MMV8CallNative(api_name, args_json) —— 抓页面调了哪个 API
                            const api = readStdString(args[1]);
                            const arg = readStdString(args[2]);
                            if (api) msg.api = api;
                            if (arg) msg.apiArgs = arg;
                        } else {
                            // 从第 2/3 个参数附近找 URL 线索（GURL/std::string 常在这两个里）
                            for (const idx of [1, 2]) {
                                const cand = sniffStrings(args[idx], 320);
                                if (cand.length) {
                                    msg.urls = cand;
                                    break;
                                }
                            }
                        }
                    }
                    send(msg);
                }
            });
            ok += 1;
        } catch (e) {
            send({ type: "fail", name: h.name, rva: h.rva, err: String(e) });
        }
    }
    send({ type: "installed", ok: ok, total: HOOKS.length });
}

rpc.exports = {
    info() {
        const base = modBase();
        return { base: base ? base.toString() : null, hooks: HOOKS.map(labelFor) };
    },
    listModules() {
        return Process.enumerateModules().filter(m => /flue|WeChat/.test(m.name))
            .map(m => ({ name: m.name, base: m.base.toString(), size: m.size }));
    }
};

install();
