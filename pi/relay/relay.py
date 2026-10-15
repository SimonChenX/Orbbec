#!/usr/bin/env python3
"""ob-relay —— 树莓派侧的「瘦采集转发」进程。

职责只有两件：把设备里的数据读出来 → 按 OBF1 帧协议推给服务器。
**不做渲染、不做编码、不做 Web**（那些都在服务器 /home/OrbbecPiApi）。

两条铁律：

1. **绝不阻塞采集**。每条流是「采集线程 → 最新帧队列(容量1) → 发送线程」，
   队列满就丢最旧的一帧。所以网络再慢、服务器再卡，采集都不会被拖住。
2. **断线自愈**。发送失败就关连接、指数退避重连；重连期间采集照常（只是丢帧）。

配置见 /etc/default/ob-relay。
"""

import ctypes as C
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import obproto  # noqa: E402


# ---------------------------------------------------------------- 配置
def _env(k, d):
    v = os.environ.get(k, "")
    v = v.strip() if v else ""
    return v or d


HOST = _env("OB_RELAY_HOST", "192.168.5.20")
PORT = int(_env("OB_RELAY_PORT", "9501"))
PSK = _env("OB_RELAY_PSK", "")
WHO = _env("OB_RELAY_WHO", socket.gethostname())

COLOR_DEV = _env("OB_COLOR_DEV", "/dev/video0")
COLOR_SIZE = _env("OB_COLOR_SIZE", "640x480")
COLOR_FPS = int(_env("OB_COLOR_FPS", "30"))

# ⚠️ 本设备的深度只有 320x240 可用：640x480 有效像素仅 2%、1280x1024 仅 4.6%
#    （320x240 是 70%+）。已验证与本机带宽无关，是设备/档位本身的问题。别往上调。
DEPTH_SIZE = _env("OB_DEPTH_SIZE", "320x240")
DEPTH_FPS_CAP = int(_env("OB_DEPTH_FPS", "30"))
# 深度原始流开 zlib：614KB/帧 压到 ~63KB（约 10 倍），树莓派侧 level 1 约 12ms/帧。
# 不开的话 640x480@30 要吃 18MB/s，加上彩色会顶满链路。
DEPTH_ZLIB = _env("OB_RELAY_DEPTH_ZLIB", "1") == "1"

# ---------------------------------------------------------------- 相机总开关
# ⭐ 相机总开关：默认**关**（OB_RELAY_CAMERA=0）
#    关的时候树莓派完全不碰相机：不起彩色/深度/红外任何线程，只保留**控制通道**与心跳，
#    所以网页上的「相机」开关仍然能一点即开（约 5 秒）。省 CPU、省链路带宽。
CAMERA_ENABLE = _env("OB_RELAY_CAMERA", "0") == "1"

# ⭐ 传感器：深度 + 红外（默认两路都开）
#    ⚠️ 关键：必须用**两条独立的 pipeline**、各自 start_with_config。
#       把两条流塞进同一个 pipeline 时，配置能过、深度也正常，但 frameset 里永远没有
#       IR 帧（实测 depth 29fps / IR 0 帧）—— 早期「深度与红外互斥」的结论就是这么来的，
#       那个结论是**错的**。两条独立 pipeline 实测各 29.3fps 同时满帧（2026-10-14 复核）。
DEPTH_ENABLE = _env("OB_RELAY_DEPTH", "1") == "1"
# 红外默认关：10bit 噪声数据 zlib 只压 ~1.5×（深度 7.2×），单它就占 ~130Mbps。
# 网页页头的「红外」开关一点即开，不用登机器。
IR_ENABLE = _env("OB_RELAY_IR", "0") == "1"
IR_SIZE = _env("OB_IR_SIZE", "640x480")
IR_FPS_CAP = int(_env("OB_IR_FPS", "30"))

