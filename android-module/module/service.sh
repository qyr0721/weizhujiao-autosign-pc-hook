#!/system/bin/sh
# 微助教自动签到 · 开机服务
MODDIR=${0%/*}
BB=/data/adb/ksu/bin/busybox
[ -x "$BB" ] || BB=busybox

mkdir -p "$MODDIR/web" "$MODDIR/web/cgi-bin"
chmod 755 "$MODDIR/scripts/tm_flow.sh" "$MODDIR/scripts/tm_supervisor.sh" 2>/dev/null
chmod 755 "$MODDIR/web/cgi-bin/action" 2>/dev/null

# 本地监控面板（仅 127.0.0.1，外部不可达）
pkill -9 -f "httpd -p 127.0.0.1:8080" >/dev/null 2>&1
$BB httpd -p 127.0.0.1:8080 -h "$MODDIR/web" >/dev/null 2>&1

# 重启流程与守护（只匹配本模块的脚本，避免误伤其他模块）
pkill -9 -f "tm_supervisor.sh" >/dev/null 2>&1
pkill -9 -f "scripts/tm_flow.sh" >/dev/null 2>&1
rm -rf /data/local/tmp/tm_flow.lock
sleep 1

setsid sh "$MODDIR/scripts/tm_supervisor.sh" "$MODDIR" >/dev/null 2>&1 &

exit 0
