# 逆向取号方案：让 WMPF 在后台自己完成 OAuth 跳转

> 目标（本轮明确）：不改签到逻辑（另有项目已实现），只解决**取号自动化**——
> 让微信内置浏览器/小程序在**后台**跳到那个 `oauth2/authorize` 链接，
> 由客户端原生层完成授权，从而拿到新的 openid。
>
> 方法：静态分析 + frida hook。分析对象 `flue.dll`（237 MB），版本 25799。
> 本文所有 RVA 都来自实际扫描，可用 `tools/pe_scan.py` 复现。

---

## 1. 静态分析结果：接管点在 a8key 导航拦截层

### 1.1 直接证据（字符串表命中）

在 `flue.dll` 里扫 OAuth 相关常量，命中一处密集的字符串簇（RVA 0x0BFDD8C0 起）：

| RVA | 字符串 |
|---|---|
| 0x0BFDD8F0 | `a8key_redirect_limit` |
| 0x0BFDD905 | `IsRedirectLimitReached` |
| 0x0BFDD92D | `A8KeyNavigationThrottle::CheckURL` |
| 0x0BFDD968 | `A8KeyNavigationThrottle::Redirect` |
| 0x0BFDD997 | `WillRedirectRequest` |
| 0x0BFDD9AB | `open.weixin.qq.com` |
| 0x0BFDDAA0 | `oauth2/authorize`（与 `/connect/oauth2/authorize` 重叠共享） |
| 0x0BFDD842 | **`wechat_redirect limit reached, cancelling navigation`** |
| 0x0BFDD9F3 | 源码路径 `..\..\flue\browser\a8key\a8_key_rate_limiter.cc` |
| 0x0BFDDA23 | `A8KeyRateLimiter: duplicate request for ` / `cache expired for ` / `cache hit for ` |

**结论：OAuth 的接管不是"某个页面脚本"，而是浏览器进程里的一个 Chromium 导航节流器
（`A8KeyNavigationThrottle`）。** 它挂在导航管线上，判定 URL 后决定"改写并重定向"。
源码路径 `flue/browser/a8key/...` 也印证这一点。

### 1.2 函数表（`.pdata` 精确边界）

用 `.pdata`（异常目录）反查，比按 `0xCC` 对齐猜函数准得多：

| RVA | 大小 | 依据 | 判断 |
|---|---|---|---|
| **0x025FE320** | 0x21BF | 引用 `CheckURL` 日志、`IsRedirectLimitReached`、`a8key_redirect_limit`、`open.weixin.qq.com` | **`A8KeyNavigationThrottle::CheckURL`**（主判定） |
| **0x02600690** | 0x117C | 引用 `CheckURL` + `Redirect` 日志 | **`::Redirect`**（执行改写/重定向） |
| 0x02601F70 | 0x6D9 | 三条 `A8KeyRateLimiter` 日志 | 限流器（duplicate / cache hit / expired） |
| 0x026040D0 | 0x3B4 | 引用 `open.weixin.qq.com` | host 判定辅助 |
| 0x02642BD0 / 0x02643060 | 0x483 / 0x483 | 同上（成对，疑似模板实例） | host 判定辅助 |
| 0x02657FE0 | 0x1ED | 同上 | host 判定辅助 |

### 1.3 另一条更轻的路：JS API 层

同一份 dll 里还有：

| RVA/字符串 | 含义 |
|---|---|
| `flue.mojom.WeixinJSBridge` | 页面 ↔ 原生的桥 |
| `OpenUrlWithExtra`（0x0BFF2D70 附近，源码 `flue/browser/runtime/js_api/api_processor.cc`） | **一个"打开 URL 到额外 webview"的 JS API** |
| `ApiProcessor::MMV8CallNative(const std::string&, const std::string&)` | 所有 JS API 的统一入口：**第一个参数是 api 名，第二个是参数 JSON** |
| 0x02789300（0x16E6）/ 0x027B9A40（0x11BB） | `MMV8CallNative` 的两个实现/分支 |

也就是说：**页面里 `WeixinJSBridge.invoke('<api>', {...})` 最终都会落到 0x02789300**。
挂住它，就能看到任何页面调了哪些 API、传了什么——
既能为"用 `openUrlWithExtra` 触发跳转"铺路，也能反过来看清微信自己是怎么做的。

---

## 2. 三条触发路径（按代价排序）

### 路径 A｜页内调用 `openUrlWithExtra`（最轻，推荐先试）

