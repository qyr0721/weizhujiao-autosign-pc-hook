# 微助教自动签到（PC / 微信内置浏览器）

本项目在 Windows 平台实现微助教课堂签到的自动化：自动获取凭证、轮询签到任务并提交结果。  
全过程无需人工点击、屏幕截图或鼠标模拟；凭证失效时自动重新获取并继续运行。

> **⚠️ 首次使用必须手动授权一次**
>
> 软件不会在第一次运行时凭空拿到凭证——需要你在微信里先手动走一遍签到入口，  
> 让微信客户端完成一次 OAuth 授权。此后自动续期才能在后台接管。
>
> **操作步骤（仅首次需要）：**
>
> 1. 在微信里打开「微助教服务号」，找到老师发的签到通知（或任意班级通知）
> 2. **右键点击通知卡片**（或点击「查看详情」），在弹出的菜单中点击 **「签到(C)」**
> 3. 接下来会跳转到微信授权页面，**点击「确认授权」**（或「允许」）
> 4. 授权完成后会进入签到页面，至此首次授权完成
>
> 之后运行本程序即可全自动签到。若长时间后凭证失效导致签到中断，**重新执行上述步骤一次**即可恢复。

---

## 功能

| 能力     | 说明                                                        |
| ------ | --------------------------------------------------------- |
| 自动签到   | 以 5 秒间隔轮询 `active_signs`，发现进行中的签到立即提交                     |
| 自动续期凭证 | openid 失效（HTTP 401）时自动重新获取，全程在后台完成                        |
| 幂等提交   | `repeat sign in` / `已经签到` / `errorCode 305` 均判定为已签到，不重复提交 |
| 多课程    | 依据接口返回内容处理，无需预先配置课程标识                                     |
| GPS 签到 | `isGPS` 为真时按 `jitter_deg` 对经纬度施加抖动后提交                     |
| 进程自愈   | 签到进程异常退出后由主循环重新获取凭证并拉起                                    |

---

## 实现原理

签到接口仅依赖一个有效的 openid：

```
GET  /wechat-api/v1/class-attendance/student/active_signs   （请求头携带 openId）
POST /wechat-api/v1/class-attendance/student-sign-in        {"courseId":..,"signId":..}
```

该 openid 由微信客户端在 OAuth 回调过程中签发，无法通过普通 HTTP 请求获得。实测表明，  
以任意 User-Agent（PC 微信、手机微信、Chrome）请求  
`open.weixin.qq.com/connect/oauth2/authorize`，服务端均返回同一份约 1 KB 的占位页面，  
且不产生 302 跳转；真正的授权流程发生在客户端原生层。

因此，本项目的 hook 仅用于使微信内置浏览器在后台完成一次授权，从而取得新的 openid：

```
[触发]  CDP Page.navigate -> open.weixin.qq.com/connect/oauth2/authorize?appid=..&redirect_uri=..
[接管]  A8KeyNavigationThrottle::CheckURL -> A8KeyRateLimiter -> ::Redirect（微信内置导航拦截层）
[授权]  原生层完成授权，302 -> v18.teachermate.cn/api/v1/wechat/r?code=..
[落地]  302 -> v18.teachermate.cn/wechat-pro-ssr/student/sign?openid=<32hex>
[取号]  解析目标 URL 中的 openid，交由签到模块使用
```