SDK_LIB = _env("OB_SDK_LIB", "/opt/orbbec/SDK/lib/libOrbbecSDK.so")
SDK_CWD = _env("OB_SDK_CWD", "/opt/orbbec/Example/bin")
SENSOR_DEPTH = 3
SENSOR_IR = 1

STATUS_EVERY = float(_env("OB_RELAY_STATUS_SEC", "1"))
VERBOSE = _env("OB_RELAY_VERBOSE", "0") == "1"

START = time.time()


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ---------------------------------------------------------------- 链路
class Link(object):
    """一条 TCP 连接：负责建连 + HELLO 鉴权 + 发帧 + 退避重连。

    send() 用 sendall（阻塞，带超时）：一帧要么整帧发完、要么失败后丢弃重连，
    **不会发半帧**破坏流。发送失败即丢帧（本来就是最新帧优先）。
    """

    def __init__(self, name):
        self.name = name
        self.sock = None
        self.lock = threading.Lock()
        self.backoff = 1.0
        self.next_try = 0.0
        self.sent = 0
        self.dropped = 0
        self.errors = 0
        self.reconnects = 0

    def connect(self):
        s = socket.create_connection((HOST, PORT), timeout=5)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(5)
        payload = obproto.hello_payload(PSK, WHO + "/" + self.name)
        s.sendall(obproto.pack(obproto.TYPE_HELLO, 0, obproto.now_us(), 0, 0,
                               obproto.FMT_JSON, payload))
        self.sock = s
        self.backoff = 1.0
        log("%s: 已连接 %s:%d" % (self.name, HOST, PORT))

    def send(self, frame):
        """True=已发出；False=未发出（丢帧，必要时下轮重连）。"""
        now = time.time()
        with self.lock:
            if self.sock is None:
                if now < self.next_try:
                    return False
                try:
                    self.connect()
                except Exception as ex:
                    self.errors += 1
                    self.next_try = now + self.backoff
                    log("%s: 连接失败(%s)，%.1fs 后重试" % (self.name, ex, self.backoff))
                    self.backoff = min(self.backoff * 2, 30.0)
                    return False
            try:
                self.sock.sendall(frame)
                self.sent += 1
                return True
            except Exception as ex:
                self.errors += 1
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
                self.next_try = time.time() + 1.0
                self.reconnects += 1
                log("%s: 发送失败(%s)，断开重连" % (self.name, ex))
                return False

    def online(self):
        return self.sock is not None

    def ensure(self):
        """确保已连接（带退避），给控制通道用。"""
        with self.lock:
            if self.sock is not None:
                return True
            now = time.time()
            if now < self.next_try:
                return False
            try:
                self.connect()
                return True
            except Exception as ex:
                self.errors += 1
                self.next_try = now + self.backoff
                log("%s: 连接失败(%s)，%.1fs 后重试" % (self.name, ex, self.backoff))
                self.backoff = min(self.backoff * 2, 30.0)
                return False

    def reset(self):
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except Exception:
                    pass
            self.sock = None
            self.next_try = time.time() + 1.5

    def recv(self, timeout=10.0):
        """读一整帧（控制通道用）。超时返回 (None, None)。"""
        with self.lock:
            sock = self.sock
        if sock is None:
            return None, None
        try:
            sock.settimeout(timeout)
            hb = b""
            while len(hb) < obproto.HEADER_LEN:
                chunk = sock.recv(obproto.HEADER_LEN - len(hb))
                if not chunk:
                    raise ConnectionError("对端关闭")
                hb += chunk
            hdr = obproto.unpack_header(hb)
            payload = b""
            while len(payload) < hdr["paylen"]:
                chunk = sock.recv(min(65536, hdr["paylen"] - len(payload)))
                if not chunk:
                    raise ConnectionError("对端关闭")
                payload += chunk
            return hdr, payload
        except socket.timeout:
            return None, None
        except Exception as ex:
            log("%s: 读失败(%s)，重连" % (self.name, ex))
            self.reset()
            return None, None


