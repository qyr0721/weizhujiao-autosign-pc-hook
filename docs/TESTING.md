# 测试说明

离线能测的都测了；需要真微信的部分单独列出，不外推。

```bash
python test/run_all.py       # 3 套一起跑
```

```
mux        PASS   8 条   CDP 代理多客户端复用
classify   PASS  12 条   授权触发判定 + URL 构造
signcore   PASS  14 条   签到核心行为契约
3/3 套通过（34 条断言）
```

---

## 1. 三套各测什么

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

- 结构上无 `in_hours` / `class_hours`（时段闸门已移除）
- 起步用 `poll`，连续空窗后降到 `idle_poll`，一有签到立刻恢复 `poll`
- 提交体 `{courseId, signId}` 与契约一致；同一 `signId` 只提交一次
- `repeat sign in` / `errorCode 305` 视为已签
- `401` → 返回码 2 + 写 `openid.stale`（触发续号）
- 无号时 `--once` 返回 3

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
是测试环境问题，不是产品问题。该闸门现已移除（改为空窗退避，不看时钟）。
