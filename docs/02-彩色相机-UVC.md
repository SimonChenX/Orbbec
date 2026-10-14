# 02 · 彩色链路（UVC）

> 结论：**彩色不需要 Orbbec SDK**。它就是标准 UVC 摄像头，`/dev/video0`，`ffmpeg` 直接出图。

---

## 1. ✅ 图像方向：**不需要旋转**（2026-10-14 实测更正）

> ⚠️ **本节在 2026-10-14 被实测推翻并更正。**
> 旧结论「彩色必须旋转 180°」的依据（`samples/` 里那组 raw/fixed 对照）**本身是错的**，
> 且记录来自**橙子派机位**。**在当前的树莓派机位上：彩色原帧、深度原帧都是正立的，两路都不要旋转。**

实测证据（决定性判据是「几何法」——俯拍桌面时，深度图里**近端在下方、远端在上方**）：

1. **几何法**：原始深度帧逐行中位深度 → 近端桌面在**下方**（116~163），远端背景在**上方**（234）。
   → 深度原帧正立。（复现：`np.fromfile('/tmp/depth_0.bin', dtype='<u2').reshape(480,640)` 后逐行取中位数。）
2. **彩色独立验证**：对四个方向跑 DNN 人脸检测（YuNet）→ 0° 置信度 **0.912**，
   裁出核实为**真人侧脸**（有五官/皮肤/头发，不是椅背）；180° 掉到 0.713。
3. **旧对照样本自相矛盾**：`color_raw_1280x720.jpg` 与 `color_fixed_1280x720.jpg`
   实测**同向**（归一化相关 +0.857；`max|raw − rot180(fixed)| = 255`）——
   所谓「fixed」根本没做 180° 旋转。据此得出的结论不可采信。
4. **需求方当场确认：0° 画面是对的**（2026-10-14）。

代码默认值：`viewer.py` 里 `ROT_COLOR = 0`、`ROT_DEPTH = 0`；
运行时可用 `ob-rotate <0|90|180|270>` 一键切换。

### 正确抓帧命令（不旋转）

```bash
ffmpeg -hide_banner -loglevel error \
  -f v4l2 -input_format mjpeg -video_size 1280x720 \
  -i /dev/video0 -frames:v 1 \
  -y /opt/orbbec/cap/color.jpg
```

> 若将来相机被**重新装反**导致画面颠倒，用 `-vf "hflip,vflip"`（= 180°，最快、无插值）。
> 三种等价写法：`hflip,vflip` / `rotate=PI`（有插值，略慢）/ `transpose=2,transpose=2`。

### 用 ImageMagick 事后转（备用）

```bash
magick color_raw.jpg -rotate 180 color_fixed.jpg
```

> 开发主机（Kali）**没有 ffmpeg，但有 ImageMagick**（`magick` / `convert`）。树莓派两者都有。

### ⚠️ 关于 `../samples/` 里的旧对照样本

| 文件 | 文档原说明 | 实测修正 |
|---|---|---|
| `color_raw_1280x720.jpg` | 「未旋转（颠倒）」 | 与 fixed **同向**，实为**正立** |
| `color_fixed_1280x720.jpg` | 「已旋转 180°（正确）」 | 与 raw **同向**，**并未旋转** |
| `tri_labeled.jpg` | A=未旋转 / B=深度 / C=已旋转 | 标签据此失真，勿再作为方向依据 |

> 这组样本**不能**用来判断方向（见上）。深度/IR 是否要旋转的问题已在
> [`05-未决问题.md`](05-未决问题.md) §1 **闭环**：深度也不需要旋转。

---

## 2. 基本抓帧（不旋转）

```bash
# 单帧
ffmpeg -f v4l2 -input_format mjpeg -video_size 1280x720 \
  -i /dev/video0 -frames:v 1 -y out.jpg

# 持续录制 5 秒
ffmpeg -f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 30 \
  -i /dev/video0 -t 5 -c:v copy out.mp4
```

`-input_format mjpeg` 明确指定走 MJPG，否则 ffmpeg 可能协商到 YUYV 而掉到 10fps。

---

## 3. 参数速查

| 项 | 值 |
|---|---|
| 设备 | `/dev/video0`（`/dev/video1` 是元数据节点，别用） |
| 总线 | USB 2.0 |
| 最佳格式 | **MJPG** |
| 最佳分辨率/帧率 | **1280x720 @ 30fps** |
| 备选 | 640x480@30（更快、更省带宽） |
| 未压缩格式 | YUYV（720p 只有 10fps，不推荐） |

格式清单详见 [`01-硬件与环境.md`](01-硬件与环境.md) §4。

### 用 OpenCV 读（如果后续要接视觉算法）

```python
import cv2
cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
ok, frame = cap.read()
# 本机位原帧就是正立的，不需要旋转（若相机装反才加 cv2.rotate(frame, cv2.ROTATE_180)）
```

> 树莓派上装 OpenCV 属于**重活**，按铁律应在 x86 主机上交叉编译或改用
> `pip install opencv-python-headless` 的 aarch64 wheel。当前环境**未装**。

---

## 4. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `Device or resource busy` | 彩色被别的进程占用 | `fuser -v /dev/video0` 找到后杀掉 |
| 抓到全黑/全绿 | 协商到了错误格式 | 显式加 `-input_format mjpeg` |
| 帧率只有 10fps | 走成了 YUYV | 同上 |
| 图像颠倒 | 相机被装反了（**本机位不会**） | 加 `-vf "hflip,vflip"`，或 `ob-rotate 180` |
| 曝光过曝 | 自动曝光 | `-vf` 前加 `-exposure auto` 或用 `v4l2-ctl -c exposure_auto=1` |

画面偏绿 / 白平衡异常属该机型常态（run.log 里也能看到 SDK 侧同样的警告），
**不影响结构判读**，如后续要做颜色相关的算法需要自行做白平衡校正。