def put_latest(q, item):
    """最多缓存 1 帧：队列满就丢最旧的，保证发的永远是最新帧。"""
    while True:
        try:
            q.put_nowait(item)
            return True
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass


# ---------------------------------------------------------------- 相机参数
CAMERA_CONF = _env("OB_RELAY_CAMERA_CONF", "/opt/orbbec/relay/camera.json")
# 这台 UVC 的所有可调项（含范围），供网页生成控件
CAMERA_SPEC = [
    {"key": "brightness", "label": "亮度", "min": -64, "max": 64, "step": 1, "default": 0},
    {"key": "contrast", "label": "对比度", "min": 0, "max": 95, "step": 1, "default": 32},
    {"key": "saturation", "label": "饱和度", "min": 0, "max": 128, "step": 1, "default": 110},
    {"key": "gamma", "label": "伽马（暗部提亮）", "min": 100, "max": 300, "step": 1, "default": 100},
    {"key": "sharpness", "label": "锐度", "min": 1, "max": 7, "step": 1, "default": 2},
    {"key": "backlight_compensation", "label": "背光补偿（逆光场景关键）", "min": 0, "max": 3, "step": 1, "default": 1},
    {"key": "hue", "label": "色相", "min": -2000, "max": 2000, "step": 1, "default": 0},
]
CAMERA_BOOL = [
    {"key": "white_balance_automatic", "label": "自动白平衡", "default": 1},
]
CAMERA_DEFAULTS = {s["key"]: s["default"] for s in CAMERA_SPEC}
CAMERA_DEFAULTS.update({s["key"]: s["default"] for s in CAMERA_BOOL})


def _v4l2(args):
    try:
        r = subprocess.run(["v4l2-ctl", "-d", COLOR_DEV] + args,
                           capture_output=True, text=True, timeout=8)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as ex:
        return 1, str(ex)


def read_camera_settings():
    """读回设备当前值（v4l2-ctl -l 的输出解析）。"""
    rc, out = _v4l2(["-l"])
    cur = {}
    if rc != 0:
        return cur
    import re
    for line in out.splitlines():
        m = re.match(r"\s*(\w+)\s+0x[0-9a-f]+\s+\((int|bool|menu)\)\s*:(.*)", line)
        if not m:
            continue
        key, _, tail = m.group(1), m.group(2), m.group(3)
        vm = re.search(r"value=(-?\d+)", tail)
        if vm:
            cur[key] = int(vm.group(1))
    return cur


def apply_camera_settings(controls):
    args = []
    for k, v in controls.items():
        args += ["-c", "%s=%s" % (k, v)]
    if not args:
        return True, "没有要应用的项"
    rc, out = _v4l2(args)
    if rc != 0:
        return False, out.strip()[:300]
    try:
        with open(CAMERA_CONF, "w") as f:
            json.dump({k: int(v) for k, v in controls.items()}, f)
    except Exception as ex:
        log("camera: 参数已应用但落盘失败 %s" % ex)
    return True, "已应用 %d 项" % len(controls)


def restore_camera_settings():
    """开机/重启时把上次保存的参数重新打到设备上（UVC 控制项在重新插拔后会丢）。"""
    try:
        with open(CAMERA_CONF) as f:
            saved = json.load(f)
    except Exception:
        saved = CAMERA_DEFAULTS
    ok, msg = apply_camera_settings(saved)
    log("camera: 启动时恢复参数 %s（%s）" % ("成功" if ok else "失败", msg))


