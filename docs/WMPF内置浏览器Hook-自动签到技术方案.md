# 通过 Hook 微信内置浏览器（Embedded Browser）实现自动签到 —— 技术方案

> 输入依据：
> 1. `evi0s/WMPFDebugger` @ `9be0b2d`（本地 `2026-10-24-16-54-55/WMPFDebugger`，`EXTENSION.md` 与上游 `main` 逐字节一致，仅 CRLF/LF 差异）
> 2. `frida/hook.js`、`src/index.ts`、`src/cli.ts`、`src/platform/win32.ts`、`ADAPTATION.md`、`FAQ.zh.md` 源码
> 3. 本项目上一轮实测交接：`WMPF调试通道-CDP方案技术交接.md`（微信 4.1.15.56 / WMPF 25799 / frida 17.18）
> 4. 业务侧已有实现：`weizhujiao-autosign-pc/signer.py`（`active_signs` / `student-sign-in` 接口形态）
>
> 标注约定：**【实测】**= 本项目已在本机跑通并有证据；**【源码】**= 由仓库代码直接读出；**【待验证】**= 由机制推导、尚未实测，§6 给了可证伪实验。

---

## 0. 结论速览

> ⚠️ **本文 §1~§4 的注入链路分析仍然有效，但 §0 的"价值判断"与 §2 的立论前提已被实测推翻。**
> 实测发现：签到接口只认 `openId` 头，不需要 cookie / Referer / Origin；openid 至少 71 分钟有效，
> 且它是微助教签发的**站点会话标识**（SSR 里叫 `session.openId`），不是微信 openid。
> 因此**页内 agent 对签到不是必需的**，纯 HTTP 即可完成。更正细节与证据见
> 《实测更正-openid性质与链路简化.md》。以下保留原文以便对照，涉及该前提的段落已标注。

**原文立论（保留）：这条路线成立，但它的价值点不是"读出 openid"，而是"把签到动作搬进页面里执行"。**

| 问题 | 结论 |
|---|---|
| hook 谁 | `WeChatAppEx.exe` 的**浏览器主进程**（带 `--flue-runtime-dir` 的那个），不是 `Weixin.exe`，也不是 renderer |
| 注入点 | `flue.dll` 两个函数：`AppletIndexContainer::OnLoadStart` 与 CDP 消息过滤器 `SendToClientFilter` |
| 调试接口 | 两级：`ws://localhost:9421`（protobuf 私有协议，小程序侧连入）+ `ws://localhost:62000`（标准明文 CDP，我们连出） |
| EXTENSION.md 的增量 | 把"顶层 target 列表只能看 URL"升级为 **`Target.attachToTarget(flatten)` 拿到 per-tab session**，从而对任意标签页（含服务号 H5）拥有 `Runtime` / `Page` / `Network` 会话 |
| 签到靠什么 | 原文：页内 `fetch` 钩子 + 页内轮询提交，不抽取 openid。<br>**实测更正**：接口只认 `openId` 头，纯 HTTP 即可；页内执行并非必需（见《实测更正》） |
| 最大硬伤 | ~~授权（OAuth）仍只能由原生层产生；CDP 三条触发路径已实测封死~~<br>**实测更正**：OAuth 确实是纯客户端行为（三种 UA 都只返回 1050B 空壳），但**点一次链接就够，且结果至少 71 分钟有效**，所以"频繁触发"这个痛点不成立 |
| 前置条件 | 必须**先开一个任意小程序**点燃通道，且**全程不能关**；同一时刻**只能有一个 CDP 客户端** |

一句话（原文）：**CDP 不是触发器，但可以是"页面内的手"。** 上一轮把 CDP 当"读取器"用（读 URL 里的 openid），撞上 13~15 分钟轮换的死墙；本轮把它当"执行器"用（在页面自己的同源上下文里发请求），绕开了轮换问题本身。

一句话（更正后）：**那堵"13 分钟死墙"其实不存在。** openid 至少 71 分钟有效、接口不校验来源，所以真正唯一需要微信客户端的动作只有"取一次号"；此后纯 HTTP 就能跑完。CDP 的价值退回到"低成本读一次 openid"，页内执行属于可选优化而非必需。

---

## 1. 目标进程与注入环节

### 1.1 进程树与选主规则

微信 4.x 的内置浏览器是独立的 Chromium 系（CEF 变体，运行时叫 WMPF / RadiumWMPF / flue），进程名统一 `WeChatAppEx.exe`，一父多子：

