#!/bin/sh
# 在树莓派上一键安装/更新（幂等，可重复执行）。
# 用法：  sh /opt/orbbec/viewer/deploy/install.sh
set -e
SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/orbbec/viewer

echo "[1/4] 同步代码 -> $DEST"
install -d "$DEST/web" "$DEST/deploy"
if [ "$SRC" = "$DEST" ]; then
  echo "      (源码目录即部署目录，跳过拷贝)"
else
  install -m 0644 "$SRC/viewer.py"        "$DEST/viewer.py"
  install -m 0644 "$SRC/web/index.html"   "$DEST/web/index.html"
  install -m 0644 "$SRC/web/style.css"    "$DEST/web/style.css"
  install -m 0644 "$SRC/web/app.js"       "$DEST/web/app.js"
fi

echo "[2/4] 安装 systemd 服务"
install -m 0644 "$SRC/deploy/ob-viewer.service" /etc/systemd/system/ob-viewer.service
[ -f /etc/default/ob-viewer ] || install -m 0644 "$SRC/deploy/default-ob-viewer" /etc/default/ob-viewer

echo "[3/4] 安装 kiosk 脚本"
if [ -f /home/Pi/dashboard/deploy/kiosk.sh ] && [ ! -f /home/Pi/dashboard/deploy/kiosk.sh.orig-calendar ]; then
  cp -a /home/Pi/dashboard/deploy/kiosk.sh /home/Pi/dashboard/deploy/kiosk.sh.orig-calendar
  echo "      原 kiosk.sh 已备份为 kiosk.sh.orig-calendar"
fi
install -m 0755 "$SRC/deploy/kiosk.sh" /home/Pi/dashboard/deploy/kiosk.sh 2>/dev/null || \
  echo "      (未找到 dashboard 目录，跳过)"
[ "$SRC" = "$DEST" ] || install -m 0755 "$SRC/deploy/switch-kiosk.sh" "$DEST/deploy/switch-kiosk.sh"
chmod +x "$DEST/deploy/switch-kiosk.sh"

echo "[4/4] 启动服务"
systemctl daemon-reload
systemctl enable ob-viewer >/dev/null 2>&1 || true
systemctl restart ob-viewer
sleep 3
systemctl --no-pager --lines=0 status ob-viewer || true
echo
echo "完成。健康检查： curl -s http://127.0.0.1:8790/api/status"