# ---------------------------------------------------------------- 网页可调项
STREAM_SPEC = [
    # 相机总开关：关上时树莓派不采集任何画面（只留控制通道），随用随开。
    {"key": "OB_RELAY_CAMERA", "label": "相机总开关",
     "options": ["1", "0"], "option_labels": {"1": "开", "0": "关"},
     "current": lambda: "1" if CAMERA_ENABLE else "0"},
    # 两路可以同时开（各跑独立 pipeline）。关掉某一路能省树莓派 CPU 和链路带宽：
    # 红外 640x480 未压缩约 85MB/s 量级，深度的原始流另有 zlib 压缩开关。
    {"key": "OB_RELAY_DEPTH", "label": "深度图（可鼠标测距）",
     "options": ["1", "0"], "option_labels": {"1": "开", "0": "关"},
     "current": lambda: "1" if DEPTH_ENABLE else "0"},
    {"key": "OB_RELAY_IR", "label": "红外图（近红外 850nm，非热成像）",
     "options": ["1", "0"], "option_labels": {"1": "开", "0": "关"},
     "current": lambda: "1" if IR_ENABLE else "0"},
    {"key": "OB_COLOR_SIZE", "label": "彩色分辨率", "options": ["640x480", "1280x720"], "current": lambda: COLOR_SIZE},
    {"key": "OB_COLOR_FPS", "label": "彩色帧率", "options": ["30", "15"], "current": lambda: str(COLOR_FPS)},
    {"key": "OB_DEPTH_SIZE", "label": "深度分辨率", "options": ["320x240", "640x480"], "current": lambda: DEPTH_SIZE},
    {"key": "OB_DEPTH_FPS", "label": "深度帧率上限", "options": ["30", "15", "10"], "current": lambda: str(DEPTH_FPS_CAP)},
    # ⚠️ 本机型 IR 只有 640x480 出图：320x240 档位在列表里有、能起流，
    #    但永远等不到 frameset（实测 0 帧；640x480 是 29fps）。别加 320x240 选项。
    {"key": "OB_IR_SIZE", "label": "红外分辨率（本机型仅 640x480 出图）",
     "options": ["640x480"], "current": lambda: IR_SIZE},
    {"key": "OB_IR_FPS", "label": "红外帧率上限", "options": ["30", "15", "10"], "current": lambda: str(IR_FPS_CAP)},
    {"key": "OB_RELAY_DEPTH_ZLIB", "label": "原始流压缩(zlib)", "options": ["1", "0"], "current": lambda: "1" if DEPTH_ZLIB else "0"},
]
ENV_FILE = _env("OB_RELAY_ENV_FILE", "/etc/default/ob-relay")


def read_stream_settings():
    return {s["key"]: s["current"]() for s in STREAM_SPEC}


def write_stream_settings(values):
    """写回 /etc/default/ob-relay（只改这几个键），然后由调用方重启进程生效。"""
    allowed = {s["key"] for s in STREAM_SPEC}
    try:
        with open(ENV_FILE) as f:
            lines = f.readlines()
    except Exception as ex:
        return False, "读不到 %s: %s" % (ENV_FILE, ex)
    seen = set()
    out = []
    for ln in lines:
        k = ln.split("=", 1)[0].strip() if "=" in ln and not ln.startswith("#") else None
        if k in allowed and k in values:
            out.append("%s=%s\n" % (k, values[k]))
            seen.add(k)
        else:
            out.append(ln)
    for k in allowed - seen:
        if k in values:
            out.append("%s=%s\n" % (k, values[k]))
    try:
        with open(ENV_FILE, "w") as f:
            f.writelines(out)
    except Exception as ex:
        return False, "写入失败: %s" % ex
    return True, "已写入 %s" % ENV_FILE


