# 模型文件（不入库，自行下载）

`ob-pose` 用 mediapipe 的 PoseLandmarker，需要下面这个 `.task` 模型（约 5.8MB）。
模型文件**不放进 git**（二进制、体积大），部署时按下面命令拉一次即可。

## 1. 姿态模型（必需）

```sh
mkdir -p /home/OrbbecPiApi/models
# 国内直连 Google Storage 通常被墙，走代理下载
export https_proxy=http://<你的代理>:7890 http_proxy=http://<你的代理>:7890
curl -L -o /home/OrbbecPiApi/models/pose_landmarker_lite.task \
  "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
```

可选换成更准的版本（更慢）：

| 模型 | 体积 | 特点 |
|---|---|---|
| `pose_landmarker_lite` | 5.8MB | **默认**，720p 约 41ms/帧 |
| `pose_landmarker_full` | 9.4MB | 更准，约 2 倍耗时 |
| `pose_landmarker_heavy` | 30MB | 最准，约 4 倍耗时 |

把 URL 里的 `lite` / `float16` 相应替换即可；改完更新 `/etc/default/ob-pose` 的 `OB_POSE_MODEL`。

## 2. 依赖

```sh
pip3 install --break-system-packages mediapipe pillow numpy
```

* Python 3.13 可用（mediapipe 1.1.0）。
* ⚠️ mediapipe 1.x **移除了旧的 `mp.solutions` API**，本项目用的是新的
  `mediapipe.tasks.python.vision.PoseLandmarker`，网上老教程里的 `mp.solutions.pose` 在 1.x 上不存在。
* 首次加载会打印 `Feedback manager requires a model with a single signature inference`
  之类的 WARNING，属正常噪音（systemd unit 里已把 `TF_CPP_MIN_LOG_LEVEL=2` 压掉）。

## 3. 本仓库里另外两个 ONNX（备选方案，未使用）

`tools/` 里没有包含它们。若不想装 mediapipe，也可以走 onnxruntime 路线
（OpenCV Zoo 的 MediaPipe pose ONNX + person detection ONNX），但需要自己实现
锚点/NMS/ROI 裁剪逻辑，比直接用 mediapipe 麻烦得多，**不推荐**。