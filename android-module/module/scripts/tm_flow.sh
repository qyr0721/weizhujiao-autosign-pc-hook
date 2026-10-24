#!/system/bin/sh
# 微助教全自动签到 · 主流程
# 用法：sh tm_flow.sh <MODDIR>
#
# 1) 用 root 拉起微信内部 WebViewUI，塞入公众号的 OAuth 授权地址
# 2) 微信静默授权 → 服务端发新 openid → 停在签到页
# 3) 从微信进程内存扫出 student/sign?openid=xxx，逐个验证，留下能返回 200 的
# 4) 每 15 秒轮询 active_signs，命中就 POST 提交
# 5) openid 到期（实测 15 分钟）或 401 时自动重来

MODDIR=${1:-/data/adb/modules/tm-autosign}
WEB="$MODDIR/web"
OAUTH='https://open.weixin.qq.com/connect/oauth2/authorize?appid=wxa153455f3ef1d9f9&redirect_uri=https%3A%2F%2Fv18.teachermate.cn%2Fapi%2Fv1%2Fwechat%2Fr%3Fm%3Ds_signin&response_type=code&scope=snsapi_base&state=&connect_redirect=1#wechat_redirect'

OID_FILE=/data/local/tmp/tm_oid.txt
EN_FILE=/data/local/tmp/tm_enabled      # 1=运行 0=暂停（面板开关）
CMD_FILE=/data/local/tmp/tm_cmd         # 面板按钮投递的命令
LOG=/data/local/tmp/tm_flow.log
UA='Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36 MicroMessenger/8.0.47'
REF='https://app.teachermate.com.cn/wechat-pro-ssr/student/sign'
B='https://www.teachermate.com.cn/wechat-api/v1/class-attendance'

# ---------------------------------------------------------------- 个人配置
# 坐标等私有参数放在 config.conf（不随仓库发布），首次使用复制 config.example.conf
CONF="$MODDIR/config.conf"
[ -f "$CONF" ] || CONF="$MODDIR/config.example.conf"
[ -f "$CONF" ] && . "$CONF"

# 坐标可随时从面板改，单独存一个文件，每轮重新读取
COORD_FILE="$MODDIR/coord.conf"
[ -f "$COORD_FILE" ] && . "$COORD_FILE"

LAT="${LAT:-}"
LON="${LON:-}"
JITTER_DEG="${JITTER_DEG:-0.00003}"   # 坐标抖动（度）：0.00003 ≈ 3 米
INTERVAL="${INTERVAL:-5}"             # 轮询间隔（秒）
REFRESH_EVERY="${REFRESH_EVERY:-660}" # 主动续号周期（秒）
WK_START="${WK_START:-7}"             # wakelock 生效时段（小时）
WK_END="${WK_END:-23}"                # 超过则释放，省电

log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"; }
# 通知必须以 shell(uid 2000) 身份发：uid 0 发出去会被系统静默丢弃。
# 消息先写文件，再由降权后的脚本读取，避免引号/JSON 破坏命令行。
notify() {
    printf '%s' "$1" > /data/local/tmp/tm_notify.txt 2>/dev/null
    chmod 644 /data/local/tmp/tm_notify.txt 2>/dev/null
    su 2000 -c "sh /data/local/tmp/tm_notify.sh" >/dev/null 2>&1
}


# 熄屏后内核会 suspend，sleep/setInterval 全被冻住，轮询会从 5 秒退化到几十秒。
# 用内核 wakelock 顶住 CPU（屏幕仍然熄灭，只是不让它睡死）。
hold_wake() {
    [ -w /sys/power/wake_lock ] || return 0
    grep -q tm_sign /sys/power/wake_lock 2>/dev/null && return 0
    echo tm_sign > /sys/power/wake_lock 2>/dev/null
}
release_wake() {
    [ -w /sys/power/wake_unlock ] || return 0
    grep -q tm_sign /sys/power/wake_lock 2>/dev/null || return 0
    echo tm_sign > /sys/power/wake_unlock 2>/dev/null
}
wake_in_window() {
    _h=$(date +%H)
    [ "$_h" -ge "$WK_START" ] && [ "$_h" -lt "$WK_END" ]
}

# 屏幕是否熄灭（注意：亮屏→熄灭会先进入 Dozing，再进入 Asleep，两个都要算）
screen_off() {
    dumpsys power 2>/dev/null | grep -qE "mWakefulness=(Asleep|Dozing)" && return 0
    dumpsys display 2>/dev/null | grep -q "mScreenState=OFF" && return 0
    return 1
}