# ---------------------------------------------------------------- 控制通道
def handle_control(cmd):
    """处理服务器下发的命令，返回给服务器回执（JSON 可序列化）。"""
    cid = cmd.get("id")
    action = cmd.get("action")
    try:
        if action == "get_config":
            cam = read_camera_settings()
            # 注意：STREAM_SPEC 里的 current 是 lambda，不能直接 json 序列化，这里剥掉
            spec_out = [{"key": s["key"], "label": s["label"], "options": s["options"]} for s in STREAM_SPEC]
            return {"ack": cid, "ok": True, "camera": cam, "camera_spec": CAMERA_SPEC + CAMERA_BOOL,
                    "camera_defaults": CAMERA_DEFAULTS, "stream": read_stream_settings(),
                    "stream_spec": spec_out, "who": WHO, "uptime_s": int(time.time() - START)}

        if action == "set_camera":
            ctl = {k: int(v) for k, v in (cmd.get("controls") or {}).items()}
            ok, msg = apply_camera_settings(ctl)
            return {"ack": cid, "ok": ok, "msg": msg, "camera": read_camera_settings()}

        if action == "reset_camera":
            ok, msg = apply_camera_settings(CAMERA_DEFAULTS)
            return {"ack": cid, "ok": ok, "msg": msg, "camera": read_camera_settings()}

        if action == "set_stream":
            vals = cmd.get("values") or {}
            ok, msg = write_stream_settings(vals)
            if not ok:
                return {"ack": cid, "ok": False, "msg": msg}
            return {"ack": cid, "ok": True, "msg": msg + "，采集端即将重启生效（约 5 秒）",
                    "restart": True}

        if action == "restart":
            return {"ack": cid, "ok": True, "msg": "采集端即将重启", "restart": True}

        return {"ack": cid, "ok": False, "msg": "未知命令: %s" % action}
    except Exception as ex:
        return {"ack": cid, "ok": False, "msg": "执行出错: %s" % ex}


def control_worker(link):
    """常驻控制通道：服务器下发命令，本端执行并回执。命令行与数据流分开，互不影响。"""
    while True:
        if not link.ensure():
            time.sleep(2)
            continue
        hdr, payload = link.recv(timeout=10)
        if hdr is None:
            # 控制通道平时静默：超时就发个保活，免得服务器端读超时把连接掐掉
            link.send(obproto.pack(obproto.TYPE_STATUS, 0, obproto.now_us(), 0, 0,
                                   obproto.FMT_JSON, b'{"keepalive":true}'))
            continue
        if hdr["type"] == obproto.TYPE_CONTROL:
            try:
                cmd = json.loads(payload.decode("utf-8"))
            except Exception as ex:
                log("control: 命令解析失败 %s" % ex)
                continue
            resp = handle_control(cmd)
            log("control: %s -> %s" % (cmd.get("action"), resp.get("msg") or "ok"))
            try:
                body = json.dumps(resp).encode("utf-8")
            except Exception as ex:
                # 回执本身序列化失败不能让控制线程死掉，否则通道静默失联
                log("control: 回执序列化失败 %s" % ex)
                body = json.dumps({"ack": cmd.get("id"), "ok": False,
                                   "msg": "回执序列化失败: %s" % ex}).encode("utf-8")
            link.send(obproto.pack(obproto.TYPE_STATUS, 0, obproto.now_us(), 0, 0,
                                   obproto.FMT_JSON, body))
            if resp.get("restart"):
                log("control: 稍后退出，由 systemd 拉起以应用新参数")
                time.sleep(1.0)
                os._exit(0)


def sender_loop(name, link, q, stats):
    while True:
        frame = q.get()
        if link.send(frame):
            stats["sent"] += 1
        else:
            stats["dropped"] += 1


# ---------------------------------------------------------------- 彩色
def color_worker(q, stats):
    vf = "null"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "v4l2", "-input_format", "mjpeg",
           "-video_size", COLOR_SIZE, "-framerate", str(COLOR_FPS),
           "-i", COLOR_DEV,
           "-c:v", "copy", "-f", "mjpeg", "-"]
    seq = 0
    while True:
        log("color: 启动 ffmpeg", " ".join(cmd))
        p = None
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            fd = p.stdout.fileno()
            buf = b""
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
                    seq = (seq + 1) & 0xFFFFFFFF
                    w, h = (int(x) for x in COLOR_SIZE.lower().split("x"))
                    put_latest(q, obproto.pack(obproto.TYPE_COLOR, seq, obproto.now_us(),
                                               w, h, obproto.FMT_MJPEG, jpg))
                    stats["frames"] += 1
        except Exception as ex:
            log("color: 出错 %s" % ex)
        finally:
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
        time.sleep(2)