机制细节与实测数据见 [`docs/DESIGN.md`](docs/DESIGN.md)。  
调试通道基于 [evi0s/WMPFDebugger](https://github.com/evi0s/WMPFDebugger)（通过 frida hook `flue.dll`），  
本项目为其下游应用，并附带一个多客户端复用补丁。

---

## 工作流程

```
首次使用（仅一次）
  在微信「微助教服务号」中右键点击签到通知 → 菜单点「签到(C)」
    → 跳转授权页 → 点「确认授权」→ 进入签到页（此时微信已持有可复用的会话）

日常使用
  运行 start.bat / wxsign.exe
    │
    ├─ 代理守护        确保 WMPFDebugger 运行并监听 9421 / 62000，已运行则复用
    ├─ 通道守护        检测小程序是否已连接调试通道
    │
    ├─ 签到模块（常驻）signer/sign_pc.py
    │     └─ 每 5 秒请求 active_signs
    │          发现签到 → POST student-sign-in → 记录 signId（幂等）
    │          返回 401 → 写入 openid.stale 并以退出码 2 结束
    │
    └─ 凭证续期（按需）主循环捕获退出码 2 后立即重新获取凭证并重启签到模块
                        （不设冷却，失效即取）
```

---

## 微信版本要求

hook 中的 RVA 偏移依据 WMPF 运行时 `25799` 扫描获得，与微信版本严格绑定。

| 项目         | 值                                                                                                              |
| ---------- | -------------------------------------------------------------------------------------------------------------- |
| 微信 PC      | **4.1.15.56**                                                                                                  |
| WMPF 运行时   | **25799**                                                                                                      |
| 安装包        | [`WeChatWin_4.1.15.exe`](https://github.com/qyr0721/weizhujiao-autosign-pc-hook/releases/latest)（发布页附件，245 MB） |
| 安装包 sha256 | `4422bad98617dd095005c7879b2190f188ffa9ad6abf79e075358cf981a3d9e4`                                             |

`wxsign.exe --check` 会读取本机实际版本并与上述值比对，不一致时给出提示。

### 关闭微信自动更新

微信升级会导致 WMPF 版本变化，使 `flue.dll` 中的函数偏移失效。轻则无法获取凭证，  
重则 `Interceptor.replace` 写入错位地址并导致浏览器进程崩溃。因此应锁定版本。

**方式一：通过设置关闭**

```
微信左下角菜单（≡）→ 设置 → 通用（通用设置）
  → 取消勾选「有更新时自动升级微信」
```

**方式二：重命名更新程序**

```
打开微信安装目录（快捷方式 → 右键 → 打开文件所在位置）
  → 进入版本子目录，例如 4.1.15.56
  → 将 WeixinUpdate.exe 重命名为 WeixinUpdate.exe.bak
```

> 若升级后仍需使用本项目：先以 `tools/pe_scan.py` 重新扫描 `flue.dll` 获取新的 RVA  
> （参见「版本适配」），再更新 `tools/hook_a8key.js`。

---

## 部署

### 运行依赖

程序已内嵌 Python 运行时，**无需安装 Python**。但其 hook 依赖 WMPFDebugger（基于 Node.js），  
因此仍需以下组件：

| 组件                         | 便携版            | 集成版     |
| -------------------------- | -------------- | ------- |
| 操作系统 Windows x64           | 必需             | 必需      |
| 微信 PC 4.1.15.56            | 使用者安装，并关闭自动更新  | 同左      |
| Node.js 22+                | 首次运行时从网络获取     | **已内置** |
| WMPFDebugger + tsx + frida | 首次运行时从网络获取     | **已内置** |
| 偏移配置                       | 首次运行时探测生成      | **已内置** |
| Python 3.10+               | 不需要（仅源码运行方式需要） | 不需要     |

### 版本选择

| 版本      | 文件                      | 体积                    | 联网        | 适用             |
| ------- | ----------------------- | --------------------- | --------- | -------------- |
| **集成版** | `wxsign-integrated.zip` | 约 111 MB（解压后约 300 MB） | **完全不需要** | 网络受限或希望解压即用的环境 |
| **便携版** | `wxsign-portable.exe`   | 约 9 MB                | 首次运行需联网   | 可接受首次下载的环境     |

两者的签到逻辑完全一致，区别仅在运行环境的获取方式。

**集成版**：解压后运行目录内的 `wxsign.exe` 即可，全程不访问网络。

**便携版**：单个可执行文件，首次启动时检测缺失组件并自动部署，之后直接使用已部署的内容，  
不会重复下载。部署过程如下：

```
Node 运行时      下载便携版压缩包并解压至 runtime\node，不写入系统目录
WMPFDebugger     使用 git clone；未检测到 git 时改用 codeload 压缩包
依赖 tsx / frida 执行 npm install（frida 预编译绑定约 130 MB）
偏移配置         执行一次 --auto-detect 并将结果写入 frida\config\win32\
```

> 偏移配置步骤要求微信处于运行状态（需附加至进程进行扫描）。若当时微信未启动，该步骤会失败，  
> 但不会阻止运行：程序在检测到配置缺失时会自动附加 `--auto-detect` 参数，仅每次启动多耗时约 10 秒。

部署产物全部位于程序目录（`runtime\node`、`WMPFDebugger`），不写入注册表、不修改 PATH 环境变量。

如需复用系统中已存在的组件，可通过环境变量指定路径：

```bat
set WZ_WMPF_DIR=D:\tools\WMPFDebugger
set WZ_NODE=C:\Program Files\nodejs\node.exe
```

禁用自动部署：

```bat
wxsign-portable.exe --no-setup
```

### 启动签到程序

程序启动后依次执行环境自检、启动调试代理并显示运行状态：

```
┌──────────────────────────────────────────────────────────┐
│  微助教自动签到  v1.3                                    │
│  by qyr0721                                              │
│  github.com/qyr0721/weizhujiao-autosign-pc-hook          │
└──────────────────────────────────────────────────────────┘

  微信 4.1.15.56   ·   WMPF 25799
  自检  正常

  ─────────────────────────────

  │ 监视中 · 号 4026a8fb

    通道已建立，无需再操作
    小程序可以缩到后台或最小化

  ─────────────────────────────
  签到  12:34:56  签到成功
  ─────────────────────────────

  日志  logs\wxsign-20261025-0258.log
  Ctrl+C 退出，不影响正在运行的小程序
```

> 可执行文件仅包含调度与签到逻辑，不含 WMPFDebugger 与微信安装包（后者见发布页附件）。

**方式二：源码运行**

```bash
git clone https://github.com/qyr0721/weizhujiao-autosign-pc-hook.git
cd weizhujiao-autosign-pc-hook
pip install websockets

python run.py --check     # 检测代理、通道与当前凭证状态
start.bat                 # 常驻运行（凭证获取与签到同时进行）
```

可选配置项：

```bash
cp signer/config.example.json signer/config.json
# 配置 lat / lon（GPS 签到）、poll
```

> `signer/config.json` 已列入 `.gitignore`，不会被提交。

### 调试通道说明

WMPF 调试通道仅在**小程序启动瞬间**建立（hook 位于 `AppletIndexContainer::OnLoadStart`），  
且断开后不会自动重连。因此启动顺序如下：

```
1. 启动 run.py / start.bat / wxsign.exe
2. 在微信中打开任意一个小程序一次，此后可将其置于后台
```

建议使用微信官方的「小程序示例」：在微信「小程序」面板中搜索该名称即可。  
该程序体积较小、启动迅速且长期可用。

> 微信文章页与公众号页面不属于小程序，不会触发 WMPF 运行时初始化，无法替代。

> **注意**：小程序以调试模式启动（`scene=1101`、`remoteDebugMode=1`），  
> 9421 端口上的 WebSocket 连接为其生命周期所依赖。终止调试代理将导致微信立即关闭该小程序，  
> 因此本项目退出时不会关闭代理进程。

---

## 目录结构

| 路径                                             | 说明                                                                      |
| ---------------------------------------------- | ----------------------------------------------------------------------- |
| `run.py` / `start.bat`                         | 主入口：代理守护、通道守护、凭证获取与签到调度                                                 |
| `wizard.py`                                    | 主程序源码（便携版与集成版共用）                                                        |
| `installer.py`                                 | 环境部署模块，由主程序在检测到组件缺失时调用                                                  |
| `uikit.py`                                     | 主程序与部署模块共用的终端渲染与系统探测                                                    |
| `signcore.py`                                  | 签到核心：轮询、提交、幂等判定、GPS                                                     |
| `signer/sign_pc.py`                            | 签到命令行入口（`signcore` 封装）                                                  |
| `get_openid.py`                                | 凭证获取：多策略触发（S0/S3/S1/S2）与落点捕获                                            |
| `channel_keeper.py`                            | 三态探测（代理未运行 / 通道断开 / 可用）与代理托管                                            |
| `cdp_sign_agent.py`                            | 最小 CDP 客户端（`Target.attachToTarget` 单标签页会话）                              |
| `tools/pe_scan.py`                             | PE 静态分析：字符串、RIP 相对引用、`.pdata` 函数边界                                      |
| `tools/probe_a8key.py` + `tools/hook_a8key.js` | a8key 导航拦截层观测                                                           |
| `tools/trigger_oauth.py`                       | 授权触发方式验证（`href` / `open` / `nav`）                                       |
| `tools/oid_probe.py`                           | openid 探测：单次校验、生命周期观测、签到页 SSR 校验                                        |
| `tools/start_proxy.js`                         | 无终端环境下以脱离方式启动代理                                                         |
| `patches/proxy-mux.ts`                         | CDP 代理多客户端复用（请求 id 重写）                                                  |
| `docs/`                                        | [`DESIGN.md`](docs/DESIGN.md) 实现机制；[`TESTING.md`](docs/TESTING.md) 测试说明 |
| `test/`                                        | 离线测试（`python test/run_all.py`）                                          |

---

## 注意事项

1. **首次必须手动授权一次**（见页首说明）。  
   在微信「微助教服务号」右键点签到通知 → 点「签到(C)」→ 确认授权。  
   没有这一步，程序无法取得有效凭证，取号会一直失败。  
   之后凭证失效时程序会自动重新取号；若自动取号长期失败，重做一次上述手动授权即可恢复。
2. **新的授权会使此前的 openid 失效。**  
   凭证续期仅在检测到 HTTP 401 时执行，不会按固定周期重新获取。  
   签到模块通过退出码 2 通知主循环，而非自行周期性刷新。
3. **`active_signs` 返回 200 不能作为凭证有效的依据。**  
   凭证过期与当前无签到任务均返回 200，仅无效或被顶替的凭证返回 401。  
   签到页 SSR 亦不校验凭证，仅回显 `session.openId`。因此不存在低成本的凭证校验方式，  
   只能通过实际提交验证。

---

## 版本适配

`tools/hook_a8key.js` 中的 RVA 与 WMPF 版本严格绑定（内置 **25799**）。微信升级后需重新扫描：

```bash
python tools/pe_scan.py "<路径>\flue.dll" \
  --kw "A8KeyNavigationThrottle::CheckURL" "open.weixin.qq.com" \
  --json a8key_scan.json
```

`.pdata` 提供的函数边界较为准确，优于依据 `0xCC` 填充对齐进行推断。  
`OnLoadStart` 偏移可使用 WMPFDebugger 的 `--auto-detect` 参数获取。

---

## 相关项目

- **Android root 版**：[qyr0721/weizhujiao-autosign-android](https://github.com/qyr0721/weizhujiao-autosign-android)（MIT）  
  以 Magisk / KernelSU 模块形式实现，通过 root 静默授权并读取 Cookie 数据库获取凭证。  
  签到接口契约与本项目一致。

---

## 许可与免责

- 本项目仅供学习与技术研究使用（浏览器调试协议、导航拦截机制、PE 静态分析）。
- 本项目通过修改微信运行时内存打通调试通道，属于绕过客户端自身限制的行为。  
  使用者应自行承担相应风险，该行为可能违反微信软件许可协议并存在账号风险。
- 微信安装包由腾讯发布，此处仅为便于版本锁定而提供，版权与许可归腾讯所有；  
  建议优先从 [pc.weixin.qq.com](https://pc.weixin.qq.com/) 获取。
- `patches/proxy-mux.ts` 为 WMPFDebugger（GPLv2）的衍生修改，因此本仓库整体采用  
  **GPL-2.0-only**；其中 `src/third-party` 的版权归腾讯所有。
- 上游项目与贡献者：**evi0s/WMPFDebugger** 及其全部贡献者。
- 请勿将本项目用于违反所在学校或机构规定的用途。
