#!/system/bin/sh
# 微助教自动签到 · 守护进程：flow 挂了就拉起
MODDIR=${1:-/data/adb/modules/tm-autosign}
LOG=/data/local/tmp/tm_supervisor.log

lock=/data/local/tmp/tm_supervisor.lock
if [ -f "$lock/pid" ] && kill -0 "$(cat "$lock/pid" 2>/dev/null)" 2>/dev/null; then
    exit 0
fi
rm -rf "$lock"; mkdir -p "$lock" 2>/dev/null || exit 0
echo $$ > "$lock/pid"
trap 'rm -rf "$lock"' EXIT INT TERM

echo "$(date '+%m-%d %H:%M:%S') supervisor start" >> "$LOG"

while true; do
    if ! pgrep -f "scripts/tm_flow.sh" >/dev/null 2>&1; then
        echo "$(date '+%m-%d %H:%M:%S') flow 不在，拉起" >> "$LOG"
        setsid sh "$MODDIR/scripts/tm_flow.sh" "$MODDIR" >/dev/null 2>&1 &
        sleep 5
    fi
    # 面板挂了也顺手拉起
    if ! pgrep -f "httpd -p 127.0.0.1:8080" >/dev/null 2>&1; then
        /data/adb/ksu/bin/busybox httpd -p 127.0.0.1:8080 -h "$MODDIR/web" >/dev/null 2>&1
    fi
    sleep 30
done