# 锁屏/熄屏时 WebView 不会真正加载，必须先把屏幕唤醒。
# 返回 0 表示"是我们唤醒的"，续号结束后要负责再睡回去。
ensure_awake() {
    screen_off || return 1
    input keyevent 224 >/dev/null 2>&1      # KEYCODE_WAKEUP
    sleep 1
    wm dismiss-keyguard >/dev/null 2>&1     # 无安全锁时可直接划过锁屏
    sleep 1
    return 0
}

# 关掉残留的 WebView 页面：点左上角 ✕；点不掉再用 BACK
close_webview() {
    _n=0
    while [ $_n -lt 3 ]; do
        case "$(dumpsys window 2>/dev/null | grep mCurrentFocus | head -1)" in
            *WebViewUI*) input tap 62 226; sleep 2; _n=$((_n+1)) ;;
            *) return 0 ;;
        esac
    done
    case "$(dumpsys window 2>/dev/null | grep mCurrentFocus | head -1)" in
        *WebViewUI*) input keyevent 4; sleep 1 ;;
    esac
}

# 在基准坐标上做小幅随机偏移，避免每次提交的经纬度一字不差
jitter_coord() {
    _seed=$(( $(date +%s) + ${RANDOM:-0} + $$ ))
    awk -v lat="$LAT" -v lon="$LON" -v d="$JITTER_DEG" -v seed="$_seed" 'BEGIN{
        srand(seed);
        printf "%.6f %.6f\n", lat + (rand()-0.5)*d, lon + (rand()-0.5)*d;
    }'
}

# ---------------------------------------------------------------- 单实例锁
LOCK=/data/local/tmp/tm_flow.lock
if [ -f "$LOCK/pid" ] && kill -0 "$(cat "$LOCK/pid" 2>/dev/null)" 2>/dev/null; then
    log "already running (pid $(cat "$LOCK/pid")), exit"
    exit 0
fi
rm -rf "$LOCK"; mkdir -p "$LOCK" 2>/dev/null || exit 0
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT INT TERM

# ---------------------------------------------------------------- 面板
STATE="启动中"
ACTIVE="—"
SIGNED_INFO="—"
COORD_TXT="—"
OID=""
LAST_REFRESH_TXT="—"
LAST_REFRESH=0
LAST_ATTEMPT=0

# 写面板数据。所有插值都要消毒：JSON 里的双引号会截断 JS 字符串，
# 反斜杠和反引号也会破坏语法。日志用字符串数组，避免模板字面量的坑。
san() { printf '%s' "$1" | tr '\n\r' '  ' | sed 's/["\\]/ /g'; }
sanlog() { tail -n 14 "$LOG" 2>/dev/null | sed 's/["\\]/ /g' | awk '{printf "\"%s\",", $0}' | sed 's/,$//'; }

write_status() {
    [ -d "$WEB" ] || return 0
    OID_SHOW="—"
    [ -n "$OID" ] && OID_SHOW="$(echo "$OID" | cut -c1-8)…$(echo "$OID" | cut -c25-32)"
    {
        echo "window.STATUS={"
        echo "time:\"$(date '+%H:%M:%S')\","
        echo "state:\"$(san "$STATE")\","
        echo "stateCls:\"$([ "$STATE" = "轮询中" ] && echo ok || echo warn)\","
        echo "oid:\"$(san "$OID_SHOW")\","
        echo "active:\"$(san "$ACTIVE")\","
        echo "activeCls:\"$(echo "$ACTIVE" | grep -q '无签到' && echo '' || echo 'warn')\","
        echo "signed:\"$(san "$SIGNED_INFO")\","
        echo "coords:\"$(san "$COORD_TXT")\","
        echo "lat:\"$(san "$LAT")\","
        echo "lon:\"$(san "$LON")\","
        echo "enabled:\"$([ "$(cat "$EN_FILE" 2>/dev/null)" = "0" ] && echo 0 || echo 1)\","
        echo "refresh:\"$(san "$LAST_REFRESH_TXT")\","
        echo "log:[$(sanlog)]"
        echo "};"
    } > "$WEB/status.js.tmp" 2>/dev/null && mv "$WEB/status.js.tmp" "$WEB/status.js"
}

