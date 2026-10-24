#!/system/bin/sh
# KernelSU 模块「操作」按钮：确保面板在跑，然后打开它
MODDIR=${0%/*}
BB=/data/adb/ksu/bin/busybox
[ -x "$BB" ] || BB=busybox

if ! pgrep -f "httpd -p 127.0.0.1:8080" >/dev/null 2>&1; then
    $BB httpd -p 127.0.0.1:8080 -h "$MODDIR/web" >/dev/null 2>&1
    sleep 1
fi

# 面板数据若超过 2 分钟没更新，顺手把流程拉起来
if [ -f "$MODDIR/web/status.js" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$MODDIR/web/status.js" 2>/dev/null || echo 0) ))
    if [ "$age" -gt 120 ] && ! pgrep -f "scripts/tm_flow.sh" >/dev/null 2>&1; then
        setsid sh "$MODDIR/scripts/tm_flow.sh" "$MODDIR" >/dev/null 2>&1 &
    fi
fi

am start -a android.intent.action.VIEW -d "http://127.0.0.1:8080/" >/dev/null 2>&1
echo "监控面板：http://127.0.0.1:8080"
