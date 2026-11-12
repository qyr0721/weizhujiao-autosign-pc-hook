# 微助教自动签到（PC / 微信内置浏览器）

<p align="center">
  <img src="docs/img/logo.png" width="96" alt="wxsign logo">
</p>

本项目在 Windows 平台实现微助教课堂签到的自动化：自动获取凭证、轮询签到任务并提交结果。  
全过程无需人工点击、屏幕截图或鼠标模拟；凭证失效时自动重新获取并继续运行。

**by Hikari** · [github.com/qyr0721/weizhujiao-autosign-pc-hook](https://github.com/qyr0721/weizhujiao-autosign-pc-hook)

---

## 教程

### 我用deepseek生成了一个超级详细，3岁小孩也能懂的教程：[使用教程](https://home.mowan.qzz.io/posts/weizhujiao-autosign-pc-hook/)

---

## 功能

| 能力     | 说明                                                        |
| ------ | --------------------------------------------------------- |
| 自动签到   | 以 5 秒间隔轮询 `active_signs`，发现进行中的签到立即提交                     |
| 自动续期凭证 | openid 失效（HTTP 401）时自动重新获取，全程在后台完成                        |
| 幂等提交   | `repeat sign in` / `已经签到` / `errorCode 305` 均判定为已签到，不重复提交 |
| 多课程    | 依据接口返回内容处理，无需预先配置课程标识                                     |
| GPS 签到 | `isGPS` 为真时按 `jitter_deg` 对经纬度施加抖动后提交                     |
| 进程保活   | 签到进程异常退出后由主循环重新获取凭证并拉起                                    |
| 版本自适应 | 微信更新换了 WMPF 版本后，自动重探或直接用内置偏移写回，无需降级或手工重扫          |

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

机制细节与实测数据见 [docs/DESIGN.md](docs/DESIGN.md)。  
调试通道基于 [evi0s/WMPFDebugger](https://github.com/evi0s/WMPFDebugger)（通过 frida hook `flue.dll`），  
本项目为其下游应用，并附带一个多客户端复用补丁。

---

## 工作流程

```
  双击 wxsign-portable.exe / start.bat（源码方式）
    │
    ├─ 偏移自检        当前 WMPF 版本的配置缺失/指纹不符 → 自动探测并写回
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

---

## 微信版本与偏移自适应

| 项目 | 值 |
| --- | --- |
| 内置偏移库 | WMPF **26134**（可直接落地，无需扫描；见 `known_offsets.py`） |
| a8key 观测基准 | 微信 PC **4.1.15.56** / WMPF **25799**（仅调试观测用） |
| 实际支持 | 内置命中 → 直接写配置；未命中 → 由 WMPFDebugger `--auto-detect` 探测当前 `flue.dll` 的 RVA，按版本写回 |

自动适配流程（`offsets.py`）：

1. 读取本机当前 WMPF 版本，计算 `flue.dll` 的轻量指纹（size + mtime，不读 237MB 算 hash）；
2. 检查 `WMPFDebugger/frida/config/win32/addresses.<版本>.json`：**缺失 / 配置损坏 /
   指纹不符 / 上次留下 `*.stale` 标记** → 判定需要更新；
3. 先查**内置偏移库**：有该版本就直接写盘（离线、秒级、微信不用开）；
   没有才跑 `node .../tsx src/index.ts --auto-detect`（限时 90 秒），解析输出里的
   `detected hook offsets: {...}`；
4. 原子写回配置，并在 sidecar `.meta.json` 里记版本、指纹、来源（builtin / auto-detect /
   import / legacy）与时间（不改 WMPFDebugger 自己的配置格式）；
5. 运行中若代理日志出现明确的偏移错误，主循环下一轮会「停代理 → 修复 → 按新偏移重启」。
   通道抖动、`no-reply` 之类**不会**被误判，不会为了无关错误把小程序杀掉。

连续失败 2 次后停止反复重探，退回 WMPFDebugger 自带的 `--auto-detect` 临时模式
（每次启动多等约 10 秒），程序仍可用。运行中重探需要停代理，9421 会短暂断开，
**完成后请重新打开一次小程序**；启动时重探则完全无感。

### 扫描不出偏移时（内置偏移库）

`--auto-detect` 是静态特征扫描，微信改掉特征后它就再也命中不了——这时**不是没有偏移可用**，
只是拿不到新的。已知版本的 RVA 可以直接写，项目按版本内置了一批：

```bash
python offsets.py --check            # 看当前版本 / 指纹 / 配置来源 / 内置库有没有
python offsets.py --list-builtin     # 内置库覆盖了哪些版本
python offsets.py --seed             # 用内置值直接写入（不扫描、不需要微信）
python offsets.py --import f.json    # 导入外部 addresses json（上游 PR / 别人给的）
```

内置值落盘后来源记为 `builtin`。若这份值之后仍被判定失效，程序**不会**反复重写它，
而是转去扫描——避免陷入「写同一份 → 报错 → 再写」的死循环。版本不符时默认拒绝写入
（打错位置的 RVA 会崩进程），确认过才加 `--force`。

手工检查与强制重探：

```bash
python offsets.py --check            # 只看当前版本 / 指纹 / 配置状态
python offsets.py --detect           # 强制重扫主偏移（跳过内置库、忽略失败次数）
python offsets.py --rescan-a8key     # 更新调试观测用的 a8key RVA，见「版本适配」
```

偏移从哪来、怎么给新版本补一份，见 [docs/OFFSETS.md](docs/OFFSETS.md)。

源码运行与 exe 都可用 `--no-auto-offsets` 关闭自动适配。


---

## 部署

### 运行依赖

程序已内嵌 Python 运行时，**无需安装 Python**。但其 hook 依赖 WMPFDebugger（基于 Node.js），  
因此仍需以下组件：

| 组件                         | 便携版            | 集成版     |
| -------------------------- | -------------- | ------- |
| Node.js 22+                | 首次运行时从网络获取     | **已内置** |
| WMPFDebugger + tsx + frida | 首次运行时从网络获取     | **已内置** |
| 偏移配置                       | 内置库命中则离线直写，否则按当前版本自动探测生成 | 同左（离线可用，不依赖扫描） |

### 版本选择

| 版本      | 文件                      | 体积                    | 联网        | 适用             |
| ------- | ----------------------- | --------------------- | --------- | -------------- |
| **集成版** | `wxsign-integrated.zip` | 约 111 MB（解压后约 300 MB） | **不需要** | 网络受限&希望解压即用的环境 |
| **便携版** | `wxsign-portable.exe`   | 约 11 MB               | 需要   | 可接受首次下载的环境     |

两者的签到逻辑完全一致，区别仅在运行环境的获取方式。

**便携版**首次启动时检测缺失组件并自动部署，之后直接使用已部署的内容，  
不会重复下载。部署过程如下：

```
Node 运行时      下载便携版压缩包并解压至 runtime\node，不写入系统目录
WMPFDebugger     使用 git clone；未检测到 git 时改用 codeload 压缩包
依赖 tsx / frida 执行 npm install（frida 预编译绑定约 130 MB）
偏移配置         先查内置偏移库（有则直接写入，离线秒完成）；没有再跑一次 --auto-detect
                 结果写入 frida\config\win32\addresses.<ver>.json
```

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

**便携版**：双击 `wxsign-portable.exe`，程序自动开始值守：

- 标题框下是**环境行**（微信 / WMPF 版本、自检状态，绿点=正常）
- `▌` 开头的**动作行**实时显示当前在做什么（注入 hook / 等待小程序 / 取号 / 监视中）
- **签到行**显示最近一次签到结果（✓ 成功 / ✕ 失败）
- 面板底部：**按 T 打开在线教程**（[网页版教程](https://home.mowan.qzz.io/posts/weizhujiao-autosign-pc-hook/)），`Ctrl+C` 退出

面板就绪后按教程打开小程序建立通道，之后老师一发起签到，约 5 秒内自动提交。

**源码运行**

```bash
git clone https://github.com/qyr0721/weizhujiao-autosign-pc-hook.git
cd weizhujiao-autosign-pc-hook
pip install websockets

python wizard.py --check  # 环境自检
python wizard.py          # 面板式常驻（同 exe）
python run.py             # 纯日志常驻（无面板）
```


### 配置文件（签到坐标等）

配置文件名固定为 `config.json`，位置在**程序所在目录的 `signer` 子目录**：

**首次运行程序会自动生成配置文件**，只需把 `lat` / `lon` 改成自己的坐标即可。
也可以手动创建，或在程序目录直接放一个 `config.json`（会被自动识别）。

| 字段 | 说明 |
|---|---|
| `lat` / `lon` | 签到上报的经纬度。**只有 GPS 签到会用到**；留 `null` 则不发送坐标 |
| `jitter_deg` | 坐标抖动幅度（度），默认 `0.00003` ≈ ±1.6 米，避免每次提交完全相同 |
| `poll` | 轮询间隔（秒），默认 5 |

**如何获取坐标（实际上系统不会验证坐标数据，即使上报为"null"也可完成位置签到）**

打开[高德坐标拾取器](https://gaode.com/ssr/coordinate-picker)，复制显示的经纬度，依次填入 `lat`（纬度）和 `lon`（经度），保存后**重启程序**（配置只在启动时读取一次）

> 坐标系用 **GCJ-02**（腾讯、高德都是）。百度地图拾取器给的是 BD-09，差几百米，需先转换。

### 调试通道说明

WMPF 调试通道仅在**小程序启动瞬间**建立（hook 位于 `AppletIndexContainer::OnLoadStart`），  
且断开后不会自动重连。

---

## 构建与发布

```bash
pip install pyinstaller websockets
python build.py portable       # dist/wxsign-portable.exe（单文件，首次运行联网部署）
python build.py integrated     # dist/wxsign-integrated.zip（内置 Node/WMPFDebugger，离线）
python build.py all

python tools/verify_bundle.py dist/wxsign-portable.exe   # 确认 exe 里装的是当前源码
```

集成版需要本机已有完整 Node 便携目录和 `npm install` 过的 WMPFDebugger，
可用 `--node-dir` / `--wmpf-dir` 指定；缺失组件不会硬塞进包，构建日志会写明。

推送 `v*` tag 会触发 `.github/workflows/release.yml`：在 Windows runner 上装依赖、
跑离线测试、构建便携版、校验 PYZ 内容，最后用仓库自带的 `GITHUB_TOKEN` 创建
Release 并上传 exe。集成版体积大且依赖本机组件，需本地构建后手动补传。

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

两套 RVA，别混在一起：

| 用途 | 落点 | 版本变化后 |
| --- | --- | --- |
| 签到主流程（打通调试通道） | `WMPFDebugger/frida/config/win32/addresses.<ver>.json` | **自动**：内置库命中直接写，未命中由 `offsets.py` 调 `--auto-detect` 重探写回 |
| 调试观测（看 a8key 导航拦截） | `a8key_hooks.<ver>.json` | 按需一条命令重扫；不影响签到 |

`OnLoadStart` / `SendToClientFilter` 的偏移在程序启动前自动就绪：先查内置库
（`known_offsets.py`），没有才走 WMPFDebugger 的 `--auto-detect`。也可以手动
`python offsets.py --seed`（写内置值）或 `python offsets.py --detect`（强制扫描）。

a8key 观测钩子不参与签到，只供 `tools/probe_a8key.py` 观察 OAuth 被哪一层接管。
它内置的是 25799 的 RVA；换版本后要生成新表才准。因为要静态分析 237MB 的
`flue.dll`（可能几分钟），这一步**刻意不放进启动流程**：

```bash
python offsets.py --rescan-a8key
# 生成 a8key_hooks.<当前版本>.json，probe_a8key.py 会自动按版本加载它

# 等价的底层命令（一般不用手输）：
python tools/pe_scan.py "<路径>\flue.dll" \
  --kw "A8KeyNavigationThrottle::CheckURL" "open.weixin.qq.com" \
  --json a8key_hooks.<ver>.json
```

`.pdata` 提供的函数边界较为准确，优于依据 `0xCC` 填充对齐进行推断。  
`tools/hook_a8key.js` 只是 frida 脚本模板，配置由 `probe_a8key.py` 注入 `@@CONFIG@@`，
不需要手工改里面的 RVA。

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
- `patches/proxy-mux.ts` 为 WMPFDebugger（GPLv2）的衍生修改，因此本仓库整体采用  
  **GPL-2.0-only**；其中 `src/third-party` 的版权归腾讯所有。
- 请勿将本项目用于违反所在学校或机构规定的用途。
- 本库只能作为学习用途，造成的任何问题与本库开发者无关，如侵犯到你的权益，请联系删除

---

## 致谢
- 上游项目与贡献者：[evi0s/WMPFDebugger](https://github.com/evi0s/WMPFDebugger) 及其全部贡献者。