```
小程序/H5 页面  --WeixinJSBridge.invoke('openUrlWithExtra', {url: <oauth>})-->
   ApiProcessor::MMV8CallNative(0x2789300)  ->  打开新 webview  ->  正常导航管线
   ->  A8KeyNavigationThrottle::CheckURL  ->  Redirect  ->  OAuth  ->  回调  ->  sign?openid=...
```

- 优点：不碰界面、不需要重放 IPC；用现成的常驻小程序 + CDP `Runtime.evaluate` 一行即可。
- 未知：`api_processor.cc` 里能看到 `IsAllowedAsDiscoveryPageApi`、`jsapi verify error`、
  `system:access_denied` —— **存在权限/来源校验**。第三方小程序可能被拒。
- 验证成本极低：在常驻小程序里 evaluate 一次，看返回值 + 钩子是否打到 `openUrlWithExtra`。

### 路径 B｜CDP `Page.navigate`（已试过，失败原因现在有解释）

上一轮在 `liteapp` 容器页上 `Page.navigate` 到授权链接，只拿到 1050B 空壳。
现在有两个新线索：

1. `wechat_redirect limit reached, cancelling navigation` —— **存在重定向次数上限**，
   反复尝试会直接把这次导航**取消**；
2. 该判定在 `CheckURL` 里，而它是否被执行取决于该 webview 的节流器链。

所以：**不要再凭结果猜，直接挂钩子看 `CheckURL` 有没有被调用、走到哪个分支**。
这也是本轮工具存在的意义。

### 路径 C｜原生入口重放（最重，兜底）

扫描发现两个函数引用了 `OpenUrl` 字符串：`0x035383D0`(0x714)、`0x0359DFB0`(0x1904)，
疑似 `Weixin.exe -> WeChatAppEx.exe` 的"打开 URL"IPC 处理端。
若 A/B 都不行，就在这两个函数上取参数结构，用 frida 直接构造调用。工作量最大。

---

## 3. 取号闭环：openid 到底在哪一步出现

```
[触发] oauth2/authorize?appid=wxa153455f3ef1d9f9&redirect_uri=.../api/v1/wechat/r?m=s_signin
[原生] A8Key 改写 + 授权  ->  302 到  https://v18.teachermate.cn/api/v1/wechat/r?m=s_signin&code=XXXX
[站点] /api/v1/wechat/r 用 code 换会话（实测：无 code→400 invalid param；假 code→500 internal error）
       ->  302 到  https://v18.teachermate.cn/wechat-pro-ssr/student/sign?openid=<32hex>
[取号] 读这个 URL 的 openid 参数即可
```

捕获点三选一：

- **target URL**（`Target.getTargets` / `Page.frameNavigated` 里的 URL）——最省事；
- `A8KeyNavigationThrottle::Redirect` 的入参（改写后的 URL）——能更早拿到；
- 页内 `location.href`（`Runtime.evaluate`）。

> 注意：`sign?openid=` 出现的时机取决于 `/api/v1/wechat/r` 的 302。若它返回 200 而不是 302，
> 说明 code 无效（假 code 实测就是 500），此时钩子应能在 `Redirect` 处看出问题。

---

## 4. 现在就能跑的实验

```bash
# 1) 只看不碰：挂 16 个钩子，然后你正常操作微信（打开任意 H5 / 点链接）
python probe_a8key.py --seconds 120

# 2) 挂钩子 + 主动让某个标签页导航到授权链接（需要常驻小程序提供通道）
python probe_a8key.py --cdp-nav "https://open.weixin.qq.com/connect/oauth2/authorize?appid=wxa153455f3ef1d9f9&redirect_uri=https%3A%2F%2Fv18.teachermate.cn%2Fapi%2Fv1%2Fwechat%2Fr%3Fm%3Ds_signin&response_type=code&scope=snsapi_base&state=&connect_redirect=1#wechat_redirect" --seconds 40

# 3) 重新扫描（换微信版本后必须做）
python tools/pe_scan.py "<runtime>\flue.dll" --kw "A8KeyNavigationThrottle::CheckURL" "open.weixin.qq.com" --json a8key_scan.json
```

**判读表**（`probe_a8key.py` 的命中汇总）：

| 现象 | 含义 | 下一步 |
|---|---|---|
| `A8KeyThrottle::CheckURL` 有命中，`Redirect` 无 | 导航进了判定，但没被接管（可能被限流或来源不对） | 看 `mark:redirect_limit` / `A8KeyRateLimiter` 是否命中 |
| 两者都命中 | **接管成功**，OAuth 已触发 | 去 target 列表里找 `student/sign?openid=` |
| `A8KeyRateLimiter` 命中 `duplicate/cache` | 撞限流，等冷却或换 webview | 别连续重试 |
| 全部无命中 | 该导航没走这条节流器链（换 target / 换触发方式） | 走路径 A 或 C |
| `MMV8CallNative` 命中且 `jsapi=openUrlWithExtra` | 路径 A 有效，页面能调这个 API | 直接用它做触发器 |

