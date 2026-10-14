#!/bin/sh
# 在「服务器整屏流」「本地预览」「日历信息屏」之间切换 kiosk 显示内容。
#
#   sh switch-kiosk.sh server     切到服务器合成整屏流（默认，最省树莓派 CPU）
#   sh switch-kiosk.sh local      切到树莓派本地预览（服务器/网络挂了时的降级方案）
#   sh switch-kiosk.sh calendar   切回日历信息屏
#   sh switch-kiosk.sh            查看当前指向
#
# ⚠️ server 模式与 local 模式互斥：两者都要占用相机，脚本会自动停掉另一个服务。
set -e
URLFILE=/etc/pi-kiosk-url
SRV="http://camera.internal.jiayouexp.com/screen.html"
LOC="http://127.0.0.1:8790/"
CAL="http://127.0.0.1:8787/"

restart_kiosk() {
  pkill -f "user-data-dir=/home/simon/.config/dashboard[-]kiosk" 2>/dev/null || true
  sleep 1
  echo "kiosk 正在重启…"
}

case "$1" in
  server)
    echo "$SRV" > "$URLFILE"
    systemctl stop pi-dashboard 2>/dev/null || true
    echo "==> 启用 ob-relay（采集转发），停掉本地 viewer（抢相机）"
    systemctl start ob-relay 2>/dev/null || true
    systemctl stop ob-viewer 2>/dev/null || true
    echo "kiosk -> 服务器整屏流 $SRV"
    restart_kiosk
    ;;
  local)
    echo "$LOC" > "$URLFILE"
    systemctl stop pi-dashboard 2>/dev/null || true
    echo "==> 降级：停掉 ob-relay，启动本地 ob-viewer"
    systemctl stop ob-relay 2>/dev/null || true
    sleep 1
    systemctl start ob-viewer 2>/dev/null || true
    echo "kiosk -> 本地预览 $LOC"
    restart_kiosk
    ;;
  calendar)
    echo "$CAL" > "$URLFILE"
    systemctl start pi-dashboard 2>/dev/null || true
    echo "kiosk -> 日历信息屏 $CAL"
    restart_kiosk
    ;;
  "")
    echo "当前指向: $(cat $URLFILE 2>/dev/null || echo "$SRV (默认)")"
    echo "ob-relay : $(systemctl is-active ob-relay 2>/dev/null)"
    echo "ob-viewer: $(systemctl is-active ob-viewer 2>/dev/null)"
    exit 0
    ;;
  *)
    echo "用法: $0 {server|local|calendar}" >&2
    exit 2
    ;;
esac