#!/bin/sh
# 在聚合服务器上构建并安装 ob-hub（幂等，可反复跑）。
set -e

SRC="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$SRC/ob-hub"
UNIT=/etc/systemd/system/ob-hub.service
ENVF=/etc/default/ob-hub

echo "==> 构建 $BIN"
cd "$SRC"
go build -o "$BIN" .
echo "    $(ls -la "$BIN" | awk '{print $5}') 字节"

echo "==> 安装 unit"
if [ "$(cd "$SRC/deploy" && pwd)" != "$(dirname "$UNIT" 2>/dev/null || echo /nonexistent)" ]; then
  cp "$SRC/deploy/ob-hub.service" "$UNIT"
fi
chmod 644 "$UNIT"

echo "==> 安装配置（已存在则不覆盖，避免冲掉 PSK）"
if [ ! -f "$ENVF" ]; then
  cp "$SRC/deploy/default-ob-hub" "$ENVF"
  chmod 600 "$ENVF"
  echo "    已生成 $ENVF（注意填 OB_HUB_PSK）"
else
  chmod 600 "$ENVF"
  echo "    已存在 $ENVF，保持不动"
fi

systemctl daemon-reload
echo "==> 完成。启动：systemctl enable --now ob-hub"