#!/bin/sh
# 启动 Chromium kiosk 全屏显示。
# 由 labwc autostart 调用（外面套 lwrespawn，退出会自动重启）。
#
# 显示哪个页面由 /etc/pi-kiosk-url 决定：
#   缺失                                  -> 日历信息屏 http://127.0.0.1:8787/（原有行为，向后兼容）
#   写 http://127.0.0.1:8790/             -> 树莓派本地预览（降级方案）
#   写 http://camera.internal.jiayouexp.com/screen.html -> 服务器合成整屏流（默认）
# 切换请用：/opt/orbbec/viewer/deploy/switch-kiosk.sh {server|local|calendar}
URL="${KIOSK_URL:-$(cat /etc/pi-kiosk-url 2>/dev/null || echo 'http://127.0.0.1:8787/')}"

# 健康检查要打在**站点根**上，不能拼在页面路径后面
# （例如 .../screen.html/api/status 必然 404，会白等满 30 秒才启动）。
BASE=$(printf '%s' "$URL" | sed -E 's#^([A-Za-z][A-Za-z0-9+.-]*://[^/]+).*#\1#')
i=0
while [ "$i" -lt 15 ]; do
  if curl -sf -o /dev/null "$BASE/api/status" 2>/dev/null || \
     curl -sf -o /dev/null "$BASE/api/state" 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 1
done

exec chromium \
  --kiosk "${URL}" \
  --ozone-platform=wayland \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --disable-session-crashed-bubble \
  --disable-translate \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 \
  --user-data-dir="${HOME}/.config/dashboard-kiosk"