# ---------------------------------------------------------------- 取号
scan_memory() {
    for PID in $(pidof com.tencent.mm); do
        awk '$2 ~ /^r/ && ($6=="" || $6 ~ /^\[/) { split($1,a,"-"); print a[1], a[2] }' /proc/$PID/maps |
        while read -r s e; do
            ss=$((0x$s)); ee=$((0x$e)); len=$((ee-ss))
            [ "$len" -le 0 ] && continue
            [ "$len" -gt 134217728 ] && len=134217728
            dd if=/proc/$PID/mem bs=4096 skip=$((ss/4096)) count=$((len/4096)) 2>/dev/null
        done | strings | grep -o 'student/sign?openid=[0-9a-f]*' | sed 's/.*openid=//' | sort -u
    done
}

# 从 XWeb 的 Cookie 库读当前会话。微信把 session 存成 base64 的 {"openId":"..."}
cookie_oids() {
    ( for f in "/data/data/com.tencent.mm/app_xweb_data/Profile 1/Cookies" \
                "/data/data/com.tencent.mm/app_xweb_data/Default/Cookies"; do
        [ -f "$f" ] || continue
        grep -a -o 'eyJvcGVuSWQ[A-Za-z0-9+/=]*' "$f" 2>/dev/null | sort -u | while read -r v; do
            echo "$v" | base64 -d 2>/dev/null | sed -n 's/.*"openId":"\([0-9a-f][0-9a-f]*\)".*/\1/p'
        done
    done ) | sort -u
}

try_oid() {
    code=$(curl -s -m 10 -A "$UA" -H "Referer: $REF" -H "openId: $1" \
                -o /tmp/_t.json -w '%{http_code}' "$B/student/active_signs")
    [ "$code" = "200" ]
}

refresh_oid() {
    STATE="续号中"
    write_status
    log "refresh: 打开授权页"
    LAST_ATTEMPT=$(date +%s)

    # 锁屏/熄屏下 WebView 不加载，先唤醒；记下是不是我们唤醒的
    WOKE=0
    ensure_awake && WOKE=1
    [ "$WOKE" = "1" ] && log "refresh: 屏幕原本熄灭，已唤醒"

    # 记住当前已有的候选；新授权会把它们顶掉，所以只认"新增"的那个
    BEFORE=$(cookie_oids | tr '\n' ' ')

    close_webview
    am start -f 0x18000000 -n com.tencent.mm/.plugin.webview.ui.tools.WebViewUI --es rawUrl "$OAUTH" >/dev/null 2>&1
    sleep 8

    # 关键：关闭页面会立即触发 Cookie 落盘（实测 2 秒内）。
    # 一直开着页面反而要等 30 秒才写盘，盲区大三倍。
    close_webview
    log "refresh: 页面已关，等 Cookie 落盘"

    _i=0
    while [ $_i -lt 15 ]; do
        sleep 1
        for oid in $(cookie_oids); do
            case " $BEFORE " in *" $oid "*) continue ;; esac
            if try_oid "$oid"; then
                OID="$oid"
                echo "$oid" > "$OID_FILE"
                log "refresh: 新 openid 可用 -> $oid"
                LAST_REFRESH=$(date +%s)
                LAST_REFRESH_TXT="$(date '+%H:%M:%S') 已续号"
                STATE="轮询中"
                write_status
                [ "$WOKE" = "1" ] && input keyevent 223 >/dev/null 2>&1   # 睡回去
                return 0
            fi
        done
        _i=$((_i+1))
    done

    log "refresh: 15 秒内没等到新 openid"
    STATE="续号失败"
    LAST_REFRESH_TXT="$(date '+%H:%M:%S') 失败，15 秒后重试"
    write_status
    notify "自动取号失败，请手动打开一次签到页"
    # 无论锁屏还是亮屏，一律 15 秒后重试
    LAST_REFRESH=$(( $(date +%s) - REFRESH_EVERY + 15 ))
    [ "$WOKE" = "1" ] && input keyevent 223 >/dev/null 2>&1
    return 1
}

# ---------------------------------------------------------------- 主循环
log "==== flow start (module)"
[ -f "$EN_FILE" ] || echo 1 > "$EN_FILE"
rm -f "$CMD_FILE"

# 降权发通知的辅助脚本（放 /data/local/tmp 才能被 shell 读到）
cat > /data/local/tmp/tm_notify.sh <<'EOS'
#!/system/bin/sh
MSG=$(cat /data/local/tmp/tm_notify.txt 2>/dev/null)
[ -z "$MSG" ] && MSG="微助教签到"
cmd notification post -S bigtext -t "微助教签到" tm_sign "$MSG" >/dev/null 2>&1
EOS
chmod 755 /data/local/tmp/tm_notify.sh

STATE="启动中"; write_status

signed=""
last=""

