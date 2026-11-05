# 测试说明

离线能测的都测了；需要真微信的部分单独列出，不外推。

```bash
python test/run_all.py       # 7 套一起跑
```

```
mux        SKIP   —      CDP 代理多客户端复用（仓库未附带 test/mux_test.ts，且需 tsx）
classify   PASS  12 条   授权触发判定 + URL 构造
signcore   PASS  13 条   签到核心行为契约
ui         PASS  61 条   面板布局对齐 / 日志→状态映射 / ANSI 定点重绘
installer  PASS  19 条   安装器：zip slip 防护 / 解码 / 失败识别
loop       PASS  26 条   主循环时序 / 异步不阻塞 / 残留号拒收
offsets    PASS  57 条   偏移版本自适应 / 自动探测 / 失效兜底 / 版本不符不阻断
6/7 套通过，1 套跳过（188 条断言）
```

缺少可选依赖的套标成 SKIP 而不是 FAIL，避免把「仓库没带这个文件」误报成回归。

---

## 1. 各套分别测什么

### mux（`test/mux_test.ts`）— 需要 `tsx`

对应 `patches/proxy-mux.ts`，验证「应答单播 / 事件广播」：

- 上游 `id` 去重（A=1、B=2 不串）
- 各自收到自己的回复且 `id` 还原
- 断线客户端的悬挂回复不会串给他人

> 原代理把上行结果广播给所有客户端、请求靠自增 `id` 配对，
> 两个客户端各自从 `id=1` 计数必然串扰。修好后 DevTools 与脚本可以并存。

### classify（`test/classify_test.py`）— 纯 python

对应 `tools/trigger_oauth.py` 的判定函数，决定「这次导航到底被没被接管」：

| 判定 | 含义 |
|---|---|
| `INTERCEPTED` | 真授权页（有内容、有表单/跳转） |
| `NATIVE_WINDOW` | 被原生接管成新窗口 |
| `STUB` | 空壳页（1 KB 左右，无内容） |
| `BLOCKED` | 明确报错 |

`STUB` 与 `INTERCEPTED` 必须严格区分，否则会把「没被接管」误判成「可以自动化」。

### signcore（`test/signcore_test.py`）— 纯 python

对应 `signcore.py`，用假 `req` 驱动真循环：

- 结构上无 `in_hours` / `class_hours`（时段闸门已移除），也无 `idle_poll` / `idle_after`（空闲退避已移除）
- 轮询间隔恒为 `poll`：无论是否有进行中的签到，实测间隔一致
- 提交体 `{courseId, signId}` 与契约一致；同一 `signId` 只提交一次
- `repeat sign in` / `errorCode 305` 视为已签
- `401` → 返回码 2 + 写 `openid.stale`（触发续号）
- 无号时 `--once` 返回 3

### ui（`test/ui_test.py`）— 纯 python

对应 `wizard.py` 的面板层。**这一套抓出过一个上线即崩的 bug**：
`Term.c(text, None)` 在非 plain 模式下拼 `None + str`，真实终端第一次渲染就抛异常，
而 plain 模式因为短路返回原文本，测不到。所以现在两条渲染路径都要测。

- 宽度计算：中文 2 列、混排、截断后仍等于目标宽度（截断标记用 ASCII `...`，
  因为 `…` 的东亚宽度有歧义，不同终端 1/2 列不一致会把边框挤歪）
- 标题框 5 行同宽（`TOTAL=60`），超长动作/签到文本不影响对齐
- 日志 → 面板映射：值守 / 断连 / 取号 / 换号 / 签到成功 / 幂等 / 失败 / 401
- 通道未通时报 401，必须说「等待小程序连接（连上后自动换号）」，不能假装在换号
- ANSI 定点重绘：内容没变时一个字符都不输出；只变动作行时不重画标题

### offsets（`test/offsets_test.py`）— 纯 python

对应 `offsets.py`，用临时目录造假的 WMPFDebugger / `flue.dll`，完全不碰真实微信：

- 新版本缺配置、配置损坏、空配置、**同版本号下 dll 被替换**（指纹变化）都判为需更新
- `detected hook offsets:` 解析、原子写回、元数据补齐、成功后不重复探测
- 连续失败达上限退回 `--auto-detect` 兜底，`--detect` 强制重试可突破
- 只认明确的偏移错误，`no-reply` / WS 抖动 / 小程序连接事件不误判
   （误判会白白重启代理、把小程序杀掉）
- `preflight` 在 WMPF 版本不是 25799 时仍判定「环境就绪」，也不再提示降级微信

### installer / loop — 纯 python

- installer：zip slip 防护、GBK/UTF-8 解码、下载失败不被当成成功
- loop：取号成功后不空等一个轮询周期、`oid_state` 走线程池、导航前残留旧号拒收

---

## 2. 真机验证的顺序（别跳层）

| 层 | 看什么 | 通过标准 |
|---|---|---|
| L0 环境 | 微信进程 / WMPF 版本 / WMPFDebugger / Node | `wxsign.exe --check` 全绿 |
| L1 通道 | `miniapp client connected` | 9421 + 62000 都在监听 |
| L2 附着 | `Target.getTargets` 能列出 tab | 看到签到页或任意 H5 |
| L3 取号 | `Page.navigate` 后 `ERR_ABORTED` + 新 target | 新 target URL 含不同的 `openid=` |
| L4 签到 | `POST student-sign-in` 响应含 `signRank` | 学生端显示已签到 |

---

## 3. 离线测不到的部分

这些必须真机，不能靠桩件模拟：

- CSP 是否挡住 main world 注入
- `sessionId` 事件是否回流
- 真实 401 的触发时机（`200` 不可信，见 `DESIGN.md` §5）
- a8key 限流的实际阈值与冷却窗口
- 不同微信版本的 RVA 偏移

---

## 4. 已知的假警报

曾有一次测试「失败」是因为跑在凌晨，被 `classHours=[7,23)` 正确拦住——
是测试环境问题，不是产品问题。该时段闸门现已移除，轮询间隔固定为 `poll`，不受本地时间影响。