```
Weixin.exe (Qt51514QWindowIcon 主窗口)
└── WeChatAppEx.exe  ← 浏览器主进程，带 --flue-runtime-dir=...\RadiumWMPF\25799\extracted\runtime
    ├── --type=renderer   × N   （每个标签页/每个站点上下文一个）
    ├── --type=gpu-process
    ├── --type=utility
    └── --type=utility
```

**【实测】本机当前形态**：主进程 `pid=51240`，子进程 9 个（5 renderer / 1 gpu / 2 utility / 1 未分类）。WMPF 版本仍为 `25799`。

选主算法（`src/platform/win32.ts`，【源码】）：

1. 枚举所有 `WeChatAppEx.exe`；
2. 取所有进程 `ppid` 的**众数** → 该值就是父进程 pid；
3. 在列表里找回该 pid 的进程即主进程；
4. 从它的 `--flue-runtime-dir` 参数里正则抽数字，**取最后一个**即 WMPF 版本。

> 坑（【实测】）：`device.enumerateProcesses(scope="metadata")` 在 **frida-python** 绑定上会挂死（73s 无响应），Node 绑定正常。所以 Python 侧要自建注入器时，进程发现走 PowerShell CIM，别用 frida 枚举。

### 1.2 注入链（frida → flue.dll → 私有调试通道）

```
┌─────────────────────────────── Node 进程 (src/index.ts) ───────────────────────────────┐
│ 3) debugServer   ws://:9421   ⇄  WMPF 运行时（protobuf WARemoteDebug）                  │
│ 4) proxyServer   ws://:62000  ⇄  我们 / DevTools（明文 CDP JSON）                        │
│ 2) fridaServer   attach(主进程 pid) → createScript(hook.js, @@CONFIG@@) → load()         │
└──────────────────────────────────────┬─────────────────────────────────────────────────┘
                                       │ frida agent
┌──────────────────────────────────────▼─── WeChatAppEx.exe (主进程) ───────────────────┐
│ 1a) Interceptor.attach(flue.dll + 0x2d5a240)   // AppletIndexContainer::OnLoadStart    │
│ 1b) Interceptor.replace(flue.dll + 0x385e1d0)  // CDP 消息过滤器 → 转调 +0x9f0abb0       │
│     └─ 每次小程序启动：debug_flag=1 / scene=1101 / ws 串改写 :9421 / remoteDebugMode=1  │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

关键认识：**注入不是"注入到某个标签页"，而是注入到承载所有标签页的那个浏览器进程。** 小程序与内置浏览器 H5 是同一进程里的不同 tab，共享同一个调试出口——这正是 EXTENSION.md 能成立的根本原因（【源码】+【实测】target 列表里 page/webview 混排）。

### 1.3 两处 patch 各自在干什么

**(1) `OnLoadStart`（hook 主入口）**【源码，`frida/hook.js:178`】

```js
Interceptor.attach(base.add(config.LoadStartHookOffset), {
  onEnter(args) {
    // args[1] = debug_flag，低字节强制置 1 —— 对应 IDA 里的 if ((_BYTE)a2) 分支
    if (args[1].and(0xff).toInt32() !== 1)
      args[1] = args[1].and(ptr("0xffffffffffffff00")).or(1);
    handleOnLoadStart(args[0], config);   // args[0] = AppletIndexContainer*
  }
});
```

`args[1]` 就是"是否走调试分支"的开关。`ADAPTATION.md` 里那段反编译 `if ((_BYTE)a2) result = sub_182B02350(...)` 讲的就是它——置 1 后才会进入构造 `ws://localhost:9421` 的那段代码。

**(2) 结构体改写**【源码，解引用链来自 `addresses.25799.json`】

| 目标 | 偏移链（`a1` = `AppletIndexContainer*`） | 动作 |
|---|---|---|
| `MiniAppLaunchConfig*` | `a1 + 64 → +1584 → +8` | — |
| `RemoteDebugConfig*` | `launchConfig + 1512 → +16` | — |
| `scene` | `remoteDebugConfig + 456` | **白名单校验通过后**写 `1101` |
| WebSocket 地址 | `launchConfig + 624` | 改写成 `ws://localhost:9421` |
| `remoteDebugMode` | `remoteDebugConfig + 724` | 写 `1` |

`scene` 白名单（【源码】`:138`）只含 `1005/1007/1008/1011/1012/1027/1035/1037/1053/1074/1145/1178/1256/1260/1302/1308`。**不在表内直接 `return`，一个字节都不改** —— 表现为"小程序开了但 9421 没连上"。`1101` 是"远程调试场景"，写进去后整条运行时都会按开发态走。

WebSocket 字符串有**两种内存表示**，hook 都得处理（否则会写坏堆）：

