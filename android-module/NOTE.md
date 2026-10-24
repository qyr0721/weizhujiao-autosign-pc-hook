# 来源与说明

本目录内容来自 https://github.com/qyr0721/weizhujiao-autosign （MIT），**原样保留**，未做修改。

- 目标平台：**Android**（Magisk / KernelSU 模块，root 拉起微信 OAuth 静默授权 + 读 Cookie 库取号）
- 本项目根目录是 **Windows / PC 微信** 实现（WMPF CDP 取号 + `signer/sign_pc.py` 签到）
- 两者签到请求契约一致：`/student/active_signs`、`/student-sign-in`、幂等判定 `repeat sign in` / `errorCode 305`

保留它作为对照实现与设计参考：Android 侧走系统级 root，PC 侧走浏览器调试协议，取号路径不同、提交逻辑同源。
