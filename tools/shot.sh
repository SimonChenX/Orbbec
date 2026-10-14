#!/bin/bash
# 截图 v6（最终）：用 --virtual-time-budget 等页面画出来；--timeout 会在首帧前拍，得到白屏。
# 三个坑：残留代理env / HTTPS-First / 脏 profile（必须每次新 user-data-dir）
OUT="$1"; URL="$2"; W="${3:-1600}"; H="${4:-1000}"; VT="${5:-14000}"
PROF=$(mktemp -d /tmp/cr-shot.XXXXXX)
rm -f "$OUT"
timeout $((VT/1000 + 45)) env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  /usr/bin/chromium --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --no-first-run --user-data-dir="$PROF" --hide-scrollbars --window-size="${W},${H}" \
  --ignore-certificate-errors --allow-running-insecure-content \
  --disable-features=HttpsUpgrades,HttpsFirstBalancedModeAutoEnable,HTTPS-FirstMode \
  --virtual-time-budget="$VT" --screenshot="$OUT" "$URL" \
  2>&1 | grep -vE "ssl_client|handshake" | tail -2
rm -rf "$PROF"
if [ -s "$OUT" ]; then
  echo "  OK $(stat -c%s "$OUT") bytes -> $OUT"
else
  echo "  失败：没有产出图片"
fi