- 长表示：`{char* data; size_t len; size_t cap | 高位标记}`。判定方式是读 `ptr+23` 的有符号字节，`< 0` 即长表示 → 写指针所指缓冲区 + 改 `+8` 的 u64 长度；
- 短表示（SSO，≤23 字节内联）：直接 `writeUtf8String` 到 `ptr`，长度写 `ptr+23` 的 u8。

**(3) CDP 过滤器**【源码，`frida/hook.js:20`】

对应符号线索 `SendToClientFilter` / `devtools_message_filter_applet_webview.cc`。新版本走 `CastToJson` 蹦床：用 `Interceptor.replace` 把过滤器换成 `NativeCallback`，回调里先调原始 `CastToJson(jsonOut, cborInput)` 再原样返回——相当于**把过滤判定整体短路为放行**。旧版本兜底路径更粗暴：`onLeave` 时若 `retval+8 == 6` 就写 0。

> 风险提示：`Interceptor.replace` 一旦偏移错位就是**进程级崩溃**（不是脚本报错）。改版本适配时优先用 `--auto-detect`，不要手改这个偏移。

### 1.4 调试协议：两级服务、两种编码

| 端口 | 方向 | 编码 | 说明 |
|---|---|---|---|
| **9421** | 小程序 → 我们 | protobuf `WARemoteDebug_DebugMessage` + 私有 codex 封装 | **硬编码在 hook.js 里，不可改** |
| **62000** | 我们 → 我们 | 明文 CDP JSON | `src/cli.ts` 的 `CDP_PORT`，可用 `--cdp-port` 改 |

投递逻辑（【源码，`src/index.ts`】）：

- 下行：CDP 客户端发来 JSON 字符串 → `{jscontext_id:"", op_id: rand(0..100), payload}` → `codex.wrapDebugMessageData(..., "chromeDevtools", 0)` → protobuf encode → **广播**给所有小程序 WS；
- 上行：protobuf decode → `codex.unwrapDebugMessageData` → **只有 `category === "chromeDevtoolsResult"` 会被转发**给 CDP 客户端。

两个直接推论：

1. **其他 category（小程序自定义 jscontext 消息、AppService 日志等）被丢弃**，我们只看得到 CDP 这一条信道；
2. `chromeDevtoolsResult` 里既装 **reply**（有 `id`）也装 **event**（有 `method` 无 `id`）。上一轮 `watch` 能收到 `Page.frameNavigated`、`Target.targetCreated`（【实测】），说明 **事件回流是通的** —— 这是本方案敢于用"页内主动上报"的前提（§6 H1）。

### 1.5 可用调试接口矩阵

| 接口 | 状态 | 依据 | 本方案用途 |
|---|---|---|---|
| `Target.getTargets` / `setDiscoverTargets` | ✅ | 【实测】27 个 target | 发现签到页 |
| `Target.attachToTarget{flatten:true}` | ✅ | 【实测】+ EXTENSION.md | **核心**：拿 per-tab session |
| `Page.enable` / `getFrameTree` / `frameNavigated` | ✅ | 【实测】 | 判断页面就绪、URL 变化 |
| `Runtime.evaluate`（含 `awaitPromise`/`returnByValue`） | ✅ | 【实测】 | **核心**：注入 agent、拉状态、兜底执行 |
| `Page.navigate`（普通 page） | ✅ 技术成功 | 【实测】返回 loaderId | **禁止用于授权**（见 §5.2） |
| `Page.navigate`（小程序 webview） | ❌ 业务拦截 | 【实测】落 `jump2h5err` | 不用 |
| `Target.createTarget` | ❌ `-32000 Not supported` | 【实测】 | 不用 |
| `Page.reload` / `Page.addScriptToEvaluateOnNewDocument` | 【待验证】 | 机制推导 | 让 agent 跨刷新存活 |
| `Network.enable` + `getResponseBody` | 【待验证】 | 机制推导 | 抓签到请求模板 |
| `Runtime.consoleAPICalled` / `Runtime.addBinding` | 【待验证】 | 事件回流已证实，域能力未证实 | agent 主动上报（优化项，非必需） |
| `Schema.getDomains` | ⚠️ 半残（commands 为空） | 【实测】 | 不依赖 |

> 上一轮已验证过 `Target.attachToTarget` + `Runtime.evaluate` 能在 page target 上工作，但**是否对"服务的 H5 tab"同样成立、sessionId 是否被上游正确路由**，属于本轮要新增确认的部分（§6 E2/E3）。

---

## 2. 为什么"搬进页面"是唯一稳的路

### 2.1 重新理解 openid 的 13 分钟轮换

