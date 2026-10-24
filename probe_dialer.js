/*
 * probe_dialer.js — 找出浏览器进程里"谁在拨 ws://localhost:9421"
 *
 * 目的：点火依赖之所以存在，是因为远程调试客户端只在「小程序 OnLoadStart」时被创建。
 *      如果拨号点是某个通用函数（而非小程序专属），我们就能：
 *        a) 直接调用它来"伪造点火"，不用真的开小程序；
 *        b) 或者找到 H5 页面是否也走同一条路 —— 那点火就彻底不需要了。
 *
 * 手法：hook ws2_32!connect / WSAConnect，只关心目标端口 == PORT 的连接，
 *      命中后打印带 模块!符号+偏移 的调用栈（偏移跨版本比绝对地址稳）。
 *
 * 参数：在 Python 侧用 script.load() 前后设置 send 的 PORT 常量即可（见 probe_dialer.py）。
 *      此处默认 9421。
 */
const PORT = 9421;
const MAX_BT = 14;
const seen = {};

function fmtAddr(addr) {
    try {
        const s = DebugSymbol.fromAddress(addr);
        if (s && s.moduleName) {
            const m = Process.findModuleByName(s.moduleName);
            const off = m ? addr.sub(m.base) : ptr(0);
            return s.moduleName + "!" + (s.name || "?") + "+0x" + off.toString(16);
        }
        return addr.toString();
    } catch (e) {
        return String(addr);
    }
}

function portOf(sockaddr) {
    // sockaddr_in / sockaddr_in6 的端口都在 offset 2，网络字节序（大端）
    try {
        const fam = sockaddr.readU16();
        if (fam !== 2 && fam !== 23) return 0;
        return (sockaddr.add(2).readU8() << 8) | sockaddr.add(3).readU8();
    } catch (e) {
        return 0;
    }
}

function report(ctx, via) {
    let bt;
    try {
        bt = Thread.backtrace(ctx, Backtracer.ACCURATE).map(fmtAddr);
    } catch (e) {
        bt = ["<backtrace failed: " + e + ">"];
    }
    const key = via + "|" + bt.slice(0, 3).join("<");
    if (seen[key]) return;
    seen[key] = true;
    send({ type: "dialer", via: via, bt: bt.slice(0, MAX_BT) });
}

function findExport(mod, name) {
    // frida 17 的 JS 运行时移除了 Module.findExportByName，需要多路兼容
    try {
        if (typeof Module.findExportByName === "function") {
            const p = Module.findExportByName(mod, name);
            if (p) return p;
        }
    } catch (e) {}
    try {
        const m = Process.getModuleByName(mod);
        const p = m.getExportByName(name);
        if (p) return p;
    } catch (e) {}
    try {
        return Module.getGlobalExportByName(name);
    } catch (e) {}
    return null;
}

function hookConnect(exportName) {
    const addr = findExport("ws2_32.dll", exportName);
    if (!addr) {
        send({ type: "miss", name: exportName });
        return false;
    }
    Interceptor.attach(addr, {
        onEnter(args) {
            try {
                const sa = args[1];
                if (sa.isNull()) return;
                if (portOf(sa) !== PORT) return;
                report(this.context, exportName);
            } catch (e) {}
        }
    });
    return true;
}

// 先报家底，确认 hook 装在了正确的进程/模块上
const flue = Process.findModuleByName("flue.dll");
send({
    type: "ready",
    pid: Process.id,
    platform: Process.platform,
    flue: flue ? flue.base.toString() : null,
    modules: Process.enumerateModules().length,
    port: PORT
});

hookConnect("connect");
hookConnect("WSAConnect");
