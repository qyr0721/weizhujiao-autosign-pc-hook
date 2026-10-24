# wmpf-openid —— 微信内置浏览器 OAuth 取号（后台全自动）

> 用途：让 **PC 微信内置浏览器在后台自行完成一次 OAuth 授权**，从而自动拿到
> 微助教（`v18.teachermate.cn`）签发的 `openid`。
> 不点菜单、不截图、不模拟鼠标。

---

## 它到底是什么

微助教的 `openid` 只能由微信客户端在 OAuth 回调里签发，普通 HTTP 请求拿不到——
`open.weixin.qq.com/connect/oauth2/authorize` 对任何 UA 都只返回一份 1KB 的空壳，
真正的授权发生在**客户端原生层**。

本项目通过 WMPF 的 CDP 调试通道，让内置浏览器把一个页面导航到授权链接，
触发微信自己的 a8key 导航拦截层完成授权，再从落地的签到页 URL 里取 `openid`：

```
[触发]  Page.navigate -> open.weixin.qq.com/connect/oauth2/authorize?appid=...&redirect_uri=...
[拦截]  A8KeyNavigationThrottle::CheckURL  ->  A8KeyRateLimiter  ->  ::Redirect
[授权]  微信原生层完成授权，302 到  v18.teachermate.cn/api/v1/wechat/r?code=...
[落地]  302 到  v18.teachermate.cn/wechat-pro-ssr/student/sign?openid=<32hex>
[取号]  读这个 URL 的 openid 参数
```

上游调试通道来自 [evi0s/WMPFDebugger](https://github.com/evi0s/WMPFDebugger)（frida hook `flue.dll`）。
本仓库是它的**下游应用**，另外附了一个多客户端复用补丁。

---

## 前置条件

| 项 | 说明 |
|---|---|
| 系统 | Windows x64（本项目在 微信 4.1.15.56 / WMPF **25799** 上验证） |
| Python | 3.10+（用到 `asyncio` / `websockets`） |
| Node | 22+（跑 WMPFDebugger） |
| 依赖 | `pip install websockets`；WMPFDebugger 按其 README 安装（frida 原生绑定） |
| 上游仓库 | 单独 clone `WMPFDebugger`，本仓库不包含它 |

路径可用环境变量覆盖，避免绑定某台机器：

```bash
set WZ_WMPF_DIR=D:\tools\WMPFDebugger
set WZ_NODE=C:\Program Files\nodejs\node.exe
```

---

## 快速开始

```bash
git clone <this-repo>
cd wmpf-openid
pip install websockets

# 1) 首次：确认代理与通道
python run.py --check

# 2) 常驻（推荐）
start.bat
```

`run.py --check` 会打印三行：代理是否在跑、通道是否可用、当前 openid 的状态。

### 关于「通道」

WMPF 的调试通道只在**小程序启动那一刻**建立（hook 打在 `AppletIndexContainer::OnLoadStart`），
且**不会自动重连**。所以顺序必须是：

```
1. 先起代理         python run.py  /  start.bat
2. 再开任意小程序    微信里点开一次即可，之后可以缩到后台不管
```

> **重要**：小程序是以「调试态」被拉起的（`scene=1101`、`remoteDebugMode=1`），
> 9421 那条 WebSocket 就是它的生命线。**关掉代理 = 微信立刻关掉小程序**。
> 本项目因此默认「退出不关代理」。

---

## 主要脚本

| 文件 | 作用 |
|---|---|
| `run.py` / `start.bat` | 一键入口：代理守护 + 通道守护 + 只在必要时取号 |
| `get_openid.py` | 取号核心：发现标签页 → 触发 → 捕获 → 落盘 |
| `channel_keeper.py` | 三态探测（代理死 / 通道断 / 可用）+ 代理托管 |
| `cdp_sign_agent.py` | 最小 CDP 客户端（`Target.attachToTarget` per-tab 会话） |
| `oid_probe.py` | openid 探针（单次校验 / 生命周期观测 / 签到页 SSR 校验） |
| `page_agent.js` | 页内 agent（可选；纯 HTTP 方案下非必需） |
| `hook_a8key.js` / `probe_a8key.py` | a8key 拦截层观察（逆向用） |
| `tools/pe_scan.py` | 无 IDA 的 PE 扫描器：字符串 / RIP 相对引用 / `.pdata` 函数边界 |
| `patches/proxy-mux.ts` | CDP 代理多客户端复用补丁（id 重写） |

### 取号策略（`get_openid.py`）

按代价从低到高，每步只试一次：

| 策略 | 做法 | 说明 |
|---|---|---|
| S0 | 扫 target 列表，URL 里已有 `openid` | 最省事，但要**跳过 401 的死号** |
| S3 | `Page.navigate` 到授权链接 | 实测最可靠，走 a8key 拦截 |
| S1 | 页内 `location.href = url` | 兜底 |
| S2 | 页内 `WeixinJSBridge.invoke('openUrlWithExtra')` | 兜底（有权限校验） |

---

## 三个必须知道的坑

1. **新授权会把旧 openid 顶成 401。**
   所以续号只能是「检测到 401 才续」，**绝不能定时续**——定时续等于不停废号。

2. **`active_signs` 的 200 不能证明号有效。**
   无论过期还是真的没有签到，接口都返回 200；只有「假号 / 被顶掉」才 401。
   签到页 SSR 也不校验，纯粹回显 `session.openId`。**没有廉价验号接口。**

3. **a8key 有重定向限流**（`wechat_redirect limit reached, cancelling navigation`）。
   不要循环重试，本项目所有触发都带 cooldown。

---

## 版本适配

`hook_a8key.js` 里的 RVA 与 WMPF 版本死绑（本仓库内置的是 **25799**）。
微信升级后先重扫：

```bash
python tools/pe_scan.py "<路径>\flue.dll" \
  --kw "A8KeyNavigationThrottle::CheckURL" "open.weixin.qq.com" \
  --json a8key_scan.json
```

`.pdata` 给出的函数边界是精确的，比按 `0xCC` 对齐猜靠得住。
上游 WMPFDebugger 的 `OnLoadStart` 偏移则用它的 `--auto-detect`。

---

## 法律与免责

- 本项目仅供**学习与技术研究**（浏览器调试协议、导航拦截机制、PE 静态分析）。
- 它通过 patch 微信运行时内存打通调试通道，属于绕过客户端自身限制；使用后果自负，
  可能违反微信软件许可协议，存在账号风险。
- `patches/proxy-mux.ts` 是 WMPFDebugger（GPLv2）的衍生修改，因此本仓库整体以
  **GPL-2.0-only** 发布。`WMPFDebugger` 的 `src/third-party` 代码版权归腾讯所有。
- 上游与贡献者：**evi0s/WMPFDebugger** 及其所有贡献者。
- 请勿用于违反所在学校/机构规定的用途。
