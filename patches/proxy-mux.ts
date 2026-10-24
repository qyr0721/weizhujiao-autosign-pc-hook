import { EventEmitter } from "node:events";
import WebSocket, { WebSocketServer } from "ws";

type Logger = { info: (...args: any[]) => void; error: (...args: any[]) => void };

/**
 * 多客户端 CDP 复用代理。
 *
 * 上游（小程序报活的那条 WebSocket）只认"一条连接内的唯一 id"。
 * 原版 proxyServer 把每个客户端的自增 id 原样透传、并把所有上行广播给所有
 * 客户端，于是两个客户端各自从 1 开始计数的 id 会冲突，回复串到对方身上
 * （表现为：监听脚本崩溃 / 命令返回别人的结果）。
 *
 * 这里做的是标准的 id 重写复用：
 *   客户端 -> 上游：分配全局唯一 id，记录 (全局id -> 客户端, 客户端本地id)
 *   上游 -> 客户端：有 id 的是应答，查表还原本地 id 并只发给发起者；
 *                  没有 id 的是事件（frameNavigated / consoleAPICalled 等），广播。
 *
 * 单客户端行为与原版完全一致（id 序列从 1 开始，只是数值由全局计数器给出）。
 */
export const createProxyMux = (
    port: number,
    emitter: EventEmitter,
    logger: Logger,
): WebSocketServer => {
    const wss = new WebSocketServer({ port, maxPayload: 64 * 1024 * 1024 });
    logger.info(`[server] proxy server (mux) running on ws://localhost:${port}`);
    logger.info(
        `[server] link: devtools://devtools/bundled/inspector.html?ws=127.0.0.1:${port}`,
    );

    let globalId = 0;
    const owner = new Map<number, { ws: WebSocket; localId: number }>();
    const clients = new Set<WebSocket>();

    const forget = (ws: WebSocket) => {
        clients.delete(ws);
        for (const [gid, o] of owner) {
            if (o.ws === ws) owner.delete(gid);
        }
    };

    wss.on("connection", (ws: WebSocket) => {
        clients.add(ws);
        logger.info(`[cdp] CDP client connected (active=${clients.size})`);

        ws.on("message", (raw) => {
            const text = raw.toString();
            let msg: any = null;
            try {
                msg = JSON.parse(text);
            } catch {
                msg = null;
            }
            if (!msg || typeof msg.id !== "number") {
                // 非命令（或畸形消息）按原样下发，交给上游判断
                emitter.emit("proxymessage", text);
                return;
            }
            const gid = ++globalId;
            owner.set(gid, { ws, localId: msg.id });
            msg.id = gid;
            emitter.emit("proxymessage", JSON.stringify(msg));
        });

        ws.on("error", (err) => {
            logger.error("[cdp] CDP client err:", err);
        });

        ws.on("close", () => {
            forget(ws);
            logger.info(`[cdp] CDP client disconnected (active=${clients.size})`);
        });
    });

    emitter.on("cdpmessage", (text: string) => {
        let msg: any = null;
        try {
            msg = JSON.parse(text);
        } catch {
            return;
        }
        if (!msg) return;

        if (typeof msg.id === "number") {
            const o = owner.get(msg.id);
            if (!o) {
                // 上游偶尔会带出自己的 id（例如内部重放），丢弃比串扰安全
                logger.error(`[cdp] orphan reply id=${msg.id}, dropped`);
                return;
            }
            owner.delete(msg.id);
            msg.id = o.localId;
            if (o.ws.readyState === WebSocket.OPEN) {
                o.ws.send(JSON.stringify(msg));
            }
            return;
        }

        // 事件：广播（DevTools 与我们的脚本可同时观察）
        for (const ws of clients) {
            if (ws.readyState === WebSocket.OPEN) ws.send(text);
        }
    });

    return wss;
};
