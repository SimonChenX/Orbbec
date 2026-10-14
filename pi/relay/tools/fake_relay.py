#!/usr/bin/env python3
"""fake_relay —— 模拟树莓派的 relay，用于在没有设备时调试后端。

生成：会动的彩色 JPEG + 合成的深度 uint16 斜坡，按 OBF1 协议推给 hub。
用法：python3 fake_relay.py [host] [port] [psk]
"""

import io
import math
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import obproto  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9501
PSK = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("OB_RELAY_PSK", "")
CW, CH = 640, 480
DW, DH = 320, 240


def connect():
    s = socket.create_connection((HOST, PORT), timeout=5)
    s.sendall(obproto.pack(obproto.TYPE_HELLO, 0, obproto.now_us(), 0, 0,
                           obproto.FMT_JSON, obproto.hello_payload(PSK, "fake-relay")))
    return s


def make_color(t):
    img = Image.new("RGB", (CW, CH), (20, 24, 30))
    d = ImageDraw.Draw(img)
    for i in range(0, CW, 64):
        d.line([(i, 0), (i, CH)], fill=(40, 46, 56))
    for i in range(0, CH, 64):
        d.line([(0, i), (CW, i)], fill=(40, 46, 56))
    x = int((math.sin(t) * 0.5 + 0.5) * (CW - 80))
    y = int((math.cos(t * 1.7) * 0.5 + 0.5) * (CH - 80))
    d.rectangle([x, y, x + 80, y + 80], fill=(255, 140, 30))
    d.text((12, 12), "FAKE COLOR  t=%.1f" % t, fill=(230, 240, 250))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=78)
    return buf.getvalue()


def make_depth(t):
    raw = bytearray(DW * DH * 2)
    for yy in range(DH):
        for xx in range(0, DW, 2):
            # 一个会随时间变化的平面 + 一个方块
            v = int(200 + 600 * (yy / float(DH)) + 150 * math.sin(t + xx * 0.02))
            if 100 <= xx <= 180 and 60 <= yy <= 150:
                v = 300
            if v < 0:
                v = 0
            if v > 4000:
                v = 4000
            struct.pack_into("<H", raw, (yy * DW + xx) * 2, v)
            struct.pack_into("<H", raw, (yy * DW + xx + 1) * 2, v)
    return bytes(raw)


def main():
    print("fake_relay -> %s:%d psk=%s" % (HOST, PORT, "有" if PSK else "无"))
    s = connect()
    t0 = time.time()
    n_c = n_d = 0
    last_c = last_d = 0.0
    while True:
        t = time.time() - t0
        now = time.time()
        if now - last_c >= 1 / 30.0:
            last_c = now
            n_c += 1
            s.sendall(obproto.pack(obproto.TYPE_COLOR, n_c, obproto.now_us(), CW, CH,
                                   obproto.FMT_MJPEG, make_color(t)))
        if now - last_d >= 1 / 20.0:
            last_d = now
            n_d += 1
            s.sendall(obproto.pack(obproto.TYPE_DEPTH, n_d, obproto.now_us(), DW, DH,
                                   obproto.FMT_Y16, make_depth(t)))
        if int(t) != int(t - 0.05) and int(t) % 10 == 0:
            import json
            s.sendall(obproto.pack(obproto.TYPE_STATUS, 0, obproto.now_us(), 0, 0, obproto.FMT_JSON,
                                   json.dumps({"who": "fake-relay", "fake": True,
                                               "color_frames": n_c, "depth_frames": n_d}).encode()))
            time.sleep(1)


if __name__ == "__main__":
    main()