上一轮的关键实测：`openid` 13 分钟就换值，旧的直接 401。但**微信 openid 按 (appid, user) 本应是稳定标识**。两者矛盾，唯一自洽的解释是：

> 签到页 URL 里的 `openid` **不是微信侧 openid，而是微助教服务端在 OAuth 回调里签发的一张会话票据**（或直接把 openid 当 session key 用，另有服务端 TTL）。

这个推断改变了整个设计前提：

- 它不是"身份"，而是**页面持有的短期凭证**；
- 它天然属于该页面（生成它的是该页面的 OAuth 回调），**提取到页面外就失去续期能力**；
- 因此正确姿势不是"更快地抓"，而是**根本不抓**——让请求从页面里发出去。

（该推断未证实，§6 H5 给了验证办法；即使推断错了，本方案依然成立，只是理由退化为"避免跨进程搬运凭证"。）

> **2026-10-25 实测：推断对了一半，结论错了一半。**
> - 对的部分：它确实是站点签发的会话标识而非微信 openid（SSR 里就叫 `session.openId`，32 位 hex；微信 openid 是 28 字符 base64 风格）。
> - 错的部分：**"短期凭证"不成立**——签发 71 分钟后仍 200；而且接口**不校验 cookie / Referer / Origin**，
>   所以"提取到页面外就失去能力"也是错的，纯 HTTP 拿着 openid 就能跑通。
> - 因此"根本不抓、让请求从页面里发出去"这个设计选择**是多余的**。详见《实测更正-openid性质与链路简化.md》。

### 2.2 页内执行相对外部 HTTP 客户端的三个结构性优势（已被实测否决）

| 维度 | 外部 HTTP（现有 `signer.py`） | 页内 `fetch`（本方案） |
|---|---|---|
| 凭证 | ~~13~15 min 失效即 401~~ **实测：至少 71 分钟有效，可缓存** | 页面自己持有/续期，无失效窗口（**但外部同样没有窗口问题**） |
| 请求指纹 | 手工伪造 UA / Referer / Origin | 由 XWEB 网络栈真实发出（**实测：该接口不校验 Referer/Origin，伪造与否都 200**） |
| 鉴权头/签名 | 服务端若加了时间戳、指纹、风控头，需逆向 | 直接复用页面自己的请求模板（**实测：只有 `openId` 一个头，无签名**） |

**结论：这三条优势在本接口上全部不成立。页内执行只剩一个用途——低成本读出 openid；签到本身用纯 HTTP 即可。**

第二点是决定性的：即便 openid 还能用，外部请求的 TLS 指纹、UA token、header 顺序都和内嵌浏览器不同，属于服务端可以识别的差异。页内发起把这个问题一次性消掉。

### 2.3 但触发仍然无解（必须先讲清边界）

已实测封死的三条路（【实测】，不要重复踩）：

1. `Target.createTarget` → `Not supported`；
2. `Page.navigate` 到 `open.weixin.qq.com/connect/oauth2/authorize` → 落到 1012 字节空壳 JS 分流页，UA 是微信时分支为空，**零跳转**；
3. 从小程序 webview 跨域导航到授权链接 → 业务域名白名单，落 `mp.weixin.qq.com/mp/waerrpage?...type=jump2h5err`。

机理：PC 微信的 `oauth2/authorize` 由**客户端原生层拦截处理**（点服务号菜单走的就是原生通道），渲染引擎里加载这个 URL 不会触发 OAuth。

**所以本方案的定位必须诚实：它自动化"已经打开并授权的签到页"，不生产授权。** 授权仍由一次人工点击（服务号菜单/模板消息）产生，此后所有签到动作全自动。这在实践中已经足够——签到是"每天上课时段要签"，不是"从零建立会话"。

---

## 3. 自动签到流程设计

### 3.1 三层架构

```
┌─ L3 业务层 ─────────────────────────────────────────────────────────────┐
│ signer.py / notifier.py：签到窗口判定、多课并行、幂等、通知、state.json   │
├─ L2 宿主驱动 ───────────────────────────────────────────────────────────┤
│ cdp_sign_agent.py：单客户端持有 62000，发现→附着→注入→轮询→回传          │
├─ L1 页内 agent ─────────────────────────────────────────────────────────┤
│ page_agent.js：fetch/XHR 钩子、凭证自捕获、active_signs 轮询、提交签到   │
└─────────────────────────────────────────────────────────────────────────┘
```

**职责边界**：L1 只做"手里有页面才能做的事"（发请求、读 DOM）；L2 只做"页面做不到的事"（发现/附着/注入/重启 agent）；L3 不感知 CDP 的存在。

