# 微助教自动签到

面向 Android（Magisk / KernelSU）的微助教课堂签到自动化模块：**自动取号、自动轮询、自动提交、本地网页面板**。

---

## 特性

| 能力 | 说明 |
|---|---|
| 自动取号 | 无需复制链接，root 拉起微信 OAuth 静默授权，从 Cookie 库读取新 `openid` |
| 自动检测 | 5 秒轮询 `/student/active_signs`，一次请求覆盖所有课程 |
| 自动提交 | 命中即提交，5 秒内完成；重复提交自动识别为幂等 |
| 多课程 | 接口返回哪门课就签哪门，无需配置课程号 |
| 锁屏可用 | 熄屏时自动唤醒 → 续号 → 睡回去；wakelock 防内核挂起 |
| 本地面板 | `127.0.0.1:8080`，含手动续号、暂停/启用开关、实时日志 |
| 通知 | 签到成功/失败/已签到/取号失败均推送系统通知 |
| 自愈 | 进程被杀 30 秒内拉起，开机自启 |

---

## 快速开始

1. **刷入模块**：把 `module/` 打包成 zip，在 Magisk / KernelSU 里安装（或直接用 Release 里的 zip）。
2. **填配置**：
   ```bash
   # 手机 root shell
   cp /data/adb/modules/tm-autosign/config.example.conf \
      /data/adb/modules/tm-autosign/config.conf
   vi /data/adb/modules/tm-autosign/config.conf   # 填教室经纬度
   ```
3. **重启手机**，然后浏览器打开 `http://127.0.0.1:8080` 确认状态为「轮询中」。

> 首次启动会自动打开一次微信授权页（约 8 秒）完成取号，不需要任何人工操作。
**注意：在模块进行取号操作时，请不要点击屏幕，等待其自动完成取号后关闭，否则取号失败。**

---

## 工作原理

微助教的 H5 会话凭证是一个叫 `openid` 的 token，**每次进页面都会重新生成，约 15 分钟失效**。所以"抓一次永久用"不可行，必须周期性重新走一次微信授权。

```
每 15 秒（或收到 401）
   ├─ root 拉起微信内部 WebViewUI，塞入公众号的 OAuth 授权地址
   ├─ 微信静默授权（snsapi_base）→ 服务端签发新 openid → 停在签到页
   ├─ 8 秒后关闭页面（关闭动作会立即触发 Cookie 落盘）
   ├─ 从 XWeb 的 Cookie 库读出 session（base64 的 {"openId":"..."}）
   └─ 逐个验证候选，留下能返回 200 的

每 5 秒
   ├─ GET  /wechat-api/v1/class-attendance/student/active_signs
   └─ 命中 → POST /wechat-api/v1/class-attendance/student-sign-in
             {"courseId":…, "signId":…, "lat":…, "lon":…}
```

### 反解出来的接口

| 用途 | 请求 |
|---|---|
| 进行中的签到 | `GET https://www.teachermate.com.cn/wechat-api/v1/class-attendance/student/active_signs` |
| 课程列表 | `GET …/student/courses` |
| 提交签到 | `POST …/student-sign-in` |
| 签到历史 | `GET …/student/history?courseId=…` |

鉴权只有一个请求头：`openId: <32 位 hex>`。

授权入口（`appid` 与 `redirect_uri` 来自公众号菜单）：

```
https://open.weixin.qq.com/connect/oauth2/authorize
  ?appid=wxa153455f3ef1d9f9
  &redirect_uri=https%3A%2F%2Fv18.teachermate.cn%2Fapi%2Fv1%2Fwechat%2Fr%3Fm%3Ds_signin
  &response_type=code&scope=snsapi_base&state=&connect_redirect=1#wechat_redirect
```

---

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `LAT` / `LON` | 无 | 教室坐标，仅 `isGPS>0` 时随报文提交。**也可以直接在面板「坐标设置」里改**，保存后 5 秒内生效，无需重启 |
| `JITTER_DEG` | `0.00003` | 坐标随机抖动（约 ±3 米） |
| `INTERVAL` | `5` | 轮询间隔（秒） |
| `REFRESH_EVERY` | `15` | 主动续号周期（秒） |
| `WK_START` / `WK_END` | `7` / `23` | 持有 wakelock 的时段 |

---

## 面板

`http://127.0.0.1:8080`（KernelSU 模块页也可打开）

- 状态、当前 openid、签到情况、最近结果、提交坐标
- **手动续号**：立刻重新取号，不等 11 分钟
- **暂停 / 启用**：暂停时释放 wakelock、停止请求
- 最近 14 行日志，3 秒刷新

按钮走 CGI（`web/cgi-bin/action`），只往命令文件写一个词，由主循环消费——不会打断正在进行的续号。

---

## 已知限制

1. **同时进行的两场签到只处理第一场**。先后发起没问题（一场结束再一场），并列发起会漏第二场。
2. **锁屏续号会点亮屏幕约 8 秒**。这是 WebView 必须在前台加载的代价。
3. **有安全锁（PIN/图案）时无法续号**。`wm dismiss-keyguard` 只在无安全锁时有效。请在开发者选项中开启**不使用锁屏**，并在停止使用后关闭选项以保证设备安全性。
4. **服务端改版会失效**。授权地址、接口路径、字段名任一变化都需要跟进（[docs/技术复盘.md](docs/技术复盘.md) 记录了如何重新定位）。

---

## 排错

```bash
# 看日志
tail -f /data/local/tmp/tm_flow.log

# 手动重启
su -c "pkill -9 -f tm_flow.sh"     # 守护进程 30 秒内会拉起

# 检查通知是否发出（必须以 shell 身份，root 发的会被系统丢弃）
su 2000 -c "cmd notification post -t test t 'hello'"

# 检查面板
curl -s http://127.0.0.1:8080/status.js
```

| 现象 | 排查方向 |
|---|---|
| 一直「续号失败」 | 微信是否被杀、授权地址是否失效、是否有安全锁 |
| 收到通知但没声音 | 手机静音/震动模式；通知通道音量 |
| 检测不到签到 | openid 是否过期（看面板 openid 与日志 401） |

---

## 免责声明

- 本项目仅用于**个人学习与自动化研究**。
- 自动签到可能违反你所在学校的考勤规定，被认定后可能面临处分；教师端可见签到时间排名、~~IP、坐标与距离~~。
- 请只在**本人确实在场**时使用；`LAT/LON` 填假坐标属于伪造位置(**虽然最终证明服务器对位置坐标没有任何限制**)。
- 使用 root 与 Xposed 类工具存在被微信风控的风险，后果自负。
- 作者不对因使用本工具造成的任何后果负责。

## 致谢

- 早期思路参考 [recolic/micro-teaching-assistant-fucker](https://github.com/recolic/micro-teaching-assistant-fucker)（2019 年的旧接口实现）
- KernelSU 模块 WebUI 规范：<https://kernelsu.org/zh_TW/guide/module-webui.html>

## License

MIT
