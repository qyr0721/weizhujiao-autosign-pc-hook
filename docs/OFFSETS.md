# 偏移：从哪来、为什么会有内置库、新版本怎么补

本文只讲**签到主流程**的那套 RVA（打开调试通道用的 3 个 hook + 结构体偏移），
不涉及 a8key 观测钩子（那是另一套，见 README「版本适配」）。

---

## 1. 这些数字是干什么的

WMPFDebugger 往 `flue.dll` 里打两处 patch，靠的是 flue.dll 里 **固定 RVA**：

| 键 | 作用 |
|---|---|
| `LoadStartHookOffset` | `AppletIndexContainer::OnLoadStart`，把小程序拉成调试态（`scene=1101`、调试服务端改成 `ws://localhost:9421`） |
| `CDPFilterHookOffset` | `SendToClientFilter`，短路 CDP 消息过滤器，否则调试消息被运行时丢掉 |
| `CastToJsonHookOffset` | 结构体转 JSON 的公共函数，hook 里用来回读 config |
| `MiniAppConfigStructOffsets` | 上面那个 config 结构体的字段偏移（启动配置、远程调试配置、scene、ws 地址字符串、调试模式标志） |

RVA 与 `flue.dll` 的具体构建一对一绑定。微信换 WMPF 版本 → 换了 dll → 这些数字全变。
**打错位置的后果不是报错，而是 frida 往错地址写内存 → `EXCEPTION_ACCESS_VIOLATION`**，
所以任何「差不多就行」的做法都不能接受。

---

## 2. 三条拿到偏移的路

| 来源 | 怎么来 | 可靠性 | 需要什么 |
|---|---|---|---|
| `--auto-detect` 扫描 | WMPFDebugger 内置的静态特征扫描 | 特征没被改动时可靠 | 微信在运行，约 10 秒 |
| 内置偏移库（`known_offsets.py`） | 已确认过的同版本数值，随程序分发 | 与来源同等（人工确认过） | 什么都不需要，纯写盘 |
| `--import` 导入 json | 上游 PR / 别人给的文件 / IDA 人工分析 | 取决于文件本身 | 一份 `addresses.<ver>.json` |

`offsets.py` 的顺序是 **内置库 → 扫描**，不是反过来。原因很实际：
扫描会失效（微信改特征、换编译器），而写盘永远不会失效。
「扫不出来」只说明拿不到新数字，不说明已知数字不能用。

---

## 3. 内置库长什么样

`known_offsets.py` 里的 `BUILTIN` 就是一个版本号到配置的字典，
**内容与上游 `frida/config/win32/addresses.<ver>.json` 完全一致**：

```python
BUILTIN = {
    "26134": {
        "Version": 26134,
        "LoadStartHookOffset": "0x2DEDC70",
        "CDPFilterHookOffset": "0x39D4E80",
        "CastToJsonHookOffset": "0xBB418E0",
        "MiniAppConfigStructOffsets": {
            "LaunchConfigOffsets": [64, 1616, 8],
            "RemoteDebugConfigOffsets": [1544, 16],
            "SceneOffset": 480,
            "WebSocketURLStringOffset": 648,
            "RemoteDebugModeOffset": 748,
        },
    },
}
```

刻意不做字段转换：中间多一层映射，就有和上游漂移的可能，而漂移的代价是崩进程。

落盘时写成 `<WMPFDebugger>/frida/config/win32/addresses.<当前版本>.json`，
旁边一并写 `.meta.json`（记录版本、`flue.dll` 指纹、来源），
来源写 `builtin`。之后同一份文件再被判失效，程序**不会**拿它反复重写，
而是转去扫描——否则就是死循环。

---

## 4. 新版本怎么补

### 4.1 先看有没有

```bash
python offsets.py --check          # 当前版本 / 指纹 / 配置来源 / 内置库有没有
python offsets.py --list-builtin   # 内置库覆盖了哪些版本
```

`--check` 里那行 `内置偏移 : 有（WMPF 26134，可直接 --seed 写入）` 就是命中。

### 4.2 有内置、但机器上还没写

正常情况下程序启动会自己写（部署、启动自检、运行时自愈三处都会走这条路）。
要手动落地：

```bash
python offsets.py --seed
```

不需要微信运行，不需要扫描，写完立刻可用。

### 4.3 手上有一份新版本的 json

```bash
python offsets.py --import addresses.26140.json
```

- 文件里的 `Version` 必须和本机 WMPF 版本一致，否则默认拒绝（防错位）；
- 结构校验会先跑一遍：少字段、写成十进制、数组长度不对都会被拦下；
- 确认过「这份就是给本机的」才加 `--force`。

### 4.4 什么都没有，只能自己搞一份

1. 先试 `python offsets.py --detect`，看上游扫描能不能扫出来；
   能扫出来就直接被写进配置，不用管内置库。
2. 扫不出来就人工定位（需要 IDA/x64dbg 之类）：
   - 用 `python tools/pe_scan.py <flue.dll> --kw AppletIndexContainer::OnLoadStart` 找字符串/引用；
   - 用 `.pdata` 取函数边界（比按 `0xCC` 对齐猜可靠得多，见 DESIGN.md §4）；
   - `CDPFilterHookOffset` 对应 `SendToClientFilter`，`CastToJsonHookOffset` 对应结构体转 JSON 的函数；
   - 结构体偏移要靠调试器在 `OnLoadStart` 断点处对照实际内存（`scene`、ws 地址字符串、`remoteDebugMode`）。
3. 得到数值后，用 `--import` 先验证能不能跑通（跑不通就丢掉，不影响已有配置）。
4. 验证通过再写进 `known_offsets.py`，按第 3 节的格式加一项。

### 4.5 加进内置库前自检

```bash
python -c "import known_offsets as K; print(K.validate(K.get('26140')))"   # 应为 (True, 'ok')
python test/offsets_test.py                                                 # 全套偏移测试
```

`validate` 只查结构和取值范围（0x 十六进制、RVA 在 32 位内、指针链长度 3/2），
**不判断 RVA 是否真的指向那个函数**——那件事只能靠真机跑通来证明（见 TESTING.md L1/L2）。

---

## 5. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `内置偏移库没有 WMPF xxxxx（现有：26134）` | 版本不在库里 | `--import` 自己的 json，或 `--seed --force`（确认过再上） |
| `--seed --force` 后小程序起不来 / 微信崩溃 | 版本不符，RVA 错位 | 删掉 `addresses.<ver>.json` + `.meta.json`，重开小程序，改用 `--detect` |
| 面板显示「已应用内置偏移」但通道还是不通 | 代理是旧的（还带着旧偏移）在跑 | 结束 node 进程后重启程序；运行中更新过偏移的话，重新打开一次小程序 |
| 每次启动都提示偏移失效 | 该版本内置值不对 | 换成 `--import` 的正确数值；内置那份被判失效后不会再被自动重写 |
| `--check` 显示 `配置来源: auto-detect` 且状态 stale | 扫描结果也过时了（同版本 dll 被热替换） | `--detect` 重扫；成功后会重新写指纹 |

---

## 6. 为什么不用「按版本号自动猜」

结构体偏移（`SceneOffset` 480→456→…）在同一大版本里常常连续变化，
但 hook RVA 是按编译产物随机分布的，没有任何单调规律。
用「相邻版本 ±N」去猜，等于随机写内存。**宁可让用户看到「没有这个版本的偏移」，
也不要写一个看起来像对的数字。**
