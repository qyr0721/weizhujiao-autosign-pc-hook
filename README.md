# 微助教自动签到（PC / 微信内置浏览器）

> Windows 上把微助教课堂签到做成全自动：**自动续号 → 5 秒轮询 → 命中即提交**。
> 不点菜单、不截图、不模拟鼠标。号失效了自己换，换完自己接着签。

---

## 特性

| 能力 | 说明 |
|---|---|
| **自动签到** | 5 秒轮询 `active_signs`，命中立刻 POST，通常 5 秒内完成 |
| **自动续号** | openid 失效（401）时自动用签到页换一个新的，全程后台，无需人工 |
| **幂等** | `repeat sign in` / `已经签到` / `errorCode 305` 一律视为已签，不重复提交 |
| **多课程** | 接口返回哪门课就签哪门，不用配课程号 |
| **GPS 签到** | `isGPS` 为真时按 `jitter_deg` 抖动提交经纬度 |
| **时段闸门** | 只在 `class_hours` 内工作，半夜不瞎跑 |
| **自愈** | 签到侧一退出就续号并拉起，`run.py` 常驻监督 |
| **双平台** | PC 版（本目录）+ Android 版（`android-module/`，原样引入） |

---

## 为什么需要 hook 微信

签到本身只需要**一个有效的 openid**：

```
GET  /wechat-api/v1/class-attendance/student/active_signs   （带 openId 头）
POST /wechat-api/v1/class-attendance/student-sign-in        {"courseId":..,"signId":..}
```

但 openid 只能由微信客户端在 OAuth 回调里签发。直接请求授权链接没有用——实测
`open.weixin.qq.com/connect/oauth2/authorize` 对**任何 UA**（PC 微信 / 手机微信 / 普通 Chrome）
都返回同一份 1KB 空壳，真正的授权发生在**客户端原生层**。

所以 hook 的唯一目的，是让微信内置浏览器**在后台自己走一次授权**，从而拿到新号：

```
[触发]  CDP Page.navigate -> open.weixin.qq.com/connect/oauth2/authorize?appid=..&redirect_uri=..
[拦截]  A8KeyNavigationThrottle::CheckURL -> A8KeyRateLimiter -> ::Redirect   （微信自己的导航拦截层）
[授权]  原生层完成授权，302 -> v18.teachermate.cn/api/v1/wechat/r?code=..
[落地]  302 -> v18.teachermate.cn/wechat-pro-ssr/student/sign?openid=<32hex>
[取号]  读这个 URL 的 openid -> 交给签到侧
```

