# 设计说明：为什么这样实现，以及哪些结论是实测出来的

本文只记录**当前成立的事实**与**设计取舍**。过程中的试错、作废结论已剔除。

---

## 1. 一句话

签到只需要一个有效的 `openid`；`openid` 只能由微信客户端在 OAuth 回调里签发；
所以用 frida patch 微信内置浏览器，让它在后台自己走一次授权，从落地 URL 读号。

```
[触发] CDP Page.navigate -> open.weixin.qq.com/connect/oauth2/authorize?appid=..&redirect_uri=..
[接管] A8KeyNavigationThrottle::CheckURL -> A8KeyRateLimiter -> ::Redirect
[授权] 原生层完成，302 -> v18.teachermate.cn/api/v1/wechat/r?code=..
[落地] 302 -> v18.teachermate.cn/wechat-pro-ssr/student/sign?openid=<32hex>
[取号] 读 target URL 里的 openid -> 交给签到侧
```

---

## 2. 注入点

**目标进程**：`WeChatAppEx.exe` 里带 `--flue-runtime-dir` 的那个浏览器主进程
（不是 `Weixin.exe`，也不是 renderer）。小程序与服务号 H5 是同一进程的不同 tab，
共用同一个调试出口——这是「注入一次，所有标签页可见」的原因。

调试通道来自 [WMPFDebugger](https://github.com/evi0s/WMPFDebugger)，它向 `flue.dll` 打两处 patch：

| patch | 位置 | 作用 |
|---|---|---|
| 1 | `AppletIndexContainer::OnLoadStart` | 强制开调试分支 → 写 `scene=1101` → 把调试服务端改成 `ws://localhost:9421` |
| 2 | `SendToClientFilter` | 短路 CDP 消息过滤器，否则调试消息被运行时丢弃 |

两个端口分工：

| 端口 | 协议 | 方向 |
|---|---|---|
| 9421 | protobuf `WARemoteDebug` | 小程序连进来（硬编码） |
| 62000 | 明文 CDP | 我们连出去 |

**这决定了通道的生命周期**：patch 写在「那一次小程序启动」的 config 结构体里，
所以 9421 上的连接就是小程序的命脉——代理一断，微信立刻把它关掉；它也不会自动重连。

---

## 3. OAuth 为什么必须过客户端

实测三种 UA 请求授权链接：

| UA | 结果 |
|---|---|
| 微信 PC UA（含 `MicroMessenger` / `WindowsWechat` / `XWEB/25799`） | 1050 B 空壳，**无 302** |
| 普通 Chrome | 同上 |
| 手机微信 UA | 同上 |

**改 UA 没有用**：客户端在网络请求发出之前就把 URL 拦走了。真正的授权发生在原生层，
HTTP 层看不到任何 code。

回调端点则确实需要真 code：`/api/v1/wechat/r?m=s_signin` 不带 code → `400`，假 code → `500`。

---

## 4. 接管 OAuth 的是 a8key 拦截层

对 `flue.dll`（237 MB）做静态扫描，在 RVA `0x0BFDD8C0` 命中一组字符串：

```
A8KeyNavigationThrottle::CheckURL
A8KeyNavigationThrottle::Redirect
IsRedirectLimitReached / a8key_redirect_limit
wechat_redirect limit reached, cancelling navigation
..\..\flue\browser\a8key\a8_key_rate_limiter.cc
open.weixin.qq.com / oauth2/authorize
```

用 `.pdata`（异常目录）拿到精确函数边界：

| RVA | 大小 | 判断 |
|---|---|---|
| `0x025FE320` | 0x21BF | `CheckURL`（同时引用限流、host、日志） |
| `0x02600690` | 0x117C | `Redirect`（改写并重定向） |
| `0x02601F70` | 0x6D9 | `A8KeyRateLimiter` |
| `0x02789300` | — | `ApiProcessor::MMV8CallNative`（所有 JS API 的总入口） |

**实测（挂着钩子时用一篇文章页导航到授权链接）**：

```
[hit] A8KeyThrottle::CheckURL
[hit] A8KeyRateLimiter
[hit] A8KeyThrottle::Redirect
Page.navigate -> net::ERR_ABORTED        ← 被 Redirect 接管，不是失败
几秒后 target 列表出现新签到页，带全新 openid
```

所以**触发方式**（`get_openid.py` 里按代价排序）：

| 策略 | 手段 | 说明 |
|---|---|---|
| S0 | 现成 URL 里已有 openid | 最快，但会先校验 401，跳过死号 |
| S3 | `Page.navigate` 到授权链接 | **实测可用**，为首选 |
| S1 | 页内 `location.href` | 渲染器发起，同样会被接管 |
| S2 | `WeixinJSBridge.invoke('openUrlWithExtra')` | 兜底 |

`flue.dll` 的 196 个导出全是 Chromium/napi 那套（`FlueMain` 是 DLL 入口），
**没有可直接调用的「打开 URL」入口**，所以只能走导航管线。

---

## 5. openid 的真实性质

它是**微助教签发的 32 位 hex 站点会话标识**（SSR 里就叫 `session.openId`），
不是微信 openid（微信的是 28 字符 base64 风格）。

三条实测结论，直接决定代码写法：

| 结论 | 证据 | 代码含义 |
|---|---|---|
| `200` 不能证明号有效 | 过期号、真无签到都返回 `200` | 不要「先验后用」 |
| 签到页 SSR 不校验 | 真号与随机 32hex 的页面归一化后 **sha1 相同** | 页面回显不能当探针 |
| 只有 `401` 可信 | 假号 / 被顶掉的号返回 `401` | 只在 401 时才续号 |

**新授权会顶掉旧号**：续号后立刻验证上一个号 → `401`。
所以续号只能是「检测到 401 才续」，**绝不能定时续**——定时续等于不停废号。

接口本身很宽松：只要 `openId` 头，不校验 cookie、Referer、Origin。

---

## 6. 需要记住的坑

1. **a8key 有重定向限流**（`wechat_redirect limit reached, cancelling navigation`）。
   触发要带 cooldown，别循环重试。
2. **代理活多久，小程序活多久**。项目退出时故意不关代理；单次模式也不静默退出。
3. **RVA 与 WMPF 版本死绑**。内置 **25799**（微信 4.1.15.56）。换版本必须重扫，
   否则 `Interceptor.replace` 偏移错位 = 进程级崩溃。
4. **通道只在「小程序启动那一刻」建立**，不会重连。顺序必须是：先起代理，再开小程序。
5. **`.pdata` 比 `0xCC` 猜测可靠**。函数边界一定要从异常目录取。

---

## 7. 风险

- patch 运行时内存属于绕过客户端自身的调试限制，存在账号风控成本；
- WMPFDebugger 为 GPLv2，本仓库整体 GPL-2.0-only，二次分发需履约；
- 上游若改结构，偏移全废，需重新静态分析。