### 3.2 完整调用链

```
[0] 前置（人工，一次性）
    node src/index.ts --auto-detect?          # 1) 起代理，等 "[frida] script loaded"
    打开任意小程序                              # 2) 点燃：scene 命中白名单 → 9421 有 client
    点微助教【签到】                            # 3) 授权+H5 落地（原生层产出）
    ——此后小程序窗口保持不关，签到页标签也不要关——

[1] 发现      Target.setDiscoverTargets{discover:true}
              Target.getTargets → 筛 type=="page" 且 url 匹配 /student/sign/

[2] 附着      Target.attachToTarget{targetId, flatten:true} → sessionId
              之后所有命令带 sessionId

[3] 会话      Runtime.enable / Page.enable（session 作用域）

[4] 注入      Runtime.evaluate{expression: <page_agent.js 源码>}
              + Page.addScriptToEvaluateOnNewDocument（兜底，待验证）

[5] 自举      agent 读 location.search 拿初始凭证；
              钩住 window.fetch / XHR，之后所有 teachermate 请求自动补货

[6] 轮询      agent 每 5s：GET .../class-attendance/student/active_signs
              headers: { openId: <页面自己的> }   credentials:"include"

[7] 命中      items 非空 → 逐条 POST .../student-sign-in
              body: { courseId, signId, lat?, lon? }（isGPS 时带坐标）

[8] 回传      window.__wzSignAgent.state  ←  L2 每 2s Runtime.evaluate 拉一次
              （可选优化：Runtime.addBinding → bindingCalled 事件推送）

[9] 落库      L3 写 state.json / 发通知；重复签到按 errorCode=305 视为成功
```

### 3.3 与现有业务代码的对接点

现有 `signer.py` 的取号策略是 `openid_source = auto|cdp|mem`（CDP 抓 URL → 内存扫描兜底）。本方案**不改这个开关的语义**，而是**新增一档**：

```
openid_source = "auto"   →  page_agent(首选) → cdp(抓号) → mem(扫描)   [建议]
              = "page"   →  只用页内 agent（无 openid 搬运）
              = "cdp"    →  维持现状
              = "mem"    →  维持现状（无需小程序常驻）
```

`signer.py` 里 `poll_signs()` / `sign_one()` 的 URL、header、body 形态**完全保留**，作为页内 agent 的镜像实现——两边保持同一份接口契约，页内失败可无缝降级到外部 HTTP。

### 3.4 页内 agent 的六个实现要点

1. **双钩子**：`window.fetch` 与 `XMLHttpRequest.prototype.open/send` 都要钩。SSR 页面可能走 XHR，只钩 fetch 会漏。
2. **凭证来源优先级**：`location.search` 的 `openid` → 钩子捕获到的 `openId` 请求头 → cookie。三者都有才稳。
3. **绝不硬编码域名路径**：从捕获到的真实 URL 反推 `API_BASE`，服务端换路径时不至于瞎。
4. **幂等优先于重试**：命中 `"signRank"` 即成功；`"repeat sign in"` / `账号已签到` / `errorCode:305` 视为已签，不要重试——重复提交会加重风控。
5. **失败不打转**：401 时**不要**尝试在页内 `location.replace(oauth_url)` 自动续期（大概率被原生层拦成 `jump2h5err` 或空壳页，反而把**能用的页面搞死**）。正确做法是把 `state.auth = "expired"` 上报，由 L3 提示人工点一次菜单重新打开签到页。§6 H2 允许在隔离实验里验证这条路，但**默认关闭**（`allowRenauth:false`）。
6. **上报不依赖事件**：主通道用 `Runtime.evaluate` 轮询 `window.__wzSignAgent.state`（已验证可用）；`console.log` 与 `Runtime.addBinding` 只作优化。

### 3.5 为什么不用 `Page.navigate` 刷新页面

`Page.navigate` 在普通 page target 上**技术上会成功**（【实测】返回 loaderId、URL 改变）。但对签到页是净损失：

- 可能把带 `openid` 的 URL 变成服务端重新挑战的路径；
- 一旦跳到非白名单域，回不来；
- 刷新会重置页内 agent，需依赖 `addScriptToEvaluateOnNewDocument`（待验证）。

需要"重载"时优先 `Page.reload`（保留原 URL），且必须已有 agent 的跨文档存活方案。

---

## 4. 验证方法（分层、可判定）

原则：**逐层放行**。下层没过不要看上层现象，否则会把"通道没通"误判成"签到逻辑有 bug"。