# ---------------------------------------------------------------- 深度（SDK）
lib = None
_P = C.POINTER(C.c_void_p)
_depth_scale = [1.0]


def _err():
    return C.c_void_p(None)


def _chk(err, what):
    if err.value:
        m = lib.ob_error_message(err.value)
        lib.ob_delete_error(err.value)
        raise RuntimeError("%s: %s" % (what, m.decode("utf-8", "replace") if m else "?"))


def load_sdk():
    global lib
    lib = C.CDLL(SDK_LIB)
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
        ("ob_frameset_ir_frame", C.c_void_p, [C.c_void_p, _P]),
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
    ]:
        f = getattr(lib, n)
        f.restype = r
        f.argtypes = a
    log("depth: SDK 已加载", SDK_LIB)


def sensor_worker(q, stats, kind):
    """采集「深度」或「红外」。kind = "depth" | "ir"。

    ⚠️ 深度与红外必须各自跑**独立的 pipeline**（本函数一个实例对应一条流）。
    把两条流放进同一个 pipeline 时 IR 恒为 0 帧——早期「互斥」结论的成因。
    """
    os.chdir(SDK_CWD)  # SDK 从 CWD 找 OrbbecSDKConfig_v1.0.xml
    load_sdk()

    is_ir = kind == "ir"
    sensor = SENSOR_IR if is_ir else SENSOR_DEPTH
    size = IR_SIZE if is_ir else DEPTH_SIZE
    fps_cap = IR_FPS_CAP if is_ir else DEPTH_FPS_CAP
    ftype = obproto.TYPE_IR if is_ir else obproto.TYPE_DEPTH
    frame_getter = "ob_frameset_ir_frame" if is_ir else "ob_frameset_depth_frame"
    log("sensor[%s]: 目标分辨率 %s" % (kind, size))

    want_w, want_h = (int(x) for x in size.lower().split("x"))
    while True:
        pipe = cfg = None
        try:
            e = _err(); pipe = lib.ob_create_pipeline(C.byref(e)); _chk(e, "create_pipeline")
            e = _err(); cfg = lib.ob_create_config(C.byref(e)); _chk(e, "create_config")
            if not is_ir:
                e = _err(); lib.ob_config_set_depth_scale_require(cfg, True, C.byref(e)); _chk(e, "scale_require")

            e = _err(); lst = lib.ob_pipeline_get_stream_profile_list(pipe, sensor, C.byref(e))
            _chk(e, "profile list")
            e = _err(); cnt = lib.ob_stream_profile_list_count(lst, C.byref(e)); _chk(e, "profile count")
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

            e = _err(); lib.ob_config_enable_stream(cfg, pick[0], C.byref(e)); _chk(e, "enable stream")
            e = _err(); lib.ob_pipeline_start_with_config(pipe, cfg, C.byref(e)); _chk(e, "pipeline start")
            log("sensor[%s]: 流已启动 %dx%d" % (kind, pick[1], pick[2]))

            seq = 0
            t_last = 0.0
            while True:
                e = _err(); fs = lib.ob_pipeline_wait_for_frameset(pipe, 2000, C.byref(e))
                if e.value:
                    lib.ob_delete_error(e.value); e.value = None; continue
                if not fs:
                    continue
                e = _err(); df = getattr(lib, frame_getter)(fs, C.byref(e))
                if e.value or not df:
                    if e.value:
                        lib.ob_delete_error(e.value); e.value = None
                    e = _err(); lib.ob_delete_frame(fs, C.byref(e))
                    continue

                e = _err(); fw = lib.ob_video_frame_width(df, C.byref(e))
                e = _err(); fh = lib.ob_video_frame_height(df, C.byref(e))
                e = _err(); size = lib.ob_frame_data_size(df, C.byref(e))
                if not is_ir:
                    e = _err(); sc = lib.ob_depth_frame_get_value_scale(df, C.byref(e))
                    if sc and sc > 0:
                        _depth_scale[0] = float(sc)
                e = _err(); ptr = lib.ob_frame_data(df, C.byref(e))
                if not ptr or size < fw * fh * 2:
                    e = _err(); lib.ob_delete_frame(df, C.byref(e))
                    e = _err(); lib.ob_delete_frame(fs, C.byref(e))
                    continue

                now = time.time()
                # 节流留 20% 容差：相机周期正好 33.3ms，阈值取等值会因抖动丢掉一半帧
                if fps_cap > 0 and now - t_last < (1.0 / fps_cap) * 0.8:
                    e = _err(); lib.ob_delete_frame(df, C.byref(e))
                    e = _err(); lib.ob_delete_frame(fs, C.byref(e))
                    continue
                t_last = now

                # 只做一次内存拷贝，原样转发原始 uint16（渲染在服务器做）
                raw = C.string_at(ptr, fw * fh * 2)
                e = _err(); lib.ob_delete_frame(df, C.byref(e))
                e = _err(); lib.ob_delete_frame(fs, C.byref(e))

                # 深度图很平滑，zlib 能压 ~10 倍，值得花这点 CPU 换带宽
                payload, flags = raw, 0
                if DEPTH_ZLIB:
                    packed = zlib.compress(raw, 1)
                    if len(packed) < len(raw):
                        payload, flags = packed, obproto.FLAG_ZLIB
                stats["raw_bytes"] += len(raw)
                stats["wire_bytes"] += len(payload) + obproto.HEADER_LEN

                seq = (seq + 1) & 0xFFFFFFFF
                put_latest(q, obproto.pack(ftype, seq, obproto.now_us(),
                                           fw, fh, obproto.FMT_Y16, payload, flags=flags))
                stats["frames"] += 1
        except Exception as ex:
            log("sensor: 出错 %s" % ex)
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