while true; do
    # ---- 面板命令 ----
    if [ -f "$CMD_FILE" ]; then
        _cmd=$(cat "$CMD_FILE" 2>/dev/null)
        rm -f "$CMD_FILE"
        case "$_cmd" in
            refresh)
                log "面板：手动续号"
                LAST_REFRESH=0
                ;;
            on)
                echo 1 > "$EN_FILE"; log "面板：已启用"
                ;;
            off)
                echo 0 > "$EN_FILE"; log "面板：已暂停"
                ;;
        esac
    fi

    # 面板可能刚改过坐标，每轮重新读一次
    [ -f "$COORD_FILE" ] && . "$COORD_FILE" 2>/dev/null

    # ---- 暂停状态 ----
    if [ "$(cat "$EN_FILE" 2>/dev/null)" = "0" ]; then
        STATE="已暂停（面板开关）"
        ACTIVE="—"
        write_status
        release_wake
        sleep 5
        continue
    fi

    now=$(date +%s)
    elapsed=$((now - LAST_REFRESH))

    if [ "$elapsed" -ge "$REFRESH_EVERY" ]; then
        refresh_oid
    fi

    [ -z "$OID" ] && OID=$(cat "$OID_FILE" 2>/dev/null)
    if [ -z "$OID" ]; then
        sleep 10
        continue
    fi

    STATE="轮询中"
    resp=$(curl -s -m 12 -A "$UA" -H "Referer: $REF" -H "openId: $OID" "$B/student/active_signs")

    case "$resp" in
        *"登录信息失效"*|*"unauthorized"*)
            log "401 会话过期，立即续号"
            ACTIVE="会话过期"
            write_status
            # 刚续过还是 401，说明这轮取号没成功，稍等再试，别原地打转
            if [ $(( $(date +%s) - LAST_ATTEMPT )) -lt 15 ]; then
                STATE="续号失败，等待重试"
                write_status
                sleep 5
            else
                LAST_REFRESH=0
            fi
            continue
            ;;
    esac

    if [ "$resp" = "[]" ]; then
        ACTIVE="无签到"
    else
        ACTIVE="签到中：$resp"
    fi

    if [ "$resp" != "$last" ]; then
        log "active -> $resp"
        last="$resp"
    fi

    signId=$(echo "$resp" | sed -n 's/.*"signId":[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)
    courseId=$(echo "$resp" | sed -n 's/.*"courseId":[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)
    isGPS=$(echo "$resp" | sed -n 's/.*"isGPS":[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)

    # 已签过的签到仍会留在 active_signs 里，这时要显示"已完成"而不是"签到中"
    if [ -n "$signId" ] && [ "$signed" = "$signId" ]; then
        ACTIVE="已签到（等待结束）"
    fi

    if [ -n "$signId" ] && [ -n "$courseId" ] && [ "$signed" != "$signId" ]; then
        body="{\"courseId\":$courseId,\"signId\":$signId"
        if [ -n "$LAT" ] && [ -n "$LON" ] && [ "$isGPS" != "0" ]; then
            _c=$(jitter_coord)
            JLAT=${_c% *}
            JLON=${_c#* }
            COORD_TXT="$JLAT, $JLON"
            body="$body,\"lat\":$JLAT,\"lon\":$JLON"
        fi
        body="$body}"
        log "POST $body"

        r=$(curl -s -m 15 -A "$UA" -H "Referer: $REF" -H "openId: $OID" \
                  -H 'Content-Type: application/json' -X POST -d "$body" "$B/student-sign-in")
        log "resp: $r"

        case "$r" in
            *signRank*)
                signed="$signId"
                SIGNED_INFO="$(date '+%H:%M:%S') 成功 $r"
                log "OK signId=$signId"
                notify "签到成功 $r"

                ;;
            *"repeat sign in"*|*"已经签到"*)
                # 已经签过：幂等，必须当成功，否则每 5 秒重发一次
                signed="$signId"
                SIGNED_INFO="$(date '+%H:%M:%S') 已签到（幂等）"
                log "already signed signId=$signId"
                notify "已签到（此前已完成）$r"
                ;;
            *errorCode*)
                SIGNED_INFO="$(date '+%H:%M:%S') 失败 $r"
                log "signin failed: $r"
                notify "签到失败 $r"

                ;;
            *)
                SIGNED_INFO="$(date '+%H:%M:%S') 异常 $r"
                notify "签到返回异常 $r"
                ;;
        esac
    fi

    # 上课时段顶住 CPU，避免熄屏后内核挂起把轮询拖慢；其余时段释放省电
    if wake_in_window; then hold_wake; else release_wake; fi

    write_status
    sleep $INTERVAL
done
