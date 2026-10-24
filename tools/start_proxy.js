/**
 * start_proxy.js — 以"脱离父进程"的方式常驻 WMPFDebugger 代理
 *
 * 为什么需要它：Bash/PowerShell 工具调用结束时，子进程会被回收；
 * 而调试通道要求"代理先起来，再启动小程序"——顺序反了就白搭。
 * 所以代理必须活过工具调用。
 *
 * 用 node 的 detached + unref，进程挂到独立进程组，父进程退出也不受影响；
 * stdout/stderr 重定向到 proxy.log 便于事后查看。
 *
 * 用法：node tools/start_proxy.js
 */
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const WMPF = "C:/Users/Admin/WorkBuddy/2026-10-24-16-54-55/WMPFDebugger";
const NODE = "C:/Users/Admin/.workbuddy/binaries/node/versions/22.22.2-6/node.exe";
const TSX = path.join(WMPF, "node_modules", "tsx", "dist", "cli.mjs");
const LOG = path.join(WMPF, "proxy.log");

// 已经在跑就别重复起（重复 attach 会把 frida 挂两次）
const net = require("node:net");
function portOpen(port) {
    return new Promise((resolve) => {
        const s = net.connect({ port, host: "127.0.0.1" }, () => {
            s.destroy();
            resolve(true);
        });
        s.on("error", () => resolve(false));
        s.setTimeout(800, () => {
            s.destroy();
            resolve(false);
        });
    });
}

(async () => {
    if (await portOpen(62000)) {
        console.log("proxy already running (62000 listening)");
        process.exit(0);
    }
    const out = fs.openSync(LOG, "a");
    const child = spawn(NODE, [TSX, "src/index.ts"], {
        cwd: WMPF,
        detached: true,
        windowsHide: true,
        stdio: ["ignore", out, out],
    });
    child.unref();
    console.log("proxy spawned pid=" + child.pid + "  log=" + LOG);

    // 等端口起来
    for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 500));
        if (await portOpen(62000)) {
            console.log("proxy ready (62000 listening)");
            process.exit(0);
        }
    }
    console.log("proxy did NOT come up within 20s — check " + LOG);
    process.exit(1);
})();
