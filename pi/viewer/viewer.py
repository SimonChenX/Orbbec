#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orbbec Astra Pro Plus —— 彩色 + 深度 实时预览服务（树莓派 / 零编译）
=====================================================================

浏览器端点：

    GET /                     网页（800x480 全屏 kiosk 用）
    GET /color.mjpg           彩色 MJPEG 流（ffmpeg 走 UVC /dev/video0，0° 时原生透传）
    GET /depth.mjpg           深度 MJPEG 流（Orbbec SDK v1 + ctypes，伪彩 + 量程图例）
    GET /snapshot/color.jpg   单帧快照（排障 / 取证）
    GET /snapshot/depth.jpg   单帧快照
    GET /api/status           JSON 状态（帧率、有效像素、深度统计、错误）

设计要点
--------
* 彩色与深度在 USB 上是**两个独立设备**，两条链路并行，互不阻塞。
* 深度相关调用必须在 /opt/orbbec/Example/bin 下运行（SDK 从 CWD 找 config XML）。
* 深度量程自适应（p5/p95 平滑跟踪）—— 近距桌面场景才不会糊成一片颜色。
* 全部参数走环境变量（见 /etc/default/ob-viewer），改配置不用动代码。
* 纯 stdlib + numpy + PIL，不在目标机上编译任何东西。
"""

import ctypes as C
import io
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def _env(k, d):
    return os.environ.get(k, d)


# ------------------------------------------------------------------ 配置
BIND = _env("OB_VIEW_BIND", "127.0.0.1")
PORT = int(_env("OB_VIEW_PORT", "8790"))
WEB_ROOT = _env("OB_VIEW_WEB",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))

COLOR_DEV = _env("OB_COLOR_DEV", "/dev/video0")
# 720p 只有 15/30 两档；屏上面板仅 ~376px 宽，640x480(4:3) 显示得比 720p(16:9) 更大且流量只 1/3。
COLOR_SIZE = _env("OB_COLOR_SIZE", "640x480")
COLOR_FPS = int(_env("OB_COLOR_FPS", "30"))      # v4l2 720p 仅支持 30 / 15
ROT_COLOR = int(_env("OB_ROTATE_COLOR", "0"))    # 树莓派实测：原帧已正立，勿转
ROT_DEPTH = int(_env("OB_ROTATE_DEPTH", "0"))    # 深度原帧亦正立（几何判据实测）
DEPTH_FPS = int(_env("OB_DEPTH_FPS", "30"))      # 深度渲染节流上限（640x480@30 原生）
DEPTH_SIZE = _env("OB_DEPTH_SIZE", "320x240")
JPEG_Q = int(_env("OB_JPEG_Q", "70"))

# 服务启动标识：进程每重启一次就不同。页面的 app.js 拿它判断「服务重启了，要重建 MJPEG 连接」——
# 只靠帧号 seq 会漏判，因为 seq 在重启后会从 0 重新计数。
BOOT_ID = "%d-%d" % (os.getpid(), int(time.time()))

SDK_LIB = _env("OB_SDK_LIB", "/opt/orbbec/SDK/lib/libOrbbecSDK.so")
SDK_CWD = _env("OB_SDK_CWD", "/opt/orbbec/Example/bin")

FONT_LAT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

SENSOR_DEPTH = 3
FMT_NAME = {8: "Y16", 10: "Y10", 11: "Y11", 12: "Y12"}

log_lock = threading.Lock()


def log(*a):
    with log_lock:
        print("[%s] %s" % (time.strftime("%H:%M:%S"), " ".join(str(x) for x in a)),
              flush=True)


# --------------------------------------------------------------- 最新帧槽
class Feed(object):
    """线程安全的最新帧槽：生产者 publish，消费者 wait 拿下一帧。"""

    def __init__(self, name):
        self.name = name
        self.cv = threading.Condition()
        self.seq = 0
        self.jpeg = None
        self.meta = {}
        self.error = None
        self.clients = 0
        self.state = "starting"
        self._n = 0
        self._t0 = time.time()
        self.fps = 0.0

    def publish(self, jpeg, meta=None):
        with self.cv:
            self.jpeg = jpeg
            self.seq += 1
            self._n += 1
            now = time.time()
            if now - self._t0 >= 2.0:
                self.fps = round(self._n / (now - self._t0), 1)
                self._n = 0
                self._t0 = now
            if meta:
                self.meta = meta
            self.state = "ok"
            self.error = None
            self.cv.notify_all()

    def fail(self, msg):
        with self.cv:
            self.error = str(msg)
            self.state = "error"
            log(self.name, "ERROR:", msg)
            self.cv.notify_all()

    def wait(self, last_seq, timeout=5.0):
        with self.cv:
            self.cv.wait_for(lambda: self.seq != last_seq, timeout)
            return self.seq, self.jpeg


color_feed = Feed("color")
depth_feed = Feed("depth")


# ------------------------------------------------------------ 伪彩 LUT
_TURBO = [(0.00, (48, 18, 59)), (0.10, (70, 107, 227)), (0.20, (33, 181, 231)),
          (0.30, (40, 222, 155)), (0.40, (126, 244, 73)), (0.50, (212, 226, 44)),
          (0.60, (251, 172, 38)), (0.70, (250, 105, 16)), (0.80, (228, 45, 4)),
          (0.90, (171, 11, 3)), (1.00, (122, 4, 3))]


def build_lut():
    import numpy as np
    ts = np.linspace(0.0, 1.0, 256)
    xs = np.array([p[0] for p in _TURBO])
    cs = np.array([p[1] for p in _TURBO], dtype=float)
    lut = np.stack([np.interp(ts, xs, cs[:, i]) for i in range(3)], axis=1)
    return lut.astype(np.uint8)


LUT = build_lut()

# 768 字节调色板：索引 0 保留给「无效像素」= 黑，1..255 = LUT[1..255]。
# 用它走 Image "P" 模式，把上色交给 C 层（树莓派实测比 LUT[idx] 花式索引快 ~2.4 倍）。
import numpy as _np
PAL = _np.zeros(768, _np.uint8)
PAL[3:768] = LUT[1:].reshape(-1)


def _font(size):
    try:
        from PIL import ImageFont
        return ImageFont.truetype(FONT_LAT, size)
    except Exception:
        return None


FONT_S = _font(15)
FONT_M = _font(18)


# ------------------------------------------------------------ 彩色生产者
def _rot_filter(deg):
    """彩色 180° 用 hflip+vflip（最快、无插值）。"""
    return {0: "null", 90: "transpose=1", 180: "hflip,vflip",
            270: "transpose=2"}.get(deg, "null")


def color_worker():
    """ffmpeg 读 UVC 彩色 -> 旋转 -> MJPG 管道 -> 解析成帧。"""
    vf = _rot_filter(ROT_COLOR)

    base = ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", COLOR_SIZE, "-framerate", str(COLOR_FPS),
            "-i", COLOR_DEV]
    if vf == "null":
        # 不需要旋转：直接透传相机原生 MJPEG，省掉解码+重编码。
        # 树莓派 4 核上这是跑满 30fps 的关键（重编码只能到 ~25fps 且占满一个核）。
        cmd = base + ["-c:v", "copy", "-f", "mpjpeg", "-"]
    else:
        # 需要旋转：必须解码-旋转-重编码
        cmd = base + ["-vf", vf, "-f", "mpjpeg", "-q:v", "5", "-"]

    while True:
        log("color: 启动 ffmpeg", " ".join(cmd))
        p = None
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, bufsize=0)
            fd = p.stdout.fileno()
            buf = b""
            w = h = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise RuntimeError("ffmpeg 退出，返回码 %s" % p.wait())
                buf += chunk
                if len(buf) > 16 * 1024 * 1024:
                    buf = buf[-1024 * 1024:]
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s < 0:
                        buf = buf[-1:] if buf[-1:] == b"\xff" else b""
                        break
                    e = buf.find(b"\xff\xd9", s + 2)
                    if e < 0:
                        if s > 0:
                            buf = buf[s:]
                        break
                    jpg = buf[s:e + 2]
                    buf = buf[e + 2:]
                    if len(jpg) < 512:
                        continue
                    color_feed.publish(jpg, {"bytes": len(jpg),
                                             "size": COLOR_SIZE,
                                             "rotate": ROT_COLOR,
                                             "device": COLOR_DEV})
        except Exception as ex:
            color_feed.fail(ex)
        finally:
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
        time.sleep(2)


# ------------------------------------------------------------ SDK 绑定
lib = None
_P = C.POINTER(C.c_void_p)


def load_sdk():
    global lib
    lib = C.CDLL(SDK_LIB)

    def bind(name, restype, argtypes):
        f = getattr(lib, name)
        f.restype = restype
        f.argtypes = argtypes

    for n, r, a in [
        ("ob_create_pipeline", C.c_void_p, [_P]),
        ("ob_create_config", C.c_void_p, [_P]),
        ("ob_pipeline_get_stream_profile_list", C.c_void_p, [C.c_void_p, C.c_int, _P]),
        ("ob_stream_profile_list_count", C.c_uint32, [C.c_void_p, _P]),
        ("ob_stream_profile_list_get_profile", C.c_void_p, [C.c_void_p, C.c_int, _P]),
        ("ob_video_stream_profile_width", C.c_uint32, [C.c_void_p, _P]),
        ("ob_video_stream_profile_height", C.c_uint32, [C.c_void_p, _P]),
        ("ob_stream_profile_format", C.c_int, [C.c_void_p, _P]),
        ("ob_config_enable_stream", None, [C.c_void_p, C.c_void_p, _P]),
        ("ob_config_set_depth_scale_require", None, [C.c_void_p, C.c_bool, _P]),
        ("ob_pipeline_start_with_config", None, [C.c_void_p, C.c_void_p, _P]),
        ("ob_pipeline_stop", None, [C.c_void_p, _P]),
        ("ob_pipeline_wait_for_frameset", C.c_void_p, [C.c_void_p, C.c_uint32, _P]),
        ("ob_frameset_depth_frame", C.c_void_p, [C.c_void_p, _P]),
        ("ob_video_frame_width", C.c_uint32, [C.c_void_p, _P]),
        ("ob_video_frame_height", C.c_uint32, [C.c_void_p, _P]),
        ("ob_depth_frame_get_value_scale", C.c_float, [C.c_void_p, _P]),
        ("ob_frame_format", C.c_int, [C.c_void_p, _P]),
        ("ob_frame_index", C.c_uint64, [C.c_void_p, _P]),
        ("ob_frame_data", C.c_void_p, [C.c_void_p, _P]),
        ("ob_frame_data_size", C.c_uint32, [C.c_void_p, _P]),
        ("ob_delete_frame", None, [C.c_void_p, _P]),
        ("ob_delete_pipeline", None, [C.c_void_p, _P]),
        ("ob_delete_config", None, [C.c_void_p, _P]),
        ("ob_delete_stream_profile_list", None, [C.c_void_p, _P]),
        ("ob_delete_error", None, [C.c_void_p]),
        ("ob_error_message", C.c_char_p, [C.c_void_p]),
        ("ob_error_function", C.c_char_p, [C.c_void_p]),
    ]:
        bind(n, r, a)
    log("depth: SDK 已加载", SDK_LIB)


def _err():
    return C.c_void_p(None)


def _chk(lib_, err, what):
    if err.value:
        m = lib_.ob_error_message(err.value)
        f = lib_.ob_error_function(err.value)
        msg = "%s: %s (%s)" % (what,
                               m.decode("utf-8", "replace") if m else "?",
                               f.decode("utf-8", "replace") if f else "?")
        lib_.ob_delete_error(err.value)
        err.value = None
        raise RuntimeError(msg)


_rng = {"lo": None, "hi": None, "alpha": 0.12}


def depth_render(arr, np, Image, ImageDraw):
    """uint16 深度(已换算 mm) -> 伪彩 JPEG bytes + 统计。"""
    h, w = arr.shape
    valid = arr > 0
    n_valid = int(np.count_nonzero(valid))
    total = w * h

    if n_valid == 0:
        img = Image.new("RGB", (w, h), (0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((8, 8), "no valid depth", font=FONT_M,
               fill=(255, 80, 80), stroke_width=1, stroke_fill=(0, 0, 0))
        if ROT_DEPTH == 180:
            img = img.transpose(Image.Transpose.ROTATE_180)
        b = io.BytesIO()
        img.save(b, "JPEG", quality=JPEG_Q)
        return b.getvalue(), {"valid_pct": 0.0, "min_mm": 0, "max_mm": 0,
                              "center_mm": 0, "n_valid": 0}

    v = arr[valid]
    sub = v[::4] if v.size > 20000 else v
    lo = float(np.percentile(sub, 5))
    hi = float(np.percentile(sub, 95))
    if hi - lo < 40.0:
        hi = lo + 40.0
    if _rng["lo"] is None:
        _rng["lo"], _rng["hi"] = lo, hi
    else:
        a = _rng["alpha"]
        _rng["lo"] += a * (lo - _rng["lo"])
        _rng["hi"] += a * (hi - _rng["hi"])
        if _rng["hi"] - _rng["lo"] < 40.0:
            _rng["hi"] = _rng["lo"] + 40.0
    rlo, rhi = _rng["lo"], _rng["hi"]

    # 调色板路径：比 LUT[idx] 花式索引 + 掩码赋值快约 2.4 倍（Pi4 实测 28.7ms -> 11.7ms）
    # 索引 0 保留为「无效像素」= 黑，有效深度映射到 1..255
    idx = np.clip((arr - rlo) * (254.0 / (rhi - rlo)), 0.0, 254.0).astype(np.uint8)
    idx += 1
    idx[~valid] = 0
    img = Image.fromarray(idx, "P")
    img.putpalette(PAL.tobytes())
    img = img.convert("RGB")

    if ROT_DEPTH == 180:
        img = img.transpose(Image.Transpose.ROTATE_180)
    elif ROT_DEPTH == 90:
        img = img.transpose(Image.Transpose.ROTATE_90)
    elif ROT_DEPTH == 270:
        img = img.transpose(Image.Transpose.ROTATE_270)

    d = ImageDraw.Draw(img)
    cy, cx = h // 2, w // 2
    c = arr[max(0, cy - 3):cy + 4, max(0, cx - 3):cx + 4]
    cvalid = c[c > 0]
    center = float(cvalid.mean()) if cvalid.size else 0.0
    txt = "center %.0fmm   %.0f-%.0f   valid %.0f%%" % (
        center, float(v.min()), float(v.max()), 100.0 * n_valid / total)
    d.text((10, 8), txt, font=FONT_M, fill=(255, 255, 255),
           stroke_width=2, stroke_fill=(0, 0, 0))

    # 量程图例（右上，红=远 蓝=近）
    bx1, bx2 = w - 26, w - 14
    by1, by2 = 34, h - 14
    steps = by2 - by1
    if steps > 10:
        grad = np.linspace(1.0, 0.0, steps)[:, None]
        gi = (grad * 255.0).astype(np.uint8)
        band = np.repeat(LUT[gi], bx2 - bx1, axis=1)
        img.paste(Image.fromarray(band, "RGB"), (bx1, by1))
        d.text((w - 30, by1 - 18), "%.0f" % rhi, font=FONT_S,
               fill=(255, 255, 255), anchor="ra",
               stroke_width=1, stroke_fill=(0, 0, 0))
        d.text((w - 30, by2 + 2), "%.0fmm" % rlo, font=FONT_S,
               fill=(255, 255, 255), anchor="ra",
               stroke_width=1, stroke_fill=(0, 0, 0))

    b = io.BytesIO()
    img.save(b, "JPEG", quality=JPEG_Q, subsampling=2)
    return b.getvalue(), {"valid_pct": round(100.0 * n_valid / total, 1),
                          "n_valid": n_valid,
                          "min_mm": round(float(v.min()), 1),
                          "max_mm": round(float(v.max()), 1),
                          "center_mm": round(center, 1),
                          "range_mm": [round(rlo, 1), round(rhi, 1)]}


# ------------------------------------------------------------ 深度生产者
def depth_worker():
    import numpy as np
    from PIL import Image, ImageDraw

    os.chdir(SDK_CWD)          # SDK 从 CWD 找 OrbbecSDKConfig_v1.0.xml
    load_sdk()

    want_w, want_h = (int(x) for x in DEPTH_SIZE.lower().split("x"))
    while True:
        pipe = cfg = None
        try:
            e = _err(); pipe = lib.ob_create_pipeline(C.byref(e)); _chk(lib, e, "create_pipeline")
            e = _err(); cfg = lib.ob_create_config(C.byref(e)); _chk(lib, e, "create_config")
            e = _err(); lib.ob_config_set_depth_scale_require(cfg, True, C.byref(e)); _chk(lib, e, "depth_scale_require")

            e = _err(); lst = lib.ob_pipeline_get_stream_profile_list(pipe, SENSOR_DEPTH, C.byref(e))
            _chk(lib, e, "depth profile list")
            e = _err(); cnt = lib.ob_stream_profile_list_count(lst, C.byref(e))
            _chk(lib, e, "depth profile count")

            cands = []
            for i in range(cnt):
                e = _err(); pr = lib.ob_stream_profile_list_get_profile(lst, i, C.byref(e))
                if e.value:
                    lib.ob_delete_error(e.value); e.value = None; continue
                e = _err(); w = lib.ob_video_stream_profile_width(pr, C.byref(e))
                e = _err(); h = lib.ob_video_stream_profile_height(pr, C.byref(e))
                e = _err(); fmt = lib.ob_stream_profile_format(pr, C.byref(e))
                cands.append((pr, w, h, fmt))
            pick = next((c for c in cands if (c[1], c[2]) == (want_w, want_h)), None) \
                or max(cands, key=lambda c: c[1] * c[2])

            e = _err(); lib.ob_config_enable_stream(cfg, pick[0], C.byref(e)); _chk(lib, e, "enable depth")
            e = _err(); lib.ob_pipeline_start_with_config(pipe, cfg, C.byref(e)); _chk(lib, e, "pipeline start")
            log("depth: 流已启动 %dx%d %s" % (pick[1], pick[2], FMT_NAME.get(pick[3], pick[3])))

            scale = 1.0
            t_last = 0.0
            while True:
                e = _err(); fs = lib.ob_pipeline_wait_for_frameset(pipe, 2000, C.byref(e))
                if e.value:
                    lib.ob_delete_error(e.value); e.value = None
                    continue
                if not fs:
                    continue
                e = _err(); df = lib.ob_frameset_depth_frame(fs, C.byref(e))
                if e.value or not df:
                    if e.value:
                        lib.ob_delete_error(e.value); e.value = None
                    e = _err(); lib.ob_delete_frame(fs, C.byref(e))
                    continue

                e = _err(); fw = lib.ob_video_frame_width(df, C.byref(e))
                e = _err(); fh = lib.ob_video_frame_height(df, C.byref(e))
                e = _err(); size = lib.ob_frame_data_size(df, C.byref(e))
                e = _err(); fidx = lib.ob_frame_index(df, C.byref(e))
                e = _err(); sc = lib.ob_depth_frame_get_value_scale(df, C.byref(e))
                if sc and sc > 0:
                    scale = sc
                e = _err(); ptr = lib.ob_frame_data(df, C.byref(e))
                if not ptr or size < fw * fh * 2:
                    e = _err(); lib.ob_delete_frame(df, C.byref(e))
                    e = _err(); lib.ob_delete_frame(fs, C.byref(e))
                    continue

                buf = C.string_at(ptr, fw * fh * 2)
                arr = np.frombuffer(buf, dtype="<u2").reshape(fh, fw).astype(np.float32)
                arr *= scale

                e = _err(); lib.ob_delete_frame(df, C.byref(e))
                e = _err(); lib.ob_delete_frame(fs, C.byref(e))

                now = time.time()
                if DEPTH_FPS > 0 and now - t_last < 1.0 / DEPTH_FPS:
                    continue
                t_last = now

                jpg, meta = depth_render(arr, np, Image, ImageDraw)
                meta["frame"] = int(fidx)
                meta["scale"] = scale
                meta["size"] = "%dx%d" % (fw, fh)
                depth_feed.publish(jpg, meta)
        except Exception as ex:
            depth_feed.fail(ex)
            time.sleep(3)
        finally:
            for obj, deleter in ((cfg, "ob_delete_config"), (pipe, "ob_delete_pipeline")):
                if obj:
                    try:
                        e = _err(); getattr(lib, deleter)(obj, C.byref(e))
                        if e.value:
                            lib.ob_delete_error(e.value)
                    except Exception:
                        pass


# ------------------------------------------------------------ HTTP
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ob-viewer/1.0"

    def log_message(self, fmt, *args):   # 静音，日志交给 systemd
        pass

    def _bytes(self, ctype, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._bytes("application/json; charset=utf-8",
                    json.dumps(obj, ensure_ascii=False).encode("utf-8"), code)

    def _stream(self, feed):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=--obframe")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        feed.clients += 1
        last = -1
        try:
            while True:
                seq, jpeg = feed.wait(last, 5.0)
                if seq == last:
                    continue
                last = seq
                if not jpeg:
                    continue
                self.wfile.write(b"\r\n----obframe\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n")
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            feed.clients -= 1

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/color.mjpg", "/color.mjpeg"):
            return self._stream(color_feed)
        if path in ("/depth.mjpg", "/depth.mjpeg"):
            return self._stream(depth_feed)

        if path == "/snapshot/color.jpg":
            with color_feed.cv:
                j = color_feed.jpeg
            if not j:
                return self._json({"error": "no color frame yet"}, 503)
            return self._bytes("image/jpeg", j)

        if path == "/snapshot/depth.jpg":
            with depth_feed.cv:
                j = depth_feed.jpeg
            if not j:
                return self._json({"error": "no depth frame yet"}, 503)
            return self._bytes("image/jpeg", j)

        if path in ("/api/status", "/healthz"):
            def finfo(f):
                with f.cv:
                    return {"state": f.state, "error": f.error,
                            "fps": f.fps, "seq": f.seq, "clients": f.clients,
                            "meta": f.meta}
            ok = (color_feed.state == "ok" and depth_feed.state == "ok")
            return self._json({"ok": ok, "boot": BOOT_ID,
                               "color": finfo(color_feed),
                               "depth": finfo(depth_feed),
                               "config": {"port": PORT, "color_size": COLOR_SIZE,
                                          "color_fps": COLOR_FPS,
                                          "rotate_color": ROT_COLOR,
                                          "rotate_depth": ROT_DEPTH,
                                          "depth_fps": DEPTH_FPS}},
                              200 if ok else 503)

        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        if ".." in rel or rel.startswith("/"):
            return self._json({"error": "bad path"}, 400)
        fp = os.path.join(WEB_ROOT, rel)
        if not os.path.isfile(fp):
            return self._json({"error": "not found", "path": path}, 404)
        try:
            with open(fp, "rb") as fh:
                data = fh.read()
        except Exception as ex:
            return self._json({"error": str(ex)}, 500)
        ext = os.path.splitext(fp)[1].lower()
        return self._bytes(MIME.get(ext, "application/octet-stream"), data)


def main():
    if not os.path.isdir(WEB_ROOT):
        log("warning: web root 不存在", WEB_ROOT)
    for t, name in ((color_worker, "color"), (depth_worker, "depth")):
        th = threading.Thread(target=t, name=name, daemon=True)
        th.start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    log("ob-viewer 监听 http://%s:%d/  (web=%s)" % (BIND, PORT, WEB_ROOT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()