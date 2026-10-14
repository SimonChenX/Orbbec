#!/bin/sh
# 安装 ob-relay（幂等）。
#
# 两种用法：
#   1) 树莓派/另一台机器「远程采集」→ 推给服务器：
#        sh deploy/install-relay.sh
#   2) 相机直接插在**跑 ob-hub 的那台服务器**上（本机直连，不走网络）：
#        sh deploy/install-relay.sh --local
#      会把 OB_RELAY_HOST 设成 127.0.0.1，数据和命令都走回环，完全不占网络。
set -e

LOCAL=0
[ "$1" = "--local" ] && LOCAL=1

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/orbbec/relay
UNIT=/etc/systemd/system/ob-relay.service
ENVF=/etc/default/ob-relay

MODE_LABEL="远程采集"
[ "$LOCAL" = 1 ] && MODE_LABEL="本机直连"
echo "==> 安装到 $DEST （模式：$MODE_LABEL）"
mkdir -p "$DEST"
if [ "$SRC" = "$DEST" ]; then
  echo "    源与目标同目录，跳过拷贝"
else
  cp -f "$SRC/relay.py" "$SRC/obproto.py" "$DEST/"
fi
mkdir -p "$DEST/deploy" "$DEST/tests" "$DEST/tools"
[ "$SRC" = "$DEST" ] || {
  cp -f "$SRC"/deploy/*.sh "$SRC"/deploy/*.service "$SRC"/deploy/default-ob-relay "$DEST/deploy/" 2>/dev/null || true
  cp -f "$SRC"/tests/*.py "$DEST/tests/" 2>/dev/null || true
  cp -f "$SRC"/tools/*.py "$DEST/tools/" 2>/dev/null || true
}
chmod +x "$DEST/deploy/"*.sh 2>/dev/null || true

echo "==> 安装 unit"
cp -f "$SRC/deploy/ob-relay.service" "$UNIT"
chmod 644 "$UNIT"

echo "==> 安装配置（已存在则不覆盖，避免冲掉 PSK）"
if [ ! -f "$ENVF" ]; then
  cp "$SRC/deploy/default-ob-relay" "$ENVF"
  chmod 600 "$ENVF"
  echo "    已生成 $ENVF（注意填 OB_RELAY_PSK）"
else
  chmod 600 "$ENVF"
  echo "    已存在 $ENVF，保持不动"
fi

if [ "$LOCAL" = 1 ]; then
  echo "==> 本机直连：OB_RELAY_HOST 改为 127.0.0.1"
  if grep -q '^OB_RELAY_HOST=' "$ENVF"; then
    sed -i 's|^OB_RELAY_HOST=.*|OB_RELAY_HOST=127.0.0.1|' "$ENVF"
  else
    printf 'OB_RELAY_HOST=127.0.0.1\n' >> "$ENVF"
  fi
  echo "    PSK 必须与 /etc/default/ob-hub 的 OB_HUB_PSK 一致（本机的话直接抄过去）"
fi

systemctl daemon-reload
echo "==> 完成。启动：systemctl enable --now ob-relay"

# ---- 依赖自检（本机直连时最容易缺的几样）----
echo "==> 依赖自检"
for c in ffmpeg v4l2-ctl python3; do
  command -v "$c" >/dev/null 2>&1 && echo "    ✅ $c" || echo "    ❌ 缺 $c（apt install ffmpeg v4l-utils）"
done
[ -f /opt/orbbec/SDK/lib/libOrbbecSDK.so ] \
  && echo "    ✅ Orbbec SDK" \
  || echo "    ❌ 缺 Orbbec SDK：从 orbbec.com.cn / OrbbecSDK releases 下 linux_x64 版，解到 /opt/orbbec/SDK"