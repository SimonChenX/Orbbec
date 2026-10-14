#!/usr/bin/env python3
"""ob-pose —— 彩色画面的骨骼（人体姿态）分析侧车进程。

为什么单独一个进程、而不是塞进 ob-hub：
  * 推理吃 CPU、依赖还多（mediapipe/onnx），崩了不能连累主服务；
  * 不想用的时候 systemctl stop ob-pose 就彻底没了，hub 和其余功能照常。

数据流（全部走本机回环，不占网络）：

    ob-hub /snapshot/color.jpg  ──拉──>  ob-pose  ──mediapipe 推理──>  POST /api/pose
                                                                          │
    浏览器画骨架  <──────────────── GET /api/pose ────────────────────────┘

配置见 /etc/default/ob-pose。
"""

import io
import json
import os
import sys
import time
import urllib.request

import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mpp
from mediapipe.tasks.python import vision
from PIL import Image

# ---------------------------------------------------------------- 配置
def _env(k, d):
    v = os.environ.get(k, "")
    return (v.strip() if v else "") or d


HUB = _env("OB_POSE_HUB", "http://127.0.0.1:9500")
MODEL = _env("OB_POSE_MODEL", "/home/OrbbecPiApi/models/pose_landmarker_lite.task")
FPS = float(_env("OB_POSE_FPS", "8"))
MIN_CONF = float(_env("OB_POSE_MIN_CONF", "0.4"))
TIMEOUT = float(_env("OB_POSE_TIMEOUT", "3"))


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ---------------------------------------------------------------- 模型
def build():
    opts = vision.PoseLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_path=MODEL),
        running_mode=vision.RunningMode.IMAGE,
        num_poses=1,
        min_pose_detection_confidence=MIN_CONF,
        min_pose_presence_confidence=MIN_CONF,
        min_tracking_confidence=MIN_CONF,
    )
    return vision.PoseLandmarker.create_from_options(opts)


# ---------------------------------------------------------------- IO
def grab_color():
    """从 hub 拉一张彩色快照（本机回环，很快）。"""
    with urllib.request.urlopen(HUB + "/snapshot/color.jpg", timeout=TIMEOUT) as r:
        return r.read()


def post_pose(obj):
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(HUB + "/api/pose", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status


# ---------------------------------------------------------------- 主循环
def main():
    log("ob-pose 启动：hub=%s 模型=%s 目标 %.1f fps" % (HUB, os.path.basename(MODEL), FPS))
    lm = build()
    log("模型已加载（lazy 首次推理会慢一点）")

    interval = 1.0 / FPS if FPS > 0 else 0.0
    fails = 0
    n_ok = n_none = 0

    while True:
        t0 = time.time()
        try:
            jpg = grab_color()
            img = Image.open(io.BytesIO(jpg)).convert("RGB")
            w, h = img.size
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.asarray(img))

            t1 = time.time()
            res = lm.detect(mp_img)
            infer_ms = (time.time() - t1) * 1000

            out = {
                "ts_us": int(time.time() * 1_000_000),
                "w": w,
                "h": h,
                "model": os.path.basename(MODEL).replace(".task", ""),
                "infer_ms": round(infer_ms, 1),
                "landmarks": [],
                "world": [],
                "score": 0.0,
            }
            if res.pose_landmarks:
                pts = res.pose_landmarks[0]
                # [x, y, z, visibility, presence]（归一化坐标，x/y 相对画面，可能超出 0~1：人在画面外时模型会外推）
                out["landmarks"] = [[round(p.x, 4), round(p.y, 4), round(p.z, 4),
                                     round(p.visibility or 0, 3), round(p.presence or 0, 3)]
                                    for p in pts]
                if res.pose_world_landmarks:
                    out["world"] = [[round(p.x, 4), round(p.y, 4), round(p.z, 4)]
                                    for p in res.pose_world_landmarks[0]]
                n_ok += 1
            else:
                n_none += 1

            post_pose(out)
            fails = 0
        except Exception as ex:
            fails += 1
            log("出错(%d): %s" % (fails, ex))
            if fails == 5:
                # hub 还不认识这个接口 / 版本不匹配时给个明确提示
                log("提示：若持续 404/403，检查 ob-hub 版本是否支持 /api/pose")
            time.sleep(min(2.0 * fails, 10.0))
            continue

        if n_ok % 60 == 0 and n_ok > 0:
            log("已推理 %d 帧（检出人 %d 次 / 无人 %d 次）" % (n_ok + n_none, n_ok, n_none))

        # 匀速：扣掉本次耗时，避免"推理慢就变成低帧率"之外的额外漂移
        rest = interval - (time.time() - t0)
        if rest > 0:
            time.sleep(rest)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)