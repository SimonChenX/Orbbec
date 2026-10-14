#!/bin/sh
# =============================================================
# Orbbec Astra Pro Plus —— 一键抓齐 彩色 + 深度 + IR
# 树莓派零编译运行。
#
#   sh /root/capture_all.sh [彩色帧数] [深度帧数] [IR帧数]
#
# 可配置(环境变量):
#   ROTATE_COLOR  彩色旋转角度, 默认 180 (需求方要求; 设 0 则不旋转)
#   COLOR_W/H     彩色分辨率, 默认 1280x720
#   OUTDIR        输出目录, 默认 /opt/orbbec/cap
#   SDKROOT       SDK 根, 默认 /opt/orbbec
# =============================================================
set -u

ROTATE_COLOR="${ROTATE_COLOR:-180}"
COLOR_W="${COLOR_W:-1280x720}"
OUTDIR="${OUTDIR:-/opt/orbbec/cap}"
SDKROOT="${SDKROOT:-/opt/orbbec}"

COLOR_N="${1:-1}"
DEPTH_N="${2:-2}"
IR_N="${3:-2}"

BIN="$SDKROOT/Example/bin"
LIBPATH="$SDKROOT/SDK/lib"

echo "=============================================="
echo " Orbbec Astra Pro Plus 抓帧"
echo "   彩色: ${COLOR_W} x ${COLOR_N} 帧 (旋转 ${ROTATE_COLOR}°)"
echo "   深度: ${DEPTH_N} 帧    IR: ${IR_N} 帧"
echo "   输出: ${OUTDIR}"
echo "=============================================="

mkdir -p "$OUTDIR" || exit 1

# ---------- 1) 彩色 (UVC, 不需要 SDK) ----------
case "$ROTATE_COLOR" in
  180) VF="hflip,vflip" ;;
  90)  VF="transpose=1" ;;
  270) VF="transpose=2" ;;
  0|"") VF="" ;;
  *)   echo "[警告] 未知 ROTATE_COLOR=$ROTATE_COLOR, 按不旋转处理"; VF="" ;;
esac

if [ -n "$VF" ]; then
  ffmpeg -hide_banner -loglevel error -f v4l2 -input_format mjpeg \
    -video_size "$COLOR_W" -i /dev/video0 -frames:v "$COLOR_N" \
    -vf "$VF" -y "$OUTDIR/color.jpg"
else
  ffmpeg -hide_banner -loglevel error -f v4l2 -input_format mjpeg \
    -video_size "$COLOR_W" -i /dev/video0 -frames:v "$COLOR_N" \
    -y "$OUTDIR/color.jpg"
fi
if [ $? -eq 0 ]; then
  echo "[OK] 彩色 -> $OUTDIR/color.jpg"
else
  echo "[失败] 彩色抓帧 (检查 /dev/video0 是否被占用: fuser -v /dev/video0)"
fi

# ---------- 2) 深度 + IR (SDK, 必须在 bin 目录下跑) ----------
cd "$BIN" || { echo "[失败] 找不到 $BIN"; exit 1; }

run_sensor() {                       # $1=脚本 $2=帧数 $3=日志 $4=标签
  if [ ! -f "$1" ]; then
    echo "[跳过] 没找到 $1"; return
  fi
  # SDK 的日志很吵(含 info/warning),全部收进日志文件,只回显关键行
  LD_LIBRARY_PATH="$LIBPATH" python3 "$1" "$2" > "$3" 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then
    echo "[OK] $4"
    grep -aE '^(选用|  帧|IR profile|\[OK\]|\[致命\])' "$3" | sed 's/^/       /'
  else
    echo "[失败] $4 (rc=$rc) —— 看 $3; 关键错误:"
    grep -aiE 'error|failed|错误' "$3" | head -5 | sed 's/^/       /'
  fi
}

run_sensor /root/ob_depth.py "$DEPTH_N" "$OUTDIR/depth.log" "深度 -> $OUTDIR/depth_*.png"
run_sensor /root/ob_ir.py    "$IR_N"    "$OUTDIR/ir.log"    "IR   -> $OUTDIR/ir_*.png"

echo "----------------------------------------------"
ls -l "$OUTDIR"/color.jpg "$OUTDIR"/depth_*.png "$OUTDIR"/ir_*.png 2>/dev/null
echo "完成。"