# ---------------------------------------------------------------- 心跳
def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


def heartbeat_loop(link, cstats, sensor_stats):
    while True:
        time.sleep(STATUS_EVERY)
        d = sensor_stats.get("depth") or {}
        i = sensor_stats.get("ir") or {}
        st = {
            "who": WHO,
            "uptime_s": int(time.time() - START),
            "camera_enabled": CAMERA_ENABLE,
            "depth_enabled": DEPTH_ENABLE,
            "ir_enabled": IR_ENABLE,
            "color_frames": cstats["frames"],
            "color_sent": cstats["sent"],
            "color_dropped": cstats["dropped"],
            "color_size": COLOR_SIZE,
            "color_fps_target": COLOR_FPS,
            "depth_frames": d.get("frames", 0),
            "depth_sent": d.get("sent", 0),
            "depth_dropped": d.get("dropped", 0),
            "depth_size": DEPTH_SIZE,
            "depth_scale": _depth_scale[0],
            "depth_fps_cap": DEPTH_FPS_CAP,
            "ir_frames": i.get("frames", 0),
            "ir_sent": i.get("sent", 0),
            "ir_dropped": i.get("dropped", 0),
            "ir_size": IR_SIZE,
            "ir_fps_cap": IR_FPS_CAP,
            "cpu_temp_c": cpu_temp(),
            "link_online": link.online(),
            "link_reconnects": link.reconnects,
            "ts_us": obproto.now_us(),
        }
        payload = json.dumps(st).encode("utf-8")
        ok = link.send(obproto.pack(obproto.TYPE_STATUS, 0, obproto.now_us(), 0, 0,
                                    obproto.FMT_JSON, payload))
        if VERBOSE or not ok:
            log("status: %s" % ("已发送" if ok else "未发送(离线)"))


