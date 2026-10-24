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
| **空窗降频** | 没有课时降到 30 秒一次探活，一有签到立刻回到 5 秒（不看本地时钟） |
| **自愈** | 签到侧一退出就续号并拉起，`run.py` 常驻监督 |

---

## 为什么需要 hook 微信

签到本身只需要**一个有效的 openid**：

```
GET  /wechat-api/v1/class-attendance/student/active_signs   （带 openId 头）
POST /wechat-api/v1/class-attendance/student-sign-in        {"courseId":..,"signId":..}
```

但 openid 只能由微信客户端在 OAuth 回调里签发。直接请求授权链接没有用——实测
`open.weixin.qq.com/connect/oauth2/authorize` 对**任何 UA**（PC 微信 / 手机微信 / 普通 Chrome）
都返回同一份 1 KB 空壳，真正的授权发生在**客户端原生层**。

所以 hook 的唯一目的，是让微信内置浏览器**在后台自己走一次授权**，从而拿到新号：

```
[触发]  CDP Page.navigate -> open.weixin.qq.com/connect/oauth2/authorize?appid=..&redirect_uri=..
[接管]  A8KeyNavigationThrottle::CheckURL -> A8KeyRateLimiter -> ::Redirect   （微信自己的导航拦截层）
[授权]  原生层完成授权，302 -> v18.teachermate.cn/api/v1/wechat/r?code=..
[落地]  302 -> v18.teachermate.cn/wechat-pro-ssr/student/sign?openid=<32hex>
[取号]  读这个 URL 的 openid -> 交给签到侧
```

细节与实测证据见 [`docs/DESIGN.md`](docs/DESIGN.md)。
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

## 微信版本：必须锁在 4.1.15.56

**hook 里的 RVA 偏移是按 WMPF 运行时 `25799` 扫出来的，与微信版本死绑。**