| 层 | 动作 | 通过判据 | 失败含义 |
|---|---|---|---|
| L0 环境 | `node -e "require('frida').getLocalDevice().then(d=>console.log(d.name))"` | 输出 `Local System` | 原生绑定缺失（重下 tar.gz） |
| L0 环境 | PowerShell 枚举 `WeChatAppEx.exe` | 找到带 `--flue-runtime-dir` 的进程，版本号 25799 | 微信没起 / 版本变了 |
| L0 环境 | `cat frida/config/win32/addresses.25799.json` | 文件存在 | 需 `--auto-detect` |
| L1 通道 | 跑 `src/index.ts` | 打印 `script loaded, WMPF version: 25799` + `you can now open any miniapps` | frida 权限低于微信（FAQ） |
| L1 通道 | 打开小程序 | 打印 `[miniapp] miniapp client connected` | scene 不在白名单 → 加 `--debug-frida` 看 `[hook] scene:` |
| L1 指纹 | `Target.getTargets` | 出现 `...?isScan=1101&...` 的 webview | `isScan=1101` 是 hook 强改 scene 的指纹，出现即证明 patch 生效 |
| L2 发现 | 点开签到页后 `getTargets` | 有 `type=page` 且 url 含 `/student/sign?openid=` | 页面没真打开 / 通道断 |
| L2 附着 | `attachToTarget{flatten:true}` | 返回 `sessionId` | 上游未实现 flatten |
| **L2 会话隔离** | session 内 `Runtime.evaluate{"location.href"}` | 返回**签到页 URL**（不是小程序 URL） | sessionId 未被路由，退化为命中主会话 |
| L2 上下文真实性 | `Runtime.evaluate{"navigator.userAgent"}` | 含 `MicroMessenger` + `WindowsWechat` + `XWEB/25799` | 附到了错误 target |
| L3 注入 | evaluate 后读 `window.__wzSignAgent.version` | 有值 | 页面 CSP / 上下文被隔离 |
| L3 钩子 | 在页面上手动点一次"刷新/签到" | `__wzSignAgent.lastReq.url` 非空且含 teachermate | 钩子装晚于页面初始化 / 走了 iframe |
| L3 网络 | agent 自己的 `active_signs` 调用 | HTTP 200（业务时段 `[]` 也算通） | 401 = 凭证过期；403/CORS = 附着页面不对 |
| L4 闭环 | 上课时段命中 | POST 响应含 `signRank` | 见 §5 排查表 |
| **L4 负向对照** | 把同一 openid 拿到外部 `urllib` 发同样请求 | 200 后 13 分钟变 401 | 反向证明"页内执行"的收益是真实的，而非服务端没校验 |

**判据设计说明**：L2 的"会话隔离"和 L4 的"负向对照"是本方案最能暴露错误实现的两个检查点——前者防止"以为附着了其实还在主会话"，后者防止"以为绕过了轮换其实只是运气好"。

---

## 5. 限制、坑与注意事项

### 5.1 硬性使用约束

| # | 约束 | 后果 | 应对 |
|---|---|---|---|
| 1 | **必须先开小程序点燃通道**，且全程不关 | 关了 = 调试会话终止（EXTENSION.md 明确写死） | 选体积小、启动快的小程序当"点火器"；L2 检测断连后提示重启流程 |
| 2 | ~~**同一时刻只能一个 CDP 客户端**~~ **已解除** | 原 `proxyServer` 把上行广播给所有客户端、请求靠自增 `id` 配对 → 两个客户端串扰 | 已实现 id 重写复用代理（`src/proxy-mux.ts`，默认启用，`--legacy-proxy` 回退）。见《限制解除-工程方案.md》§1，离线测试 8 断言全过 |
| 3 | scene 白名单 | 某些入口打开的小程序静默不 patch | `--debug-frida` 看 `[hook] scene:`；必要时扩展白名单 |
| 4 | 9421 端口硬编码 | 端口冲突即起不来 | 起前 `netstat` 检查 |
| 5 | 版本漂移 | 微信更新 → WMPF 号变 → 偏移失效（地址写死在 flue.dll 里，任何补丁都会挪） | `--auto-detect` 重跑；失败按 `ADAPTATION.md` 手工适配；`Interceptor.replace` 偏移错位会**崩进程** |
| 6 | 进程权限 | node 权限低于微信 → `Cannot read properties of undefined (reading 'parameters')` | node 提权或微信降权（FAQ） |
| 7 | 全局代理 | FAQ #119：全局代理（如 yakit）会打断 WS → `Invalid WebSocket frame: invalid opcode 0` → 小程序闪退 | 把 `127.0.0.1,localhost` 加入 `NO_PROXY`；本机 `HTTP_PROXY=127.0.0.1:56778` 需确认 `ws` 不接管（Node `ws` 默认不读环境代理，但仍建议显式排除） |
| 8 | 操作顺序 | 必须"先起代理 → 再开小程序 → 再开 DevTools/脚本"，反了要重来 | 固化进启动脚本 |
| 9 | Element/DOM 面板不实时 | EXTENSION.md 明说：Element 面板不会更新 | 不要指望 DOM 面板；用 `Runtime.evaluate` 读 DOM |

