#!/system/bin/sh
# 卸载时清理
pkill -f tm_flow.sh >/dev/null 2>&1
pkill -f "httpd -p 127.0.0.1:8080" >/dev/null 2>&1
rm -rf /data/local/tmp/tm_flow.lock
rm -f /data/local/tmp/tm_oid.txt
exit 0
