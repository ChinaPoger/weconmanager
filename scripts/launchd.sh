#!/usr/bin/env bash
# 用 LaunchAgent 管 wecom-svr,避免 `launchctl submit` 默认 KeepAlive
# 在「端口已被占用」时把启动预检打成死循环。
#
#   scripts/launchd.sh start    # 写入 plist 并加载;已在跑则直接成功
#   scripts/launchd.sh stop
#   scripts/launchd.sh status
#
# KeepAlive 只在非 0 退出时拉起,并且至少隔 10 秒,防止再出现几千次弹框。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="io.codex.wecom-svr"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DATA="${REPO}/.data"
WECOM="${REPO}/.venv/bin/wecom-svr"

usage() {
    echo "用法: $0 start|stop|status" >&2
    exit 2
}

[[ $# -eq 1 ]] || usage

mkdir -p "$DATA" "$(dirname "$PLIST")"

write_plist() {
    if [[ ! -x "$WECOM" ]]; then
        echo "找不到 $WECOM,先 uv pip install -e ." >&2
        exit 1
    fi
    cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>WorkingDirectory</key>
    <string>${REPO}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${WECOM}</string>
        <string>--port</string>
        <string>8090</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>${DATA}/wecom-svr.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${DATA}/wecom-svr.stderr.log</string>
</dict>
</plist>
EOF
}

job_loaded() {
    launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1
}

case "$1" in
    start)
        write_plist
        if job_loaded; then
            launchctl kickstart -k "${DOMAIN}/${LABEL}" >/dev/null
        else
            launchctl bootstrap "$DOMAIN" "$PLIST"
        fi
        echo "已加载 ${LABEL} (plist ${PLIST})"
        ;;
    stop)
        if job_loaded; then
            launchctl bootout "${DOMAIN}/${LABEL}"
            echo "已卸载 ${LABEL}"
        else
            echo "${LABEL} 未加载"
        fi
        ;;
    status)
        if job_loaded; then
            launchctl print "${DOMAIN}/${LABEL}" | awk '
                /state =/ || /pid =/ || /runs =/ || /last exit/ || /path =/
            '
        else
            echo "${LABEL} 未加载"
        fi
        lsof -nP -iTCP:8090 -sTCP:LISTEN 2>/dev/null || true
        ;;
    *)
        usage
        ;;
esac