def main():
    log("ob-relay 启动：%s:%d  who=%s  psk=%s" % (
        HOST, PORT, WHO, "已设置" if PSK else "未设置"))
    if CAMERA_ENABLE:
        log("相机总开关=开：彩色 %s @%dfps %s | 深度 %s | 红外 %s（深度/红外各跑独立 pipeline）" % (
            COLOR_SIZE, COLOR_FPS, COLOR_DEV,
            (DEPTH_SIZE if DEPTH_ENABLE else "关"),
            (IR_SIZE if IR_ENABLE else "关")))
    else:
        log("相机总开关=关（OB_RELAY_CAMERA=0）：不采集任何画面、不碰相机，仅保留控制通道与心跳；"
            "网页页头「相机」开关一点即开（约 5 秒）")

    control_link = Link("control")
    # 彩色链路对象**始终创建**：相机没开时不发帧，但心跳要走它上报状态（hub 只认数据链路上的状态帧）
    color_link = Link("color")
    cq = queue.Queue(maxsize=1)
    cstats = {"frames": 0, "sent": 0, "dropped": 0}

    threads = [threading.Thread(target=control_worker, args=(control_link,), daemon=True, name="control")]

    if CAMERA_ENABLE:
        # 把上次保存的相机参数打回设备（UVC 控制项重插后会丢）
        try:
            restore_camera_settings()
        except Exception as ex:
            log("camera: 恢复参数失败 %s" % ex)
        threads.append(threading.Thread(target=color_worker, args=(cq, cstats), daemon=True, name="cap-color"))
        threads.append(threading.Thread(target=sender_loop, args=("color", color_link, cq, cstats),
                                        daemon=True, name="tx-color"))

    # 深度 / 红外：**各一个采集线程 + 各一条链路**（同 pipeline 开两条流时 IR 恒 0 帧）
    sensor_stats, sensor_links = {}, {}
    for kind, enabled in (("depth", DEPTH_ENABLE), ("ir", IR_ENABLE)):
        if not CAMERA_ENABLE or not enabled:
            log("sensor[%s]: %s，不起线程" % (
                kind, "相机总开关关" if not CAMERA_ENABLE else "配置为关闭"))
            continue
        q = queue.Queue(maxsize=1)
        st = {"frames": 0, "sent": 0, "dropped": 0, "raw_bytes": 0, "wire_bytes": 0}
        lk = Link(kind)
        sensor_stats[kind] = st
        sensor_links[kind] = lk
        threads.append(threading.Thread(target=sensor_worker, args=(q, st, kind),
                                        daemon=True, name="cap-" + kind))
        threads.append(threading.Thread(target=sender_loop, args=(kind, lk, q, st),
                                        daemon=True, name="tx-" + kind))

    threads.append(threading.Thread(target=heartbeat_loop,
                                    args=(color_link, cstats, sensor_stats),
                                    daemon=True, name="heartbeat"))
    for t in threads:
        t.start()
    log("已启动 %d 个线程%s" % (len(threads), "" if CAMERA_ENABLE else "（相机未开）"))

    while True:
        time.sleep(30)
        if not CAMERA_ENABLE:
            log("统计: 相机总开关=关，未采集（控制通道在线，网页可直接开）")
            continue
        parts = ["彩色 采%d/发%d/丢%d" % (cstats["frames"], cstats["sent"], cstats["dropped"])]
        for kind in ("depth", "ir"):
            st = sensor_stats.get(kind)
            if st:
                parts.append("%s 采%d/发%d/丢%d" % (kind, st["frames"], st["sent"], st["dropped"]))
        parts.append("重连 彩色%d %s" % (
            color_link.reconnects,
            " ".join("%s%d" % (k, v.reconnects) for k, v in sorted(sensor_links.items()))))
        log("统计: " + " | ".join(parts))


if __name__ == "__main__":
    main()