| 项 | 值 |
|---|---|
| 微信 PC | **4.1.15.56** |
| WMPF 运行时 | **25799** |
| 安装包 | [`WeChatWin_4.1.15.exe`](https://github.com/qyr0721/weizhujiao-autosign-pc-hook/releases/latest)（Release 附件，245 MB） |
| 安装包 sha256 | `4422bad98617dd095005c7879b2190f188ffa9ad6abf79e075358cf981a3d9e4` |

`wxsign.exe --check` 会直接读你机器上的实际版本并对照，不一致就告警。

### 装完之后：关闭自动更新（重要）

微信一升级，WMPF 版本就变，`flue.dll` 里的函数偏移全部失效 —— 轻则取号失败，
重则 `Interceptor.replace` 打在错位地址上，**直接把浏览器进程打崩**。所以务必锁版本。

**方法一（推荐，设置里关）**

```
微信左下角「≡」（三条横线）→ 设置 → 通用（通用设置）
  → 找到「有更新时自动升级微信」→ 取消勾选
```

**方法二（加固，改文件名）**

```
打开微信安装目录（微信快捷方式右键 → 打开文件所在位置）
  → 进入版本号子目录，例如 4.1.15.56
  → 把 WeixinUpdate.exe 改名为 WeixinUpdate.exe.bak
```

> 升级后确实想继续用：先用 `tools/pe_scan.py` 重扫 `flue.dll` 拿新 RVA
> （见「版本适配」），再更新 `tools/hook_a8key.js`。

---

## 快速开始

### 前置

| 项 | 说明 |
|---|---|
| 系统 | Windows x64 |
| 微信 | **4.1.15.56**（WMPF 25799），并关闭自动更新 |
| Python | 3.10+，`pip install websockets`（仅源码运行需要） |
| Node | 22+，用于跑 WMPFDebugger |
| 上游 | 单独 clone [WMPFDebugger](https://github.com/evi0s/WMPFDebugger) 并按它的 README 装 frida 绑定 |

路径可用环境变量覆盖，不必和作者机器一致：

```bash
set WZ_WMPF_DIR=D:\tools\WMPFDebugger
set WZ_NODE=C:\Program Files\nodejs\node.exe
```

### 跑起来

**方式一：下载打包好的 exe（推荐给不想装 Python 的人）**

到 [Releases](https://github.com/qyr0721/weizhujiao-autosign-pc-hook/releases) 下载 `wxsign.exe`，双击即可。
它会先自检环境（微信进程 / WMPF 版本 / WMPFDebugger / Node），再一步步告诉你要点哪里：

```
【1/4】环境自检
【2/4】启动调试代理（自动）
【3/4】请在微信里：① 左侧「小程序」→ 点任意小程序（必做）
                    ② 通讯录→公众号→微助教→底部「签到」（建议）
【4/4】自动取号 + 签到已启动
```

> exe 只含调度与签到逻辑，**不含** WMPFDebugger 与微信安装包（后者在 Release 附件里）。
> 不想直接跑可以用 `wxsign.exe --check` 只做自检。

**方式二：源码运行**

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
# 填 lat / lon（GPS 签到用）、poll、idle_poll / idle_after
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
| `wizard.py` | 引导式启动器（`wxsign.exe` 的源码） |
| `signcore.py` | 签到核心（轮询 / 提交 / 幂等 / GPS / 空窗退避） |
| `signer/sign_pc.py` | 签到 CLI（`signcore` 的薄壳） |
| `get_openid.py` | 续号核心：多策略触发（S0/S3/S1/S2）+ 落点捕获 |
| `channel_keeper.py` | 三态探测（代理死 / 通道断 / 可用）+ 代理托管 |
| `cdp_sign_agent.py` | 最小 CDP 客户端（`Target.attachToTarget` per-tab 会话） |
| `tools/pe_scan.py` | 无 IDA 的 PE 扫描器：字符串 / RIP 引用 / `.pdata` 函数边界 |
| `tools/probe_a8key.py` + `tools/hook_a8key.js` | a8key 拦截层观察（逆向用） |
| `tools/trigger_oauth.py` | 授权触发三变体（`href` / `open` / `nav`）重测 |
| `tools/oid_probe.py` | openid 探针（单次校验 / 生命周期观测 / SSR 校验） |
| `tools/start_proxy.js` | 脱离终端常驻启动代理（无 shell 可用的环境） |
| `patches/proxy-mux.ts` | CDP 代理多客户端复用（id 重写） |
| `docs/` | [`DESIGN.md`](docs/DESIGN.md) 机制与取舍；[`TESTING.md`](docs/TESTING.md) 测试说明 |
| `test/` | 离线测试（`python test/run_all.py`） |

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

`tools/hook_a8key.js` 里的 RVA 与 WMPF 版本死绑（内置 **25799**）。微信升级后先重扫：

```bash
python tools/pe_scan.py "<路径>\flue.dll" \
  --kw "A8KeyNavigationThrottle::CheckURL" "open.weixin.qq.com" \
  --json a8key_scan.json
```

`.pdata` 给出的函数边界是精确的，比按 `0xCC` 对齐猜靠得住。
上游的 `OnLoadStart` 偏移用 WMPFDebugger 的 `--auto-detect`。

---

## 姊妹项目

- **Android 版**：[qyr0721/weizhujiao-autosign](https://github.com/qyr0721/weizhujiao-autosign)（MIT）
  Magisk / KernelSU 模块，走 root 静默授权 + 读 Cookie 库取号。签到接口契约与本文一致。

---

## 法律与免责

- 本项目仅供**学习与技术研究**（浏览器调试协议、导航拦截机制、PE 静态分析）。
- 它通过 patch 微信运行时内存打通调试通道，属于绕过客户端自身限制；后果自负，
  可能违反微信软件许可协议，存在账号风险。
- 微信安装包由腾讯发布，此处仅为方便锁定版本而转载，版权与许可归腾讯所有；
  建议优先从 [pc.weixin.qq.com](https://pc.weixin.qq.com/) 获取。
- `patches/proxy-mux.ts` 是 WMPFDebugger（GPLv2）的衍生修改，故本仓库整体
  **GPL-2.0-only**；其 `src/third-party` 版权归腾讯所有。
- 上游与贡献者：**evi0s/WMPFDebugger** 及其所有贡献者。
- 请勿用于违反所在学校/机构规定的用途。