### 5.2 明确不要再试的方向（已封死，重复投入=浪费）

- `Target.createTarget` 新建标签页；
- `Page.navigate` 到 `open.weixin.qq.com/connect/oauth2/authorize`（空壳页）；
  > ⚠️ **本条结论已降级为"未证实"**：该实验是在 `liteapp.weixin.qq.com`（小程序容器页）上做的，
  > 不是服务号 H5。两者共用调试通道但不共用导航策略。用 per-tab 附着在真实 H5 标签上重测的方法见
  > 《限制解除-工程方案.md》§3（`trigger_oauth.py`，三变体 href/open/nav）。
- 从小程序 webview 跨域导航（业务域名白名单 `jump2h5err`）；
- 普通浏览器（含手机 UA）打开授权链接（服务端"请在微信客户端打开链接"存根页）；
- 页内 `location.replace(oauth_url)` 自动续期 —— **默认禁用**，仅允许在隔离实验里验证（见 H2）。

### 5.3 账号与合规风险（必须让使用者知情）

1. **客户端完整性**：本方案 patch 微信运行时二进制内存，属绕过客户端自身调试限制。微信侧若加强校验（如 flue 启动期自检、调试特征上报），存在**封号/限流**风险。
2. **业务侧风控**：页内高频轮询（<5s）与异常时段集中签到是可识别特征；建议 ≥5s 轮询 + 抖动，且仅在 `class_hours` 内跑。
3. **签到性质**：GPS 签到（`isGPS`）伪造经纬度涉学术诚信与校规问题，不属于技术可行性范畴，需使用者自行判断。技术上本方案**不改变**现有 `signer.py` 的坐标策略。
4. **许可证**：WMPFDebugger 为 **GPLv2**，二次分发/修改版需遵守 GPLv2（提供源码、保留声明），且 `src/third-party` 代码版权归腾讯。仅自用不影响。
5. **稳定性**：frida 注入 + 二进制 patch 本身会让浏览器进程崩溃的概率上升（老版本 11581 甚至"连上后 renderer crash"）。**不要把签到唯一希望押在这条路上**，保留 `mem` 内存扫描兜底。

### 5.4 排查表（现象 → 根因）

| 现象 | 根因 | 定位 |
|---|---|---|
| 9421 没有 `miniapp client connected` | 没开小程序 / scene 不在白名单 / 版本没适配 | `--debug-frida` 看 `[hook]` 消息 |
| `Target.getTargets` 无响应 | 9421 上没有下游客户端 | 看 L1 |
| 附着成功但 `location.href` 是小程序 URL | sessionId 未生效 | 检查是否每条命令都带 `sessionId` |
| `active_signs` 401 | 凭证过期（页面开太久） | 需人工重开签到页；L3 标记 `auth=expired` |
| `active_signs` 200 但恒为 `[]` | 非上课时段，或课不在该账号 | 正常，不是 bug |
| POST 403/风控 | 请求形态与真实不符 | 用钩子抓到的模板回放，而非自造 body |
| 两个脚本一起跑就崩 | 多 CDP 客户端 | 强制单客户端 |
| 小程序无限加载 | 起代理前已开着小程序 | 关小程序 → 重开（FAQ） |

---

## 6. 待验证的关键假设（可证伪实验设计）

按性价比排序，每个实验都给了判据与失败退路。

**H1｜session 作用域的事件是否回流**
在 session 内 `Runtime.enable`，页内 `console.log`，观察 62000 是否收到 `Runtime.consoleAPICalled`。
- 成立 → agent 可主动推送（省掉 2s 轮询）；
- 不成立 → 保持 `Runtime.evaluate` 拉状态（已验证可行），**不影响主链路**。

**H2｜renderer-initiated 导航到授权链接是否被原生层拦截**
关键差异假设：上一轮测的是 **CDP 发起**的导航（浏览器侧），而 `location.href = oauth_url` 是**渲染器发起**，可能经过 WMPF 的导航拦截器从而走到原生 OAuth。
- 做法：仅在隔离标签页里试，观察落到 `waerrpage?type=jump2h5err` / 空壳页 / 正常授权三态之一；
- 成立 → 可做"全自动续期"，方案完整度大幅提升；
- 不成立 → 维持"人工点一次菜单"的设计，且**永久关闭** `allowRenauth`。
- ⚠️ 该实验失败会**弄坏当前可用的签到页**，必须在不需要签到的时段、另开的标签页上做。