---

## 5. 风险与注意

1. **RVA 与版本死绑**：微信升级 → `flue.dll` 换 → 所有 RVA 失效。换版本先重跑 `pe_scan.py`，
   确认 `.pdata` 边界与字符串簇位置一致，再更新 `DEFAULT_HOOKS`。
2. **限流是真的**：`wechat_redirect limit reached, cancelling navigation` 与
   `A8KeyRateLimiter` 都说明连点/连跳会被取消。**别用循环重试**，失败后要留冷却。
3. **崩溃风险**：读内存一律走 `Process.findRangeByAddress` + 长度校验（已在 `hook_a8key.js` 里实现）；
   但 `Interceptor.attach` 到错位的地址仍可能直接崩进程，改动前先备份 RVA 表。
4. **别 hook `MMV8CallNative` 太久**：它是所有 JS API 的总入口，长期挂着有性能损耗，只用于诊断。
5. **合规**：仍是绕过客户端自身限制，账号风险自负；WMPFDebugger 派生部分遵守 GPLv2。

---

## 6. 状态

| 项 | 状态 |
|---|---|
| `tools/pe_scan.py`（PE 解析 / 字符串 / RIP-xref / `.pdata` 函数边界） | ✅ 已实现并跑通 |
| a8key 机制与函数表 | ✅ 已定位（7 个函数 + 12 个标记点） |
| `hook_a8key.js` + `probe_a8key.py` | ✅ 16/16 钩子安装成功，无崩溃 |
| 触发路径 B（CDP `Page.navigate`） | ✅ **跑通**（见 §7） |
| openid 捕获闭环 | ✅ 跑通，拿到新号 |
| 触发路径 A（`openUrlWithExtra`） | ⏳ 未单独验证（B 已够用） |
| 触发路径 C（原生入口重放） | ❌ 不需要了 |

---

## 7. 实测结果：后台触发 OAuth 成立

### 7.1 实验

条件：一个小程序常驻提供通道；目标标签页选 **公众号文章页** `mp.weixin.qq.com/s/index.html`（不毁签到页）。

```
python probe_a8key.py --match "mp.weixin.qq.com" --cdp-nav "<授权链接>" --seconds 40
```

### 7.2 结果

```
[*] 主动导航 -> https://mp.weixin.qq.com/s/index.html?...
[hit] host_check_B
[hit] A8KeyThrottle::CheckURL            ← 导航进了判定
[hit] A8KeyRateLimiter                   ← 走了限流检查
[hit] A8KeyThrottle::Redirect            ← 被接管并重定向
[hit] host_check_C / A
[*] Page.navigate 返回: {"errorText": "net::ERR_ABORTED"}   ← 原导航被 abort，因为被 Redirect 接管
```

随后 `Target.getTargets` 里出现**新的签到页**：

```
page  https://v18.teachermate.cn/wechat-pro-ssr/student/sign?openid=60536dd83a0a067b61c3cf1eaa3a7cf9
```

`ERR_ABORTED` 不是失败——正是 a8key 把这次导航改成 Redirect 的证据。

### 7.3 三个结论

1. **`Page.navigate` 到 `oauth2/authorize` 会完整走 a8key 拦截并触发原生 OAuth**，全程无需人工点击。
   上一版方案里"CDP 三条路封死"的结论**作废**——根因是当时在 `liteapp.weixin.qq.com`（小程序容器页）上测，
   而且只看页面外观、没看节流器行为。
2. **新授权会把旧号顶成 401**（实测：`2e8399af…` 在新授权后立即 401）。
   所以续号策略只能是「**已 401 才续**」，绝不能定时续——定时续等于不停废号。
3. `A8KeyRateLimiter` 会命中，所以**不能连续触发**；`get_openid.py` 的 cooldown 是必需的，不是保守。

### 7.4 还剩下的摩擦点

代理进程无法在我的工具调用之外存活（实测：`&`、`cmd start`、`nohup`、node `detached+unref` 全部被回收；
只有工具自带的后台任务能活，但它一结束代理也跟着死）。而 Hook 要求"**代理先起、小程序后进**"。

当前可行姿势：让代理在一个**你自己开的终端**里常驻（`npx tsx src/index.ts`），
之后小程序只需要连一次，我这边一条命令就能取号；代理不重启就不用重进。