调试通道来自 [evi0s/WMPFDebugger](https://github.com/evi0s/WMPFDebugger)（frida hook `flue.dll`），
本仓库是它的下游应用，并附了一个多客户端复用补丁。

---

## 工作流程

```
双击 start.bat
  │
  ├─ 代理守护          确保 WMPFDebugger 在跑（9421 / 62000），已在跑就复用
  ├─ 通道守护          微信里得有一个小程序连着（见下）
  │
  ├─ 签到侧（常驻）    signer/sign_pc.py
  │     └─ 每 5s GET active_signs
  │          命中 → POST student-sign-in → 记下 signId（幂等）
  │          401  → 写 openid.stale 并退出（exit 2）
  │
  └─ 续号（按需）      run.py 看到 exit 2 → 立刻续号 → 重启签到侧
                       （只在「没号」或「号 401」时动手，绝不乱续）
```

---

## 快速开始

### 前置

| 项 | 说明 |
|---|---|
| 系统 | Windows x64（在 微信 4.1.15.56 / WMPF **25799** 上验证） |
| Python | 3.10+，`pip install websockets` |
| Node | 22+，用于跑 WMPFDebugger |
| 上游 | 单独 clone [WMPFDebugger](https://github.com/evi0s/WMPFDebugger) 并按它的 README 装 frida |

路径可用环境变量覆盖，不必和作者机器一致：

```bash
set WZ_WMPF_DIR=D:\tools\WMPFDebugger
set WZ_NODE=C:\Program Files\nodejs\node.exe
```

### 跑起来

```bash
git clone https://github.com/qyr0721/weizhujiao-autosign-pc-hook.git
cd weizhujiao-autosign-pc-hook
pip install websockets

python run.py --check     # 一眼看状态：代理 / 通道 / 当前号
start.bat                 # 常驻（取号 + 签到一起跑）
```

签到参数（可选）：

```bash
cp signer/config.example.json signer/config.json
# 填 lat / lon（GPS 签到用）、poll、class_hours
```

> `signer/config.json` 在 `.gitignore` 里，不会被提交。

### 关于「通道」

WMPF 的调试通道只在**小程序启动那一刻**建立（hook 打在 `AppletIndexContainer::OnLoadStart`），
而且**不会自动重连**。所以顺序是：

```
1. 先起 run.py / start.bat
2. 再在微信里打开任意小程序一次 —— 之后可以缩到后台不管
```

> **重要**：小程序是以「调试态」被拉起的（`scene=1101`、`remoteDebugMode=1`），
> 9421 那条 WebSocket 就是它的生命线。**关代理 = 微信立刻把小程序关掉。**
> 所以本项目退出时**不关代理**。

---

## 目录

| 路径 | 作用 |
|---|---|
| `run.py` / `start.bat` | 一键入口：代理 + 通道 + 续号 + 拉起签到侧 |
| `signer/sign_pc.py` | **PC 侧签到器**（轮询 / 提交 / 幂等 / GPS / 时段） |
| `get_openid.py` | 续号核心：多策略触发（S0/S3/S1/S2）+ 落点捕获 |
| `channel_keeper.py` | 三态探测（代理死 / 通道断 / 可用）+ 代理托管 |
| `cdp_sign_agent.py` | 最小 CDP 客户端（`Target.attachToTarget` per-tab 会话） |
| `oid_probe.py` | openid 探针（单次校验 / 生命周期观测 / 签到页 SSR 校验） |
| `hook_a8key.js` / `probe_a8key.py` | a8key 拦截层观察（逆向用） |
| `tools/pe_scan.py` | 无 IDA 的 PE 扫描器：字符串 / RIP 引用 / `.pdata` 函数边界 |
| `patches/proxy-mux.ts` | CDP 代理多客户端复用（id 重写） |
| `android-module/` | Android 版（Magisk/KernelSU，MIT，原样引入） |
| `docs/` | 机制分析、逆向路线、测试报告 |

---

## 三个必须知道的坑

1. **新授权会把旧 openid 顶成 401。**
   所以续号只能是「检测到 401 才续」，**绝不能定时续**——定时续等于不停废号。
   （这也是签到侧用 `exit(2)` 通知续号、而不是自己定时刷号的原因。）

2. **`active_signs` 的 200 不能证明号有效。**
   无论过期还是真的没有签到，接口都返回 200；只有「假号 / 被顶掉」才 401。
   签到页 SSR 也不校验，纯粹回显 `session.openId`。**没有廉价验号接口**，只能真的去签。

3. **a8key 有重定向限流**（`wechat_redirect limit reached, cancelling navigation`）。
   不要循环重试，所有触发都带 cooldown。

---

## 版本适配

`hook_a8key.js` 里的 RVA 与 WMPF 版本死绑（内置 **25799**）。微信升级后先重扫：

```bash
python tools/pe_scan.py "<路径>\flue.dll" \
  --kw "A8KeyNavigationThrottle::CheckURL" "open.weixin.qq.com" \
  --json a8key_scan.json
```

`.pdata` 给出的函数边界是精确的，比按 `0xCC` 对齐猜靠得住。
上游的 `OnLoadStart` 偏移用 WMPFDebugger 的 `--auto-detect`。

---

## 法律与免责

- 本项目仅供**学习与技术研究**（浏览器调试协议、导航拦截机制、PE 静态分析）。
- 它通过 patch 微信运行时内存打通调试通道，属于绕过客户端自身限制；后果自负，
  可能违反微信软件许可协议，存在账号风险。
- `android-module/` 来自 [qyr0721/weizhujiao-autosign](https://github.com/qyr0721/weizhujiao-autosign)，MIT。
- `patches/proxy-mux.ts` 是 WMPFDebugger（GPLv2）的衍生修改，故本仓库整体
  **GPL-2.0-only**；其 `src/third-party` 版权归腾讯所有。
- 上游与贡献者：**evi0s/WMPFDebugger** 及其所有贡献者。
- 请勿用于违反所在学校/机构规定的用途。