**H3｜H5 标签页关闭后 target 是否消失、能否恢复**
关闭签到页 → `Target.targetDestroyed`？重开一次菜单能否重新出现同名 target？
- 决定 L2 是否需要处理 target 生命周期（重连、重新附着）。

**H4｜`Page.addScriptToEvaluateOnNewDocument` 是否被 flue bridge 放行**
放行 → agent 天然跨刷新存活；不放行 → 必须监听 `Page.frameNavigated` 后手动重注入。

**H5｜openid 究竟是微信 openid 还是微助教签发的会话票据**
两次授权拿两个 openid，在外部用同一 cookie 分别请求 `active_signs`，观察：
- 响应头有无 `Set-Cookie` 轮换、payload 里是否有用户标识字段；
- 若旧 openid 401 但页面行为正常 → 支持"站点票据"推断（§2.1）。

---

## 7. 下一步演进（可选）

1. **全自动续期**：若 H2 成立，页内 agent 可在 401 时自主重走授权；若 H2 不成立，则转向原生层——在 `Weixin.exe` 与 `WeChatAppEx.exe` 之间定位"打开 URL"的 mmmojo IPC（`mmmojo_64.dll`）并重放，这是唯一能彻底摆脱 UI 自动化的方向，但工作量是逆向级别（上一轮 `ipc_probe.py` / `trace_openurl.py` 只做到侦察）。
2. **多账号**：本机登了两个微信号，`openid` 按 appid+user 隔离。多账号需按窗口/会话区分 target，页内 agent 天然按 tab 隔离，比外部 HTTP 更干净。
3. **启动收敛**：把"起代理 → 点火小程序 → 等签到页"封装成一个常驻守护进程，断连自愈。

---

## 附录 A：关键位置索引

```
WMPFDebugger/
  src/index.ts             debugServer(9421) / proxyServer(62000) / fridaServer
  src/cli.ts               端口常量 DEBUG_PORT=9421, CDP_PORT=62000
  src/platform/win32.ts    主进程识别（ppid 众数）+ 版本解析
  frida/hook.js            OnLoadStart / CDPFilter 两处 patch（9421 硬编码，@@CONFIG@@）
  frida/autodetect/win32.js  偏移自动探测
  frida/config/win32/addresses.25799.json   本机 25799 偏移（核心资产）
  EXTENSION.md             内置浏览器页面调试（attachToTarget 法）
  ADAPTATION.md            新版本手工适配（找 1101 魔数的过程）
本方案新增：
  page_agent.js            页内 agent（注入用）
  cdp_sign_agent.py        宿主驱动（发现/附着/注入/轮询）
```

## 附录 B：25799 偏移速查

```json
{
  "Version": 25799,
  "LoadStartHookOffset": "0x2d5a240",
  "CDPFilterHookOffset": "0x385e1d0",
  "CastToJsonHookOffset": "0x9f0abb0",
  "MiniAppConfigStructOffsets": {
    "LaunchConfigOffsets": [64, 1584, 8],
    "RemoteDebugConfigOffsets": [1512, 16],
    "SceneOffset": 456,
    "WebSocketURLStringOffset": 624,
    "RemoteDebugModeOffset": 724
  }
}
```

## 附录 C：签到接口契约（与 `signer.py` 对齐）

```http
GET  https://www.teachermate.com.cn/wechat-api/v1/class-attendance/student/active_signs
     openId: <页面持有>
     Referer: https://app.teachermate.com.cn/wechat-pro-ssr/student/sign
     Origin:  https://app.teachermate.com.cn
→ 200 []            无进行中签到（非上课时段即此结果，不是失败）
→ 200 [{signId, courseId, isGPS, ...}]
→ 401               凭证过期/无效

POST https://www.teachermate.com.cn/wechat-api/v1/class-attendance/student-sign-in
     openId: <同上>
     Content-Type: application/json
     { "courseId": ..., "signId": ..., "lat": .., "lon": .. }   // isGPS 时带坐标
→ 含 "signRank"                成功
→ "repeat sign in" / errorCode 305   已签到（幂等，计成功）
```

页内执行时 **`Referer` / `Origin` / `User-Agent` 全部由浏览器自动生成**，无需也不应手工设置（手工设置 `Referer` 在浏览器里会被忽略或触发预检）。
